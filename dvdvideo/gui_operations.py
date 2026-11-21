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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dvdvideo.disc_details import parse_disc_details
from dvdvideo.gui_listing import Listing
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


@dataclass(frozen=True)
class OperationResult:
    status: Literal["Completed", "Cancelled", "Failed", "Verified"]
    output: str = ""
    listing: Listing | None = None
    deliver_success: bool = False


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
            with copy_command_arguments(command) as (copy_command, pass_fds):
                hooks.log("$ " + " ".join(copy_command))
                result = operation.run(
                    copy_command, collect=False, log=hooks.queue_log,
                    abandon_unresponsive=True, pass_fds=pass_fds,
                )
        else:
            result = operation.run(
                command, collect=request.collect, log=None if request.quiet_stdout else hooks.queue_log,
                stderr_log=hooks.queue_log if request.quiet_stdout else None,
                timeout=request.timeout, abandon_unresponsive=True,
            )
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
