"""Atomic preference writes and a bounded, single-worker GUI write queue."""

import json
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any


def write_preferences(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".preferences-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=True, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class PreferencesWriter:
    def __init__(self, on_error: Callable[[str], None]) -> None:
        self.on_error = on_error
        self.lock = threading.Lock()
        self.pending: tuple[Path, dict[str, Any]] | None = None
        self.thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self.lock:
            return self.thread is not None

    def submit(self, path: Path, data: dict[str, Any]) -> None:
        with self.lock:
            self.pending = (path, data)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run)
                try:
                    self.thread.start()
                except RuntimeError as error:
                    self.thread = None
                    self.pending = None
                    raise OSError("Could not start GUI preferences writer") from error

    def _run(self) -> None:
        while True:
            with self.lock:
                pending = self.pending
                self.pending = None
                if pending is None:
                    self.thread = None
                    return
            try:
                write_preferences(*pending)
            except Exception as error:  # noqa: BLE001 - report failed writes and keep processing queued saves
                self.on_error(f"Could not save GUI preferences: {type(error).__name__}: {error}")
