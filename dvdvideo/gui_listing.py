"""GTK-independent parsing of bounded inspection listings."""

import re
from dataclasses import dataclass, field

from dvdvideo.gui_preferences import (
    MAX_STREAM_PID,
    STREAM_PID,
    TrackInfo,
    normalize_stream_pid,
)

MAX_INSPECTION_ROWS = 2000
MAX_STREAM_ROWS = 512
PLAYLIST_ID = re.compile(r"[0-9]{1,5}")
DVD_STREAM_INDEX = re.compile(r"[0-9]{1,4}")
STREAM_KINDS = frozenset({"video", "audio", "subtitle", "data", "attachment"})


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


def parse_playlists(output: str) -> Listing:
    listing = Listing()
    seen: set[str] = set()
    hints: dict[str, list[str]] = {}
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
            playlist_hints = hints.get(playlist, [])
            hint = f"    ({', '.join(playlist_hints)})" if playlist_hints else ""
            listing.rows.append((
                playlist, f"Playlist {playlist}    {duration}    {chapters} chapters{hint}"
            ))
        else:
            listing.invalid += 1
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
        if len(listing.rows) >= MAX_INSPECTION_ROWS:
            listing.truncated = True
            listing.omitted_ids.add(identifier)
            continue
        listing.tracks.append(TrackInfo(identifier, kind, language))
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
        if len(listing.rows) >= MAX_STREAM_ROWS:
            listing.truncated = True
            listing.omitted_ids.add(identifier)
            continue
        listing.rows.append((identifier, "  ".join(value for value in fields[1:] if value)))
        listing.tracks.append(TrackInfo(identifier, kind, fields[3].strip().lower()))
    return listing
