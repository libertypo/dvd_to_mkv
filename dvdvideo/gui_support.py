"""GTK-independent process supervision and output validation for the GUI."""

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

import codecs
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager

MAX_INSPECTION_BYTES = 8 * 1024 * 1024
TERMINATION_GRACE = 3
MAX_DIRECT_ARGUMENT_BYTES = 64 * 1024
_unreaped_helpers: set[subprocess.Popen[bytes]] = set()
_unreaped_lock = threading.Lock()


def _reap_helper(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait()
    finally:
        with _unreaped_lock:
            _unreaped_helpers.discard(process)


@contextmanager
def copy_command_arguments(command: list[str]) -> Generator[tuple[list[str], tuple[int, ...]]]:
    size = sum(len(os.fsencode(argument)) + 1 + 8 for argument in command)
    if size <= MAX_DIRECT_ARGUMENT_BYTES:
        yield command, ()
        return
    with os.fdopen(os.memfd_create("dvd-to-mkv-arguments", os.MFD_CLOEXEC), "wb") as file:
        for argument in command[1:]:
            encoded = os.fsencode(argument)
            if b"\0" in encoded:
                raise ValueError("Command arguments cannot contain NUL bytes.")
            file.write(encoded + b"\0")
        file.write(b"--end-arguments-file\0")
        file.flush()
        path = f"/proc/self/fd/{file.fileno()}"
        yield [command[0], "--arguments-file", path], (file.fileno(),)


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

    def abandon_process(self, notify: Callable[[str], None] | None) -> None:
        assert self.process is not None
        process = self.process
        with _unreaped_lock:
            _unreaped_helpers.add(process)
        try:
            threading.Thread(target=_reap_helper, args=(process,), daemon=True).start()
        except RuntimeError as error:
            raise RuntimeError(
                "Could not start the filesystem helper cleanup thread; "
                "the helper may remain until the application exits."
            ) from error
        if notify:
            notify(
                "Helper did not exit after termination; cleanup may be delayed. "
                "Descendants in other process groups may remain.\n"
            )

    def stop_group(
        self, *, abandon_unresponsive: bool = False, notify: Callable[[str], None] | None = None,
    ) -> None:
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
        if abandon_unresponsive:
            deadline = time.monotonic() + 0.2
            while self.process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if self.process.poll() is None:
                self.abandon_process(notify)
                return
        self.process.wait()

    def run(
        self, command: list[str], collect: bool = True,
        log: Callable[[str], None] | None = None, timeout: float | None = None,
        *, input_data: bytes | None = None, stderr_log: Callable[[str], None] | None = None,
        abandon_unresponsive: bool = False, pass_fds: tuple[int, ...] = (),
        termination_log: Callable[[str], None] | None = None,
        preserve_success_on_cancel: bool = False,
        cancelled_exit_code: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.check_cancelled()
        if abandon_unresponsive:
            with _unreaped_lock:
                if _unreaped_helpers:
                    raise RuntimeError(
                        "A previous filesystem helper, copier, preview, or inspection is still terminating; "
                        "wait for its filesystem to respond."
                    )
        output: list[str] = []
        errors: list[str] = []
        size = 0
        error_size = 0
        started = time.monotonic()
        terminating = None
        timed_out = False
        cleaned = False
        pipes_abandoned = False
        input_incomplete = False
        input_offset = 0
        decoders = {
            channel: codecs.getincrementaldecoder("utf-8")(errors="replace")
            for channel in ("stdout", "stderr")
        }
        self.process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL, start_new_session=True,
            pass_fds=pass_fds,
        )
        try:
            assert self.process.stdout is not None
            assert self.process.stderr is not None
            with ExitStack() as stack:
                stdout = stack.enter_context(self.process.stdout)
                stderr = stack.enter_context(self.process.stderr)
                selector = stack.enter_context(selectors.DefaultSelector())
                selector.register(stdout, selectors.EVENT_READ, "stdout")
                selector.register(stderr, selectors.EVENT_READ, "stderr")
                stdin = stack.enter_context(self.process.stdin) if self.process.stdin is not None else None
                if stdin is not None:
                    os.set_blocking(stdin.fileno(), False)
                    if input_data:
                        selector.register(stdin, selectors.EVENT_WRITE, "stdin")
                    else:
                        stdin.close()
                while selector.get_map() or self.process.poll() is None:
                    now = time.monotonic()
                    timed_out = timed_out or (timeout is not None and now - started >= timeout)
                    if (self.cancelled.is_set() or timed_out) and terminating is None:
                        self.send_signal(signal.SIGTERM)
                        terminating = now
                        if stdin is not None and not stdin.closed:
                            selector.unregister(stdin)
                            stdin.close()
                    if self.process.poll() is not None and terminating is None:
                        if self.group_exists():
                            if log:
                                log("Stopping subprocess descendants remaining after command exit.\n")
                            self.send_signal(signal.SIGTERM)
                        terminating = now
                    if terminating is not None and now - terminating >= TERMINATION_GRACE:
                        self.send_signal(signal.SIGKILL)
                        if (abandon_unresponsive and self.process.poll() is None
                                and now - terminating >= TERMINATION_GRACE + 0.2):
                            cleaned = True
                            self.abandon_process(termination_log or log or stderr_log)
                            if timed_out and timeout is not None:
                                raise subprocess.TimeoutExpired(command, timeout)
                            self.check_cancelled()
                            raise RuntimeError("Helper did not exit after termination.")
                        if self.process.poll() is not None and now - terminating >= TERMINATION_GRACE + 0.2:
                            pipes_abandoned = any(key.data != "stdin" for key in selector.get_map().values())
                            warning_log = termination_log or log or stderr_log
                            if pipes_abandoned and warning_log:
                                warning_log(
                                    "Stopped waiting for output pipes held open after process termination; "
                                    "a detached descendant may still be running outside the managed process group.\n"
                                )
                            break
                    for key, _events in selector.select(0.1):
                        if key.data == "stdin":
                            assert input_data is not None and stdin is not None
                            try:
                                input_offset += os.write(key.fd, input_data[input_offset:input_offset + 65536])
                            except BlockingIOError:
                                continue
                            except BrokenPipeError:
                                input_incomplete = True
                                selector.unregister(stdin)
                                stdin.close()
                                continue
                            if input_offset == len(input_data):
                                selector.unregister(stdin)
                                stdin.close()
                            continue
                        data = os.read(key.fd, 8192)
                        text = decoders[key.data].decode(data, final=not data).replace("\r", "\n").replace("\x00", "")
                        callback = stderr_log if key.data == "stderr" and stderr_log is not None else log
                        if callback and text:
                            callback(text)
                        if collect and key.data == "stdout":
                            size += len(data)
                            if size > MAX_INSPECTION_BYTES:
                                raise RuntimeError("Inspection output exceeded the 8 MiB limit.")
                            output.append(text)
                        elif collect and key.data == "stderr" and text:
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
            # Only copy commands opt in: their successful exit confirms publication.
            if not (
                preserve_success_on_cancel and returncode == 0
                or cancelled_exit_code == returncode and self.cancelled.is_set()
            ):
                self.check_cancelled()
            if pipes_abandoned:
                raise RuntimeError("Subprocess output pipes remained open after termination; output is incomplete.")
            if input_incomplete and returncode == 0:
                raise RuntimeError("Subprocess closed its input before receiving the complete request.")
            if self.group_exists():
                if log:
                    log("Stopping subprocess descendants remaining after command exit.\n")
                self.stop_group()
                cleaned = True
            return subprocess.CompletedProcess(command, returncode, "".join(output), "".join(errors))
        finally:
            if not cleaned and (self.process.poll() is None or self.group_exists()):
                self.stop_group(
                    abandon_unresponsive=abandon_unresponsive,
                    notify=termination_log or log or stderr_log,
                )


def validate_output_paths(
    output: str, is_bluray: bool, name: str, playlists: list[str], operation: ManagedProcess,
    formats: dict[str, str] | None = None,
) -> None:
    operation.check_cancelled()
    paths = [output]
    if is_bluray and os.path.isdir(output):
        if name != filename_stem(name):
            raise ValueError(
                "Filename must be at most 160 UTF-8 bytes, without path separators, "
                "reserved characters, or leading/trailing dots."
            )
        if name.lower().endswith((".mkv", ".m2ts")):
            raise ValueError("Filename must be a stem without an .mkv or .m2ts extension.")
        paths = [
            os.path.join(output, (name if name and len(playlists) == 1
                                  else f"{name + '-' if name else ''}playlist-{pid}")
                         + "." + (formats or {}).get(pid, "mkv"))
            for pid in playlists
        ]
        paths += [path + ".chapters.txt" for path in paths if path.endswith(".m2ts")]
    elif not is_bluray and os.path.isdir(output):
        raise ValueError("DVD output must be an MKV filename, not a directory.")
    elif is_bluray and len(playlists) > 1:
        raise ValueError("Multiple Blu-ray playlists require an existing output directory.")
    elif is_bluray and output.lower().endswith(".m2ts"):
        paths.append(output + ".chapters.txt")
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
