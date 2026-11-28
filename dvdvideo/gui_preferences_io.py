"""Atomic preference writes and a bounded, single-worker GUI write queue."""

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

import json
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

PreferenceSnapshot = Callable[[], dict[str, Any]]
MAX_PREFERENCES_BYTES = 4 * 1024 * 1024


class PreferencesDurabilityError(OSError):
    """Replacement committed, but crash durability could not be confirmed."""


def write_preferences(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".preferences-", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=True, indent=2)
            file.write("\n")
            if file.tell() > MAX_PREFERENCES_BYTES:
                raise ValueError("preferences file exceeds the 4 MiB limit")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise PreferencesDurabilityError(
            f"Preferences were replaced at {path}, but crash durability could"
            f" not be confirmed: {error}"
        ) from error


class PreferencesWriter:
    def __init__(self, on_error: Callable[[str], None]) -> None:
        self.on_error = on_error
        self.lock = threading.Lock()
        self.pending: tuple[Path, PreferenceSnapshot] | None = None
        self.thread: threading.Thread | None = None
        self.failed: tuple[Path, PreferenceSnapshot] | None = None
        self._error: str | None = None
        self._abandoned = False

    @property
    def busy(self) -> bool:
        with self.lock:
            return self.thread is not None

    def submit(self, path: Path, snapshot: PreferenceSnapshot) -> None:
        with self.lock:
            if self._abandoned:
                raise OSError(
                    "GUI preferences writer was abandoned during shutdown"
                )
            self.pending = (path, snapshot)
            self._start()

    def abandon(self) -> None:
        with self.lock:
            self._abandoned = True
            self.pending = None

    @property
    def error(self) -> str | None:
        with self.lock:
            return self._error

    def retry(self) -> None:
        with self.lock:
            if (
                self._abandoned
                or self.thread is not None
                or self.failed is None
            ):
                raise OSError(
                    "No failed GUI preferences write is available to retry"
                )
            self.pending = self.failed
            self._start()

    def _start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            try:
                self.thread.start()
            except RuntimeError as error:
                self.thread = None
                self.pending = None
                raise OSError(
                    "Could not start GUI preferences writer"
                ) from error

    def _run(self) -> None:
        while True:
            with self.lock:
                pending = self.pending
                self.pending = None
                if pending is None:
                    self.thread = None
                    return
            try:
                path, snapshot = pending
                data = snapshot()
                with self.lock:
                    if self._abandoned:
                        self.thread = None
                        return
                write_preferences(path, data)
            except Exception as error:  # noqa: BLE001 - report failed writes and keep processing queued saves
                message = (
                    f"GUI preferences write failed: {type(error).__name__}:"
                    f" {error}"
                )
                with self.lock:
                    self.failed = pending
                    self._error = message
                self.on_error(message)
            else:
                with self.lock:
                    self.failed = None
                    self._error = None
