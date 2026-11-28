"""Read-only HDMV playlist candidates from Movie Object references."""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dvdvideo.bluray_disc_type import (
    INDEX_PATH,
    MAX_INDEX_BYTES,
    NavigationEntry,
    parse_navigation_entries,
    read_disc_metadata,
)
from dvdvideo.bluray_inventory import (
    Inventory,
    authoring_labels,
    format_analysis,
    inventory,
    report_text,
    unique_clip_duration,
)

MAX_MOVIE_OBJECT_BYTES = 16 * 1024 * 1024
MOVIE_OBJECT_PATH = "BDMV/MovieObject.bdmv"
NAVIGATION_LIMITATION = (
    "HDMV grades describe direct references and local register assignments,"
    " not verified playback routes or feature identification. Conditions,"
    " object jumps and Interactive Graphics menu-button commands are not"
    " traced."
)


@dataclass(frozen=True)
class MovieObjects:
    playlists: list[set[int]]
    indirect_playback_commands: int
    register_playlists: list[set[int]] = field(default_factory=list[set[int]])


def parse_movie_objects(data: bytes) -> MovieObjects:
    if (
        len(data) < 50
        or data[:4] != b"MOBJ"
        or data[4:8] not in (b"0100", b"0200", b"0300")
    ):
        raise ValueError("Invalid or unsupported Movie Object header.")
    end = 44 + int.from_bytes(data[40:44], "big")
    if end < 50 or end > len(data):
        raise ValueError("Truncated Movie Object section.")
    count = int.from_bytes(data[48:50], "big")
    offset = 50
    objects: list[set[int]] = []
    register_objects: list[set[int]] = []
    indirect = 0
    for _ in range(count):
        if offset + 4 > end:
            raise ValueError("Truncated Movie Object record.")
        command_count = int.from_bytes(data[offset + 2:offset + 4], "big")
        offset += 4
        if offset + command_count * 12 > end:
            raise ValueError("Truncated Movie Object commands.")
        targets: set[int] = set()
        has_indirect_goto = False
        for pc in range(command_count):
            position = offset + pc * 12
            instruction = int.from_bytes(data[position:position + 4], "big")
            group, subgroup = (instruction >> 27) & 3, (instruction >> 24) & 7
            branch = (instruction >> 16) & 15
            if group == 0 and subgroup == 0 and branch == 1:
                if instruction & (1 << 23):
                    targets.add(
                        int.from_bytes(
                            data[position + 4 : position + 8], "big"
                        )
                    )
                else:
                    has_indirect_goto = True
        playlists: set[int] = set()
        register_playlists: set[int] = set()
        for pc in range(command_count):
            position = offset + pc * 12
            instruction = int.from_bytes(data[position:position + 4], "big")
            group, subgroup = (instruction >> 27) & 3, (instruction >> 24) & 7
            branch = (instruction >> 16) & 15
            if group == 0 and subgroup == 2 and branch in (0, 1, 2):
                operands = instruction >> 29
                if operands != (1 if branch == 0 else 2):
                    raise ValueError(
                        "Invalid HDMV playback command operand count."
                    )
                if instruction & (1 << 23):
                    number = int.from_bytes(
                        data[position + 4 : position + 8], "big"
                    )
                    if number > 99999:
                        raise ValueError("Invalid HDMV playlist reference.")
                    playlists.add(number)
                else:
                    register = int.from_bytes(
                        data[position + 4 : position + 8], "big"
                    )
                    previous = (
                        int.from_bytes(
                            data[position - 12 : position - 8], "big"
                        )
                        if pc
                        else 0
                    )
                    earlier = (
                        int.from_bytes(
                            data[position - 24 : position - 20], "big"
                        )
                        if pc >= 2
                        else 0
                    )
                    # A Goto or failed comparison may enter playback without
                    # executing the Move.
                    assignment = (
                        pc > 0
                        and not has_indirect_goto
                        and pc not in targets
                        and (pc < 2 or (earlier >> 27) & 3 != 1)
                        and previous >> 29 == 2
                        and (previous >> 27) & 3 == 2
                        and (previous >> 24) & 7 == 0
                        and previous & 31 == 1
                        and not previous & (1 << 23)
                        and previous & (1 << 22)
                        and register <= 0xFFF
                        and int.from_bytes(
                            data[position - 8 : position - 4], "big"
                        )
                        == register
                    )
                    if assignment:
                        number = int.from_bytes(
                            data[position - 4 : position], "big"
                        )
                        if number <= 99999:
                            register_playlists.add(number)
                            continue
                    indirect += 1
        offset += command_count * 12
        objects.append(playlists)
        register_objects.append(register_playlists)
    return MovieObjects(objects, indirect, register_objects)


def navigation_grades(
    entries: list[NavigationEntry], objects: MovieObjects, playlists: set[int],
) -> tuple[dict[int, list[str]], list[str]]:
    roles: dict[int, set[str]] = {}
    for entry in entries:
        if entry.kind != "HDMV":
            raise ValueError("HDMV analysis requires an HDMV-only disc.")
        if entry.reference >= len(objects.playlists):
            raise ValueError("Index references a missing HDMV Movie Object.")
        roles.setdefault(entry.reference, set()).add(entry.role)
    referenced: dict[int, set[str]] = {}
    for index, numbers in enumerate(objects.playlists):
        register_numbers: set[int] = (
            objects.register_playlists[index]
            if objects.register_playlists
            else set()
        )
        for number in numbers | register_numbers:
            referenced.setdefault(number, set()).update(
                roles.get(index, {"other"})
            )
    warnings = [NAVIGATION_LIMITATION]
    missing = sorted(referenced.keys() - playlists)
    if missing:
        warnings.append(
            "HDMV references missing playlists: "
            + ", ".join(f"{n:05}" for n in missing)
        )
    if objects.indirect_playback_commands:
        warnings.append(
            f"{objects.indirect_playback_commands} register-dependent HDMV"
            " playback command(s) could not be assigned to playlists."
        )
    grades = {
        number: sorted(referenced.get(number, {"unreferenced"}))
        for number in sorted(playlists)
    }
    return grades, warnings


def analyze(
    source: Path, *, include_pairwise: bool = True,
) -> tuple[Inventory, dict[int, list[str]], list[str]]:
    entries = parse_navigation_entries(
        read_disc_metadata(source, INDEX_PATH, MAX_INDEX_BYTES)
    )
    objects = parse_movie_objects(
        read_disc_metadata(source, MOVIE_OBJECT_PATH, MAX_MOVIE_OBJECT_BYTES)
    )
    referenced = {
        number for playlists in objects.playlists for number in playlists
    }
    result = inventory(
        source, hdmv_referenced=referenced, include_pairwise=include_pairwise
    )
    register_referenced = {
        number
        for playlists in objects.register_playlists
        for number in playlists
    }
    for group in result["ranking"]:
        numbers = sorted(register_referenced.intersection(group["playlists"]))
        if numbers:
            group["reasons"].append(
                "Local HDMV register assignments indicate playlist(s) "
                + ", ".join(f"{number:05}" for number in numbers)
                + "; this does not establish reachability or the main feature."
            )
    grades, warnings = navigation_grades(
        entries,
        objects,
        {playlist["playlist"] for playlist in result["playlists"]},
    )
    for playlist in result["playlists"]:
        if playlist["duration_45khz"] > unique_clip_duration(
            playlist["clips"]
        ):
            grades[playlist["playlist"]].append("repeated")
    # Keep register-derived hints from changing direct-reference ordering.
    title_referenced = {
        number
        for entry in entries if entry.kind == "HDMV" and entry.role == "title"
        for number in objects.playlists[entry.reference]
    }
    result["ranking"].sort(key=lambda group: not any(
        number in title_referenced for number in group["playlists"]
    ))
    return result, grades, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--gui-analysis",
        action="store_true",
        help="Print playlist grades for Inspect",
    )
    output.add_argument(
        "--report", action="store_true", help="Print an advisory text report"
    )
    parser.add_argument(
        "source", type=Path, help="Blu-ray ISO image or mounted disc root"
    )
    args = parser.parse_args()
    try:
        result, grades, warnings = analyze(
            args.source, include_pairwise=not args.gui_analysis
        )
        if args.gui_analysis:
            labels = authoring_labels(args.source)
            data: object = {
                "groups": [rank["playlists"] for rank in result["ranking"]],
                "labels": {
                    str(number): report_text(label)
                    for number, label in (labels or {}).items()
                },
                "navigation": {
                    str(number): values for number, values in grades.items()
                },
                "warnings": warnings,
            }
        elif args.report:
            print(format_analysis(result, authoring_labels(args.source)))
            print("\nHDMV navigation references (unverified)")
            for number, values in grades.items():
                print(f"  {number:05}: {', '.join(values)}")
            print("\n" + "\n".join(warnings))
            return 0
        else:
            data = {
                **result,
                "navigation": grades,
                "navigation_limitations": warnings,
            }
        json.dump(data, sys.stdout)
        print()
    except (OSError, UnicodeError, ValueError) as error:
        print(f"HDMV analysis failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
