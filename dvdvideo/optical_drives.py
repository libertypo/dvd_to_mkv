"""Discover Linux optical drives and identify readable DVD/Blu-ray media."""

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

import errno
import fcntl
import os
import re
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dvdvideo.gui_support import ManagedProcess

CDROM_DRIVE_STATUS = 0x5326
CDSL_CURRENT = 0x7FFFFFFF
CDS_DISC_OK = 4


@dataclass(frozen=True)
class OpticalDrive:
    path: str
    name: str
    media: str | None
    status: str


def discover_drives(
    identify: Callable[[str, ManagedProcess], str],
    operation: ManagedProcess,
    progress: Callable[[], None] = lambda: None,
    found: Callable[[OpticalDrive], None] = lambda _drive: None,
    *,
    sysfs: Path = Path("/sys/class/block"),
    devices: Path = Path("/dev"),
) -> list[OpticalDrive]:
    drives: list[OpticalDrive] = []
    progress()
    for entry in sorted(sysfs.iterdir()):
        operation.check_cancelled()
        progress()
        path = str(devices / entry.name)
        name = entry.name
        media = None
        try:
            # Missing device/type is normal for partitions and non-SCSI.
            try:
                device_type = (
                    (entry / "device/type").read_text(encoding="ascii").strip()
                )
            except FileNotFoundError:
                continue
            if device_type != "5":
                continue
            if re.fullmatch(r"[a-zA-Z0-9_-]+", entry.name) is None:
                raise RuntimeError("Invalid optical device name in sysfs.")
            progress()
            model = (
                (entry / "device/model")
                .read_text(encoding="utf-8", errors="replace")
                .strip()
            )
            name = model or name
            progress()
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                progress()
                if not stat.S_ISBLK(os.fstat(fd).st_mode):
                    raise RuntimeError("Device path is not a block device.")
                progress()
                present = (
                    fcntl.ioctl(fd, CDROM_DRIVE_STATUS, CDSL_CURRENT)
                    == CDS_DISC_OK
                )
            finally:
                os.close(fd)
            if present:
                progress()
                media = identify(path, operation)
                if media not in ("dvd", "bluray"):
                    media = None
                    raise RuntimeError(
                        "Disc was not recognized as DVD or Blu-ray."
                    )
                status = "Blu-ray" if media == "bluray" else "DVD"
            else:
                status = "No readable disc"
        except OSError as error:
            status = (
                "No readable disc"
                if error.errno == errno.ENOMEDIUM
                else f"Unavailable: {error}"
            )
        except (
            UnicodeError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as error:
            status = f"Unavailable: {error}"
        drive = OpticalDrive(path, name, media, status)
        drives.append(drive)
        found(drive)
    return drives
