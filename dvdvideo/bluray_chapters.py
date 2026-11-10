"""Export playlist chapter markers as FFmetadata and OGM chapter text."""

# JSON schema violations are ValueError, matching the CLI's malformed-data handling.
# ruff: noqa: TRY004

import argparse
import json
import re
import sys
from pathlib import Path
from typing import cast


def milliseconds(value: object) -> int:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+:[0-5][0-9]:[0-5][0-9]\.[0-9]{3}", value) is None:
        raise ValueError(f"Invalid chapter timestamp: {value!r}")
    hours, minutes, seconds, fraction = re.split(r"[:.]", value)
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(fraction)


def playlist_title(data: object, playlist: str) -> dict[object, object]:
    if not isinstance(data, dict):
        raise ValueError("Chapter metadata must contain a titles array.")
    title_values = cast(dict[object, object], data).get("titles")
    if not isinstance(title_values, list):
        raise ValueError("Chapter metadata must contain a titles array.")
    titles = cast(list[object], title_values)
    if len(titles) != 1 or not isinstance(titles[0], dict):
        raise ValueError("Chapter metadata must describe the requested playlist only.")
    title = cast(dict[object, object], titles[0])
    if str(title.get("playlist")) != playlist:
        raise ValueError("Chapter metadata must describe the requested playlist only.")
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
        start = milliseconds(cast(dict[object, object], chapter).get("start time"))
        if start >= duration or (starts and start <= starts[-1]):
            raise ValueError("Chapter starts must increase and precede the playlist end.")
        starts.append(start)
    metadata = [";FFMETADATA1\n"]
    sidecar: list[str] = []
    for index, (start, end) in enumerate(zip(starts, starts[1:] + [duration], strict=True), 1):
        name = f"Chapter {index:03d}"
        metadata.append(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={start}\nEND={end}\ntitle={name}\n")
        hours, remainder = divmod(start, 3600000)
        minutes, remainder = divmod(remainder, 60000)
        seconds, fraction = divmod(remainder, 1000)
        timestamp = f"{hours:02d}:{minutes:02d}:{seconds:02d}.{fraction:03d}"
        sidecar.append(f"CHAPTER{index:03d}={timestamp}\nCHAPTER{index:03d}NAME={name}\n")
    return "".join(metadata), "".join(sidecar), len(starts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("playlist")
    parser.add_argument("metadata", type=Path)
    parser.add_argument("sidecar", type=Path)
    parser.add_argument("--size-file", type=Path, help="Write the expected complete playlist size in bytes.")
    args = parser.parse_args()
    try:
        data = json.load(sys.stdin)
        metadata, sidecar, count = export_chapters(data, args.playlist)
        if args.size_file is not None:
            size = playlist_title(data, args.playlist).get("filesize")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                raise ValueError("Playlist metadata must contain a positive integer filesize for copy validation.")
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
