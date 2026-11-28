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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import cast

from dvdvideo.dvd_menu_analysis import DvdMenuAnalysis
from dvdvideo.gui_preferences import (
    MAX_STREAM_PID,
    STREAM_PID,
    TrackInfo,
    normalize_stream_pid,
)
from dvdvideo.gui_translation import identity
from dvdvideo.gui_translation import identity as N_

MAX_INSPECTION_ROWS = 2000
MAX_STREAM_ROWS = 512
PLAYLIST_ID = re.compile(r"[0-9]{1,5}")
DVD_STREAM_INDEX = re.compile(r"[0-9]{1,4}")
STREAM_KINDS = frozenset({"video", "audio", "subtitle", "data", "attachment"})
PLAYLIST_DURATION = re.compile(
    r"([0-9]{1,6}):([0-5][0-9]):([0-5][0-9])(?:\.([0-9]{1,3}))?"
)
NAVIGATION_LABELS = {
    "title": N_("Disc title candidate (unverified)"),
    "menu": N_("Menu reference (unverified)"),
    "first_play": N_("Startup reference (unverified)"),
    "other": N_("Navigation reference (unverified)"),
    "unreferenced": N_("No fixed navigation reference found"),
}
DVD_NAVIGATION_LABELS = {
    "commentary": N_(
        "Title set contains director-commentary audio metadata (unverified)"
    ),
    "commentary_audio": N_(
        "Additional audio track: {language} ({channels} channels; possible"
        " commentary, unverified)"
    ),
    "commentary_metadata": N_(
        "Director-commentary audio metadata (unverified)"
    ),
}


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
    show_unselected: bool = False
    reset_playlist_selection: bool = False
    hidden_playlist_ids: set[str] = field(default_factory=set[str])


@dataclass(frozen=True)
class PlaylistAnalysis:
    ranks: dict[str, int]
    labels: dict[str, str]
    navigation: dict[str, list[str]] = field(
        default_factory=dict[str, list[str]]
    )
    warnings: list[str] = field(default_factory=list[str])
    repeated_playlists: set[str] = field(default_factory=set[str])


def parse_playlist_analysis(output: str) -> PlaylistAnalysis:
    data: object = json.loads(output)
    if not isinstance(data, dict):
        raise TypeError("Invalid playlist analysis.")
    data = cast(dict[str, object], data)
    groups, raw_labels = data.get("groups"), data.get("labels")
    if (
        not isinstance(groups, list)
        or not groups
        or not isinstance(raw_labels, dict)
    ):
        raise ValueError("Invalid playlist analysis groups or labels.")
    ranks: dict[str, int] = {}
    for rank, group in enumerate(cast(list[object], groups), start=1):
        if not isinstance(group, list) or not group:
            raise ValueError("Invalid playlist analysis group.")
        for number in cast(list[object], group):
            if (
                not isinstance(number, int)
                or isinstance(number, bool)
                or not 0 <= number <= 99999
            ):
                raise ValueError("Invalid playlist number in analysis.")
            identifier = str(number)
            if identifier in ranks:
                raise ValueError("Duplicate playlist number in analysis.")
            ranks[identifier] = rank
    labels: dict[str, str] = {}
    for identifier, label in cast(dict[object, object], raw_labels).items():
        if (
            not isinstance(identifier, str)
            or not PLAYLIST_ID.fullmatch(identifier)
            or not isinstance(label, str)
        ):
            raise ValueError("Invalid disc label in playlist analysis.")
        canonical = str(int(identifier))
        if canonical in labels:
            raise ValueError("Duplicate disc label in playlist analysis.")
        labels[canonical] = label
    raw_navigation = data.get("navigation", {})
    raw_warnings = data.get("warnings", [])
    if not isinstance(raw_navigation, dict) or not isinstance(
        raw_warnings, list
    ):
        raise TypeError("Invalid navigation analysis.")
    navigation: dict[str, list[str]] = {}
    for identifier, values in cast(
        dict[object, object], raw_navigation
    ).items():
        if (
            not isinstance(identifier, str)
            or not PLAYLIST_ID.fullmatch(identifier)
            or not isinstance(values, list)
            or not values
        ):
            raise ValueError("Invalid navigation grades.")
        grades: list[str] = []
        for value in cast(list[object], values):
            if (
                not isinstance(value, str)
                or (value not in NAVIGATION_LABELS and value != "repeated")
                or value in grades
            ):
                raise ValueError("Invalid navigation grade.")
            grades.append(value)
        canonical = str(int(identifier))
        if canonical in navigation:
            raise ValueError("Duplicate navigation grades.")
        navigation[canonical] = grades
    warnings: list[str] = []
    for warning in cast(list[object], raw_warnings):
        if not isinstance(warning, str):
            raise TypeError("Invalid navigation warning.")
        warnings.append(warning)
    raw_repeated = data.get("repeated_playlists", [])
    if not isinstance(raw_repeated, list):
        raise TypeError("Invalid repeated playlists.")
    repeated: set[str] = set()
    for number in cast(list[object], raw_repeated):
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or not 0 <= number <= 99999
        ):
            raise ValueError("Invalid repeated playlist number.")
        identifier = str(number)
        if identifier in repeated:
            raise ValueError("Duplicate repeated playlist number.")
        repeated.add(identifier)
    return PlaylistAnalysis(ranks, labels, navigation, warnings, repeated)


def apply_playlist_analysis(
    listing: Listing,
    analysis: PlaylistAnalysis,
    *,
    translate: Callable[[str], str] = identity,
) -> Listing:
    def rank(row: tuple[str, str]) -> int:
        return analysis.ranks.get(row[0], len(analysis.ranks) + 1)

    rows: list[tuple[str, str]] = []
    matched = False
    for identifier, label in sorted(listing.rows, key=rank):
        group = analysis.ranks.get(identifier)
        if group is not None:
            matched = True
            if group == 1 and analysis.navigation:
                label += "    " + translate(
                    "Possible main feature (unverified)"
                )
            else:
                label += "    " + translate("Analysis group {group}").format(
                    group=group
                )
        else:
            label += "    " + translate("Not included in playlist analysis")
        if identifier in analysis.labels:
            label += "    " + translate(
                "Disc label: {label} (unverified)"
            ).format(label=analysis.labels[identifier])
        for grade in analysis.navigation.get(identifier, []):
            if grade not in ("unreferenced", "repeated"):
                label += "    " + translate(NAVIGATION_LABELS[grade])
        rows.append((identifier, label))
    warnings = [*listing.warnings, *analysis.warnings]
    if listing.rows and not matched:
        warnings.append(
            "Playlist analysis did not match any displayed playlists."
        )
    hidden_ids = {
        identifier
        for identifier, label in analysis.labels.items()
        if re.search(
            r"(?<![A-Za-z0-9])dummy(?![A-Za-z0-9])", label, re.IGNORECASE
        )
        or re.fullmatch(r"top[\s_-]*menu", label.strip(), re.IGNORECASE)
    }
    hidden_ids.update(analysis.repeated_playlists)
    hidden_ids.update(
        identifier
        for identifier, grades in analysis.navigation.items()
        if "repeated" in grades
    )
    return replace(
        listing,
        rows=rows,
        warnings=warnings,
        show_unselected=True,
        hidden_playlist_ids=hidden_ids,
    )


def apply_dvd_navigation(
    listing: Listing,
    analysis: DvdMenuAnalysis,
    *,
    translate: Callable[[str], str] = identity,
) -> Listing:
    rows: list[tuple[str, str]] = []
    for identifier, original_label in listing.rows:
        label = original_label
        titleset = analysis.title_sets.get(int(identifier))
        if titleset in analysis.commentary_title_sets:
            label += "    " + translate(DVD_NAVIGATION_LABELS["commentary"])
        rows.append((identifier, label))
    return replace(
        listing, rows=rows, warnings=[*listing.warnings, *analysis.warnings]
    )


def _playlist_milliseconds(value: str) -> int | None:
    duration = PLAYLIST_DURATION.fullmatch(value.strip())
    if duration is None:
        return None
    hours, minutes, seconds, fraction = duration.groups()
    return (
        (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000
        + int((fraction or "").ljust(3, "0"))
    )


def _dvd_commentary_audio_hints(
    audio: list[object], translate: Callable[[str], str],
) -> tuple[str, ...]:
    tracks_by_language: dict[str, list[tuple[str, int]]] = {}
    hints: list[str] = []
    for item in audio:
        if not isinstance(item, dict):
            continue
        stream = cast(dict[str, object], item)
        language_code = stream.get("langcode")
        language_name = stream.get("language")
        language = (
            language_name.strip()
            if isinstance(language_name, str) and language_name.strip()
            else (
                language_code.strip()
                if isinstance(language_code, str) and language_code.strip()
                else "unknown"
            )
        )
        content = stream.get("content")
        if isinstance(content, str) and (
            "commentary" in content.casefold()
            or "director" in content.casefold()
        ):
            hints.append(
                translate(DVD_NAVIGATION_LABELS["commentary_metadata"])
            )
        channels = stream.get("channels")
        if (
            not isinstance(channels, int)
            or isinstance(channels, bool)
            or channels <= 0
        ):
            continue
        language_key = (
            language_code.strip().casefold()
            if isinstance(language_code, str)
            and language_code.strip().casefold()
            not in {"und", "unk", "unknown", "undefined"}
            else language.casefold()
        )
        if language_key in {"unknown", "undefined"}:
            continue
        tracks_by_language.setdefault(language_key, []).append(
            (language, channels)
        )
    for tracks in tracks_by_language.values():
        if not any(track[1] > 2 for track in tracks):
            continue
        for language, channels in tracks:
            if channels <= 2:
                hints.append(
                    translate(
                        DVD_NAVIGATION_LABELS["commentary_audio"]
                    ).format(
                        language=language, channels=channels,
                    )
                )
    return tuple(dict.fromkeys(hints))


def parse_playlists(
    output: str,
    *,
    translate: Callable[[str], str] = identity,
    include_hints: bool = True,
    minimum_minutes: int = 0,
    playlist_ranks: Mapping[str, int] | None = None,
) -> Listing:
    if not 0 <= minimum_minutes <= 600:
        raise ValueError("Invalid playlist inspection minimum.")
    lines = output.splitlines()
    if playlist_ranks is not None:
        def rank_line(line: str) -> int:
            identifier = line.partition("\t")[0]
            return (
                playlist_ranks.get(
                    str(int(identifier)), len(playlist_ranks) + 1
                )
                if PLAYLIST_ID.fullmatch(identifier)
                else len(playlist_ranks) + 1
            )

        lines.sort(key=rank_line)
    listing = Listing()
    short = unknown = 0
    seen: set[str] = set()
    hints: dict[str, list[str]] = {}
    durations: dict[str, int] = {}
    reported_longest: str | None = None
    for line in lines:
        fields = line.split("\t")
        if (
            len(fields) == 2
            and fields[0] in ("# main-playlist", "# longest-playlist")
            and PLAYLIST_ID.fullmatch(fields[1])
        ):
            labels = hints.setdefault(str(int(fields[1])), [])
            label = "Possible main feature (unverified)"
            if label not in labels:
                labels.append(label)
            if fields[0] == "# longest-playlist":
                reported_longest = str(int(fields[1]))
    for line in lines:
        if not line.strip() or line.lower() in (
            "playlist\tduration\tchapters",
            "playlist\treported_duration\tchapters",
        ):
            continue
        fields = line.split("\t", 2)
        if len(fields) == 2 and fields[0] == "# disc-name":
            listing.disc_name = fields[1]
        elif len(fields) == 2 and fields[0] == "# warning":
            listing.warnings.append(fields[1])
        elif (
            len(fields) == 2
            and fields[0] in ("# main-playlist", "# longest-playlist")
            and PLAYLIST_ID.fullmatch(fields[1])
        ):
            continue
        elif len(fields) == 3 and PLAYLIST_ID.fullmatch(fields[0]):
            playlist = str(int(fields[0]))
            if playlist in seen:
                listing.duplicates += 1
                continue
            seen.add(playlist)
            milliseconds = _playlist_milliseconds(fields[1])
            if minimum_minutes:
                if milliseconds is None:
                    unknown += 1
                    continue
                if milliseconds < minimum_minutes * 60000:
                    short += 1
                    continue
            if len(listing.rows) >= MAX_INSPECTION_ROWS:
                listing.truncated = True
                listing.omitted_ids.add(playlist)
                continue
            if milliseconds:
                durations[playlist] = milliseconds
            duration, chapters = fields[1:]
            listing.rows.append(
                (
                    playlist,
                    translate(
                        "Playlist {id}    {duration}    {chapters} chapters"
                    ).format(
                        id=playlist,
                        duration=duration,
                        chapters=chapters,
                    ),
                )
            )
        else:
            listing.invalid += 1
    if short:
        listing.warnings.append(
            f"Inspection excluded {short} playlist(s) shorter than"
            f" {minimum_minutes} minutes."
        )
    if unknown:
        listing.warnings.append(
            f"Inspection excluded {unknown} playlist(s) with missing or"
            f" unreadable durations; the {minimum_minutes}-minute minimum"
            " could not be verified."
        )
    if len(durations) > 1:
        longest = max(durations.values())
        for identifier, duration in durations.items():
            if (
                duration == longest
                and "Possible main feature (unverified)"
                not in hints.get(identifier, [])
            ):
                hints.setdefault(identifier, []).append(
                    "Possible main feature (unverified)"
                )
    if include_hints:
        listing.rows = [
            (
                identifier,
                label
                + (
                    f"    ({(
                        ', '.join(
                            translate(hint) for hint in hints[identifier]
                        )
                    )})"
                    if identifier in hints
                    else ""
                ),
            )
            for identifier, label in listing.rows
        ]
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


def parse_dvd_titles(
    output: str, *, translate: Callable[[str], str] = identity
) -> Listing:
    """Parse lsdvd JSON without treating long titles as confirmed features."""
    data: object = json.loads(output)
    if not isinstance(data, dict):
        raise TypeError("DVD title discovery did not return a title list.")
    titles = cast(dict[str, object], data).get("track")
    if not isinstance(titles, list):
        raise TypeError("DVD title discovery did not return a title list.")
    records: list[tuple[str, float, int, int, int, tuple[str, ...]]] = []
    seen: set[int] = set()
    short = 0
    for item in cast(list[object], titles):
        if not isinstance(item, dict):
            raise TypeError("DVD title discovery returned an invalid title.")
        record = cast(dict[str, object], item)
        number, duration = record.get("ix"), record.get("length")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or not 1 <= number <= 99
            or number in seen
            or not isinstance(duration, (int, float))
            or isinstance(duration, bool)
        ):
            raise ValueError(
                "DVD title discovery returned an invalid title number or"
                " duration."
            )
        try:
            duration = float(duration)
        except OverflowError as error:
            raise ValueError(
                "DVD title discovery returned an invalid title number or"
                " duration."
            ) from error
        if not math.isfinite(duration) or duration < 0:
            raise ValueError(
                "DVD title discovery returned an invalid title number or"
                " duration."
            )
        counts: list[int] = []
        for key in ("chapter", "audio", "subp"):
            value = record.get(key)
            if not isinstance(value, list):
                raise TypeError(
                    f"DVD title {number} is missing {key} information."
                )
            counts.append(len(cast(list[object], value)))
        seen.add(number)
        if duration < 5 * 60:
            short += 1
            continue
        audio_hints = _dvd_commentary_audio_hints(
            cast(list[object], record["audio"]), translate,
        )
        records.append(
            (
                str(number),
                duration,
                counts[0],
                counts[1],
                counts[2],
                audio_hints,
            )
        )
    if not seen:
        raise ValueError("DVD title discovery found no titles.")
    if not records:
        return Listing(
            warnings=[
                f"Discovery excluded all {short} DVD title(s) shorter than"
                " five minutes."
            ]
        )
    longest = max(record[1] for record in records)
    listing = Listing()
    if short:
        listing.warnings.append(
            f"Discovery excluded {short} DVD title(s) shorter than five"
            " minutes."
        )
    for number, duration, chapters, audio, subtitles, audio_hints in records:
        seconds = int(duration)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        hints: list[str] = []
        if len(records) > 1 and duration == longest and duration > 0:
            hints.append(translate("Possible main feature (unverified)"))
        suffix = f" ({', '.join(hints)})" if hints else ""
        label = (
            translate(
                "Title {id}    {duration}    {chapters} chapters, {audio}"
                " audio, {subtitles} subtitles"
            ).format(
                id=number,
                duration=f"{hours:02}:{minutes:02}:{seconds:02}",
                chapters=chapters,
                audio=audio,
                subtitles=subtitles,
            )
            + suffix
        )
        for hint in audio_hints:
            label += "    " + hint
        listing.rows.append((number, label))
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
            if len(fields) != 8 or not DVD_STREAM_INDEX.fullmatch(
                fields[0].strip()
            ):
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
            "PID\tTYPE\tCODEC\tLANG\tFORMAT",
            "PID\tTYPE\tCODEC\tLANG\tFORMAT\tDETAILS",
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
        listing.tracks.append(
            TrackInfo(identifier, kind, fields[3].strip().lower())
        )
        if len(listing.rows) >= MAX_STREAM_ROWS:
            listing.truncated = True
            listing.omitted_ids.add(identifier)
            continue
        listing.rows.append(
            (identifier, "  ".join(value for value in fields[1:] if value))
        )
    return listing
