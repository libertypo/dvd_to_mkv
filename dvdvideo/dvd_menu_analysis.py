"""Extract bounded, static DVD-Video menu references from IFO and NAV data."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, cast

from dvdvideo.gui_source import find_entry
from dvdvideo.ifo import MalformedIfoHeaderError
from dvdvideo.udf.general import MalformedUdfError, UnsupportedUdfError
from dvdvideo.volume import MalformedVolumePartError

if TYPE_CHECKING:
    from dvdvideo.gui_support import ManagedProcess
    from dvdvideo.volume import FileSetUdf

SECTOR_SIZE = 2048
MAX_IFO_BYTES = 16 * 1024 * 1024
MAX_IFO_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MENU_VOB_BYTES = 128 * 1024 * 1024
MAX_TITLESETS = 99
MAX_TITLES = 99
MAX_PGCS = 4096
MAX_COMMANDS = 4096
MENU_ANALYSIS_TIMEOUT = 60
VMGI_ID = b"DVDVIDEO-VMG"
VTSI_ID = b"DVDVIDEO-VTS"
DVD_TIME_LIMITATION = (
    "DVD navigation grades report static startup/menu references, not verified"
    " playback routes or a confirmed main feature. Menu captions are"
    " graphical; conditional commands are not evaluated."
)


@dataclass(frozen=True)
class DvdMenuAnalysis:
    startup_titles: frozenset[int]
    menu_titles: frozenset[int]
    button_titles: frozenset[int]
    title_sets: Mapping[int, int]
    commentary_title_sets: frozenset[int]
    warnings: tuple[str, ...] = (DVD_TIME_LIMITATION,)


class DvdMenuAnalysisError(ValueError):
    """DVD metadata could not be read for static navigation analysis."""


@dataclass
class _NavigationReferences:
    startup: set[int] = field(default_factory=set[int])
    menu: set[int] = field(default_factory=set[int])
    buttons: set[int] = field(default_factory=set[int])
    conditional: int = 0


def _u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise ValueError("Truncated DVD IFO table.")
    return int.from_bytes(data[offset:offset + 2], "big")


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ValueError("Truncated DVD IFO table.")
    return int.from_bytes(data[offset:offset + 4], "big")


def _bounded_end(data: bytes, offset: int, last_byte: int, label: str) -> int:
    end = offset + last_byte + 1
    if last_byte < 7 or end < offset or end > len(data):
        raise ValueError(f"Invalid or truncated DVD {label}.")
    return end


def _sector_pointer(data: bytes, field_offset: int, label: str) -> int:
    sector = _u32(data, field_offset)
    if sector > len(data) // SECTOR_SIZE:
        raise ValueError(f"DVD {label} offset exceeds its IFO file.")
    return sector * SECTOR_SIZE


def _parse_title_sets(vmgi: bytes) -> tuple[int, dict[int, tuple[int, int]]]:
    if len(vmgi) < SECTOR_SIZE or vmgi[:12] != VMGI_ID:
        raise ValueError("Invalid DVD Video Manager IFO.")
    titlesets = _u16(vmgi, 62)
    if titlesets > MAX_TITLESETS:
        raise ValueError("DVD declares too many title sets.")
    table_offset = _sector_pointer(vmgi, 196, "title search table")
    if table_offset == 0:
        raise ValueError("DVD title search table is missing.")
    count = _u16(vmgi, table_offset)
    if count > MAX_TITLES:
        raise ValueError("DVD declares too many global titles.")
    table_end = _bounded_end(
        vmgi, table_offset, _u32(vmgi, table_offset + 4), "title search table"
    )
    entries_end = table_offset + 8 + count * 12
    if entries_end > table_end:
        raise ValueError(
            "DVD title search table entries exceed their declared extent."
        )
    title_sets: dict[int, tuple[int, int]] = {}
    for index in range(count):
        entry = table_offset + 8 + index * 12
        titleset, local_title = vmgi[entry + 6], vmgi[entry + 7]
        if (
            not 1 <= titleset <= titlesets
            or not 1 <= local_title <= MAX_TITLES
        ):
            raise ValueError(
                "DVD title search table contains an invalid title reference."
            )
        title_sets[index + 1] = (titleset, local_title)
    return titlesets, title_sets


def _command_table(
    data: bytes, pgc_offset: int, parent_end: int | None = None
) -> tuple[bytes, ...]:
    extent = len(data) if parent_end is None else parent_end
    if pgc_offset < 0 or pgc_offset + 236 > extent:
        raise ValueError("Truncated DVD program chain.")
    command_offset = _u16(data, pgc_offset + 228)
    if command_offset == 0:
        return ()
    if command_offset < 236:
        raise ValueError("DVD PGC command table overlaps its fixed header.")
    start = pgc_offset + command_offset
    if start + 8 > extent:
        raise ValueError(
            "DVD PGC command table header exceeds its enclosing extent."
        )
    pre_count, post_count, cell_count = (
        _u16(data, start + field) for field in (0, 2, 4)
    )
    count = pre_count + post_count + cell_count
    if count > MAX_COMMANDS:
        raise ValueError("DVD PGC contains too many VM commands.")
    table_end = _bounded_end(
        data, start, _u16(data, start + 6), "PGC command table"
    )
    if table_end > extent:
        raise ValueError("DVD PGC command table exceeds its enclosing extent.")
    commands_start = start + 8
    commands_end = commands_start + count * 8
    if commands_end > table_end:
        raise ValueError("DVD PGC command table is truncated.")
    return tuple(
        data[offset : offset + 8]
        for offset in range(commands_start, commands_end, 8)
    )


def _pgcit_commands(
    data: bytes, table_offset: int, parent_end: int | None = None
) -> tuple[bytes, ...]:
    if table_offset == 0:
        return ()
    extent = len(data) if parent_end is None else parent_end
    if table_offset < 0 or table_offset + 8 > extent:
        raise ValueError(
            "DVD PGC information table header exceeds its enclosing extent."
        )
    count = _u16(data, table_offset)
    if count > MAX_PGCS:
        raise ValueError("DVD menu table contains too many program chains.")
    table_end = _bounded_end(
        data,
        table_offset,
        _u32(data, table_offset + 4),
        "PGC information table",
    )
    if table_end > extent:
        raise ValueError(
            "DVD PGC information table exceeds its enclosing extent."
        )
    entries_end = table_offset + 8 + count * 8
    if entries_end > table_end:
        raise ValueError(
            "DVD PGC information entries exceed their declared extent."
        )
    starts: list[int] = []
    seen: set[int] = set()
    for index in range(count):
        entry = table_offset + 8 + index * 8
        pgc_offset = table_offset + _u32(data, entry + 4)
        if (
            pgc_offset in seen
            or pgc_offset < entries_end
            or pgc_offset + 236 > table_end
        ):
            raise ValueError("DVD menu table contains an invalid PGC offset.")
        starts.append(pgc_offset)
        seen.add(pgc_offset)
    ordered = sorted(starts)
    ends = dict(pairwise([*ordered, table_end]))
    commands: list[bytes] = []
    for pgc_offset in starts:
        commands.extend(_command_table(data, pgc_offset, ends[pgc_offset]))
    return tuple(commands)


def _language_menu_commands(
    data: bytes, table_offset: int
) -> tuple[bytes, ...]:
    if table_offset == 0:
        return ()
    count = _u16(data, table_offset)
    if count > 32:
        raise ValueError("DVD menu table contains too many language units.")
    table_end = _bounded_end(
        data, table_offset, _u32(data, table_offset + 4), "menu language table"
    )
    entries_end = table_offset + 8 + count * 8
    if entries_end > table_end:
        raise ValueError(
            "DVD menu language entries exceed their declared extent."
        )
    starts: list[int] = []
    seen: set[int] = set()
    for index in range(count):
        entry = table_offset + 8 + index * 8
        language_start = table_offset + _u32(data, entry + 4)
        if (
            language_start < entries_end
            or language_start + 8 > table_end
            or language_start in seen
        ):
            raise ValueError("DVD menu language unit has an invalid offset.")
        starts.append(language_start)
        seen.add(language_start)
    ordered = sorted(starts)
    ends = dict(pairwise([*ordered, table_end]))
    commands: list[bytes] = []
    for language_start in starts:
        commands.extend(
            _pgcit_commands(data, language_start, ends[language_start])
        )
    return tuple(commands)


def _first_play_commands(vmgi: bytes) -> tuple[bytes, ...]:
    offset = _u32(vmgi, 132)
    if offset == 0:
        return ()
    if offset > len(vmgi):
        raise ValueError("DVD first-play PGC offset exceeds its IFO file.")
    return _command_table(vmgi, offset)


def _vtsi_menu_commands(vtsi: bytes) -> tuple[bytes, ...]:
    if len(vtsi) < SECTOR_SIZE or vtsi[:12] != VTSI_ID:
        raise ValueError("Invalid DVD Video Title Set IFO.")
    table_offset = _sector_pointer(vtsi, 208, "title-set menu table")
    return _language_menu_commands(vtsi, table_offset)


def _commentary_audio_flag(vtsi: bytes) -> bool:
    count = vtsi[515]
    if count > 8 or 516 + count * 8 > len(vtsi):
        raise ValueError("Invalid DVD title-set audio attributes.")
    return any(vtsi[516 + index * 8 + 5] in (3, 4) for index in range(count))


def _bits(instruction: int, start: int, count: int) -> int:
    return (instruction >> (start + 1 - count)) & ((1 << count) - 1)


def _resolve_command(
    raw: bytes,
    title_sets: Mapping[int, tuple[int, int]],
    local_titles: Mapping[tuple[int, int], frozenset[int]],
    titleset: int | None,
) -> tuple[int | None, bool]:
    instruction = int.from_bytes(raw, "big")
    if _bits(instruction, 63, 3) != 1 or _bits(instruction, 60, 1) != 1:
        return None, False
    command = _bits(instruction, 51, 4)
    conditional = _bits(instruction, 54, 3) != 0
    if command == 2:
        target = _bits(instruction, 22, 7)
        return (target if target in title_sets else None), conditional
    if command in (3, 5) and titleset is not None:
        local_title = _bits(instruction, 22, 7)
        candidates = local_titles.get((titleset, local_title), frozenset())
        return (min(candidates) if candidates else None), conditional
    return None, conditional


def _record_commands(
    commands: Iterable[bytes], role: str, titleset: int | None,
    title_sets: Mapping[int, tuple[int, int]],
    local_titles: Mapping[tuple[int, int], frozenset[int]],
    references: _NavigationReferences,
) -> None:
    destinations = {
        "startup": references.startup,
        "menu": references.menu,
        "button": references.buttons,
    }[role]
    for command in commands:
        title, conditional = _resolve_command(
            command, title_sets, local_titles, titleset
        )
        if title is not None:
            destinations.add(title)
            references.conditional += int(conditional)


PCI_DATA_SIZE = 979
PCI_BUTTON_COUNT_OFFSET = 113
PCI_BUTTON_GROUPS_OFFSET = 14
PCI_BUTTONS_OFFSET = 152
PCI_BUTTON_SIZE = 18


def _button_commands(sector: bytes) -> tuple[bytes, ...]:
    if len(sector) != SECTOR_SIZE or sector[:4] != b"\x00\x00\x01\xba":
        return ()
    packet_offset = 14 + (sector[13] & 7)
    if sector[packet_offset:packet_offset + 4] == b"\x00\x00\x01\xbb":
        if packet_offset + 6 > len(sector):
            return ()
        header_length = int.from_bytes(
            sector[packet_offset + 4 : packet_offset + 6], "big"
        )
        packet_offset += 6 + header_length
    if packet_offset + 7 > len(sector):
        return ()
    if sector[packet_offset:packet_offset + 4] != b"\x00\x00\x01\xbf":
        return ()
    packet_length = int.from_bytes(
        sector[packet_offset + 4 : packet_offset + 6], "big"
    )
    if (
        packet_length < PCI_DATA_SIZE + 1
        or packet_offset + 6 + packet_length > len(sector)
        or sector[packet_offset + 6] != 0
    ):
        return ()
    start = packet_offset + 7
    pci = sector[start:start + PCI_DATA_SIZE]
    if len(pci) != PCI_DATA_SIZE:
        return ()
    groups = (pci[PCI_BUTTON_GROUPS_OFFSET] >> 4) & 3
    button_count = pci[PCI_BUTTON_COUNT_OFFSET] & 0x3F
    if groups not in (1, 2, 3) or button_count > 36 // groups:
        return ()
    result: list[bytes] = []
    buttons_per_group = 36 // groups
    for group in range(groups):
        for button in range(button_count):
            entry = (
                PCI_BUTTONS_OFFSET
                + (group * buttons_per_group + button) * PCI_BUTTON_SIZE
            )
            result.append(pci[entry + 10:entry + 18])
    return tuple(result)


def _scan_nav_sectors(
    chunks: Iterable[bytes],
    titleset: int | None,
    title_sets: Mapping[int, tuple[int, int]],
    local_titles: Mapping[tuple[int, int], frozenset[int]],
    references: _NavigationReferences,
) -> int:
    sectors = 0
    for chunk in chunks:
        if len(chunk) % SECTOR_SIZE:
            raise ValueError("DVD menu VOB data is not sector-aligned.")
        for offset in range(0, len(chunk), SECTOR_SIZE):
            sectors += 1
            for command in _button_commands(
                chunk[offset : offset + SECTOR_SIZE]
            ):
                _record_commands(
                    (command,),
                    "button",
                    titleset,
                    title_sets,
                    local_titles,
                    references,
                )
    return sectors


def analyze_bytes(
    vmgi: bytes,
    vts_ifos: Mapping[int, bytes],
    menu_vobs: Mapping[int | None, Iterable[bytes]],
) -> DvdMenuAnalysis:
    """Analyze supplied IFO/VOB bytes; ``None`` is the VMG menu domain."""
    return _analyze_records(vmgi, vts_ifos.items(), menu_vobs)


def _analyze_records(
    vmgi: bytes, vts_ifos: Iterable[tuple[int, bytes]],
    menu_vobs: Mapping[int | None, Iterable[bytes]],
) -> DvdMenuAnalysis:
    total_ifo_bytes = len(vmgi)
    if total_ifo_bytes > MAX_IFO_TOTAL_BYTES:
        raise ValueError(
            "DVD IFO metadata exceeds the 64 MiB aggregate limit."
        )
    titlesets, title_map = _parse_title_sets(vmgi)
    local_titles_mutable: dict[tuple[int, int], set[int]] = {}
    for global_title, (titleset, local_title) in title_map.items():
        local_titles_mutable.setdefault((titleset, local_title), set()).add(
            global_title
        )
    local_titles = {
        key: frozenset(values) for key, values in local_titles_mutable.items()
    }
    references = _NavigationReferences()
    _record_commands(
        _first_play_commands(vmgi),
        "startup",
        None,
        title_map,
        local_titles,
        references,
    )
    menu_commands = _language_menu_commands(
        vmgi, _sector_pointer(vmgi, 200, "VMG menu table")
    )
    _record_commands(
        menu_commands, "menu", None, title_map, local_titles, references
    )
    commentary: set[int] = set()
    warnings = [DVD_TIME_LIMITATION]
    for titleset, vtsi in vts_ifos:
        total_ifo_bytes += len(vtsi)
        if total_ifo_bytes > MAX_IFO_TOTAL_BYTES:
            raise ValueError(
                "DVD IFO metadata exceeds the 64 MiB aggregate limit."
            )
        if not 1 <= titleset <= titlesets:
            raise ValueError("DVD title-set IFO has an invalid number.")
        _record_commands(
            _vtsi_menu_commands(vtsi),
            "menu",
            titleset,
            title_map,
            local_titles,
            references,
        )
        if _commentary_audio_flag(vtsi):
            commentary.add(titleset)
        del vtsi
    scanned_sectors = 0
    cap_reached = False
    for titleset, chunks in menu_vobs.items():
        if titleset is not None and not 1 <= titleset <= titlesets:
            raise ValueError("DVD menu VOB has an invalid title-set number.")
        remaining = MAX_MENU_VOB_BYTES // SECTOR_SIZE - scanned_sectors
        if remaining <= 0:
            cap_reached = True
            break
        iterator = _bounded_chunks(chunks, remaining)
        scanned_sectors += _scan_nav_sectors(
            iterator, titleset, title_map, local_titles, references,
        )
    if references.conditional:
        warnings.append(
            f"{references.conditional} conditional menu/startup command(s)"
            " target titles; their conditions were not evaluated."
        )
    if cap_reached or scanned_sectors >= MAX_MENU_VOB_BYTES // SECTOR_SIZE:
        warnings.append(
            f"Menu NAV scan reached the {MAX_MENU_VOB_BYTES // (1024 * 1024)}"
            " MiB limit; some menu references may be missing."
        )
    if (
        not references.startup
        and not references.menu
        and not references.buttons
    ):
        warnings.append(
            "No fixed title-targeting startup or menu commands were found."
        )
    return DvdMenuAnalysis(
        frozenset(references.startup),
        frozenset(references.menu),
        frozenset(references.buttons),
        {number: title[0] for number, title in title_map.items()},
        frozenset(commentary),
        tuple(warnings),
    )


def _bounded_chunks(
    chunks: Iterable[bytes], sector_limit: int
) -> Iterator[bytes]:
    remaining = sector_limit
    for chunk in chunks:
        if remaining <= 0:
            return
        if len(chunk) % SECTOR_SIZE:
            raise ValueError("DVD menu VOB data is not sector-aligned.")
        sectors = min(len(chunk) // SECTOR_SIZE, remaining)
        if sectors:
            yield chunk[:sectors * SECTOR_SIZE]
            remaining -= sectors


def _read_ifo_file(
    ifo: FileSetUdf.File, *, max_bytes: int | None = None
) -> bytes:
    if ifo.length <= 0 or ifo.length * SECTOR_SIZE > MAX_IFO_BYTES:
        raise ValueError("DVD IFO exceeds the supported size.")
    if max_bytes is not None and ifo.length * SECTOR_SIZE > max_bytes:
        raise ValueError(
            "DVD IFO metadata exceeds the 64 MiB aggregate limit."
        )
    data = b"".join(ifo.dump())
    if len(data) != ifo.length * SECTOR_SIZE:
        raise OSError("Incomplete read while scanning DVD IFO.")
    return data


def _file_chunks(
    file: FileSetUdf.File, remaining_bytes: int
) -> Iterator[bytes]:
    sectors = min(file.length, remaining_bytes // SECTOR_SIZE)
    yield from file.dump(sectors)


def _find_child(directory: Path, name: str) -> Path | None:
    try:
        match = find_entry(str(directory), name.upper(), lambda _path: True)
    except RuntimeError as error:
        raise ValueError(str(error)) from error
    return Path(match) if match is not None else None


def _directory_layout(source: Path) -> Path:
    if source.name.casefold() == "video_ts" and _find_child(
        source, "VIDEO_TS.IFO"
    ):
        return source
    video_ts = _find_child(source, "VIDEO_TS")
    if video_ts is not None and video_ts.is_dir():
        return video_ts
    if _find_child(source, "VIDEO_TS.IFO"):
        return source
    raise ValueError("DVD folder does not contain VIDEO_TS.IFO.")


def _path_ifo(path: Path, *, max_bytes: int | None = None) -> bytes:
    size = path.stat().st_size
    if size <= 0 or size > MAX_IFO_BYTES or size % SECTOR_SIZE:
        raise ValueError(f"Invalid DVD IFO size: {path.name}.")
    if max_bytes is not None and size > max_bytes:
        raise ValueError(
            "DVD IFO metadata exceeds the 64 MiB aggregate limit."
        )
    read_limit = (
        min(MAX_IFO_BYTES, max_bytes)
        if max_bytes is not None
        else MAX_IFO_BYTES
    )
    with path.open("rb") as stream:
        data = stream.read(read_limit + 1)
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(
            "DVD IFO metadata exceeds the 64 MiB aggregate limit."
        )
    if not data or len(data) > MAX_IFO_BYTES or len(data) % SECTOR_SIZE:
        raise ValueError(f"Invalid DVD IFO size: {path.name}.")
    return data


def _path_chunks(path: Path, remaining_bytes: int) -> Iterator[bytes]:
    size = path.stat().st_size
    if size % SECTOR_SIZE:
        raise ValueError(f"DVD menu VOB is not sector-aligned: {path.name}.")
    limit = min(size, remaining_bytes // SECTOR_SIZE * SECTOR_SIZE)
    with path.open("rb") as stream:
        while limit:
            chunk = stream.read(min(1024 * 1024, limit))
            if not chunk:
                raise OSError(
                    "Incomplete read while scanning DVD menu VOB:"
                    f" {path.name}."
                )
            yield chunk
            limit -= len(chunk)


def _analyze_directory(source: Path) -> DvdMenuAnalysis:
    video_ts = _directory_layout(source)
    vmgi = _path_ifo(
        _find_child(video_ts, "VIDEO_TS.IFO") or video_ts / "VIDEO_TS.IFO"
    )
    titlesets, _ = _parse_title_sets(vmgi)
    menu_vobs: dict[int | None, Iterable[bytes]] = {}
    vmgi_menu = _find_child(video_ts, "VIDEO_TS.VOB")
    if vmgi_menu is not None and vmgi_menu.is_file():
        menu_vobs[None] = _path_chunks(vmgi_menu, MAX_MENU_VOB_BYTES)
    total_bytes = min(
        (
            vmgi_menu.stat().st_size
            if vmgi_menu is not None and vmgi_menu.is_file()
            else 0
        ),
        MAX_MENU_VOB_BYTES,
    )
    source_warnings: list[str] = []

    def ifos() -> Iterator[tuple[int, bytes]]:
        nonlocal total_bytes
        total_ifo_bytes = len(vmgi)
        for titleset in range(1, titlesets + 1):
            prefix = f"VTS_{titleset:02d}_0"
            ifo_path = _find_child(video_ts, prefix + ".IFO")
            if ifo_path is None:
                ifo_path = _find_child(video_ts, prefix + ".BUP")
            if ifo_path is None:
                source_warnings.append(
                    f"DVD title set {titleset:02} has no readable IFO file."
                )
                continue
            data = _path_ifo(
                ifo_path, max_bytes=MAX_IFO_TOTAL_BYTES - total_ifo_bytes
            )
            total_ifo_bytes += len(data)
            vob_path = _find_child(video_ts, prefix + ".VOB")
            if (
                vob_path is not None
                and vob_path.is_file()
                and total_bytes < MAX_MENU_VOB_BYTES
            ):
                menu_vobs[titleset] = _path_chunks(
                    vob_path, MAX_MENU_VOB_BYTES - total_bytes
                )
                total_bytes += min(
                    vob_path.stat().st_size, MAX_MENU_VOB_BYTES - total_bytes
                )
            yield titleset, data
            del data

    analysis = _analyze_records(vmgi, ifos(), menu_vobs)
    return DvdMenuAnalysis(
        analysis.startup_titles, analysis.menu_titles, analysis.button_titles,
        analysis.title_sets, analysis.commentary_title_sets,
        (*analysis.warnings, *source_warnings),
    )


def _analyze_image_or_device(source: Path) -> DvdMenuAnalysis:
    from dvdvideo.media import MediaUdf

    with MediaUdf(str(source)) as media:
        vmg = media.vmg()
        vmgi = _read_ifo_file(vmg.fileset.ifo)
        titlesets, _ = _parse_title_sets(vmgi)
        menu_vobs: dict[int | None, Iterable[bytes]] = {}
        if vmg.fileset.menu_vob is not None:
            menu_vobs[None] = _file_chunks(
                vmg.fileset.menu_vob, MAX_MENU_VOB_BYTES
            )
        consumed = (
            vmg.fileset.menu_vob.length * SECTOR_SIZE
            if vmg.fileset.menu_vob is not None else 0
        )
        vts_warnings: list[str] = []

        def ifos() -> Iterator[tuple[int, bytes]]:
            nonlocal consumed
            total_ifo_bytes = len(vmgi)
            for titleset in range(1, titlesets + 1):
                try:
                    vts = media.vts(titleset)
                except (
                    MalformedIfoHeaderError,
                    MalformedVolumePartError,
                    OSError,
                    ValueError,
                ) as error:
                    vts_warnings.append(
                        f"Could not inspect DVD title set {titleset:02}:"
                        f" {error}"
                    )
                    continue
                data = _read_ifo_file(
                    vts.fileset.ifo,
                    max_bytes=MAX_IFO_TOTAL_BYTES - total_ifo_bytes,
                )
                total_ifo_bytes += len(data)
                if (
                    vts.fileset.menu_vob is not None
                    and consumed < MAX_MENU_VOB_BYTES
                ):
                    menu_vobs[titleset] = _file_chunks(
                        vts.fileset.menu_vob, MAX_MENU_VOB_BYTES - consumed,
                    )
                    consumed += min(
                        vts.fileset.menu_vob.length * SECTOR_SIZE,
                        MAX_MENU_VOB_BYTES - consumed,
                    )
                yield titleset, data
                del data

        analysis = _analyze_records(vmgi, ifos(), menu_vobs)
        return DvdMenuAnalysis(
            analysis.startup_titles,
            analysis.menu_titles,
            analysis.button_titles,
            analysis.title_sets,
            analysis.commentary_title_sets,
            (*analysis.warnings, *vts_warnings),
        )


def analyze_source(source: str | os.PathLike[str]) -> DvdMenuAnalysis:
    path = Path(source)
    try:
        if path.is_dir():
            return _analyze_directory(path)
        return _analyze_image_or_device(path)
    except (
        OSError, UnicodeError, ValueError, MalformedIfoHeaderError,
        MalformedUdfError, UnsupportedUdfError, MalformedVolumePartError,
    ) as error:
        raise DvdMenuAnalysisError(
            f"Could not read DVD navigation metadata: {error}"
        ) from error


def _analysis_titles(value: object) -> frozenset[int]:
    if not isinstance(value, list):
        raise TypeError("Invalid DVD analysis title list.")
    values = cast(list[object], value)
    if any(
        type(number) is not int or not 1 <= number <= MAX_TITLES
        for number in values
    ):
        raise ValueError("Invalid DVD analysis title number.")
    return frozenset(cast(list[int], values))


def _decode_analysis(output: str) -> DvdMenuAnalysis:
    value: object = json.loads(output)
    if not isinstance(value, dict):
        raise TypeError("Invalid DVD analysis result.")
    data = cast(dict[str, object], value)
    raw_sets = data.get("title_sets")
    warnings = data.get("warnings")
    if not isinstance(raw_sets, dict) or not isinstance(warnings, list):
        raise TypeError("Invalid DVD analysis title sets or warnings.")
    title_sets: dict[int, int] = {}
    for title, titleset in cast(dict[object, object], raw_sets).items():
        if (
            not isinstance(title, str)
            or not title.isascii()
            or not title.isdecimal()
            or not 1 <= int(title) <= MAX_TITLES
            or int(title) in title_sets
            or type(titleset) is not int
            or not 1 <= titleset <= MAX_TITLESETS
        ):
            raise ValueError("Invalid DVD analysis title set.")
        title_sets[int(title)] = titleset
    if not all(
        isinstance(warning, str) for warning in cast(list[object], warnings)
    ):
        raise ValueError("Invalid DVD analysis warning.")
    return DvdMenuAnalysis(
        _analysis_titles(data.get("startup_titles")),
        _analysis_titles(data.get("menu_titles")),
        _analysis_titles(data.get("button_titles")),
        title_sets,
        _analysis_titles(data.get("commentary_title_sets")),
        tuple(cast(list[str], warnings)),
    )


def run_analysis(
    source: str, operation: ManagedProcess, log: Callable[[str], None]
) -> DvdMenuAnalysis:
    command = [
        sys.executable,
        "-c",
        (
            "import sys; sys.path.insert(0, sys.argv.pop(1)); from"
            " dvdvideo.dvd_menu_analysis import main; raise SystemExit(main())"
        ),
        str(Path(__file__).parent.parent),
        "--",
        source,
    ]
    try:
        result = operation.run(
            command, timeout=MENU_ANALYSIS_TIMEOUT, abandon_unresponsive=True,
            stderr_log=log, termination_log=log,
        )
        if result.returncode:
            raise DvdMenuAnalysisError(
                "DVD navigation helper failed:"
                f" {(
                    result.stderr.strip() or f'exit status {result.returncode}'
                )}"
            )
        return _decode_analysis(result.stdout)
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        raise DvdMenuAnalysisError(
            f"Could not read DVD navigation metadata: {error}"
        ) from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    args = parser.parse_args()
    try:
        analysis = analyze_source(args.source)
        json.dump({
            "startup_titles": sorted(analysis.startup_titles),
            "menu_titles": sorted(analysis.menu_titles),
            "button_titles": sorted(analysis.button_titles),
            "title_sets": dict(analysis.title_sets),
            "commentary_title_sets": sorted(analysis.commentary_title_sets),
            "warnings": analysis.warnings,
        }, sys.stdout)
        print()
    except (OSError, ValueError) as error:
        print(f"DVD menu analysis failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
