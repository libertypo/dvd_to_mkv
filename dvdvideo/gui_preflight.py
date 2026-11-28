"""Isolated filesystem checks needed before starting a GUI operation."""

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

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import cast

from dvdvideo import gui_support
from dvdvideo.bluray_chapters import playlist_title
from dvdvideo.gui_commands import build_disc_details_command
from dvdvideo.gui_source import validate_source_links
from dvdvideo.gui_support import (
    ManagedProcess,
    OperationCancelled,
    validate_output_paths,
)
from dvdvideo.gui_translation import identity, load_translation
from dvdvideo.output_space import required_bytes, source_bytes
from dvdvideo.preview import preview_command

PREFLIGHT_TIMEOUT = 15
SOURCE_UNPINNED_WARNING = (
    "Warning: The source is not pinned and may change or be replaced. "
    "Use a source protected from changes and keep the same disc inserted."
)


@dataclass(frozen=True)
class OperationPreflight:
    source_is_block_device: bool = False
    directory_output: bool = False
    output_path: str | None = None
    preview_command: tuple[str, ...] | None = None
    output_error: str | None = None
    source_classified: bool = False
    copy_summary: str | None = None
    ambiguous_output: bool = False
    source_is_directory: bool = False
    reviewed_identity: tuple[int, int] | None = None


def copy_summary(
    source: str,
    script: str,
    output: str,
    name: str,
    playlists: list[str],
    formats: dict[str, str],
    directory: bool,
    bluray: bool,
    operation: ManagedProcess,
    dvd_title: str = "1",
    *,
    translate: Callable[[str], str] = identity,
    directory_fd: int | None = None,
) -> str:
    absolute_source = (
        source if os.path.isabs(source) else os.path.join(os.getcwd(), source)
    )
    lines = [
        translate("Source: {path}").format(path=absolute_source),
        translate(SOURCE_UNPINNED_WARNING),
        "",
    ]
    if bluray:
        lines += [
            translate(
                "Warning: MKV and M2TS copies may omit the 3D dependent view;"
                " 3D preservation is not verified."
            ),
            "",
        ]
    sizes: list[tuple[int, int]] = []
    for playlist in playlists if bluray else [dvd_title]:
        operation.check_cancelled()
        output_format = formats[playlist] if bluray else "mkv"
        path = output
        if bluray and directory:
            stem = (
                name
                if name and len(playlists) == 1
                else f"{name + '-' if name else ''}playlist-{playlist}"
            )
            path = os.path.join(output, f"{stem}.{output_format}")
        if bluray:
            result = operation.run(
                build_disc_details_command(
                    script, source, is_bluray=True, playlist=playlist
                ),
                timeout=60,
                abandon_unresponsive=True,
            )
            if result.returncode:
                raise RuntimeError(
                    translate(
                        "Cannot estimate playlist {playlist}: {error}"
                    ).format(
                        playlist=playlist,
                        error=result.stderr.strip(),
                    )
                )
            size = playlist_title(json.loads(result.stdout), playlist).get(
                "filesize"
            )
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or not 0 < size <= 2**63 - 1
            ):
                raise ValueError(
                    "Playlist size must be a positive integer no greater than"
                    " 2^63 - 1."
                )
        else:
            size = source_bytes(source)
        need = required_bytes(size, output_format, is_bluray=bluray)
        sizes.append((size, need))
        lines += [
            (
                translate("Playlist {playlist} - {format}")
                if bluray
                else translate("DVD title {playlist} - {format}")
            ).format(
                playlist=playlist,
                format=output_format.upper(),
            ),
            translate("Output: {path}").format(
                path=(
                    path
                    if os.path.isabs(path)
                    else os.path.join(os.getcwd(), path)
                ),
            ),
        ]
        if output_format == "m2ts":
            lines += [
                translate(
                    "M2TS contains the unfiltered playlist output; it may omit"
                    " a 3D dependent view."
                ),
                translate(
                    "Chapter sidecar (if chapters exist): {path}"
                ).format(
                    path=(
                        path
                        if os.path.isabs(path)
                        else os.path.join(os.getcwd(), path)
                    )
                    + ".chapters.txt",
                ),
            ]
        lines.append("")
    retained = peak = 0
    for size, need in sizes:
        peak = max(peak, retained + need)
        retained += size + (size + 19) // 20
    folder = output if directory else os.path.dirname(output) or "."
    available = shutil.disk_usage(
        directory_fd if directory_fd is not None else folder
    ).free
    lines += [
        translate(
            "Estimated peak space including temporary files and reserve:"
            " {gib:.2f} GiB ({size} bytes)"
        ).format(
            gib=peak / 1024**3,
            size=peak,
        ),
        translate("Available space: {gib:.2f} GiB ({size} bytes)").format(
            gib=available / 1024**3, size=available
        ),
        translate(
            "Conservative estimate, not the final output size. Free space is"
            " checked again when copying."
        ),
    ]
    if available < peak:
        raise ValueError(
            translate("Not enough free space for this copy selection.")
            + "\n"
            + "\n".join(lines)
        )
    return "\n".join(lines)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    return cast(dict[str, object], value)


def perform_preflight(
    request: object, operation: ManagedProcess
) -> dict[str, object]:
    data = _object(request, "preflight request")
    source = data.get("source")
    is_bluray = data.get("is_bluray")
    is_preview = data.get("preview")
    playlist = data.get("preview_playlist")
    dvd_title = data.get("dvd_title", "1")
    output = data.get("output")
    if (
        not isinstance(source, str) or not isinstance(is_bluray, bool)
        or not isinstance(is_preview, bool)
        or (playlist is not None and not isinstance(playlist, str))
        or not isinstance(dvd_title, str)
    ):
        raise ValueError(
            "preflight request has invalid source or media settings"
        )

    source_is_block_device = False
    source_is_directory = False
    source_classified = False
    if source:
        validate_source_links(source)
        try:
            source_mode = os.stat(source).st_mode
        except FileNotFoundError:
            pass
        else:
            source_classified = True
            source_is_block_device = stat.S_ISBLK(source_mode)
            source_is_directory = stat.S_ISDIR(source_mode)

    command = None
    if is_preview:
        command = preview_command(
            source,
            is_bluray=is_bluray,
            playlist=playlist,
            operation=operation,
            dvd_title=dvd_title,
        )

    directory_output = False
    output_path = None
    output_error = None
    summary = None
    ambiguous_output = False
    reviewed_identity = None
    if output is not None:
        spec = _object(output, "output settings")
        path, name, playlists, formats = (
            spec.get("path"),
            spec.get("name"),
            spec.get("playlists"),
            spec.get("formats"),
        )
        output_is_bluray = spec.get("is_bluray")
        if (
            not isinstance(path, str) or not isinstance(name, str)
            or not isinstance(playlists, list) or not isinstance(formats, dict)
            or not isinstance(output_is_bluray, bool)
        ):
            raise ValueError("preflight request has invalid output settings")
        playlist_values = cast(list[object], playlists)
        format_values = cast(dict[object, object], formats)
        if not all(
            isinstance(item, str) for item in playlist_values
        ) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in format_values.items()
        ):
            raise ValueError("preflight request has invalid output settings")
        playlist_strings = cast(list[str], playlists)
        format_strings = cast(dict[str, str], formats)
        output_path = path
        directory_output = os.path.isdir(path)
        ambiguous_output = (
            output_is_bluray
            and not directory_output
            and len(playlist_strings) == 1
            and not os.path.lexists(path)
            and os.path.splitext(path.rstrip("/"))[1].lower()
            not in (".mkv", ".m2ts")
        )
        if (
            output_is_bluray
            and not directory_output
            and len(playlist_strings) == 1
        ):
            if playlist_strings[0] not in format_strings:
                raise ValueError(
                    "No output format is set for playlist"
                    f" {playlist_strings[0]}"
                )
            extension = "." + format_strings[playlist_strings[0]]
            if ambiguous_output:
                output_path = output_path.rstrip("/") + extension
            elif os.path.splitext(output_path)[1].lower() != extension:
                output_path = os.path.splitext(output_path)[0] + extension
        summarize = spec.get("summarize") is True
        if not (summarize and ambiguous_output):
            try:
                if summarize:
                    folder = (
                        output_path
                        if directory_output
                        else os.path.dirname(output_path) or "."
                    )
                    descriptor = os.open(
                        folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
                    )
                    try:
                        info = os.fstat(descriptor)
                        pinned = f"/proc/self/fd/{descriptor}"
                        checked_path = (
                            pinned
                            if directory_output
                            else os.path.join(
                                pinned, os.path.basename(output_path)
                            )
                        )
                        validate_output_paths(
                            checked_path,
                            output_is_bluray,
                            name,
                            playlist_strings,
                            operation,
                            formats=format_strings,
                        )
                        script = spec.get("script")
                        if not isinstance(script, str):
                            raise ValueError("Missing copier for copy summary")  # noqa: TRY004
                        language = spec.get("language", "en")
                        if not isinstance(language, str) or language not in (
                            "en",
                            "system",
                            "it_IT",
                            "ro_RO",
                        ):
                            raise ValueError(
                                "Invalid GUI language for copy summary"
                            )
                        summary = copy_summary(
                            source,
                            script,
                            output_path,
                            name,
                            playlist_strings,
                            format_strings,
                            directory_output,
                            output_is_bluray,
                            operation,
                            dvd_title,
                            translate=load_translation(language).gettext,
                            directory_fd=descriptor,
                        )
                        current = os.stat(folder)
                        reviewed_identity = (info.st_dev, info.st_ino)
                        if reviewed_identity != (
                            current.st_dev,
                            current.st_ino,
                        ):
                            raise ValueError(
                                "The output directory changed while preparing"
                                " the copy summary."
                            )
                    finally:
                        os.close(descriptor)
                else:
                    validate_output_paths(
                        output_path, output_is_bluray, name, playlist_strings,
                        operation, formats=format_strings,
                    )
            except (OSError, ValueError) as error:
                output_error = str(error)

    return {
        "source_is_block_device": source_is_block_device,
        "source_is_directory": source_is_directory,
        "source_classified": source_classified,
        "directory_output": directory_output,
        "output_path": output_path,
        "ambiguous_output": ambiguous_output,
        "preview_command": command,
        "output_error": output_error,
        "copy_summary": summary,
        "reviewed_identity": reviewed_identity,
    }


def run_operation_preflight(
    source: str,
    is_bluray: bool,
    output: dict[str, object] | None,
    preview: bool,
    preview_playlist: str | None,
    operation: ManagedProcess,
    log: Callable[[str], None],
    dvd_title: str = "1",
) -> OperationPreflight | None:
    if not source and output is None and not preview:
        return None
    request: dict[str, object] = {
        "source": source,
        "is_bluray": is_bluray,
        "output": output,
        "preview": preview,
        "preview_playlist": preview_playlist,
        "dvd_title": dvd_title,
    }
    command = [
        sys.executable, "-c",
        ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
         "from dvdvideo.gui_preflight import main; raise SystemExit(main())"),
        str(Path(__file__).parent.parent),
    ]
    result = operation.run(
        command,
        timeout=(
            PREFLIGHT_TIMEOUT
            + 60 * max(1, len(cast(list[str], output.get("playlists", []))))
            if output is not None and output.get("summarize") is True
            else PREFLIGHT_TIMEOUT
        ),
        input_data=json.dumps(request, ensure_ascii=True).encode("ascii"),
        abandon_unresponsive=True,
        stderr_log=log,
    )
    if result.returncode:
        raise RuntimeError(
            f"Operation preflight failed with exit status {result.returncode}"
        )
    try:
        data = _object(json.loads(result.stdout), "preflight result")
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "Operation preflight returned invalid JSON"
        ) from error
    error = data.get("error")
    if error is not None:
        if not isinstance(error, str):
            raise RuntimeError("Operation preflight returned an invalid error")
        raise RuntimeError(error)
    source_is_block_device = data.get("source_is_block_device")
    source_is_directory = data.get("source_is_directory")
    source_classified = data.get("source_classified")
    directory_output = data.get("directory_output")
    output_path = data.get("output_path")
    preview_result = data.get("preview_command")
    output_error = data.get("output_error")
    summary = data.get("copy_summary")
    ambiguous_output = data.get("ambiguous_output", False)
    reviewed_identity = data.get("reviewed_identity")
    identity_values = (
        cast(list[object], reviewed_identity)
        if isinstance(reviewed_identity, list)
        else None
    )
    if (
        not isinstance(source_is_block_device, bool)
        or not isinstance(source_is_directory, bool)
        or not isinstance(source_classified, bool)
        or (source_is_block_device and not source_classified)
        or (
            source_is_directory
            and (not source_classified or source_is_block_device)
        )
        or not isinstance(directory_output, bool)
        or (output_path is not None and not isinstance(output_path, str))
        or (
            preview_result is not None and not isinstance(preview_result, list)
        )
        or (output_error is not None and not isinstance(output_error, str))
        or (summary is not None and not isinstance(summary, str))
        or not isinstance(ambiguous_output, bool)
        or (
            reviewed_identity is not None
            and (
                identity_values is None
                or len(identity_values) != 2
                or any(
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                    for value in identity_values
                )
            )
        )
    ):
        raise RuntimeError("Operation preflight returned an invalid result")
    preview_values = (
        cast(list[object], preview_result)
        if isinstance(preview_result, list)
        else None
    )
    if preview_values is not None and not all(
        isinstance(item, str) for item in preview_values
    ):
        raise RuntimeError("Operation preflight returned an invalid result")
    return OperationPreflight(
        source_is_block_device,
        directory_output,
        output_path,
        (
            tuple(cast(list[str], preview_values))
            if preview_values is not None
            else None
        ),
        output_error,
        source_classified,
        summary,
        ambiguous_output,
        source_is_directory,
        (
            (cast(int, identity_values[0]), cast(int, identity_values[1]))
            if identity_values is not None
            else None
        ),
    )


def main() -> int:
    if len(sys.argv) != 1:
        return 2
    operation = ManagedProcess()
    previous_grace = gui_support.TERMINATION_GRACE
    gui_support.TERMINATION_GRACE = 1
    previous_handlers: dict[signal.Signals, signal.Handlers] = {}

    def cancel(_signum: int, _frame: FrameType | None) -> None:
        operation.cancel()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = cast(
                signal.Handlers, signal.signal(signum, cancel)
            )
        try:
            request = json.load(sys.stdin)
            result = perform_preflight(request, operation)
        except OperationCancelled:
            return 130
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            subprocess.SubprocessError,
        ) as error:
            result = {"error": str(error)}
        sys.stdout.write(json.dumps(result, ensure_ascii=True))
        return 0
    finally:
        gui_support.TERMINATION_GRACE = previous_grace
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
