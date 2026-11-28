"""GTK-independent command preparation for the main window."""

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

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PlaylistChoice:
    identifier: str
    output_format: str
    streams: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CommandInput:
    script_path: str
    source: str
    is_bluray: bool
    inspect: bool = False
    minimum_minutes: int = 5
    output: str = ""
    output_format: str = "auto"
    selected: tuple[str, ...] = ()
    playlists: tuple[PlaylistChoice, ...] = ()
    dvd_title: str = "1"


@dataclass(frozen=True)
class CommandPlan:
    arguments: tuple[str, ...]
    output_index: int | None = None


def validate_dvd_title(title: str) -> str:
    if re.fullmatch(r"[1-9][0-9]?", title) is None:
        raise ValueError("DVD title must be between 1 and 99.")
    return title


def _source_argument(source: str) -> str:
    return "./" + source if source.startswith("-") else source


def build_bluray_streams_command(
    script_path: str, source: str, playlist: str
) -> list[str]:
    if not source:
        raise ValueError("Choose a source first.")
    return [
        script_path, "--bluray", "--device", _source_argument(source),
        "--playlist", playlist, "--list-streams",
    ]


def build_disc_details_command(
    script_path: str,
    source: str,
    *,
    is_bluray: bool,
    playlist: str | None = None,
    dvd_title: str = "1",
) -> list[str]:
    if not source:
        raise ValueError("Choose a source first.")
    if is_bluray:
        command = [
            script_path,
            "--bluray",
            "--device",
            _source_argument(source),
        ]
        if playlist is not None:
            command += ["--playlist", playlist]
        return command + ["--disc-details"]
    return [
        script_path,
        "--disc-details",
        "--",
        source,
        validate_dvd_title(dvd_title),
    ]


def prepare_command(request: CommandInput) -> CommandPlan:
    if not request.source:
        raise ValueError("Choose a source first.")
    source = _source_argument(request.source)
    if request.is_bluray:
        command = [request.script_path, "--bluray", "--device", source]
        if request.inspect:
            return CommandPlan(
                tuple(
                    command
                    + [
                        "--minlength",
                        str(request.minimum_minutes),
                        "--list-playlists",
                    ]
                )
            )
        if not request.playlists:
            raise ValueError("Select at least one playlist.")
        for playlist in request.playlists:
            command += ["--playlist", playlist.identifier]
            if playlist.output_format == "m2ts" or playlist.streams is None:
                continue
            if not playlist.streams:
                raise ValueError(
                    "Select at least one stream for playlist"
                    f" {playlist.identifier}."
                )
            for stream in playlist.streams:
                command += ["--stream-id", f"{playlist.identifier}:{stream}"]
        command += ["--output-format", request.output_format, "--"]
        output_index = len(command)
        return CommandPlan(
            tuple(command + [request.output or "."]), output_index
        )
    if request.inspect:
        title = validate_dvd_title(request.dvd_title)
        return CommandPlan(
            (request.script_path, "--list-streams-tsv", "--", source, title)
        )
    if not request.selected:
        raise ValueError("Select at least one stream.")
    if not request.output:
        raise ValueError("Choose an output MKV first.")
    command = [request.script_path]
    for identifier in request.selected:
        command += ["--stream", identifier]
    command += ["--", source]
    output_index = len(command)
    command.append(request.output)
    title = validate_dvd_title(request.dvd_title)
    if title != "1":
        command.append(title)
    return CommandPlan(tuple(command), output_index)


def build_command(request: CommandInput) -> list[str]:
    return list(prepare_command(request).arguments)
