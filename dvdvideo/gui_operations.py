"""GTK-independent operation execution and worker lifecycle."""

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

import subprocess
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from dvdvideo.disc_details import parse_disc_details
from dvdvideo.gui_commands import CommandInput, prepare_command, validate_dvd_title
from dvdvideo.gui_listing import Listing, parse_dvd_streams
from dvdvideo.gui_preferences import Profile, select_tracks
from dvdvideo.gui_preflight import run_operation_preflight
from dvdvideo.gui_support import (
    ManagedProcess,
    OperationCancelled,
    copy_command_arguments,
    validate_output_paths,
)
from dvdvideo.gui_translation import identity
from dvdvideo.preview import play_preview
from dvdvideo.verify_copy import run_verification as verify_copy

DVD_BATCH_PUBLISHED_CANCELLED = 75


@dataclass(frozen=True)
class OutputSpec:
    path: str
    is_bluray: bool
    name: str
    playlists: tuple[str, ...]
    formats: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class VerificationSpec:
    source: str
    copy: Path
    is_bluray: bool
    title: str
    streams: tuple[str, ...]


@dataclass(frozen=True)
class DvdBatchJob:
    title: str
    streams: tuple[str, ...]
    output: str
    track_summary: str = ""


@dataclass(frozen=True)
class OperationRequest:
    command: tuple[str, ...]
    script_path: str
    source: str
    is_bluray: bool
    collect: bool
    output: OutputSpec | None = None
    preview: bool = False
    preview_playlist: str | None = None
    verification: VerificationSpec | None = None
    listing_parser: Callable[[str], Listing] | None = None
    restored_source_settings: bool = False
    known_device_source: bool = False
    summary_only: bool = False
    dvd_title: str = "1"
    timeout: float | None = None
    quiet_stdout: bool = False
    language: str = "en"
    output_argument_index: int | None = None
    dvd_batch_copy: bool = False


@dataclass(frozen=True)
class OperationResult:
    status: Literal["Completed", "Cancelled", "Failed", "Verified"]
    output: str = ""
    listing: Listing | None = None
    deliver_success: bool = False
    batch_jobs: tuple[DvdBatchJob, ...] = ()
    batch_failures: tuple[str, ...] = ()
    batch_completed: tuple[str, ...] = ()
    copy_committed: bool = False


@dataclass(frozen=True)
class OperationHooks:
    log: Callable[[str], None]
    queue_log: Callable[[str], None]
    exclude_device: Callable[[str], None]
    preview_started: Callable[[], None]
    copy_started: Callable[[], None]
    output_changed: Callable[[str], None]
    confirm_non_device: Callable[[str], None] = lambda _source: None
    translate: Callable[[str], str] = identity
    batch_title_started: Callable[[str, int, int], None] = lambda _title, _current, _total: None


@dataclass
class WorkerLifecycle:
    """Track all GUI workers and the active foreground operation."""

    running: bool = False
    operation: ManagedProcess | None = None
    closing: bool = False
    jobs: list[threading.Thread] = field(default_factory=list[threading.Thread])

    def begin(self) -> ManagedProcess:
        self.running = True
        self.operation = ManagedProcess()
        return self.operation

    def finish(self) -> None:
        self.running = False
        self.operation = None

    def start_worker(self, worker: Callable[[], None], log: Callable[[str], None]) -> bool:
        thread = None
        try:
            thread = threading.Thread(target=worker, daemon=False)
            self.jobs.append(thread)
            thread.start()
        except RuntimeError as error:
            if thread is not None:
                self.jobs.remove(thread)
            log(f"Could not start background worker: {error}")
            return False
        return True

    def prune(self) -> None:
        self.jobs = [job for job in self.jobs if job.is_alive()]

    def busy(self) -> bool:
        return self.running or any(job.is_alive() for job in self.jobs)


def check_output(
    output: OutputSpec, operation: ManagedProcess, log: Callable[[str], None],
) -> bool:
    try:
        validate_output_paths(
            output.path, output.is_bluray, output.name, list(output.playlists),
            operation, formats=dict(output.formats),
        )
    except (OSError, ValueError) as error:
        log(f"Cannot validate output filename: {error}")
        return False
    return True


def execute_operation(
    request: OperationRequest, operation: ManagedProcess, hooks: OperationHooks,
) -> OperationResult:
    command = list(request.command)
    details_requested = request.collect and "--disc-details" in command
    try:
        output_payload: dict[str, object] | None = None
        if request.output is not None:
            spec = request.output
            output_payload = {
                "path": spec.path,
                "is_bluray": spec.is_bluray,
                "name": spec.name,
                "playlists": list(spec.playlists),
                "formats": dict(spec.formats),
            }
            if request.summary_only:
                output_payload.update(summarize=True, script=request.script_path, language=request.language)
        preflight = run_operation_preflight(
            request.source, request.is_bluray, output_payload,
            request.preview, request.preview_playlist, operation, hooks.log, request.dvd_title,
        )
        if preflight is not None and preflight.source_is_block_device:
            hooks.exclude_device(request.source)
            if request.restored_source_settings:
                hooks.log(
                    "The source is a block device; saved disc settings were discarded. "
                    "Review the current choices and retry the operation."
                )
                return OperationResult("Failed")
        elif preflight is not None and preflight.source_classified:
            hooks.confirm_non_device(request.source)
            if request.known_device_source:
                hooks.log(
                    "The source is no longer a block device; review the current choices "
                    "and retry the operation."
                )
                return OperationResult("Failed")
        if request.verification is not None:
            verification = request.verification
            output = verify_copy(
                verification.source, verification.copy, bluray=verification.is_bluray,
                title=verification.title, streams=list(verification.streams),
                copier=request.script_path, operation=operation, log=hooks.queue_log,
            )
            return OperationResult("Verified", output)
        if request.preview:
            if preflight is None or preflight.preview_command is None:
                raise RuntimeError("Operation preflight did not return a preview command")
            command = list(preflight.preview_command)
            hooks.log(
                "Preview plays directly from the disc in 2D, using the player's track choices. "
                "Close the player or click Cancel to stop; no copy is created."
            )
            hooks.preview_started()
        if request.output is not None:
            spec = request.output
            if preflight is None or preflight.output_path is None:
                raise RuntimeError("Operation preflight did not return an output path")
            output_path = preflight.output_path
            directory_output = preflight.directory_output
            if preflight.output_error is not None:
                hooks.log(f"Cannot validate output filename: {preflight.output_error}")
                return OperationResult("Failed")
            if output_path != spec.path:
                output_index = request.output_argument_index
                if output_index is None or not 0 <= output_index < len(command) or command[output_index] != spec.path:
                    raise RuntimeError("Copy command is missing valid output argument metadata.")
                command[output_index] = output_path
                if not request.summary_only:
                    hooks.output_changed(output_path)
            if request.summary_only:
                if preflight.copy_summary is None:
                    raise RuntimeError("Copy preflight did not return a summary")
                return OperationResult("Completed", preflight.copy_summary, deliver_success=True)
            if spec.is_bluray and spec.name and directory_output:
                command[1:1] = ["--output-name", spec.name]
            hooks.copy_started()
        if request.output is None:
            hooks.log("$ " + " ".join(command))
        if request.preview:
            result = play_preview(command, operation=operation, log=hooks.queue_log)
        elif details_requested:
            result = operation.run(
                command, timeout=60, termination_log=hooks.queue_log, abandon_unresponsive=True,
            )
            if result.stderr:
                hooks.log(result.stderr)
        elif request.output is not None:
            if request.dvd_batch_copy:
                command.insert(1, "--gui-dvd-batch")
            with copy_command_arguments(command) as (copy_command, pass_fds):
                hooks.log("$ " + " ".join(copy_command))
                result = operation.run(
                    copy_command, collect=False, log=hooks.queue_log,
                    abandon_unresponsive=True, pass_fds=pass_fds, preserve_success_on_cancel=True,
                    cancelled_exit_code=DVD_BATCH_PUBLISHED_CANCELLED if request.dvd_batch_copy else None,
                )
        else:
            result = operation.run(
                command, collect=request.collect, log=None if request.quiet_stdout else hooks.queue_log,
                stderr_log=hooks.queue_log if request.quiet_stdout else None,
                timeout=request.timeout, abandon_unresponsive=True,
            )
        if (
            request.dvd_batch_copy and result.returncode == DVD_BATCH_PUBLISHED_CANCELLED
            and operation.cancelled.is_set()
        ):
            hooks.log("DVD output was published before cancellation; cleanup may be incomplete.")
            return OperationResult("Cancelled", copy_committed=True)
        output = result.stdout
        listing = None
        if request.quiet_stdout and result.returncode:
            hooks.log("DVD title discovery failed; title 1 remains available.")
        if result.returncode == 0:
            if details_requested:
                playlist = command[command.index("--playlist") + 1] if "--playlist" in command else None
                try:
                    output = parse_disc_details(
                        output, is_bluray=request.is_bluray, playlist=playlist, dvd_title=request.dvd_title,
                        translate=hooks.translate,
                    ).text(hooks.translate)
                except ValueError as error:
                    raise RuntimeError(f"Could not read disc details: {error}") from error
            elif request.listing_parser is not None:
                try:
                    listing = request.listing_parser(output)
                except (TypeError, ValueError) as error:
                    raise RuntimeError(f"Could not read inspection output: {error}") from error
        hooks.log(f"Exit status: {result.returncode}")
        return OperationResult(
            "Completed" if result.returncode == 0 else "Failed", output, listing,
            deliver_success=result.returncode == 0 and request.collect,
        )
    except OperationCancelled:
        hooks.log(
            "Verification cancelled; the original and saved copy were not modified."
            if request.verification is not None else
            "Operation cancelled. If termination was delayed, cleanup and output publication may be "
            "incomplete; descendants outside the managed process group may remain. "
            "Previously completed outputs may remain."
        )
        return OperationResult("Cancelled")
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        hooks.log(f"{'Verification' if request.verification is not None else 'Operation'} failed: {error}")
        return OperationResult("Failed")
    except Exception:  # noqa: BLE001 - worker boundary reports unexpected errors and releases busy state
        hooks.log("Unexpected worker error:\n" + traceback.format_exc().rstrip())
        return OperationResult("Failed")


def prepare_dvd_batch(
    request: OperationRequest, titles: tuple[str, ...], profile: Profile,
    operation: ManagedProcess, hooks: OperationHooks,
) -> OperationResult:
    if not profile.audio_languages and not profile.subtitle_languages:
        hooks.log("Choose a profile with audio or subtitle languages before preparing a DVD batch.")
        return OperationResult("Failed")
    if not request.output or not request.output.path:
        hooks.log("Choose an output MKV before preparing a DVD batch.")
        return OperationResult("Failed")
    output = Path(request.output.path)
    if not output.name or output.suffix.lower() != ".mkv":
        hooks.log("Choose an output filename ending in .mkv for the DVD batch.")
        return OperationResult("Failed")
    jobs: list[DvdBatchJob] = []
    failures: list[str] = []
    summaries: list[str] = []
    try:
        if not titles or len(set(titles)) != len(titles):
            raise ValueError("Select one or more distinct DVD titles.")
        for title in titles:
            validate_dvd_title(title)
            operation.check_cancelled()
            hooks.log(f"Inspecting DVD title {title} for batch.")
            inspection = prepare_command(CommandInput(
                request.script_path, request.source, False, inspect=True, dvd_title=title,
            ))
            result = execute_operation(replace(
                request, command=inspection.arguments, output=None, collect=True,
                summary_only=False, listing_parser=parse_dvd_streams, dvd_title=title,
                output_argument_index=None,
            ), operation, hooks)
            if result.status == "Cancelled":
                return result
            listing = result.listing
            if result.status != "Completed" or listing is None or not listing.tracks or listing.invalid or listing.duplicates:
                hooks.log(f"DVD title {title}: track inspection failed or returned invalid data.")
                failures.append(title)
                continue
            streams = tuple(select_tracks(profile, listing.tracks))
            if not streams:
                hooks.log(f"DVD title {title}: the profile selected no tracks.")
                failures.append(title)
                continue
            path = str(output.with_name(f"{output.stem}-title-{int(title):02d}.mkv"))
            plan = prepare_command(CommandInput(
                request.script_path, request.source, False, output=path,
                selected=streams, dvd_title=title,
            ))
            spec = OutputSpec(path, False, "", (), ())
            result = execute_operation(replace(
                request, command=plan.arguments, output=spec, collect=True,
                summary_only=True, listing_parser=None, dvd_title=title,
                output_argument_index=plan.output_index,
            ), operation, hooks)
            if result.status == "Cancelled":
                return result
            if result.status != "Completed":
                hooks.log(f"DVD title {title}: copy preflight failed.")
                failures.append(title)
                continue
            tracks = {track.identifier: track for track in listing.tracks}
            descriptions = [
                f"  {identifier} - {tracks[identifier].kind}, "
                f"{tracks[identifier].language or hooks.translate('language unspecified')}"
                for identifier in streams
            ]
            track_summary = hooks.translate("Selected tracks") + ":\n" + "\n".join(descriptions)
            if not any(tracks[identifier].kind == "audio" for identifier in streams):
                warning = hooks.translate(
                    "Warning: no audio track matched the profile for DVD title {title}; "
                    "the output will have no audio."
                    if profile.audio_languages else
                    "Warning: no audio track was selected for DVD title {title}; "
                    "the output will have no audio."
                ).format(title=title)
                hooks.log(warning)
                track_summary += "\n" + warning
            summaries.append(result.output + "\n" + track_summary)
            jobs.append(DvdBatchJob(title, streams, path, track_summary))
        if not jobs:
            hooks.log("No DVD titles passed batch preparation.")
            return OperationResult("Failed", batch_failures=tuple(failures))
        summary = (
            hooks.translate(
                "DVD batch: titles are copied sequentially. Space estimates are per title, "
                "not for the whole batch; free space and output paths are checked again for each copy."
            ) + "\n\n"
            + "\n\n".join(summaries)
        )
        if failures:
            summary += "\n\n" + hooks.translate("Titles skipped during preparation: {titles}").format(
                titles=", ".join(failures),
            )
        return OperationResult(
            "Completed", summary, deliver_success=True,
            batch_jobs=tuple(jobs), batch_failures=tuple(failures),
        )
    except OperationCancelled:
        hooks.log("DVD batch preparation cancelled; no copies were started.")
        return OperationResult("Cancelled")
    except (OSError, ValueError) as error:
        hooks.log(f"Cannot prepare DVD batch: {error}")
        return OperationResult("Failed")


def review_dvd_batch(
    request: OperationRequest, jobs: tuple[DvdBatchJob, ...], failures: tuple[str, ...],
    operation: ManagedProcess, hooks: OperationHooks,
) -> OperationResult:
    ready: list[DvdBatchJob] = []
    skipped: list[str] = []
    summaries: list[str] = []
    for job in jobs:
        if operation.cancelled.is_set():
            return OperationResult("Cancelled")
        plan = prepare_command(CommandInput(
            request.script_path, request.source, False, output=job.output,
            selected=job.streams, dvd_title=job.title,
        ))
        result = execute_operation(replace(
            request, command=plan.arguments, output=OutputSpec(job.output, False, "", (), ()),
            collect=True, summary_only=True, listing_parser=None, dvd_title=job.title,
            output_argument_index=plan.output_index,
        ), operation, hooks)
        if result.status == "Cancelled":
            return result
        if result.status != "Completed":
            hooks.log(f"DVD title {job.title}: copy preflight failed; it remains in the batch.")
            skipped.append(job.title)
            continue
        ready.append(job)
        summaries.append(result.output + "\n" + job.track_summary)
    if not ready:
        hooks.log("No DVD titles passed copy preflight; the prepared batch was kept.")
        return OperationResult("Failed")
    summary = (
        hooks.translate(
            "DVD batch: titles are copied sequentially. Space estimates are per title, "
            "not for the whole batch; free space and output paths are checked again for each copy."
        ) + "\n\n" + "\n\n".join(summaries)
    )
    if failures:
        summary += "\n\n" + hooks.translate("Titles skipped during preparation: {titles}").format(
            titles=", ".join(failures),
        )
    if skipped:
        summary += "\n\n" + hooks.translate("Titles skipped during copy preflight (kept in batch): {titles}").format(
            titles=", ".join(skipped),
        )
    return OperationResult(
        "Completed", summary, deliver_success=True,
        batch_jobs=tuple(ready), batch_failures=failures + tuple(skipped),
    )


def execute_dvd_batch(
    request: OperationRequest, jobs: tuple[DvdBatchJob, ...], failures: tuple[str, ...],
    operation: ManagedProcess, hooks: OperationHooks,
) -> OperationResult:
    failed = list(failures)
    completed: list[str] = []
    for index, job in enumerate(jobs, 1):
        if operation.cancelled.is_set():
            hooks.log("DVD batch cancelled. Completed copies were retained.")
            return OperationResult("Cancelled", batch_completed=tuple(completed))
        hooks.batch_title_started(job.title, index, len(jobs))
        hooks.log(f"Copying DVD title {job.title} ({index}/{len(jobs)}).")
        plan = prepare_command(CommandInput(
            request.script_path, request.source, False, output=job.output,
            selected=job.streams, dvd_title=job.title,
        ))
        result = execute_operation(replace(
            request, command=plan.arguments, output=OutputSpec(job.output, False, "", (), ()),
            collect=False, summary_only=False, dvd_title=job.title, listing_parser=None,
            output_argument_index=plan.output_index, dvd_batch_copy=True,
        ), operation, hooks)
        if result.status == "Cancelled":
            if result.copy_committed:
                completed.append(job.title)
            hooks.log("DVD batch cancelled. Completed copies were retained.")
            return OperationResult("Cancelled", batch_completed=tuple(completed))
        if result.status == "Completed":
            completed.append(job.title)
        else:
            failed.append(job.title)
            hooks.log(f"DVD title {job.title} failed; continuing with the remaining titles.")
    hooks.log("DVD batch completed titles: " + (", ".join(completed) if completed else "none"))
    if failed:
        hooks.log("DVD batch failed or skipped titles: " + ", ".join(failed))
        return OperationResult("Failed", batch_failures=tuple(failed), batch_completed=tuple(completed))
    return OperationResult("Completed", batch_completed=tuple(completed))
