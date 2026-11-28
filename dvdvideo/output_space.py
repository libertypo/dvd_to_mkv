"""Conservative peak-space checks shared by terminal and GUI copies."""

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
import fcntl
import os
import shutil
import stat
import struct
import sys
from pathlib import Path

RESERVE_BYTES = 64 * 1024 * 1024
BLKGETSIZE64 = 0x80081272


def source_bytes(source: str) -> int:
    info = os.stat(source)
    if stat.S_ISREG(info.st_mode):
        size = info.st_size
    elif stat.S_ISBLK(info.st_mode):
        with open(source, "rb", buffering=0) as device:
            size = struct.unpack(
                "Q", fcntl.ioctl(device.fileno(), BLKGETSIZE64, bytes(8))
            )[0]
    elif stat.S_ISDIR(info.st_mode):
        size = 0

        def walk_error(error: OSError) -> None:
            raise error

        for directory, folders, files in os.walk(
            source, followlinks=False, onerror=walk_error
        ):
            for name in folders + files:
                path = os.path.join(directory, name)
                entry = os.lstat(path)
                if stat.S_ISLNK(entry.st_mode):
                    raise ValueError(
                        "Cannot estimate DVD folder size through a symbolic"
                        f" link: {path}"
                    )
                if stat.S_ISREG(entry.st_mode):
                    size += entry.st_size
                elif not stat.S_ISDIR(entry.st_mode):
                    raise ValueError(
                        "Cannot estimate DVD folder size for a special file:"
                        f" {path}"
                    )
    else:
        raise ValueError("Cannot estimate space for this source type.")
    if size <= 0:
        raise ValueError("Cannot estimate space from an empty source.")
    return size


def required_bytes(
    size: object, output_format: str, *, is_bluray: bool
) -> int:
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("Copy size must be a positive integer.")
    if output_format not in ("mkv", "m2ts") or (
        not is_bluray and output_format != "mkv"
    ):
        raise ValueError("Unsupported output format for space estimation.")
    # Blu-ray MKV holds the full transport stream and remuxed output at once.
    peak = size * (2 if is_bluray and output_format == "mkv" else 1)
    return peak + (size + 19) // 20 + RESERVE_BYTES


def check_space(directory: str, required: object) -> str:
    if (
        isinstance(required, bool)
        or not isinstance(required, int)
        or required <= 0
    ):
        raise ValueError("Required space must be a positive integer.")
    available = shutil.disk_usage(directory).free
    summary = (
        f"estimated peak need {required / 1024**3:.2f} GiB ({required} bytes),"
        f" available {available / 1024**3:.2f} GiB ({available} bytes)"
    )
    if available < required:
        raise ValueError(
            f"Not enough free space: {summary}. Free space or choose another"
            " output folder."
        )
    return f"Free-space check: {summary}."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--format", choices=("mkv", "m2ts"), default="mkv")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--source", help="DVD image, device, or VIDEO_TS folder."
    )
    source.add_argument(
        "--size-file", type=Path, help="Validated Blu-ray playlist byte count."
    )
    args = parser.parse_args()
    try:
        size = (
            int(args.size_file.read_text(encoding="ascii"))
            if args.size_file
            else source_bytes(args.source)
        )
        required = required_bytes(
            size, args.format, is_bluray=args.size_file is not None
        )
        print(check_space(args.directory, required), file=sys.stderr)
    except (OSError, ValueError, OverflowError) as error:
        print(
            f"Copy blocked: free-space check failed: {error}", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
