"""GTK-independent, bounded reports from Blu-ray and DVD inspection JSON."""

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

# Malformed external metadata uses the listing helpers' ValueError contract.
# ruff: noqa: TRY004

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from dvdvideo.gui_listing import MAX_INSPECTION_ROWS, MAX_STREAM_ROWS
from dvdvideo.gui_preferences import normalize_stream_pid
from dvdvideo.gui_translation import identity

MAX_CHAPTERS = 2000
MAX_VALUE_CHARS = 200
MAX_REPORT_CHARS = 512 * 1024
UNSAFE_TEXT = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e"
    r"\u2066-\u2069\ud800-\udfff]"
)


@dataclass(frozen=True)
class DetailSection:
    heading: str
    fields: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DiscDetails:
    sections: tuple[DetailSection, ...]
    notes: tuple[str, ...]
    truncated: bool = False

    def text(self, translate: Callable[[str], str] = identity) -> str:
        sections = [
            section.heading
            + "\n"
            + "\n".join(
                f"  {label}: {value}" for label, value in section.fields
            )
            for section in self.sections
        ]
        text = "\n\n".join(
            sections
            + [
                translate("Notes")
                + "\n"
                + "\n".join(f"  {note}" for note in self.notes)
            ]
        )
        if self.truncated or len(text) > MAX_REPORT_CHARS:
            return (
                text[:MAX_REPORT_CHARS]
                + "\n\n"
                + translate("Details truncated to keep the view responsive.")
            )
        return text


class SectionBuffer:
    def __init__(self) -> None:
        self.sections: list[DetailSection] = []
        self.size = 0
        self.truncated = False

    def append(self, section: DetailSection) -> None:
        size = (
            len(section.heading)
            + 2
            + sum(
                len(label) + len(value) + 5 for label, value in section.fields
            )
        )
        if self.truncated or self.size + size > MAX_REPORT_CHARS:
            self.truncated = True
            return
        self.sections.append(section)
        self.size += size


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object with string keys.")
    record = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in record):
        raise ValueError(f"{name} must be an object with string keys.")
    return cast(dict[str, object], record)


def _records(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array.")
    return cast(list[object], value)


def _value(value: object, translate: Callable[[str], str]) -> str:
    if isinstance(value, bool):
        return translate("Yes") if value else translate("No")
    if isinstance(value, str):
        return UNSAFE_TEXT.sub(" ", value)[:MAX_VALUE_CHARS].strip()
    if isinstance(value, int):
        return str(value)[:MAX_VALUE_CHARS]
    if isinstance(value, float) and math.isfinite(value):
        return str(value)
    raise ValueError(
        "Detail values must be text, finite numbers, or booleans."
    )


def _fields(
    record: dict[str, object],
    names: tuple[tuple[str, str], ...],
    translate: Callable[[str], str],
) -> tuple[tuple[str, str], ...]:
    fields: list[tuple[str, str]] = []
    for key, label in names:
        value = record.get(key)
        if value is not None and value != "":
            fields.append((translate(label), _value(value, translate)))
    size = record.get("filesize")
    if size is not None:
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= 2**63 - 1
        ):
            raise ValueError(
                "Playlist size must be a non-negative 64-bit integer."
            )
        fields.append(
            (
                translate("Playlist size"),
                translate("{gib:.2f} GiB ({size} bytes)").format(
                    gib=size / 1024**3,
                    size=size,
                ),
            )
        )
    return tuple(fields)


def _bounded(
    records: list[object],
    limit: int,
    name: str,
    notes: list[str],
    translate: Callable[[str], str],
) -> list[object]:
    if len(records) > limit:
        notes.append(
            translate(
                "{name}: showing the first {limit} of {count} entries."
            ).format(
                name=name,
                limit=limit,
                count=len(records),
            )
        )
    return records[:limit]


def parse_disc_details(
    output: str,
    *,
    is_bluray: bool,
    playlist: str | None = None,
    dvd_title: str = "1",
    translate: Callable[[str], str] = identity,
) -> DiscDetails:
    data = _object(json.loads(output), "Disc metadata")
    sections = SectionBuffer()
    notes = [
        translate(
            "These are reported metadata, not a full-disc integrity or"
            " playback check."
        )
    ]
    if is_bluray:
        disc = _object(data.get("bluray"), "Blu-ray information")
        titles = _records(data.get("titles"), "Playlists")
        if playlist is not None:
            reported = (
                str(_object(titles[0], "Playlist").get("playlist"))
                if len(titles) == 1
                else ""
            )
            if (
                re.fullmatch(r"[0-9]{1,5}", playlist) is None
                or re.fullmatch(r"[0-9]{1,5}", reported) is None
                or int(reported) != int(playlist)
            ):
                raise ValueError(
                    "Metadata must describe the requested playlist only."
                )
        sections.append(
            DetailSection(
                translate("Blu-ray disc"),
                _fields(
                    disc,
                    (
                        ("disc name", "Name"),
                        ("udf title", "Volume label"),
                        ("titles", "Reported titles"),
                        ("main playlist", "Main feature candidate"),
                        ("longest playlist", "Longest playlist candidate"),
                        ("3D content", "3D content reported"),
                        ("initial mode", "Initial mode"),
                        ("aacs", "AACS reported"),
                        ("bdplus", "BD+ reported"),
                        ("bd-j", "Java menus"),
                    ),
                    translate,
                ),
            )
        )
        notes.append(
            translate(
                "A disc's 3D label does not verify MVC in every playlist;"
                " candidate playlists are only hints."
            )
        )
        if playlist is None:
            notes.append(
                translate(
                    "Select one playlist and open Disc Details again for its"
                    " tracks and chapter markers."
                )
            )
        seen_playlists: set[int] = set()
        for item in _bounded(
            titles,
            MAX_INSPECTION_ROWS,
            translate("Playlists"),
            notes,
            translate,
        ):
            title = _object(item, "Playlist")
            identifier = str(title.get("playlist"))
            if re.fullmatch(r"[0-9]{1,5}", identifier) is None:
                raise ValueError("Invalid playlist ID in disc details.")
            if int(identifier) in seen_playlists:
                raise ValueError("Duplicate playlist ID in disc details.")
            seen_playlists.add(int(identifier))
            fields = _fields(title, (
                ("length", "Reported duration"), ("angles", "Angles"),
            ), translate)
            if "chapters" in title:
                fields += (
                    (
                        translate("Chapters"),
                        str(len(_records(title["chapters"], "Chapters"))),
                    ),
                )
            sections.append(
                DetailSection(
                    translate("Playlist {id}").format(id=int(identifier)),
                    fields,
                )
            )
            for kind in ("video", "audio", "subtitles"):
                tracks = _records(title.get(kind, []), f"{kind} tracks")
                group = translate("Playlist {id} {kind}").format(
                    id=identifier, kind=translate(kind)
                )
                for track_value in _bounded(
                    tracks, MAX_STREAM_ROWS, group, notes, translate
                ):
                    track = _object(track_value, "Track")
                    pid = track.get("stream")
                    if not isinstance(pid, str):
                        raise ValueError("Blu-ray track must contain a PID.")
                    pid = normalize_stream_pid(pid)
                    heading = translate("Playlist {id} / {kind} {pid}").format(
                        id=identifier, kind=translate(kind), pid=pid,
                    )
                    sections.append(
                        DetailSection(
                            heading,
                            _fields(
                                track,
                                (
                                    ("codec", "Codec"),
                                    ("codec name", "Codec name"),
                                    ("language", "Language"),
                                    ("format", "Format"),
                                    ("aspect ratio", "Aspect ratio"),
                                    ("framerate", "Frame rate"),
                                    ("rate", "Reported sample rate"),
                                ),
                                translate,
                            ),
                        )
                    )
            if "chapters" in title:
                _chapters(
                    title["chapters"],
                    translate("Playlist {id}").format(id=identifier),
                    True,
                    sections,
                    notes,
                    translate,
                )
    else:
        if playlist is not None:
            raise ValueError(
                "Blu-ray playlist selection is not valid for DVD details."
            )
        title = _object(data.get("format"), "DVD title information")
        tracks = _records(data.get("streams"), "DVD streams")
        from dvdvideo.gui_commands import validate_dvd_title
        validate_dvd_title(dvd_title)
        sections.append(
            DetailSection(
                translate("DVD / title {id}").format(id=dvd_title),
                _fields(
                    title,
                    (
                        ("format_long_name", "Format"),
                        ("duration", "Duration (seconds)"),
                        ("nb_streams", "Reported streams"),
                    ),
                    translate,
                ),
            )
        )
        notes.append(
            translate(
                "DVD details cover title {id}; this is not a list of every DVD"
                " title."
            ).format(id=dvd_title)
        )
        seen: set[int] = set()
        for item in _bounded(
            tracks,
            MAX_INSPECTION_ROWS,
            translate("DVD tracks"),
            notes,
            translate,
        ):
            track = _object(item, "DVD track")
            identifier = track.get("index")
            if (
                not isinstance(identifier, int)
                or isinstance(identifier, bool)
                or not 0 <= identifier <= 9999
                or identifier in seen
            ):
                raise ValueError("Invalid or duplicate DVD stream index.")
            seen.add(identifier)
            tags = _object(track.get("tags", {}), "Track tags")
            sections.append(
                DetailSection(
                    translate("Stream {id}").format(id=identifier),
                    _fields(
                        track,
                        (
                            ("codec_type", "Type"),
                            ("codec_name", "Codec"),
                            ("profile", "Profile"),
                            ("width", "Width"),
                            ("height", "Height"),
                            ("display_aspect_ratio", "Aspect ratio"),
                            ("avg_frame_rate", "Frame rate"),
                            ("channels", "Channels"),
                            ("channel_layout", "Channel layout"),
                            ("sample_rate", "Sample rate (Hz)"),
                        ),
                        translate,
                    )
                    + _fields(
                        tags,
                        (("language", "Language"), ("title", "Name")),
                        translate,
                    ),
                )
            )
        _chapters(
            data.get("chapters", []),
            translate("DVD title {id}").format(id=dvd_title),
            False,
            sections,
            notes,
            translate,
        )
    return DiscDetails(
        tuple(sections.sections), tuple(notes), sections.truncated
    )


def _chapters(
    value: object, name: str, is_bluray: bool,
    sections: SectionBuffer, notes: list[str],
    translate: Callable[[str], str],
) -> None:
    chapters = _records(value, "Chapters")
    fields: list[tuple[str, str]] = [(translate("Count"), str(len(chapters)))]
    group = translate("{name} chapters").format(name=name)
    for index, item in enumerate(
        _bounded(chapters, MAX_CHAPTERS, group, notes, translate), 1
    ):
        chapter = _object(item, "Chapter")
        key = "start time" if is_bluray else "start_time"
        start = chapter.get(key)
        if start is None:
            raise ValueError("Chapter is missing its start time.")
        label = (
            translate("Chapter {index:03d}")
            if is_bluray
            else translate("Chapter {index:03d} (seconds)")
        )
        fields.append((label.format(index=index), _value(start, translate)))
    sections.append(
        DetailSection(
            translate("{name} / chapters").format(name=name), tuple(fields)
        )
    )
