"""Build direct-disc mpv previews without copying or temporary video files."""

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
import re
import shutil
import stat
import subprocess
from collections.abc import Callable

from dvdvideo.gui_commands import validate_dvd_title
from dvdvideo.gui_support import (
    ManagedProcess,
    absolute_path,
    strip_directory_suffix,
)


def preview_command(
    source: str,
    *,
    is_bluray: bool,
    playlist: str | None,
    operation: ManagedProcess,
    dvd_title: str = "1",
) -> list[str]:
    operation.check_cancelled()
    if not source:
        raise RuntimeError("Choose a source first.")
    player = shutil.which("mpv")
    if player is None:
        raise RuntimeError(
            "Preview requires mpv. Install it to use Preview; copying doesn't"
            " need it."
        )
    if is_bluray and (
        playlist is None or re.fullmatch(r"[0-9]{1,5}", playlist) is None
    ):
        raise RuntimeError(
            "Select exactly one valid Blu-ray playlist to preview."
        )
    path = absolute_path(source)
    info = os.stat(path)
    if not (
        stat.S_ISREG(info.st_mode)
        or stat.S_ISDIR(info.st_mode)
        or stat.S_ISBLK(info.st_mode)
    ):
        raise RuntimeError(
            "Preview source must be a disc image, device, or disc folder."
        )
    if stat.S_ISDIR(info.st_mode):
        path = strip_directory_suffix(path)
        folder = os.path.basename(path).upper()
        if is_bluray and folder == "BDMV":
            path = os.path.dirname(path)
        elif not is_bluray and folder != "VIDEO_TS":
            matches = [
                os.path.join(path, entry)
                for entry in os.listdir(path)
                if entry.upper() == "VIDEO_TS"
                and os.path.isdir(os.path.join(path, entry))
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    "DVD preview requires exactly one VIDEO_TS folder."
                )
            path = matches[0]
    result = operation.run(
        [player, "--no-config", "--list-protocols"], timeout=10
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"Could not check mpv support: {detail}")
    protocol = "bd://" if is_bluray else "dvd://"
    if protocol not in result.stdout.split():
        feature = "Blu-ray" if is_bluray else "DVD"
        raise RuntimeError(
            f"This mpv build lacks the {feature} support required for preview."
        )
    operation.check_cancelled()
    command = [
        player, "--no-config", "--load-scripts=no", "--ytdl=no",
        "--resume-playback=no", "--save-position-on-quit=no", "--disc-menu=no",
        "--idle=no", "--keep-open=no", "--input-terminal=no", "--terminal=yes",
        "--msg-level=all=warn", "--title=Disc preview (2D)",
    ]
    if is_bluray:
        assert playlist is not None
        command += [
            "--bluray-device=" + path, "--", f"bd://mpls/{int(playlist)}"
        ]
    else:
        # mpv's DVD title indices start at zero, unlike the copy backend.
        title_index = int(validate_dvd_title(dvd_title)) - 1
        command += ["--dvd-device=" + path, "--", f"dvd://{title_index}"]
    return command


def play_preview(
    command: list[str],
    *,
    operation: ManagedProcess,
    log: Callable[[str], None],
) -> subprocess.CompletedProcess[str]:
    return operation.run(
        command, collect=False, log=log, abandon_unresponsive=True
    )
