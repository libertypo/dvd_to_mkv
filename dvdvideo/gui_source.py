"""GTK-independent source validation, naming, and media detection."""

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

import os
import shutil
import stat
from collections.abc import Callable

from dvdvideo.gui_support import (
    ManagedProcess,
    filename_stem,
    strip_directory_suffix,
)

DISC_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("BDMV", "INDEX.BDMV", "bluray"), ("VIDEO_TS", "VIDEO_TS.IFO", "dvd")
)
# Caps the 7z probe's virtual address space (RLIMIT_AS), not resident memory.
PROBE_ADDRESS_SPACE_LIMIT: int = 512 * 1024 * 1024
SOURCE_SYMLINK_ERROR: str = (
    "Symbolic links are not accepted as sources; choose the real file or"
    " folder."
)


def symlinked_source(source: str) -> bool:
    # Remove directory suffixes without collapsing .. across symlink ancestors.
    path = strip_directory_suffix(source)
    try:
        if not stat.S_ISLNK(os.lstat(path).st_mode):
            return False
    except (OSError, ValueError):
        return False
    # Device aliases such as /dev/cdrom are symlinks managed by the system.
    try:
        return not stat.S_ISBLK(os.stat(source).st_mode)
    except OSError:
        return True


def is_plain_directory(path: str) -> bool:
    return stat.S_ISDIR(os.lstat(path).st_mode)


def is_plain_file(path: str) -> bool:
    return stat.S_ISREG(os.lstat(path).st_mode)


def validate_source_links(source: str) -> None:
    if symlinked_source(source):
        raise RuntimeError(SOURCE_SYMLINK_ERROR)
    if not os.path.isdir(source):
        return

    def walk_error(error: OSError) -> None:
        raise error

    source_folder = os.path.basename(strip_directory_suffix(source)).upper()
    for folder, _marker, _kind in DISC_MARKERS:
        directories = (
            [source]
            if source_folder == folder
            else [
                os.path.join(source, entry)
                for entry in os.listdir(source)
                if entry.upper() == folder
            ]
        )
        for directory in directories:
            if os.path.islink(directory):
                raise RuntimeError(SOURCE_SYMLINK_ERROR)
            if os.path.isdir(directory):
                for current, folders, files in os.walk(
                    directory, followlinks=False, onerror=walk_error
                ):
                    for entry in folders + files:
                        if stat.S_ISLNK(
                            os.lstat(os.path.join(current, entry)).st_mode
                        ):
                            raise RuntimeError(SOURCE_SYMLINK_ERROR)


def source_name(source: str) -> str:
    path = strip_directory_suffix(source)
    if os.path.basename(path).upper() in ("BDMV", "VIDEO_TS"):
        path = os.path.dirname(path)
    name = os.path.basename(path)
    if name.lower().endswith((".iso", ".img")):
        name = os.path.splitext(name)[0]
    return filename_stem(name) or "disc"


def find_entry(
    directory: str, name: str, accept: Callable[[str], bool]
) -> str | None:
    # Disc folder matching ignores case; copies often use lowercase names.
    found = None
    for entry in os.listdir(directory):
        path = os.path.join(directory, entry)
        if entry.upper() == name and accept(path):
            if found is not None:
                raise RuntimeError(
                    f"Multiple case-insensitive matches for {name} in"
                    f" {directory}; resolve the ambiguity or choose a specific"
                    " disc folder."
                )
            found = path
    return found


def detect_media(source: str, probe: ManagedProcess | None = None) -> str:
    """Return "dvd" or "bluray" from the disc structure.

    Images and devices are listed with 7z.
    """
    if symlinked_source(source):
        raise RuntimeError(SOURCE_SYMLINK_ERROR)
    source_stat: os.stat_result = os.stat(source)
    if stat.S_ISDIR(source_stat.st_mode):
        source_folder = os.path.basename(
            strip_directory_suffix(source)
        ).upper()
        kinds: set[str] = set()
        for folder, marker, kind in DISC_MARKERS:
            folder_path = (
                source
                if source_folder == folder
                else find_entry(source, folder, is_plain_directory)
            )
            if folder_path and find_entry(folder_path, marker, is_plain_file):
                kinds.add(kind)
    elif stat.S_ISREG(source_stat.st_mode) or stat.S_ISBLK(
        source_stat.st_mode
    ):
        tool: str | None = shutil.which("7zz") or shutil.which("7z")
        if not tool:
            raise RuntimeError(
                "Automatic image/device detection requires 7zz or 7z. Choose"
                " DVD or Blu-ray manually."
            )
        limiter: str | None = shutil.which("prlimit")
        if not limiter:
            raise RuntimeError(
                "Automatic image/device detection requires prlimit"
                " (util-linux) to cap 7z address space. Choose DVD or Blu-ray"
                " manually."
            )
        command = [
            limiter,
            f"--as={PROBE_ADDRESS_SPACE_LIMIT}",
            "--",
            tool,
            "l",
            "-slt",
            "-ba",
            "--",
            source,
        ]
        result = (probe or ManagedProcess()).run(command, timeout=30)
        if result.returncode != 0:
            detail = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit status {result.returncode}"
            )
            raise RuntimeError(
                f"Could not inspect the source filesystem: {detail}"
            )
        paths = {
            line.removeprefix("Path = ").replace("\\", "/").strip("/").upper()
            for line in result.stdout.splitlines()
            if line.startswith("Path = ")
        }
        kinds: set[str] = {
            kind
            for folder, marker, kind in DISC_MARKERS
            if f"{folder}/{marker}" in paths
        }
    else:
        raise RuntimeError(
            "Source must be a disc image, optical device, or disc folder."
        )
    if len(kinds) != 1:
        raise RuntimeError(
            "Source has no unique BDMV/index.bdmv or VIDEO_TS/VIDEO_TS.IFO"
            " structure. Choose DVD or Blu-ray manually."
        )
    return kinds.pop()
