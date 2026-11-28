"""GTK-independent state and workers for source detection and drive scans."""

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

import argparse
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import FrameType
from typing import cast

from dvdvideo.gui_source import detect_media
from dvdvideo.gui_support import ManagedProcess, OperationCancelled
from dvdvideo.optical_drives import OpticalDrive, discover_drives

logger = logging.getLogger(__name__)


@dataclass
class SourceWorkflow:
    detection_generation: int = 0
    detection_timeout: int | None = None
    detecting: bool = False
    detection_probe: ManagedProcess | None = None
    drive_scan: ManagedProcess | None = None
    drive_scan_discarded: bool = False
    source_generation: int = 0
    chooser_generation: int = 0
    device_sources: set[str] = field(default_factory=set[str])
    applying_detection: bool = False
    detection_worker: ManagedProcess | None = None
    pending_detection: tuple[str, int] | None = None
    drive_scan_restart: bool = False

    def cancel_detection(
        self, remove_timeout: Callable[[int], object]
    ) -> None:
        self.detection_generation += 1
        if self.detection_timeout is not None:
            remove_timeout(self.detection_timeout)
            self.detection_timeout = None
        self.detecting = False
        self.pending_detection = None
        if self.detection_probe is not None:
            self.detection_probe.cancel()
            self.detection_probe = None

    def schedule_detection(
        self, source: str, schedule: Callable[[str, int], int]
    ) -> None:
        self.detection_timeout = schedule(source, self.detection_generation)

    def is_current(
        self,
        source: str,
        generation: int,
        current_source: str,
        *,
        closing: bool,
    ) -> bool:
        return (
            not closing
            and generation == self.detection_generation
            and source == current_source
        )

    def begin_detection(
        self,
        source: str,
        generation: int,
        current_source: str,
        *,
        closing: bool,
    ) -> ManagedProcess | None:
        if not self.is_current(
            source, generation, current_source, closing=closing
        ):
            return None
        self.detection_timeout = None
        if self.detection_worker is not None:
            if self.detection_probe is not self.detection_worker:
                self.pending_detection = (source, generation)
            return None
        self.detection_probe = ManagedProcess()
        self.detection_worker = self.detection_probe
        return self.detection_probe

    def finish_detection(
        self,
        source: str,
        generation: int,
        current_source: str,
        *,
        closing: bool,
    ) -> bool:
        if not self.is_current(
            source, generation, current_source, closing=closing
        ):
            return False
        self.detecting = False
        self.detection_probe = None
        return True

    def detection_finished(
        self, operation: ManagedProcess
    ) -> tuple[str, int] | None:
        if self.detection_worker is not operation:
            return None
        self.detection_worker = None
        pending = self.pending_detection
        self.pending_detection = None
        return pending

    def begin_drive_scan(self) -> ManagedProcess:
        if self.drive_scan is not None:
            raise RuntimeError("Drive discovery is already running")
        operation = ManagedProcess()
        self.drive_scan = operation
        self.drive_scan_discarded = False
        return operation

    def finish_drive_scan(
        self, operation: ManagedProcess, *, closing: bool
    ) -> bool:
        if self.drive_scan is not operation:
            return False
        self.drive_scan = None
        self.drive_scan_restart = False
        return not closing and not self.drive_scan_discarded

    def cancel_drive_scan(self, *, discard: bool = False) -> None:
        self.drive_scan_restart = False
        if self.drive_scan is not None:
            self.drive_scan.cancel()
            if discard:
                self.drive_scan_discarded = True


def detect_source(
    source: str, generation: int, probe: ManagedProcess, *,
    deliver: Callable[[str, int, str | None, str | None, bool | None], None],
    exclude_device: Callable[[str], None], log: Callable[[str], None],
    isolate: bool = False,
) -> None:
    try:
        probe.check_cancelled()
        if isolate:
            media, error, device = probe_source(source, probe, log)
            if device:
                exclude_device(source)
            if error is not None:
                deliver(source, generation, None, error, None)
                return
        else:
            device = os.path.exists(source) and stat.S_ISBLK(
                os.stat(source).st_mode
            )
            if device:
                exclude_device(source)
            media = detect_media(source, probe)
        probe.check_cancelled()
    except OperationCancelled:
        return
    except FileNotFoundError:
        deliver(source, generation, None, "Source does not exist.", None)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        deliver(source, generation, None, str(error), None)
    except Exception as error:  # noqa: BLE001 - worker boundary reports unexpected errors to Activity
        log(
            "Unexpected media detection error:\n"
            + traceback.format_exc().rstrip()
        )
        deliver(
            source,
            generation,
            None,
            f"Unexpected error: {type(error).__name__}: {error}",
            None,
        )
    else:
        deliver(source, generation, media, None, device)


def _probe_command(arguments: list[str]) -> list[str]:
    return [
        sys.executable,
        "-c",
        (
            "import sys; sys.path.insert(0, sys.argv.pop(1)); from"
            " dvdvideo.gui_source_workflow import main; raise"
            " SystemExit(main())"
        ),
        str(Path(__file__).parent.parent),
        *arguments,
    ]


def _run_probe(
    arguments: list[str],
    operation: ManagedProcess,
    log: Callable[[str], None],
    label: str,
) -> dict[str, object]:
    # Keep blocking filesystem calls out of the GUI's worker process.
    result = operation.run(
        _probe_command(arguments),
        timeout=35,
        abandon_unresponsive=True,
        stderr_log=log,
    )
    if result.returncode:
        raise RuntimeError(
            f"{label} probe failed with exit status {result.returncode}"
        )
    try:
        data: object = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(f"{label} probe returned invalid JSON") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} probe returned an invalid result")  # noqa: TRY004
    return cast(dict[str, object], data)


def probe_source(
    source: str, operation: ManagedProcess, log: Callable[[str], None],
) -> tuple[str | None, str | None, bool]:
    record = _run_probe(["--", source], operation, log, "Source")
    media, error, device = (
        record.get("media"),
        record.get("error"),
        record.get("device"),
    )
    if not isinstance(device, bool) or not (
        (
            isinstance(media, str)
            and media in ("dvd", "bluray")
            and error is None
        )
        or (media is None and isinstance(error, str) and error)
    ):
        raise RuntimeError("Source probe returned an invalid result")
    if isinstance(media, str):
        return media, None, device
    if isinstance(error, str):
        return None, error, device
    raise RuntimeError("Source probe returned neither media nor an error")


def _drive_record(item: object) -> OpticalDrive:
    if not isinstance(item, dict):
        raise RuntimeError("Drive probe returned an invalid drive")  # noqa: TRY004
    drive = cast(dict[str, object], item)
    path, name, media, status = (
        drive.get(key) for key in ("path", "name", "media", "status")
    )
    if (
        not isinstance(path, str) or not path or not isinstance(name, str)
        or not isinstance(status, str) or "media" not in drive
        or media not in (None, "dvd", "bluray")
    ):
        raise RuntimeError("Drive probe returned an invalid drive")
    return OpticalDrive(path, name, cast(str | None, media), status)


def probe_drives(
    operation: ManagedProcess, log: Callable[[str], None],
) -> tuple[list[OpticalDrive], str | None]:
    completed: list[OpticalDrive] = []
    pending = ""

    def receive(text: str) -> None:
        nonlocal pending
        pending += text
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            try:
                event: object = json.loads(line)
            except ValueError as error:
                raise RuntimeError(
                    "Drive probe returned invalid JSON"
                ) from error
            if not isinstance(event, dict):
                raise RuntimeError("Drive probe returned an invalid result")  # noqa: TRY004
            event = cast(dict[str, object], event)
            if event.get("event") == "drive":
                completed.append(_drive_record(event.get("drive")))
            elif event.get("event") != "progress" and "drives" not in event:
                raise RuntimeError("Drive probe returned an invalid result")

    try:
        result = operation.run(
            _probe_command(["--scan-drives"]),
            timeout=35,
            abandon_unresponsive=True,
            log=receive,
            stderr_log=log,
            termination_log=log,
            reset_timeout_on_stdout=True,
        )
        if result.returncode:
            return (
                completed,
                f"Drive probe failed with exit status {result.returncode}",
            )
        try:
            lines = result.stdout.splitlines()
            record: object = json.loads(lines[-1]) if lines else None
        except ValueError as error:
            raise RuntimeError("Drive probe returned invalid JSON") from error
        if not isinstance(record, dict):
            raise RuntimeError("Drive probe returned an invalid result")  # noqa: TRY004
        record = cast(dict[str, object], record)
        items, error = record.get("drives"), record.get("error")
        if not isinstance(items, list) or not (
            error is None or isinstance(error, str) and error and not items
        ):
            raise RuntimeError("Drive probe returned an invalid result")
        drives = [_drive_record(item) for item in cast(list[object], items)]
        return (completed if error else drives), error
    except OperationCancelled:
        raise
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return completed, f"Drive discovery failed: {error}"
    except Exception:  # noqa: BLE001 - worker boundary reports the traceback to Activity
        return (
            completed,
            "Unexpected drive discovery error:\n"
            + traceback.format_exc().rstrip(),
        )


def scan_drives(
    operation: ManagedProcess, *, isolate: bool = False,
    log: Callable[[str], None] = logger.warning,
    progress: Callable[[], None] = lambda: None,
    found: Callable[[OpticalDrive], None] = lambda _drive: None,
) -> tuple[list[OpticalDrive], str | None]:
    try:
        operation.check_cancelled()
        if isolate:
            drives, error = probe_drives(operation, log)
        else:
            drives, error = (
                discover_drives(detect_media, operation, progress, found),
                None,
            )
        operation.check_cancelled()
        return drives, error
    except OperationCancelled:
        return [], "Drive scan cancelled."
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return [], f"Drive discovery failed: {error}"
    except Exception:  # noqa: BLE001 - worker boundary reports the traceback to Activity
        return (
            [],
            "Unexpected drive discovery error:\n"
            + traceback.format_exc().rstrip(),
        )


def main() -> int:
    from dvdvideo import gui_support

    parser = argparse.ArgumentParser(
        description="Isolated source and drive detection for the GUI"
    )
    parser.add_argument("source", nargs="?")
    parser.add_argument("--scan-drives", action="store_true")
    args = parser.parse_args()
    if args.scan_drives == (args.source is not None):
        parser.error("Choose either a source or --scan-drives")
    operation = ManagedProcess()

    def cancel(_signum: int, _frame: FrameType | None) -> None:
        operation.cancel()

    handlers = {
        signum: signal.signal(signum, cancel)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    # Let the helper's managed 7z process stop before the supervisor kills it.
    previous_grace = gui_support.TERMINATION_GRACE
    gui_support.TERMINATION_GRACE = 1
    result: tuple[str | None, str | None] | None = None
    device = False

    def deliver(
        _source: str,
        _generation: int,
        media: str | None,
        error: str | None,
        _device: bool | None,
    ) -> None:
        nonlocal result
        result = media, error

    def exclude_device(_source: str) -> None:
        nonlocal device
        device = True

    try:
        if args.scan_drives:
            def progress() -> None:
                print(json.dumps({"event": "progress"}), flush=True)

            def found(drive: OpticalDrive) -> None:
                print(
                    json.dumps({"event": "drive", "drive": asdict(drive)}),
                    flush=True,
                )

            drives, error = scan_drives(
                operation, progress=progress, found=found
            )
            if operation.cancelled.is_set():
                return 130
            print(
                json.dumps(
                    {
                        "drives": [asdict(drive) for drive in drives],
                        "error": error,
                    }
                )
            )
            return 0
        detect_source(
            args.source,
            0,
            operation,
            deliver=deliver,
            exclude_device=exclude_device,
            log=lambda message: print(message, file=sys.stderr),
        )
        if result is None:
            return 130
        media, error = result
        print(json.dumps({"media": media, "error": error, "device": device}))
        return 0
    finally:
        gui_support.TERMINATION_GRACE = previous_grace
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
