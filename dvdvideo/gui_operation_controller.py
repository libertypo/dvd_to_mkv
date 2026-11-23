"""Coordinate GUI operation decisions and lifecycle without importing GTK."""

# @copyright: 2026 libertypo <libertypo@proton.me>
# @license: GNU GPL-3
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from dvdvideo.gui_commands import (
    CommandInput,
    CommandPlan,
    PlaylistChoice,
    build_bluray_streams_command,
    build_disc_details_command,
    prepare_command,
)
from dvdvideo.gui_listing import (
    Listing,
    parse_bluray_streams,
    parse_dvd_streams,
    parse_dvd_titles,
    parse_playlists,
)
from dvdvideo.gui_operations import (
    DvdBatchJob,
    OperationHooks,
    OperationRequest,
    OperationResult,
    OutputSpec,
    VerificationSpec,
    WorkerLifecycle,
    execute_dvd_batch,
    execute_operation,
    prepare_dvd_batch,
    review_dvd_batch,
)
from dvdvideo.gui_preferences import Profile, SourcePreferences
from dvdvideo.gui_selection import SelectionCoordinator
from dvdvideo.gui_support import ManagedProcess
from dvdvideo.gui_translation import identity


@dataclass(frozen=True)
class OperationInputs:
    script_path: str
    source: str
    is_bluray: bool
    detecting: bool
    generation: int
    preferences: SourcePreferences | None
    output_path: str
    output_format: str
    output_name: str
    minimum_minutes: int
    selected: tuple[str, ...]
    dvd_title: str = "1"
    language: str = "en"
    profile: Profile | None = None


@dataclass(frozen=True)
class ListingHooks:
    clear_rows: Callable[[], None]
    load_rows: Callable[[str | Listing], None]
    load_stream_rows: Callable[[str, str | Listing, list[str] | None], None]
    update_stream_format: Callable[[str], None]
    load_dvd_titles: Callable[[Listing], None] = lambda _listing: None


@dataclass(frozen=True)
class DialogHooks:
    show_disc_details: Callable[[str, str], None]
    present_m2ts_confirmation: Callable[[str], None]
    present_verification_dialog: Callable[[str, bool, str, list[str], int], None]
    present_copy_summary: Callable[[str, Callable[[bool], None]], None]


@dataclass(frozen=True)
class OperationUiHooks:
    log_inspect_action: Callable[[], None]
    set_busy: Callable[[bool], None]
    set_status: Callable[[str], None]
    invalidate_choosers: Callable[[], None]
    set_output_path: Callable[[str], None]
    notify_completion: Callable[[str, str], None]
    translate: Callable[[str], str] = identity
    set_dvd_batch_count: Callable[[int], None] = lambda _count: None


@dataclass(frozen=True)
class OperationRuntimeHooks:
    cancel_drive_scan: Callable[[], None]
    start_worker: Callable[[Callable[[], None]], bool]
    dispatch_ui: Callable[[Callable[[], object]], object]
    queue_log: Callable[[str], None]
    log: Callable[[str], None]
    exclude_device_source: Callable[[str], None]
    confirm_non_device: Callable[[str], None] = lambda _source: None
    is_known_device_source: Callable[[str], bool] = lambda _source: False


@dataclass(frozen=True)
class OperationControllerHooks:
    get_inputs: Callable[[], OperationInputs]
    listing: ListingHooks
    dialogs: DialogHooks
    ui: OperationUiHooks
    runtime: OperationRuntimeHooks


@dataclass(frozen=True)
class PreparedDvdBatch:
    inputs: OperationInputs
    jobs: tuple[DvdBatchJob, ...]
    failures: tuple[str, ...]


def dvd_batch_inputs_match(left: OperationInputs, right: OperationInputs) -> bool:
    return (
        left.script_path == right.script_path
        and left.source == right.source
        and left.is_bluray == right.is_bluray
        and left.generation == right.generation
        and left.output_path == right.output_path
        and left.profile == right.profile
    )


class OperationController:
    def __init__(
        self,
        lifecycle: WorkerLifecycle,
        selection: SelectionCoordinator,
        hooks: OperationControllerHooks,
    ) -> None:
        self.lifecycle = lifecycle
        self.selection = selection
        self.hooks = hooks
        self.pending_copy: object | None = None
        self.dvd_batch: PreparedDvdBatch | None = None

    def preserve_batch_on_title_change(self, previous: OperationInputs) -> None:
        batch = self.dvd_batch
        current = self.hooks.get_inputs()
        if batch is None or not dvd_batch_inputs_match(batch.inputs, previous):
            return
        updated = replace(batch.inputs, generation=current.generation)
        if dvd_batch_inputs_match(updated, current):
            self.dvd_batch = replace(batch, inputs=updated)

    def selected_for_copy(self, inputs: OperationInputs | None = None) -> list[str]:
        inputs = inputs or self.hooks.get_inputs()
        previous = inputs.preferences
        if not inputs.is_bluray and previous is not None and previous.dvd_title != inputs.dvd_title:
            previous = None
        return self.selection.selection_state.selected_ids(
            previous,
            is_bluray=inputs.is_bluray,
            selected=list(inputs.selected),
        )

    def streams_for_copy(
        self, playlist: str, inputs: OperationInputs | None = None,
    ) -> list[str]:
        inputs = inputs or self.hooks.get_inputs()
        previous = inputs.preferences
        return self.selection.selection_state.selected_streams(
            previous, playlist, self.selection.playlist_streams[playlist],
        )

    def playlist_format(self, playlist: str, inputs: OperationInputs | None = None) -> str:
        inputs = inputs or self.hooks.get_inputs()
        requested = inputs.output_format or "auto"
        if requested == "auto":
            return "m2ts" if self.selection.playlist_mvc.get(playlist, False) else "mkv"
        return requested

    def command(self, inspect: bool = False) -> list[str] | None:
        plan = self.prepare_command(inspect)
        return list(plan.arguments) if plan is not None else None

    def prepare_command(self, inspect: bool = False) -> CommandPlan | None:
        inputs = self.hooks.get_inputs()
        source = inputs.source
        if not source:
            self.hooks.runtime.log("Choose a source first.")
            return None
        is_bluray = inputs.is_bluray
        selected = () if inspect else tuple(self.selected_for_copy(inputs))
        playlists: list[PlaylistChoice] = []
        if is_bluray and not inspect:
            for identifier in selected:
                output_format = self.playlist_format(identifier, inputs)
                streams = (
                    tuple(self.streams_for_copy(identifier, inputs))
                    if output_format != "m2ts" and identifier in self.selection.playlist_streams
                    else None
                )
                playlists.append(PlaylistChoice(identifier, output_format, streams))
                if streams == ():
                    break
        request = CommandInput(
            script_path=inputs.script_path,
            source=source,
            is_bluray=is_bluray,
            inspect=inspect,
            minimum_minutes=(
                inputs.minimum_minutes if is_bluray and inspect else 2
            ),
            output=inputs.output_path if not inspect else "",
            output_format=(
                inputs.output_format or "auto"
                if is_bluray and not inspect else "auto"
            ),
            selected=selected,
            playlists=tuple(playlists),
            dvd_title=inputs.dvd_title,
        )
        try:
            return prepare_command(request)
        except ValueError as error:
            self.hooks.runtime.log(str(error))
            return None

    def output_snapshot(self) -> OutputSpec:
        inputs = self.hooks.get_inputs()
        selected = self.selected_for_copy(inputs)
        is_bluray = inputs.is_bluray
        formats = {playlist: self.playlist_format(playlist, inputs) for playlist in selected} if is_bluray else {}
        return OutputSpec(
            inputs.output_path or ("." if is_bluray else ""),
            is_bluray,
            inputs.output_name.strip(),
            tuple(selected),
            tuple(formats.items()),
        )

    def discover_dvd_titles(self, on_listing: Callable[[Listing], None] | None = None) -> None:
        inputs = self.hooks.get_inputs()
        if self.lifecycle.running or inputs.detecting or self.lifecycle.closing:
            self.hooks.runtime.log("Wait for the current operation before discovering DVD titles.")
            return
        if inputs.is_bluray or not inputs.source:
            self.hooks.runtime.log("Choose a DVD source before discovering titles.")
            return
        tool = shutil.which("lsdvd")
        if tool is None:
            self.hooks.runtime.log("DVD title discovery requires lsdvd; title 1 remains available.")
            return
        source = os.path.abspath(inputs.source)
        if os.path.basename(os.path.normpath(source)).upper() == "VIDEO_TS":
            source = os.path.dirname(os.path.normpath(source))
        self.start(
            [tool, "-Oj", "-acs", source], on_listing=on_listing or self.hooks.listing.load_dvd_titles,
            listing_parser=lambda output: parse_dvd_titles(output, translate=self.hooks.ui.translate),
            timeout=60, quiet_stdout=True,
        )

    def prepare_dvd_batch(self, titles: tuple[str, ...]) -> None:
        inputs = self.hooks.get_inputs()
        if self.lifecycle.running or self.lifecycle.closing or inputs.detecting:
            self.hooks.runtime.log("Wait for the current operation before preparing a DVD batch.")
        elif inputs.is_bluray or not inputs.source:
            self.hooks.runtime.log("Choose a DVD source before preparing a DVD batch.")
        elif self.selection.requires_reinspection:
            self.hooks.runtime.log("Inspect the current disc again before using its selections.")
        elif not titles:
            self.hooks.runtime.log("Select at least one DVD title.")
        elif inputs.profile is None:
            self.hooks.runtime.log("A language profile is required for a DVD batch.")
        else:
            self._start_dvd_batch(inputs, titles)

    def clear_dvd_batch(self) -> None:
        if self.lifecycle.running or self.lifecycle.closing:
            self.hooks.runtime.log("Wait for the current operation before clearing the DVD batch.")
            return
        self.dvd_batch = None
        self.pending_copy = None
        self.hooks.ui.set_dvd_batch_count(0)
        self.hooks.ui.set_status("Ready")
        self.hooks.runtime.log("Prepared DVD batch cleared.")

    def _start_dvd_batch(
        self, inputs: OperationInputs, titles: tuple[str, ...],
        jobs: tuple[DvdBatchJob, ...] | None = None, failures: tuple[str, ...] = (),
        *, review: bool = False,
    ) -> None:
        current = self.hooks.get_inputs()
        if (
            self.lifecycle.running or self.lifecycle.closing or current.detecting
            or not dvd_batch_inputs_match(current, inputs)
        ):
            self.hooks.runtime.log("DVD batch not started because the source or settings changed.")
            return
        operation = self.lifecycle.begin()
        self.hooks.runtime.cancel_drive_scan()
        self.hooks.ui.invalidate_choosers()
        self.hooks.ui.set_busy(True)
        self.hooks.ui.set_status(
            "Preparing DVD batch..." if jobs is None else
            "Preparing copy summary..." if review else "Copying DVD batch..."
        )
        request = OperationRequest(
            command=(), script_path=inputs.script_path, source=inputs.source, is_bluray=False,
            collect=False, output=OutputSpec(inputs.output_path, False, "", (), ()),
            restored_source_settings=(
                self.selection.restored_source is not None
                and self.selection.restored_source.source == inputs.source
                and (self.selection.restored_source.has_saved_settings
                     or "selection" in self.selection.restored_source.edited)
            ),
            known_device_source=self.hooks.runtime.is_known_device_source(inputs.source),
            language=inputs.language,
        )

        def dispatch(action: Callable[[], object]) -> None:
            self.hooks.runtime.dispatch_ui(action)

        hooks = OperationHooks(
            log=self.hooks.runtime.log, queue_log=self.hooks.runtime.queue_log,
            exclude_device=lambda path: dispatch(lambda: (
                self.hooks.runtime.exclude_device_source(path)
                if self.hooks.get_inputs().generation == inputs.generation else None
            )),
            confirm_non_device=lambda path: dispatch(lambda: (
                self.hooks.runtime.confirm_non_device(path)
                if self.hooks.get_inputs().generation == inputs.generation else None
            )),
            preview_started=lambda: None,
            copy_started=lambda: None,
            output_changed=lambda _path: None, translate=self.hooks.ui.translate,
            batch_title_started=lambda title, current, total: dispatch(
                lambda: self.show_dvd_batch_status(operation, title, current, total)
            ),
        )

        def prepared(result: OperationResult) -> None:
            if not dvd_batch_inputs_match(self.hooks.get_inputs(), inputs):
                self.hooks.runtime.log("DVD batch not started because the source or settings changed.")
                return
            self.dvd_batch = PreparedDvdBatch(inputs, result.batch_jobs, result.batch_failures)
            self.hooks.ui.set_dvd_batch_count(len(result.batch_jobs))
            self.hooks.ui.set_status("DVD batch prepared")
            self.hooks.runtime.log(
                f"DVD batch prepared with {len(result.batch_jobs)} title(s). "
                "Use Copy Selected to review the summary."
            )

        def present(result: OperationResult) -> None:
            batch = self.dvd_batch
            if (
                batch is None or not dvd_batch_inputs_match(batch.inputs, inputs)
                or not dvd_batch_inputs_match(self.hooks.get_inputs(), inputs)
            ):
                self.hooks.runtime.log("DVD batch not started because the source or settings changed.")
                return
            token = object()
            self.pending_copy = token

            def response(accepted: bool) -> None:
                if self.pending_copy is not token:
                    self.hooks.runtime.log("Ignored an outdated copy confirmation.")
                    return
                self.pending_copy = None
                if not accepted:
                    self.hooks.ui.set_status("DVD batch prepared")
                    self.hooks.runtime.log("Copy cancelled before extraction; the DVD batch was kept.")
                elif self.selection.requires_reinspection:
                    self.hooks.runtime.log("Inspect the current disc again before using its selections.")
                elif self.dvd_batch is not batch:
                    self.hooks.runtime.log("DVD batch changed; review its summary again.")
                else:
                    self._start_dvd_batch(inputs, titles, result.batch_jobs, result.batch_failures)

            self.hooks.ui.set_status("Awaiting copy confirmation")
            self.hooks.dialogs.present_copy_summary(result.output, response)

        def worker() -> None:
            result = OperationResult("Failed")
            try:
                if jobs is None:
                    assert inputs.profile is not None
                    result = prepare_dvd_batch(request, titles, inputs.profile, operation, hooks)
                elif review:
                    result = review_dvd_batch(request, jobs, failures, operation, hooks)
                else:
                    result = execute_dvd_batch(request, jobs, failures, operation, hooks)
            except Exception:  # noqa: BLE001 - report unexpected failures at the worker boundary
                import traceback
                self.hooks.runtime.log("Unexpected DVD batch error:\n" + traceback.format_exc().rstrip())
            finally:
                def finished() -> None:
                    if jobs is not None and not review and self.dvd_batch is not None:
                        remaining = tuple(
                            job for job in self.dvd_batch.jobs if job.title not in result.batch_completed
                        )
                        if remaining:
                            self.dvd_batch = PreparedDvdBatch(inputs, remaining, self.dvd_batch.failures)
                        else:
                            self.dvd_batch = None
                        self.hooks.ui.set_dvd_batch_count(len(remaining))
                    callback = prepared if jobs is None else present
                    self.finish_run(
                        (lambda _text: callback(result)) if result.deliver_success else None,
                        result.output, inputs.generation, inputs.source, False, result.status,
                        completed_copy=jobs is not None and not review,
                        notification_kind="Copy" if jobs is not None and not review else None,
                    )

                dispatch(finished)

        if not self.hooks.runtime.start_worker(worker):
            self.finish_run(status="Failed", notification_kind="Copy" if jobs is not None and not review else None)

    def show_dvd_batch_status(
        self, operation: ManagedProcess, title: str, current: int, total: int,
    ) -> None:
        if (
            self.lifecycle.operation is operation
            and not self.lifecycle.closing
            and not operation.cancelled.is_set()
        ):
            self.hooks.ui.set_status(self.hooks.ui.translate(
                "Copying DVD title {title} ({current}/{total})..."
            ).format(title=title, current=current, total=total))

    def inspect(self) -> None:
        inputs = self.hooks.get_inputs()
        if self.lifecycle.running or inputs.detecting:
            self.hooks.runtime.log("Wait for the current operation before inspecting.")
            return
        command = self.command(inspect=True)
        if command:
            requires_reinspection = self.selection.requires_reinspection
            try:
                self.hooks.listing.clear_rows()
            finally:
                self.selection.requires_reinspection = requires_reinspection
            def parser(output: str) -> Listing:
                if inputs.is_bluray:
                    return parse_playlists(output, translate=self.hooks.ui.translate)
                return parse_dvd_streams(output)

            def loaded(listing: Listing) -> None:
                self.hooks.listing.load_rows(listing)
                if inputs.is_bluray and listing.longest_playlist is not None:
                    self.inspect_playlist(listing.longest_playlist)

            self.start(command, on_listing=loaded, listing_parser=parser)

    def preview_selected(self) -> None:
        inputs = self.hooks.get_inputs()
        if self.selection.requires_reinspection:
            self.hooks.runtime.log("Inspect the current disc again before using its selections.")
            return
        if self.lifecycle.running or inputs.detecting or self.lifecycle.closing:
            self.hooks.runtime.log("Wait for the current operation before previewing.")
            return
        if not inputs.source:
            self.hooks.runtime.log("Choose a source first.")
            return
        playlist = None
        if inputs.is_bluray:
            selected = self.selected_for_copy(inputs)
            if len(selected) != 1:
                self.hooks.runtime.log("Select exactly one playlist to preview.")
                return
            playlist = selected[0]
        self.start([], preview=True, preview_playlist=playlist)

    def inspect_disc_details(self) -> None:
        inputs = self.hooks.get_inputs()
        if self.selection.requires_reinspection:
            self.hooks.runtime.log("Inspect the current disc again before using its selections.")
            return
        if self.lifecycle.running or inputs.detecting or self.lifecycle.closing:
            self.hooks.runtime.log("Wait for the current operation before reading disc details.")
            return
        source = inputs.source
        if not source:
            self.hooks.runtime.log("Choose a source first.")
            return
        is_bluray = inputs.is_bluray
        playlist = None
        if is_bluray:
            selected = self.selected_for_copy(inputs)
            if len(selected) > 1:
                self.hooks.runtime.log("Select one playlist for detailed tracks, or none for a disc overview.")
                return
            playlist = selected[0] if selected else None
        command = build_disc_details_command(
            inputs.script_path, source, is_bluray=is_bluray, playlist=playlist, dvd_title=inputs.dvd_title,
        )
        self.start(
            command, lambda output: self.hooks.dialogs.show_disc_details(output, source),
        )

    def copy_selected(self) -> None:
        inputs = self.hooks.get_inputs()
        if self.selection.requires_reinspection:
            self.hooks.runtime.log("Inspect the current disc again before using its selections.")
            return
        if self.lifecycle.running or inputs.detecting or self.lifecycle.closing:
            self.hooks.runtime.log("Wait for the current operation before copying.")
            return
        if not inputs.source:
            self.hooks.runtime.log("Choose a source first.")
            return
        if self.dvd_batch is not None:
            batch = self.dvd_batch
            if not dvd_batch_inputs_match(inputs, batch.inputs):
                self.hooks.runtime.log(
                    "The prepared DVD batch no longer matches the source or settings. "
                    "Prepare it again or clear the batch before copying."
                )
                return
            self._start_dvd_batch(
                inputs, tuple(job.title for job in batch.jobs), batch.jobs, batch.failures, review=True,
            )
            return
        if inputs.is_bluray:
            selected = self.selected_for_copy(inputs)
            for playlist in selected:
                if (
                    inputs.output_format != "m2ts"
                    and playlist not in self.selection.playlist_mvc
                ):
                    command = build_bluray_streams_command(
                        inputs.script_path, inputs.source, playlist,
                    )

                    def inspected(output: Listing, playlist: str = playlist) -> None:
                        self.hooks.listing.load_stream_rows(playlist, output, None)
                        if playlist in self.selection.playlist_mvc:
                            self.copy_selected()

                    self.start(
                        command, on_listing=inspected, listing_parser=parse_bluray_streams,
                    )
                    return
            for playlist in selected:
                if self.playlist_format(playlist, inputs) == "m2ts" and playlist not in self.selection.m2ts_confirmed:
                    saved = inputs.preferences
                    if self.selection.requires_m2ts_confirmation(playlist, saved):
                        self.hooks.dialogs.present_m2ts_confirmation(playlist)
                        return
                    self.selection.m2ts_confirmed.add(playlist)
        plan = self.prepare_command()
        if plan is not None:
            command = list(plan.arguments)
            try:
                output = self.output_snapshot()
            except ValueError as error:
                self.hooks.runtime.log(str(error))
                return
            generation = inputs.generation
            tracks, warnings = self.copy_tracks_summary(inputs)
            token = object()
            self.pending_copy = token

            def response(accepted: bool) -> None:
                if self.pending_copy is not token:
                    self.hooks.runtime.log("Ignored an outdated copy confirmation.")
                    return
                self.pending_copy = None
                if not accepted:
                    self.hooks.runtime.log("Copy cancelled before extraction; selections were kept.")
                    return
                current = self.hooks.get_inputs()
                if (
                    self.lifecycle.running or self.lifecycle.closing or current.detecting
                    or current.generation != generation or current.source != inputs.source
                    or current.is_bluray != inputs.is_bluray or self.selection.requires_reinspection
                    or current.output_format != inputs.output_format
                    or self.command() != command or self.output_snapshot() != output
                    or self.copy_tracks_summary(current) != (tracks, warnings)
                ):
                    self.hooks.runtime.log("Copy not started because the source, settings, or operation changed. Review the summary again.")
                    return
                self.start(command, output_snapshot=output, output_argument_index=plan.output_index)

            def present(summary: str) -> None:
                self.hooks.ui.set_status("Awaiting copy confirmation")
                for warning in warnings:
                    self.hooks.runtime.log(warning)
                self.hooks.dialogs.present_copy_summary(summary + "\n\n" + tracks, response)

            self.start(
                command, on_success=present, output_snapshot=output, summary_only=True,
                output_argument_index=plan.output_index,
            )

    def copy_tracks_summary(self, inputs: OperationInputs) -> tuple[str, tuple[str, ...]]:
        translate = self.hooks.ui.translate
        lines = [translate("Selected tracks")]
        warnings: list[str] = []
        playlists = self.selected_for_copy(inputs) if inputs.is_bluray else [""]
        for playlist in playlists:
            if inputs.is_bluray:
                lines.append(translate("Playlist {playlist}:").format(playlist=playlist))
                if self.playlist_format(playlist, inputs) == "m2ts":
                    lines.append("  " + translate("All tracks (original M2TS; filtering is not applied)."))
                    continue
                identifiers = (
                    self.streams_for_copy(playlist, inputs)
                    if playlist in self.selection.playlist_streams else None
                )
                tracks = self.selection.playlist_track_info.get(playlist, [])
            else:
                identifiers = self.selected_for_copy(inputs)
                tracks = self.selection.dvd_track_info
            by_id = {track.identifier: track for track in tracks}
            if identifiers is None:
                lines.append("  " + translate("All source tracks (not individually inspected)."))
            else:
                for identifier in identifiers:
                    track = by_id.get(identifier)
                    detail = (
                        f" - {track.kind}, {track.language or translate('language unspecified')}" if track else ""
                    )
                    lines.append(f"  {identifier}{detail}")
            selected_ids = set(identifiers) if identifiers is not None else set()
            selected_tracks = [track for track in tracks if identifiers is None or track.identifier in selected_ids]
            if any(track.kind == "audio" for track in selected_tracks):
                continue
            if identifiers is None or any(identifier not in by_id for identifier in identifiers):
                warning = translate(
                    "Warning: audio selection could not be determined; check whether the output includes audio."
                )
                if inputs.is_bluray:
                    warning = translate("Playlist {playlist}:").format(playlist=playlist) + " " + warning
            elif inputs.is_bluray:
                warning = translate(
                    "Warning: no audio track was selected for playlist {playlist}; the output will have no audio."
                ).format(playlist=playlist)
            else:
                warning = translate(
                    "Warning: no audio track was selected for DVD title {title}; the output will have no audio."
                ).format(title=inputs.dvd_title)
            lines.append(warning)
            warnings.append(warning)
        return "\n".join(lines), tuple(warnings)

    def confirm_m2ts_response(self, playlist: str, generation: int, accepted: bool) -> None:
        inputs = self.hooks.get_inputs()
        if (
            generation != inputs.generation
            or self.lifecycle.closing
            or self.lifecycle.running
        ):
            return
        if accepted:
            self.selection.m2ts_confirmed.add(playlist)
            self.copy_selected()
        else:
            self.hooks.runtime.log("M2TS copy cancelled; filtered selection retained.")

    def inspect_streams(self) -> None:
        inputs = self.hooks.get_inputs()
        if self.selection.requires_reinspection:
            self.hooks.ui.log_inspect_action()
            self.hooks.runtime.log("Inspect the current disc again before using its selections.")
            return
        if self.lifecycle.running or inputs.detecting or self.lifecycle.closing:
            self.hooks.ui.log_inspect_action()
            self.hooks.runtime.log("Wait for the current operation before inspecting streams.")
            return
        selected = self.selected_for_copy(inputs)
        if len(selected) != 1:
            self.hooks.ui.log_inspect_action()
            self.hooks.runtime.log("Select exactly one playlist to inspect its streams.")
            return
        self.inspect_playlist(selected[0])

    def inspect_playlist(self, playlist: str) -> None:
        inputs = self.hooks.get_inputs()
        if self.selection.requires_reinspection:
            self.hooks.runtime.log("Inspect the current disc again before using its selections.")
            return
        self.hooks.ui.log_inspect_action()
        if self.lifecycle.running or inputs.detecting or self.lifecycle.closing:
            self.hooks.runtime.log("Wait for the current operation before inspecting streams.")
            return
        source = inputs.source
        if not source:
            self.hooks.runtime.log("Choose a source first.")
            return
        if not inputs.is_bluray:
            self.hooks.runtime.log("Choose a Blu-ray source before inspecting its tracks.")
            return
        saved = inputs.preferences
        previous = self.selection.begin_stream_inspection(playlist, saved)
        if self.selection.stream_playlist == playlist:
            self.hooks.listing.update_stream_format(playlist)
        command = build_bluray_streams_command(
            inputs.script_path, source, playlist,
        )
        self.start(
            command,
            on_listing=lambda output: self.hooks.listing.load_stream_rows(playlist, output, previous),
            listing_parser=parse_bluray_streams,
        )

    def show_verify_dialog(self) -> None:
        inputs = self.hooks.get_inputs()
        if self.selection.requires_reinspection:
            self.hooks.runtime.log("Inspect the current disc again before using its selections.")
            return
        if self.lifecycle.running or inputs.detecting or self.lifecycle.closing:
            self.hooks.runtime.log("Wait for the current operation before verifying a copy.")
            return
        source = inputs.source
        if not source:
            self.hooks.runtime.log("Choose the original source before verifying a copy.")
            return
        is_bluray = inputs.is_bluray
        selected = self.selected_for_copy(inputs)
        title = selected[0] if is_bluray and len(selected) == 1 else inputs.dvd_title if not is_bluray else ""
        streams = (
            self.streams_for_copy(title, inputs)
            if is_bluray and title in self.selection.playlist_streams
            else [] if is_bluray else selected
        )
        self.hooks.dialogs.present_verification_dialog(
            source, is_bluray, title, streams, inputs.generation,
        )

    def verification_response(
        self,
        source: str,
        is_bluray: bool,
        generation: int,
        *,
        accepted: bool,
        copy_path: str,
        title: str,
        streams_text: str,
    ) -> bool:
        inputs = self.hooks.get_inputs()
        if not accepted:
            return True
        if (
            self.lifecycle.closing or self.lifecycle.running or inputs.detecting
            or generation != inputs.generation
            or is_bluray != inputs.is_bluray
            or self.selection.requires_reinspection
        ):
            self.hooks.runtime.log("Verification not started because the source or operation changed.")
        elif not copy_path:
            self.hooks.runtime.log("Choose an existing copy to verify.")
            return False
        else:
            values = [value.strip() for value in streams_text.split(",")]
            streams = [] if values == [""] else values
            verification = VerificationSpec(
                source, Path(copy_path), is_bluray, title.strip(), tuple(streams),
            )
            self.start(
                [inputs.script_path], verification=verification,
            )
        return True

    def start(
        self,
        command: list[str],
        on_success: Callable[[str], None] | None = None,
        output_snapshot: OutputSpec | None = None,
        *,
        preview: bool = False,
        preview_playlist: str | None = None,
        verification: VerificationSpec | None = None,
        listing_parser: Callable[[str], Listing] | None = None,
        on_listing: Callable[[Listing], None] | None = None,
        summary_only: bool = False,
        timeout: float | None = None,
        quiet_stdout: bool = False,
        output_argument_index: int | None = None,
    ) -> None:
        inputs = self.hooks.get_inputs()
        if self.lifecycle.closing:
            return
        if inputs.detecting:
            self.hooks.runtime.log("Detecting media type; wait a moment or choose DVD/Blu-ray manually.")
            return
        if self.lifecycle.running:
            self.hooks.runtime.log("An operation is already running.")
            return
        operation = self.lifecycle.begin()
        notification_kind = (
            "Verification" if verification is not None else
            "Copy" if output_snapshot is not None and not summary_only else None
        )
        self.hooks.runtime.cancel_drive_scan()
        self.hooks.ui.invalidate_choosers()
        generation = inputs.generation
        source = inputs.source
        is_bluray = inputs.is_bluray
        self.hooks.ui.set_busy(True)
        self.hooks.ui.set_status(
            "Preparing copy summary..." if summary_only else
            "Verifying copy..." if verification else "Preparing preview..." if preview else
            "Validating output..." if output_snapshot else "Inspecting..." if on_success or on_listing else "Copying..."
        )
        request = OperationRequest(
            command=tuple(command),
            script_path=inputs.script_path,
            source=source,
            is_bluray=is_bluray,
            collect=on_success is not None or on_listing is not None,
            output=output_snapshot,
            preview=preview,
            preview_playlist=preview_playlist,
            verification=verification,
            listing_parser=listing_parser,
            restored_source_settings=(
                self.selection.restored_source is not None
                and self.selection.restored_source.source == source
                and (
                    self.selection.restored_source.has_saved_settings
                    or "selection" in self.selection.restored_source.edited
                )
            ),
            known_device_source=self.hooks.runtime.is_known_device_source(source),
            summary_only=summary_only,
            dvd_title=inputs.dvd_title,
            timeout=timeout,
            quiet_stdout=quiet_stdout,
            language=inputs.language,
            output_argument_index=output_argument_index,
        )

        def dispatch(action: Callable[[], object]) -> None:
            self.hooks.runtime.dispatch_ui(action)

        hooks = OperationHooks(
            log=self.hooks.runtime.log,
            queue_log=self.hooks.runtime.queue_log,
            exclude_device=lambda path: dispatch(lambda: (
                self.hooks.runtime.exclude_device_source(path)
                if self.hooks.get_inputs().generation == generation else None
            )),
            confirm_non_device=lambda path: dispatch(lambda: (
                self.hooks.runtime.confirm_non_device(path)
                if self.hooks.get_inputs().generation == generation else None
            )),
            preview_started=lambda: dispatch(lambda: self.show_preview_status(operation)),
            copy_started=lambda: dispatch(lambda: self.show_copying_status(operation)),
            output_changed=lambda path: dispatch(lambda: self.update_output_path(operation, path)),
            translate=self.hooks.ui.translate,
        )

        def worker() -> None:
            result = OperationResult("Failed")
            try:
                result = execute_operation(request, operation, hooks)
            finally:
                dispatch(lambda: self.finish_run(
                    on_success if result.deliver_success else None,
                    result.output,
                    generation,
                    source,
                    is_bluray,
                    result.status,
                    result.listing,
                    output_snapshot is not None and not summary_only,
                    on_listing=on_listing if result.deliver_success else None,
                    notification_kind=notification_kind,
                ))

        if not self.hooks.runtime.start_worker(worker):
            self.finish_run(status="Failed", notification_kind=notification_kind)

    def show_copying_status(self, operation: ManagedProcess) -> None:
        if (
            self.lifecycle.operation is operation
            and not self.lifecycle.closing
            and not operation.cancelled.is_set()
        ):
            self.hooks.ui.set_status("Copying...")

    def show_preview_status(self, operation: ManagedProcess) -> None:
        if (
            self.lifecycle.operation is operation
            and not self.lifecycle.closing
            and not operation.cancelled.is_set()
        ):
            self.hooks.ui.set_status("Previewing...")

    def update_output_path(self, operation: ManagedProcess, path: str) -> None:
        if self.lifecycle.operation is operation and not self.lifecycle.closing:
            self.hooks.ui.set_output_path(path)
            self.hooks.runtime.log(f"Output format changed filename to: {path}")

    def finish_run(
        self,
        on_success: Callable[[str], None] | None = None,
        output: str = "",
        generation: int | None = None,
        source: str | None = None,
        is_bluray: bool | None = None,
        status: str = "Completed",
        listing: Listing | None = None,
        completed_copy: bool = False,
        *,
        on_listing: Callable[[Listing], None] | None = None,
        notification_kind: str | None = None,
    ) -> None:
        inputs = self.hooks.get_inputs()
        if self.lifecycle.operation is not None and self.lifecycle.operation.cancelled.is_set():
            if status == "Completed" and completed_copy:
                self.hooks.runtime.log("Cancellation arrived after the copy completed; published output was retained.")
            elif status in ("Completed", "Verified"):
                self.hooks.runtime.log("Cancellation received after subprocess completion; ignored pending results.")
                status = "Cancelled"
            on_success = None
            on_listing = None
        self.lifecycle.finish()
        self.hooks.ui.set_busy(False)
        self.hooks.ui.set_status(status)
        if self.lifecycle.closing:
            return
        if generation is not None and (
            generation != inputs.generation
            or source != inputs.source
            or is_bluray != inputs.is_bluray
        ):
            self.hooks.runtime.log("Ignored stale operation results because the source or media mode changed.")
            return
        if notification_kind is not None and status in ("Completed", "Verified", "Failed"):
            self.hooks.ui.notify_completion(notification_kind, status)
        if status == "Verified":
            self.hooks.runtime.log(output.rstrip())
        if on_listing is not None and listing is not None:
            on_listing(listing)
        if on_success is not None:
            on_success(output)
