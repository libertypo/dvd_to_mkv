"""Compare a copy with a freshly recreated reference from its source."""

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

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import cast

from dvdvideo import gui_support
from dvdvideo.gui_preferences import normalize_stream_pid
from dvdvideo.gui_support import (
    ManagedProcess,
    OperationCancelled,
    absolute_path,
)

HASH_LINE = re.compile(r"^\s*(\d+),\s*(\d+),\s*([0-9a-f]{64})(?:,.*)?$")
TYPED_HASH_LINE = re.compile(
    r"^\s*(\d+),\s*([vasdt]),\s*SHA256=([0-9a-f]{64})$"
)


def checked_run(
    operation: ManagedProcess,
    command: list[str],
    log: Callable[[str], None],
    *,
    collect: bool = True,
    pass_fds: tuple[int, ...] = (),
) -> str:
    result = operation.run(
        command,
        collect=collect,
        log=None if collect else log,
        pass_fds=pass_fds,
    )
    if collect and result.stderr:
        log(
            f"{Path(command[0]).name} diagnostics:\n{result.stderr.rstrip()}\n"
        )
    if result.returncode:
        raise RuntimeError(
            f"{Path(command[0]).name} failed with exit status"
            f" {result.returncode}."
        )
    return result.stdout


def file_hash(path: Path, operation: ManagedProcess) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    operation.check_cancelled()
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"Cannot verify non-regular file: {path}")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            while chunk := source.read(1024 * 1024):
                operation.check_cancelled()
                digest.update(chunk)
                size += len(chunk)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    operation.check_cancelled()
    if not size:
        raise RuntimeError(f"Cannot verify empty file: {path}")
    return size, digest.hexdigest()


def stream_hashes(
    path: Path, operation: ManagedProcess, log: Callable[[str], None],
    *, pass_fds: tuple[int, ...] = (),
) -> list[tuple[int | str, str]]:
    output = checked_run(
        operation,
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0",
            "-c",
            "copy",
            "-f",
            "streamhash",
            "-hash",
            "sha256",
            "-",
        ],
        log,
        pass_fds=pass_fds,
    )
    hashes: list[tuple[int | str, str]] = []
    for line in output.splitlines():
        if match := HASH_LINE.fullmatch(line) or TYPED_HASH_LINE.fullmatch(
            line
        ):
            descriptor = match.group(2)
            digest = match.group(3)
            if (
                int(match.group(1)) != len(hashes)
                or descriptor.isdecimal()
                and int(descriptor) == 0
                or digest == hashlib.sha256(b"").hexdigest()
            ):
                raise RuntimeError(
                    "Stream hash output contains missing or empty streams."
                )
            hashes.append(
                (
                    int(descriptor) if descriptor.isdecimal() else descriptor,
                    digest,
                )
            )
        elif line and not line.startswith("#"):
            raise RuntimeError(f"Unexpected stream hash output: {line[:200]}")
    if not hashes:
        raise RuntimeError("FFmpeg produced no stream hashes.")
    return hashes


def metadata(
    path: Path,
    operation: ManagedProcess,
    log: Callable[[str], None],
    *,
    pass_fds: tuple[int, ...] = (),
) -> dict[str, object]:
    output = checked_run(
        operation,
        [
            "ffprobe",
            "-v",
            "error",
            "-show_data_hash",
            "sha256",
            "-show_entries",
            (
                "stream=codec_type,codec_name,width,height,sample_rate,"
                "channels,channel_layout,extradata_hash:"
                "stream_tags=language:chapter=start_time,end_time"
            ),
            "-of",
            "json",
            str(path),
        ],
        log,
        pass_fds=pass_fds,
    )
    try:
        data: object = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"FFprobe returned invalid metadata: {error}"
        ) from error
    if not isinstance(data, dict) or not isinstance(
        cast(dict[str, object], data).get("streams"), list
    ):
        raise RuntimeError("FFprobe did not return stream metadata.")  # noqa: TRY004
    result = cast(dict[str, object], data)
    if not result["streams"]:
        raise RuntimeError("FFprobe did not return stream metadata.")
    return result


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@contextmanager
def _stable_input(
    path: Path, *, optional: bool = False
) -> Generator[tuple[Path, int] | None]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        if not optional:
            raise
        yield None
        try:
            os.stat(path)
        except FileNotFoundError:
            return
        raise RuntimeError(f"Saved file changed during verification: {path}")
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise RuntimeError(f"Cannot verify non-regular file: {path}")
        yield Path(f"/proc/self/fd/{descriptor}"), descriptor
        try:
            current = os.stat(path)
        except FileNotFoundError as error:
            raise RuntimeError(
                f"Saved file changed during verification: {path}"
            ) from error
        if _file_identity(initial) != _file_identity(os.fstat(descriptor)) or (
            _file_identity(initial) != _file_identity(current)
        ):
            raise RuntimeError(
                f"Saved file changed during verification: {path}"
            )
    finally:
        os.close(descriptor)


def verify(
    source: str, copy: Path, *, bluray: bool, title: str, streams: list[str],
    copier: str, operation: ManagedProcess, log: Callable[[str], None],
) -> str:
    operation.check_cancelled()
    copy = copy.absolute()
    if not copy.is_file():
        raise RuntimeError("Choose an existing regular copy file.")
    sidecar = Path(str(copy) + ".chapters.txt")
    with _stable_input(copy) as pinned:
        assert pinned is not None
        with (
            _stable_input(sidecar, optional=True)
            if copy.suffix.lower() == ".m2ts"
            else nullcontext(None)
        ) as chapters:
            report = _verify(
                source, copy, bluray=bluray, title=title, streams=streams,
                copier=copier, operation=operation, log=log,
                pinned_copy=pinned, pinned_sidecar=chapters,
            )
    operation.check_cancelled()
    return report


def _verify(
    source: str, copy: Path, *, bluray: bool, title: str, streams: list[str],
    copier: str, operation: ManagedProcess, log: Callable[[str], None],
    pinned_copy: tuple[Path, int], pinned_sidecar: tuple[Path, int] | None,
) -> str:
    operation.check_cancelled()
    copy = copy.absolute()
    if not copy.is_file():
        raise RuntimeError("Choose an existing regular copy file.")
    suffix = copy.suffix.lower()
    if suffix not in (".mkv", ".m2ts") or (not bluray and suffix != ".mkv"):
        raise RuntimeError("Choose an MKV copy, or an M2TS copy for Blu-ray.")
    if not re.fullmatch(r"[0-9]{1,5}", title) or (
        not bluray and not 1 <= int(title) <= 99
    ):
        raise RuntimeError(
            "Enter a valid DVD title or Blu-ray playlist number."
        )
    if not source:
        raise RuntimeError("Choose the original source.")
    if suffix == ".mkv" and not streams:
        raise RuntimeError("Select the original streams included in the MKV.")
    if bluray and suffix == ".m2ts" and streams:
        raise RuntimeError("M2TS does not support stream selections.")
    if bluray:
        if any(
            not re.fullmatch(r"0[xX][0-9a-fA-F]{1,4}", stream)
            or int(stream, 16) > 0x1FFF
            for stream in streams
        ):
            raise RuntimeError(
                "Blu-ray stream IDs must be hexadecimal PIDs in the range"
                " 0x0000–0x1fff."
            )
        if len({int(stream, 16) for stream in streams}) != len(streams):
            raise RuntimeError("Duplicate Blu-ray stream IDs are not allowed.")
        streams = [normalize_stream_pid(stream) for stream in streams]
    elif any(not re.fullmatch(r"[0-9]{1,4}", stream) for stream in streams):
        raise RuntimeError(
            "DVD stream indexes must contain 1–4 decimal digits."
        )
    elif len({int(stream) for stream in streams}) != len(streams):
        raise RuntimeError("Duplicate DVD stream indexes are not allowed.")

    source = absolute_path(source)
    details = (
        f"  Source: {source}\n"
        f"  Saved copy: {copy}\n"
        f"  {'Blu-ray playlist' if bluray else 'DVD title'}: {title}\n"
        "  Streams:"
        f" {(
            ', '.join(streams)
            if streams else 'unfiltered playlist output (M2TS)'
        )}\n"
    )
    log("\nVerification started\n" + details)
    log(
        "Re-reading the source and creating a temporary reference; this may"
        " take a long time.\n"
    )
    temporary_parent = (
        tempfile.gettempdir() if os.environ.get("TMPDIR") else str(copy.parent)
    )
    with tempfile.TemporaryDirectory(
        prefix="dvd-to-mkv-verify-", dir=temporary_parent
    ) as directory:
        log(f"Temporary reference workspace: {directory}\n")
        reference = Path(directory) / f"reference{suffix}"
        if bluray:
            command = [
                copier,
                "--bluray",
                "--device",
                source,
                "--playlist",
                str(int(title)),
                "--output-format",
                suffix[1:],
            ]
            for stream in streams:
                command += ["--stream-id", f"{int(title)}:{stream}"]
            command += ["--", str(reference)]
        else:
            command = [copier]
            for stream in streams:
                command += ["--stream", str(int(stream))]
            command += ["--", source, str(reference), str(int(title))]
        checked_run(operation, command, log, collect=False)
        operation.check_cancelled()
        if suffix == ".m2ts":
            log("Comparing complete M2TS bytes (SHA-256).\n")
            if file_hash(reference, operation) != file_hash(
                pinned_copy[0], operation
            ):
                raise RuntimeError(
                    "M2TS content differs from the original playlist."
                )
            reference_sidecar = Path(str(reference) + ".chapters.txt")
            if reference_sidecar.exists() != (pinned_sidecar is not None):
                raise RuntimeError(
                    "The M2TS chapter sidecar is missing or unexpected."
                )
            if (
                reference_sidecar.exists()
                and pinned_sidecar is not None
                and file_hash(reference_sidecar, operation)
                != file_hash(pinned_sidecar[0], operation)
            ):
                raise RuntimeError(
                    "M2TS chapter sidecar differs from the source playlist."
                )
            sidecar_result = (
                "matching"
                if reference_sidecar.exists()
                else "absent from both"
            )
            checks = (
                "  Complete M2TS content: matches\n"
                f"  Chapter sidecar: {sidecar_result}\n"
            )
            limitations = (
                "  The reference is playlist output; 3D dependent-view"
                " preservation, playback compatibility and source integrity"
                " were not checked.\n"
            )
        else:
            log(
                "Comparing selected media stream payloads (SHA-256) and"
                " metadata.\n"
            )
            reference_hashes = stream_hashes(reference, operation, log)
            copy_hashes = stream_hashes(
                pinned_copy[0], operation, log, pass_fds=(pinned_copy[1],)
            )
            reference_metadata = metadata(reference, operation, log)
            copy_metadata = metadata(
                pinned_copy[0], operation, log, pass_fds=(pinned_copy[1],)
            )
            reference_streams = reference_metadata["streams"]
            copy_streams = copy_metadata["streams"]
            if not isinstance(reference_streams, list) or not isinstance(
                copy_streams, list
            ):
                raise RuntimeError("FFprobe did not return stream metadata.")
            reference_streams = cast(list[object], reference_streams)
            copy_streams = cast(list[object], copy_streams)
            if (
                len(reference_hashes) != len(reference_streams)
                or len(copy_hashes) != len(copy_streams)
                or (
                    len(reference_streams) < len(streams)
                    if bluray
                    else len(reference_streams) != len(streams)
                )
            ):
                raise RuntimeError(
                    "Could not verify every selected MKV stream."
                )
            if reference_hashes != copy_hashes:
                raise RuntimeError(
                    "MKV stream content differs from the selected source"
                    " streams."
                )
            if reference_metadata != copy_metadata:
                raise RuntimeError(
                    "MKV stream or chapter metadata differs from the"
                    " reference."
                )
            checks = (
                f"  Selected stream payloads: {len(reference_hashes)} match\n "
                " Stream information and codec data hashes: match (where"
                " available)\n  Chapter timestamps: match (if present)\n"
            )
            limitations = (
                "  Packet timestamps, other metadata, playback compatibility, "
                "and source integrity were not checked.\n"
            )
            if bluray:
                limitations += (
                    "  The reference is playlist output; 3D dependent-view"
                    " preservation was not checked.\n"
                )
    operation.check_cancelled()
    limitations += (
        "  Concurrent changes may not always be detected; the copy may change"
        " after verification.\n"
    )
    return (
        "\nVerification report\n  Result: MATCH\n"
        + details
        + checks
        + limitations
    )


def run_verification(
    source: str, copy: Path, *, bluray: bool, title: str, streams: list[str],
    copier: str, operation: ManagedProcess, log: Callable[[str], None],
) -> str:
    request: dict[str, object] = {
        "source": source, "copy": str(copy), "bluray": bluray,
        "title": title, "streams": streams, "copier": copier,
    }
    command = [
        sys.executable,
        "-c",
        (
            "import sys; sys.path.insert(0, sys.argv.pop(1)); from"
            " dvdvideo.verify_copy import gui_main; raise"
            " SystemExit(gui_main())"
        ),
        str(Path(__file__).parent.parent),
    ]
    try:
        result = operation.run(
            command,
            input_data=json.dumps(request, ensure_ascii=True).encode("ascii"),
            stderr_log=log,
            abandon_unresponsive=True,
        )
    except OperationCancelled:
        log(
            "If verification was forcibly stopped, its temporary reference"
            " workspace may need manual cleanup.\n"
        )
        raise
    if result.returncode:
        raise RuntimeError(
            f"verification helper exited with status {result.returncode}"
        )
    if not result.stdout.startswith(
        "\nVerification report\n  Result: MATCH\n"
    ):
        raise RuntimeError("Verification helper returned an invalid report.")
    return result.stdout


def gui_main() -> int:
    operation = ManagedProcess()
    previous_grace = gui_support.TERMINATION_GRACE
    gui_support.TERMINATION_GRACE = 1
    old_handlers = {
        signum: signal.signal(
            signum, lambda _signum, _frame: operation.cancel()
        )
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        data: object = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise TypeError("Invalid verification request.")
        request = cast(dict[str, object], data)
        source, copy, bluray, title, streams, copier = (
            request.get(key)
            for key in (
                "source",
                "copy",
                "bluray",
                "title",
                "streams",
                "copier",
            )
        )
        if (
            not isinstance(source, str)
            or not isinstance(copy, str)
            or not isinstance(bluray, bool)
            or not isinstance(title, str)
            or not isinstance(streams, list)
            or not isinstance(copier, str)
            or not all(
                isinstance(stream, str)
                for stream in cast(list[object], streams)
            )
        ):
            raise ValueError("Invalid verification request.")
        report = verify(
            source,
            Path(copy),
            bluray=bluray,
            title=title,
            streams=cast(list[str], streams),
            copier=copier,
            operation=operation,
            log=lambda text: print(text, end="", file=sys.stderr, flush=True),
        )
        operation.check_cancelled()
        print(report, end="", flush=True)
        return 0
    except OperationCancelled:
        print("Verification cancelled.", file=sys.stderr, flush=True)
        return 130
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Verification failed: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        gui_support.TERMINATION_GRACE = previous_grace
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--copy", required=True, type=Path)
    parser.add_argument("--media", choices=("dvd", "bluray"), required=True)
    parser.add_argument(
        "--title", required=True, help="DVD title or Blu-ray playlist number"
    )
    parser.add_argument(
        "--stream",
        action="append",
        default=[],
        help="DVD index or Blu-ray PID",
    )
    parser.add_argument("--copier", default="dvd-to-mkv.sh")
    args = parser.parse_args()
    operation = ManagedProcess()
    old_handlers = {
        signum: signal.signal(
            signum, lambda _signum, _frame: operation.cancel()
        )
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        report = verify(
            args.source,
            args.copy,
            bluray=args.media == "bluray",
            title=args.title,
            streams=args.stream,
            copier=args.copier,
            operation=operation,
            log=lambda text: print(text, end="", flush=True),
        )
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, old_handlers)
        try:
            if signal.sigpending().intersection(old_handlers):
                operation.cancel()
            operation.check_cancelled()
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        print(report, end="", flush=True)
    except OperationCancelled:
        print(
            "Verification cancelled; the original and saved copy were not"
            " modified.",
            file=sys.stderr,
        )
        return 130
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Verification failed: {error}", file=sys.stderr)
        return 1
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
