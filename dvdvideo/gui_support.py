"""GTK-independent process supervision and output validation for the GUI."""

from __future__ import annotations

import codecs
from collections.abc import Callable
import os
import re
import selectors
import signal
import subprocess
import threading
import time


MAX_INSPECTION_BYTES = 8 * 1024 * 1024
TERMINATION_GRACE = 3


def filename_stem(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]', "_", name).strip(" .")
    return name.encode("utf-8")[:160].decode("utf-8", errors="ignore").rstrip(" .")


class OperationCancelled(Exception):
    pass


class ManagedProcess:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.process: subprocess.Popen[bytes] | None = None

    def cancel(self) -> None:
        self.cancelled.set()

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise OperationCancelled()

    def send_signal(self, sig: signal.Signals) -> None:
        assert self.process is not None
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            pass

    def group_exists(self) -> bool:
        assert self.process is not None
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def stop_group(self) -> None:
        assert self.process is not None
        self.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + TERMINATION_GRACE
        while time.monotonic() < deadline:
            self.process.poll()
            if not self.group_exists():
                break
            time.sleep(0.05)
        if self.group_exists():
            self.send_signal(signal.SIGKILL)
        self.process.wait()

    def run(
        self, command: list[str], collect: bool = True,
        log: Callable[[str], None] | None = None, timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.check_cancelled()
        output: list[str] = []
        errors: list[str] = []
        size = 0
        error_size = 0
        started = time.monotonic()
        terminating = None
        timed_out = False
        cleaned = False
        decoders = {
            channel: codecs.getincrementaldecoder("utf-8")(errors="replace")
            for channel in ("stdout", "stderr")
        }
        self.process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            assert self.process.stdout is not None
            assert self.process.stderr is not None
            with self.process.stdout as stdout, self.process.stderr as stderr, selectors.DefaultSelector() as selector:
                selector.register(stdout, selectors.EVENT_READ, "stdout")
                selector.register(stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map() or self.process.poll() is None:
                    now = time.monotonic()
                    timed_out = timed_out or (timeout is not None and now - started >= timeout)
                    if (self.cancelled.is_set() or timed_out) and terminating is None:
                        self.send_signal(signal.SIGTERM)
                        terminating = now
                    if self.process.poll() is not None and self.group_exists() and terminating is None:
                        if log:
                            log("Stopping subprocess descendants remaining after command exit.\n")
                        self.send_signal(signal.SIGTERM)
                        terminating = now
                    if terminating is not None and now - terminating >= TERMINATION_GRACE:
                        self.send_signal(signal.SIGKILL)
                    for key, _events in selector.select(0.1):
                        data = os.read(key.fd, 8192)
                        text = decoders[key.data].decode(data, final=not data).replace("\r", "\n").replace("\x00", "")
                        if log and text:
                            log(text)
                        if collect and key.data == "stdout":
                            size += len(data)
                            if size > MAX_INSPECTION_BYTES:
                                raise RuntimeError("Inspection output exceeded the 8 MiB limit.")
                            output.append(text)
                        elif collect and key.data == "stderr":
                            errors.append(text)
                            error_size += len(text)
                            # Keep a bounded diagnostic tail for probe errors.
                            while error_size > 65536 and len(errors) > 1:
                                error_size -= len(errors.pop(0))
                        if not data:
                            selector.unregister(key.fileobj)
                returncode = self.process.wait()
            if timed_out and timeout is not None:
                raise subprocess.TimeoutExpired(command, timeout)
            self.check_cancelled()
            if self.group_exists():
                if log:
                    log("Stopping subprocess descendants remaining after command exit.\n")
                self.stop_group()
                cleaned = True
            return subprocess.CompletedProcess(command, returncode, "".join(output), "".join(errors))
        finally:
            if not cleaned and (self.process.poll() is None or self.group_exists()):
                self.stop_group()


def validate_output_paths(
    output: str, is_bluray: bool, name: str, playlists: list[str], operation: ManagedProcess,
) -> None:
    operation.check_cancelled()
    paths = [output]
    if is_bluray and os.path.isdir(output):
        if name != filename_stem(name):
            raise ValueError(
                "Filename must be at most 160 UTF-8 bytes, without path separators, "
                "reserved characters, or leading/trailing dots."
            )
        paths = [
            os.path.join(output, f"{name}.mkv" if name and len(playlists) == 1
                         else f"{name + '-' if name else ''}playlist-{pid}.mkv")
            for pid in playlists
        ]
    elif not is_bluray and os.path.isdir(output):
        raise ValueError("DVD output must be an MKV filename, not a directory.")
    elif is_bluray and len(playlists) > 1:
        raise ValueError("Multiple Blu-ray playlists require an existing output directory.")
    for path in paths:
        operation.check_cancelled()
        directory = os.path.dirname(os.path.abspath(path))
        limit = os.pathconf(directory, "PC_NAME_MAX")
        operation.check_cancelled()
        if limit > 0 and len(os.fsencode(os.path.basename(path))) > limit:
            raise ValueError(f"Output filename exceeds the filesystem's {limit}-byte limit: {path}")
        if os.path.lexists(path):
            raise ValueError(f"Refusing to overwrite existing file: {path}")
    operation.check_cancelled()
