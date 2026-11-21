"""GTK-independent parsing of bounded inspection listings."""

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

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from dvdvideo.gui_preferences import (
    MAX_STREAM_PID,
    STREAM_PID,
    TrackInfo,
    normalize_stream_pid,
)
from dvdvideo.gui_translation import identity

MAX_INSPECTION_ROWS = 2000
MAX_STREAM_ROWS = 512
PLAYLIST_ID = re.compile(r"[0-9]{1,5}")
DVD_STREAM_INDEX = re.compile(r"[0-9]{1,4}")
STREAM_KINDS = frozenset({"video", "audio", "subtitle", "data", "attachment"})
PLAYLIST_DURATION = re.compile(r"([0-9]{1,6}):([0-5][0-9]):([0-5][0-9])(?:\.([0-9]{1,3}))?")


@dataclass
class Listing:
    rows: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    tracks: list[TrackInfo] = field(default_factory=list[TrackInfo])
    warnings: list[str] = field(default_factory=list[str])
    disc_name: str | None = None
    truncated: bool = False
    mvc_verified: bool = False
    invalid: int = 0
    duplicates: int = 0
    omitted_ids: set[str] = field(default_factory=set[str])
    longest_playlist: str | None = None


def parse_playlists(output: str, *, translate: Callable[[str], str] = identity) -> Listing:
    listing = Listing()
    seen: set[str] = set()
    hints: dict[str, list[str]] = {}
    durations: dict[str, int] = {}
    seen_duration_ids: set[str] = set()
    reported_longest: str | None = None
    for line in output.splitlines():
        fields = line.split("\t")
        if (
            len(fields) == 2 and fields[0] in ("# main-playlist", "# longest-playlist")
            and PLAYLIST_ID.fullmatch(fields[1])
        ):
            labels = hints.setdefault(str(int(fields[1])), [])
            label = "main feature candidate" if fields[0] == "# main-playlist" else "longest candidate"
            if label not in labels:
                labels.append(label)
            if fields[0] == "# longest-playlist":
                reported_longest = str(int(fields[1]))
        elif len(fields) == 3 and PLAYLIST_ID.fullmatch(fields[0]):
            duration = PLAYLIST_DURATION.fullmatch(fields[1].strip())
            identifier = str(int(fields[0]))
            if identifier in seen_duration_ids:
                continue
            if len(seen_duration_ids) >= MAX_INSPECTION_ROWS:
                continue
            seen_duration_ids.add(identifier)
            if duration:
                hours, minutes, seconds, fraction = duration.groups()
                milliseconds = (
                    (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000
                    + int((fraction or "").ljust(3, "0"))
                )
                if milliseconds:
                    durations[identifier] = milliseconds
    if len(durations) > 1:
        longest = max(durations.values())
        if longest > 0:
            for identifier, duration in durations.items():
                if duration == longest and "longest candidate" not in hints.get(identifier, []):
                    hints.setdefault(identifier, []).append("longest listed duration")
    for line in output.splitlines():
        if not line.strip() or line.lower() in (
            "playlist\tduration\tchapters", "playlist\treported_duration\tchapters",
        ):
            continue
        fields = line.split("\t", 2)
        if len(fields) == 2 and fields[0] == "# disc-name":
            listing.disc_name = fields[1]
        elif len(fields) == 2 and fields[0] == "# warning":
            listing.warnings.append(fields[1])
        elif (
            len(fields) == 2 and fields[0] in ("# main-playlist", "# longest-playlist")
            and PLAYLIST_ID.fullmatch(fields[1])
        ):
            continue
        elif len(fields) == 3 and PLAYLIST_ID.fullmatch(fields[0]):
            playlist = str(int(fields[0]))
            if playlist in seen:
                listing.duplicates += 1
                continue
            seen.add(playlist)
            if len(listing.rows) >= MAX_INSPECTION_ROWS:
                listing.truncated = True
                listing.omitted_ids.add(playlist)
                continue
            duration, chapters = fields[1:]
            playlist_hints = [translate(label) for label in hints.get(playlist, [])]
            if durations.get(playlist, 0) >= 40 * 60 * 1000:
                playlist_hints.append(translate("40+ minutes"))
            hint = f"    ({', '.join(playlist_hints)})" if playlist_hints else ""
            listing.rows.append((
                playlist, translate("Playlist {id}    {duration}    {chapters} chapters").format(
                    id=playlist, duration=duration, chapters=chapters,
                ) + hint,
            ))
        else:
            listing.invalid += 1
    displayed = {identifier for identifier, _label in listing.rows}
    if reported_longest in displayed and reported_longest not in durations:
        listing.longest_playlist = reported_longest
    elif durations:
        listing.longest_playlist = max(durations, key=durations.__getitem__)
    elif reported_longest in displayed:
        listing.longest_playlist = reported_longest
    elif len(listing.rows) == 1:
        listing.longest_playlist = listing.rows[0][0]
    return listing


def parse_dvd_titles(output: str, *, translate: Callable[[str], str] = identity) -> Listing:
    """Parse lsdvd's JSON without treating a long title as a confirmed feature."""
    data: object = json.loads(output)
    if not isinstance(data, dict):
        raise TypeError("DVD title discovery did not return a title list.")
    titles = cast(dict[str, object], data).get("track")
    if not isinstance(titles, list):
        raise TypeError("DVD title discovery did not return a title list.")
    records: list[tuple[str, float, int, int, int]] = []
    seen: set[int] = set()
    for item in cast(list[object], titles):
        if not isinstance(item, dict):
            raise TypeError("DVD title discovery returned an invalid title.")
        record = cast(dict[str, object], item)
        number, duration = record.get("ix"), record.get("length")
        if (
            not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 99
            or number in seen or not isinstance(duration, (int, float))
            or isinstance(duration, bool) or not math.isfinite(duration) or duration < 0
        ):
            raise ValueError("DVD title discovery returned an invalid title number or duration.")
        counts: list[int] = []
        for key in ("chapter", "audio", "subp"):
            value = record.get(key)
            if not isinstance(value, list):
                raise TypeError(f"DVD title {number} is missing {key} information.")
            counts.append(len(cast(list[object], value)))
        seen.add(number)
        records.append((str(number), float(duration), counts[0], counts[1], counts[2]))
    if not records:
        raise ValueError("DVD title discovery found no titles.")
    longest = max(record[1] for record in records)
    listing = Listing()
    for number, duration, chapters, audio, subtitles in records:
        seconds = int(duration)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        hints: list[str] = []
        if duration >= 40 * 60:
            hints.append(translate("40+ minutes"))
        if len(records) > 1 and duration == longest and duration > 0:
            hints.append(translate("longest listed duration"))
        suffix = f" ({', '.join(hints)})" if hints else ""
        listing.rows.append((
            number,
            translate("Title {id}    {duration}    {chapters} chapters, {audio} audio, {subtitles} subtitles").format(
                id=number, duration=f"{hours:02}:{minutes:02}:{seconds:02}",
                chapters=chapters, audio=audio, subtitles=subtitles,
            ) + suffix,
        ))
    return listing


def parse_dvd_streams(output: str) -> Listing:
    listing = Listing()
    seen: set[str] = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t", 7)
        if line.split()[:3] == ["INDEX", "TYPE", "CODEC"]:
            continue
        if "\t" in line:
            if len(fields) != 8 or not DVD_STREAM_INDEX.fullmatch(fields[0].strip()):
                listing.invalid += 1
                continue
            identifier, kind = fields[:2]
            language = fields[6].strip().lower()
            details = "  ".join(value for value in fields[1:] if value)
        else:
            legacy = line.split(maxsplit=1)
            if len(legacy) != 2 or not DVD_STREAM_INDEX.fullmatch(legacy[0]):
                listing.invalid += 1
                continue
            identifier, details = legacy
            kind = details.split()[0] if details.split() else ""
            language = ""
        kind = kind.strip().lower()
        if kind not in STREAM_KINDS:
            listing.invalid += 1
            continue
        identifier = str(int(identifier))
        if identifier in seen:
            listing.duplicates += 1
            continue
        seen.add(identifier)
        listing.tracks.append(TrackInfo(identifier, kind, language))
        if len(listing.rows) >= MAX_INSPECTION_ROWS:
            listing.truncated = True
            listing.omitted_ids.add(identifier)
            continue
        listing.rows.append((identifier, f"Stream {identifier}  {details}"))
    return listing


def parse_bluray_streams(output: str) -> Listing:
    listing = Listing()
    seen: set[str] = set()
    for line in output.splitlines():
        if not line.strip() or line in (
            "PID\tTYPE\tCODEC\tLANG\tFORMAT", "PID\tTYPE\tCODEC\tLANG\tFORMAT\tDETAILS"
        ):
            continue
        if line.startswith("# warning\t"):
            listing.warnings.append(line.split("\t", 1)[1])
            continue
        if line.startswith("# mvc\t"):
            pid = line.split("\t", 1)[1]
            if STREAM_PID.fullmatch(pid) and int(pid, 16) <= MAX_STREAM_PID:
                listing.mvc_verified = True
            else:
                listing.invalid += 1
            continue
        fields = line.split("\t")
        if (
            len(fields) not in (5, 6) or not STREAM_PID.fullmatch(fields[0])
            or int(fields[0], 16) > MAX_STREAM_PID
        ):
            listing.invalid += 1
            continue
        identifier = normalize_stream_pid(fields[0])
        kind = fields[1].strip().lower()
        if kind == "subtitles":
            kind = "subtitle"
        if kind not in STREAM_KINDS:
            listing.invalid += 1
            continue
        if identifier in seen:
            listing.duplicates += 1
            continue
        seen.add(identifier)
        listing.tracks.append(TrackInfo(identifier, kind, fields[3].strip().lower()))
        if len(listing.rows) >= MAX_STREAM_ROWS:
            listing.truncated = True
            listing.omitted_ids.add(identifier)
            continue
        listing.rows.append((identifier, "  ".join(value for value in fields[1:] if value)))
    return listing
