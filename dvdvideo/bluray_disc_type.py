"""Identify HDMV, BD-J, or mixed Blu-ray navigation.

This does not run disc code.
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dvdvideo.bluray_inventory import (
    disc_metadata_path,
    image_metadata_records,
    normalize_bluray_source,
    read_image_metadata,
)

DiscType = Literal["HDMV", "BD-J", "mixed"]
INDEX_PATH = "BDMV/index.bdmv"
MAX_INDEX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class NavigationEntry:
    kind: Literal["HDMV", "BD-J"]
    role: Literal["first_play", "menu", "title"]
    reference: int


def parse_navigation_entries(data: bytes) -> list[NavigationEntry]:
    if (
        len(data) < 40
        or data[:4] != b"INDX"
        or data[4:8] not in (b"0100", b"0200", b"0300")
    ):
        raise ValueError("Invalid or unsupported Blu-ray index header.")
    start = int.from_bytes(data[8:12], "big")
    if start < 40 or start + 4 > len(data):
        raise ValueError("Invalid Blu-ray index offset.")
    length = int.from_bytes(data[start:start + 4], "big")
    end = start + 4 + length
    if length < 26 or end > len(data):
        raise ValueError("Truncated Blu-ray index.")
    count = int.from_bytes(data[start + 28:start + 30], "big")
    if start + 30 + count * 12 > end:
        raise ValueError("Truncated Blu-ray title entries.")
    result: list[NavigationEntry] = []
    entries = [start + 4, start + 16]
    entries.extend(start + 30 + number * 12 for number in range(count))
    for number, offset in enumerate(entries):
        kind = data[offset] >> 6
        if kind == 1:
            reference = int.from_bytes(data[offset + 6:offset + 8], "big")
            if reference == 0xFFFF:
                if number < 2:
                    continue
                raise ValueError("Invalid HDMV title reference.")
        elif kind == 2:
            if (
                re.fullmatch(rb"[0-9]{5}", data[offset + 6 : offset + 11])
                is None
            ):
                raise ValueError("Invalid BD-J title reference.")
        else:
            raise ValueError(f"Unsupported navigation object type: {kind}.")
        role = (
            "first_play" if number == 0 else "menu" if number == 1 else "title"
        )
        result.append(NavigationEntry(
            "HDMV" if kind == 1 else "BD-J", role,
            int.from_bytes(data[offset + 6:offset + 8], "big")
            if kind == 1 else int(data[offset + 6:offset + 11]),
        ))
    if not result:
        raise ValueError("No navigation entries found in the Blu-ray index.")
    return result


def parse_disc_type(data: bytes) -> DiscType:
    kinds = {entry.kind for entry in parse_navigation_entries(data)}
    if kinds == {"HDMV", "BD-J"}:
        return "mixed"
    return "BD-J" if "BD-J" in kinds else "HDMV"


def read_disc_metadata(source: Path, relative_path: str, limit: int) -> bytes:
    source = normalize_bluray_source(source)
    if source.is_dir():
        with disc_metadata_path(source, relative_path).open("rb") as stream:
            data = stream.read(limit + 1)
    else:
        records = image_metadata_records(source, relative_path, limit)
        if (
            len(records) != 1
            or records[0][0].casefold() != relative_path.casefold()
        ):
            raise ValueError(
                f"Expected exactly one {relative_path} in the image."
            )
        data = read_image_metadata(source, *records[0])
    if len(data) > limit:
        raise ValueError(
            f"Metadata exceeds the supported size: {relative_path}."
        )
    return data


def detect_disc_type(source: Path) -> DiscType:
    return parse_disc_type(
        read_disc_metadata(source, INDEX_PATH, MAX_INDEX_BYTES)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gui-analysis",
        action="store_true",
        help="Run the matching analyser for Inspect",
    )
    parser.add_argument(
        "source", type=Path, help="Blu-ray ISO image or mounted disc root"
    )
    args = parser.parse_args()
    try:
        kind = detect_disc_type(args.source)
        if args.gui_analysis:
            module = (
                "dvdvideo.bluray_hdmv"
                if kind == "HDMV"
                else "dvdvideo.bluray_inventory"
            )
            print(
                f"Disc navigation: {kind}; running {module}.",
                file=sys.stderr,
                flush=True,
            )
            if kind == "HDMV":
                from dvdvideo.bluray_hdmv import main as analyze
            else:
                from dvdvideo.bluray_inventory import main as analyze
            return analyze()
        print(kind)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Blu-ray type detection failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
