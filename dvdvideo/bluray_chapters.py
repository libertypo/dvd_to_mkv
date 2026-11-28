"""Export playlist chapters and restore Blu-ray stream language metadata."""

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

# JSON schema violations use ValueError, matching CLI malformed-data handling.
# ruff: noqa: TRY004

import argparse
import json
import re
import sys
from pathlib import Path
from typing import cast


def milliseconds(value: object) -> int:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9]+:[0-5][0-9]:[0-5][0-9]\.[0-9]{3}", value)
        is None
    ):
        raise ValueError(f"Invalid chapter timestamp: {value!r}")
    hours, minutes, seconds, fraction = re.split(r"[:.]", value)
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(
        fraction
    )


def playlist_title(data: object, playlist: str) -> dict[object, object]:
    if not isinstance(data, dict):
        raise ValueError("Chapter metadata must contain a titles array.")
    title_values = cast(dict[object, object], data).get("titles")
    if not isinstance(title_values, list):
        raise ValueError("Chapter metadata must contain a titles array.")
    titles = cast(list[object], title_values)
    if len(titles) != 1 or not isinstance(titles[0], dict):
        raise ValueError(
            "Chapter metadata must describe the requested playlist only."
        )
    title = cast(dict[object, object], titles[0])
    reported = str(title.get("playlist"))
    if (
        re.fullmatch(r"[0-9]{1,5}", playlist) is None
        or re.fullmatch(r"[0-9]{1,5}", reported) is None
        or int(reported) != int(playlist)
    ):
        raise ValueError(
            "Chapter metadata must describe the requested playlist only."
        )
    return title


def export_chapters(data: object, playlist: str) -> tuple[str, str, int]:
    title = playlist_title(data, playlist)
    chapter_values = title.get("chapters")
    if not isinstance(chapter_values, list):
        raise ValueError("Chapter metadata must contain a chapters array.")
    chapters = cast(list[object], chapter_values)
    if not chapters:
        return ";FFMETADATA1\n", "", 0
    duration = milliseconds(title.get("length"))
    starts: list[int] = []
    for chapter in chapters:
        if not isinstance(chapter, dict):
            raise ValueError("Invalid chapter record.")
        start = milliseconds(
            cast(dict[object, object], chapter).get("start time")
        )
        if start >= duration or (starts and start <= starts[-1]):
            raise ValueError(
                "Chapter starts must increase and precede the playlist end."
            )
        starts.append(start)
    metadata = [";FFMETADATA1\n"]
    sidecar: list[str] = []
    for index, (start, end) in enumerate(
        zip(starts, starts[1:] + [duration], strict=True), 1
    ):
        name = f"Chapter {index:03d}"
        metadata.append(
            f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={start}\nEND={end}\n"
            f"title={name}\n"
        )
        hours, remainder = divmod(start, 3600000)
        minutes, remainder = divmod(remainder, 60000)
        seconds, fraction = divmod(remainder, 1000)
        timestamp = f"{hours:02d}:{minutes:02d}:{seconds:02d}.{fraction:03d}"
        sidecar.append(
            f"CHAPTER{index:03d}={timestamp}\n"
            f"CHAPTER{index:03d}NAME={name}\n"
        )
    return "".join(metadata), "".join(sidecar), len(starts)


def stream_pid(value: object) -> int:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"0x[0-9a-fA-F]+", value) is None
    ):
        raise ValueError(f"Invalid stream PID: {value!r}")
    pid = int(value, 16)
    if pid > 0x1FFF:
        raise ValueError(f"Invalid stream PID: {value!r}")
    return pid


def playlist_languages(data: object, playlist: str) -> dict[int, str]:
    title = playlist_title(data, playlist)
    languages: dict[int, str] = {}
    for kind in ("audio", "subtitles"):
        records = title.get(kind, [])
        if not isinstance(records, list):
            raise ValueError(f"Playlist {kind} metadata must be an array.")
        for record in cast(list[object], records):
            if not isinstance(record, dict):
                raise ValueError(f"Invalid {kind} stream metadata.")
            record = cast(dict[object, object], record)
            pid = stream_pid(record.get("stream"))
            language = record.get("language")
            if language is None or language == "":
                continue
            if (
                not isinstance(language, str)
                or re.fullmatch(r"[a-zA-Z]{3}", language) is None
            ):
                raise ValueError(f"Invalid stream language: {language!r}")
            language = language.lower()
            if pid in languages and languages[pid] != language:
                raise ValueError(f"Conflicting languages for PID {pid:#x}.")
            languages[pid] = language
    return languages


def language_arguments(
    data: object, playlist: str, probe: object, selected: list[str]
) -> list[str]:
    languages = playlist_languages(data, playlist)
    if not isinstance(probe, dict):
        raise ValueError("FFprobe metadata must contain a streams array.")
    streams = cast(dict[object, object], probe).get("streams")
    if not isinstance(streams, list) or not streams:
        raise ValueError(
            "FFprobe metadata must contain a nonempty streams array."
        )
    source: list[tuple[int, str]] = []
    for stream in cast(list[object], streams):
        if not isinstance(stream, dict):
            raise ValueError("Invalid FFprobe stream metadata.")
        stream = cast(dict[object, object], stream)
        pid = stream_pid(stream.get("id"))
        kind = stream.get("codec_type")
        if not isinstance(kind, str):
            raise ValueError("Invalid FFprobe stream metadata.")
        source.append((pid, kind))
    output_streams: list[tuple[int, str]] = source
    if selected:
        output_streams = []
        for selection in selected:
            pid = stream_pid(selection)
            matching = [
                (source_pid, kind)
                for source_pid, kind in source
                if source_pid == pid
            ]
            if not matching:
                raise ValueError(
                    f"Selected PID {pid:#x} is missing from the copied stream."
                )
            output_streams.extend(matching)
    arguments: list[str] = []
    for index, (pid, kind) in enumerate(output_streams):
        if pid in languages:
            arguments.extend(
                (f"-metadata:s:{index}", f"language={languages[pid]}")
            )
        elif kind in ("audio", "subtitle"):
            print(
                f"Playlist {playlist}: no disc language for PID {pid:#x};"
                " leaving its tag unchanged.",
                file=sys.stderr,
            )
    return arguments


def language_args_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Build PID-matched FFmpeg language arguments."
    )
    parser.add_argument("playlist")
    parser.add_argument("metadata", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pid", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.metadata.read_text(encoding="utf-8"))
        arguments = language_arguments(
            data, args.playlist, json.load(sys.stdin), args.pid
        )
        args.output.write_bytes(
            b"".join(
                argument.encode("ascii") + b"\0" for argument in arguments
            )
        )
    except (OSError, ValueError) as error:
        print(f"Blu-ray language import failed: {error}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    if sys.argv[1:2] == ["language-args"]:
        return language_args_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("playlist")
    parser.add_argument("metadata", type=Path)
    parser.add_argument("sidecar", type=Path)
    parser.add_argument(
        "--size-file",
        type=Path,
        help="Write the expected complete playlist size in bytes.",
    )
    parser.add_argument(
        "--validate-languages",
        action="store_true",
        help="Validate MKV language metadata before copying.",
    )
    args = parser.parse_args()
    try:
        data = json.load(sys.stdin)
        if args.validate_languages:
            playlist_languages(data, args.playlist)
        metadata, sidecar, count = export_chapters(data, args.playlist)
        if args.size_file is not None:
            size = playlist_title(data, args.playlist).get("filesize")
            if (
                not isinstance(size, int)
                or isinstance(size, bool)
                or size <= 0
            ):
                raise ValueError(
                    "Playlist metadata must contain a positive integer"
                    " filesize for copy validation."
                )
            args.size_file.write_text(str(size), encoding="ascii")
        args.metadata.write_text(metadata, encoding="utf-8")
        args.sidecar.write_text(sidecar, encoding="utf-8")
    except (OSError, ValueError) as error:
        print(f"Blu-ray chapter import failed: {error}", file=sys.stderr)
        return 1
    print(count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
