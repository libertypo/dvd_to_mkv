"""Experimental read-only Blu-ray playlist inventory, grouping and ranking."""

# JSON schema violations are ValueError, matching the existing Blu-ray helper.
# ruff: noqa: TRY004

import argparse
import hashlib
import json
import re
import subprocess
import sys
from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from dvdvideo.gui_source import find_entry
from dvdvideo.gui_support import ManagedProcess

MAX_PLAYLIST_BYTES = 16 * 1024 * 1024
MAX_INVENTORY_BYTES = 64 * 1024 * 1024
MAX_ANALYSIS_OUTPUT_BYTES = 8 * 1024 * 1024
PLAYLIST_PATH = re.compile(r"BDMV/PLAYLIST/([0-9]{5})\.mpls\Z", re.IGNORECASE)
AUTHORING_INFO_PATH = "BDMV/JAR/10000/PLsInfo.txt"
MAX_AUTHORING_INFO_BYTES = 1024 * 1024


class Clip(TypedDict):
    clip: str
    in_45khz: int
    out_45khz: int


class SubPlayItem(TypedDict):
    clips: list[str]
    in_45khz: int
    out_45khz: int


class Subpath(TypedDict):
    source: str
    type: int
    type_name: str
    repeat: bool
    items: list[SubPlayItem]


class Playlist(TypedDict):
    playlist: int
    mpls_version: str
    clips: list[Clip]
    duration_45khz: int
    subpath_count: int
    subpaths: list[Subpath]
    clip_timing_sha256: str
    mpls_sha256: str
    bluray_info: NotRequired[list[dict[str, object]]]


class Inventory(TypedDict):
    source: str
    disc: object
    playlists: list[Playlist]
    groups: list['PlaylistGroup']
    variant_differences: list['VariantDifferences']
    ranking: list['RankedGroup']
    similarities: list['PlaylistSimilarity']
    related_candidates: list['RelatedPlaylist']
    similarity_threshold_percent: int
    related_candidate_threshold_percent: int
    unmatched_bluray_info: list[dict[str, object]]
    limitations: list[str]
    selected_playlists: NotRequired[list[Playlist]]


class PlaylistGroup(TypedDict):
    variants: list[Playlist]


class VariantDifference(TypedDict):
    field: str
    values: dict[str, object]


class VariantDifferences(TypedDict):
    playlists: list[int]
    shared_stereoscopic_subpaths: list[Subpath] | None
    differences: list[VariantDifference]


class RankingSignals(TypedDict):
    duration_45khz: int
    unique_duration_45khz: int
    unique_fraction: float | None
    contains_main_hint: bool
    score_without_main_hint: float
    hdmv_fixed_references: dict[str, bool | None]


class RankedGroup(TypedDict):
    playlists: list[int]
    representative_playlist: int
    score: float
    signals: RankingSignals
    reasons: list[str]
    uncertainty: list[str]


class PlaylistSimilarity(TypedDict):
    playlists: tuple[int, int]
    shared_duration_45khz: int
    playlist_durations_45khz: tuple[int, int]
    same_base_sequence: bool


class RelatedPlaylist(TypedDict):
    playlists: tuple[int, int]
    group_representatives: tuple[int, int]
    shared_duration_45khz: int
    playlist_durations_45khz: tuple[int, int]
    shared_percentages: tuple[float, float]
    same_base_sequence: bool


RELATED_CANDIDATE_THRESHOLD_PERCENT = 60
SUBPATH_TYPE_NAMES = {
    3: 'interactive_graphics_menu',
    8: 'stereoscopic_video',
}


def _read_uint(
    data: bytes, offset: int, width: int, end: int, playlist: int
) -> int:
    if offset < 0 or width < 1 or offset + width > end:
        raise ValueError(f'{playlist:05}: truncated MPLS structure')
    return int.from_bytes(data[offset:offset + width], 'big')


def _parse_subplay_item(
    data: bytes, start: int, end: int, playlist: int
) -> SubPlayItem:
    length = _read_uint(data, start, 2, end, playlist)
    item_start = start + 2
    item_end = item_start + length
    if length < 28 or item_end > end:
        raise ValueError(f'{playlist:05}: invalid sub-play-item length')
    clip_id = data[item_start:item_start + 5]
    if (
        not re.fullmatch(rb'[0-9]{5}', clip_id)
        or data[item_start + 5 : item_start + 9] != b'M2TS'
    ):
        raise ValueError(
            f'{playlist:05}: unsupported sub-play-item clip reference'
        )
    in_time = _read_uint(data, item_start + 14, 4, item_end, playlist)
    out_time = _read_uint(data, item_start + 18, 4, item_end, playlist)
    if out_time < in_time:
        raise ValueError(f'{playlist:05}: sub-play-item ends before it starts')
    clips = [clip_id.decode('ascii')]
    if data[item_start + 12] & 1:
        clip_count = _read_uint(data, item_start + 28, 1, item_end, playlist)
        if (
            clip_count < 1
            or item_start + 29 + (clip_count - 1) * 10 > item_end
        ):
            raise ValueError(
                f'{playlist:05}: invalid multi-clip sub-play-item'
            )
        for index in range(clip_count - 1):
            extra_start = item_start + 29 + index * 10
            extra_clip = data[extra_start:extra_start + 5]
            if (
                not re.fullmatch(rb'[0-9]{5}', extra_clip)
                or data[extra_start + 5 : extra_start + 9] != b'M2TS'
            ):
                raise ValueError(
                    f'{playlist:05}: unsupported multi-clip reference'
                )
            clips.append(extra_clip.decode('ascii'))
    return {'clips': clips, 'in_45khz': in_time, 'out_45khz': out_time}


def _parse_subpath_records(
    data: bytes, start: int, end: int, count: int, playlist: int, source: str,
) -> list[Subpath]:
    result: list[Subpath] = []
    pos = start
    for _ in range(count):
        length = _read_uint(data, pos, 4, end, playlist)
        body_start = pos + 4
        body_end = body_start + length
        if length < 6 or body_end > end:
            raise ValueError(f'{playlist:05}: invalid subpath length')
        subpath_type = data[body_start + 1]
        repeat = bool(data[body_start + 3] & 1)
        item_count = data[body_start + 5]
        item_pos = body_start + 6
        items: list[SubPlayItem] = []
        for _ in range(item_count):
            item = _parse_subplay_item(data, item_pos, body_end, playlist)
            items.append(item)
            item_pos += 2 + _read_uint(data, item_pos, 2, body_end, playlist)
        result.append(
            {
                'source': source,
                'type': subpath_type,
                'type_name': SUBPATH_TYPE_NAMES.get(
                    subpath_type, f'unknown_{subpath_type}'
                ),
                'repeat': repeat,
                'items': items,
            }
        )
        pos = body_end
    return result


def _parse_subpath_extension(
    data: bytes, start: int, entry_end: int, playlist: int,
) -> list[Subpath]:
    length = _read_uint(data, start, 4, entry_end, playlist)
    block_end = start + 4 + length
    if block_end > entry_end:
        raise ValueError(
            f'{playlist:05}: subpath extension exceeds its MPLS entry'
        )
    count = _read_uint(data, start + 4, 2, block_end, playlist)
    return _parse_subpath_records(
        data, start + 6, block_end, count, playlist, 'extension_2_2',
    )


def _parse_extensions(data: bytes, playlist: int) -> list[Subpath]:
    extension_start = _read_uint(data, 16, 4, len(data), playlist)
    if extension_start == 0:
        return []
    length = _read_uint(data, extension_start, 4, len(data), playlist)
    extension_end = extension_start + 4 + length
    if extension_end > len(data):
        raise ValueError(f'{playlist:05}: extension section exceeds MPLS file')
    entry_count = _read_uint(
        data, extension_start + 11, 1, extension_end, playlist
    )
    entries_end = extension_start + 12 + entry_count * 12
    if entries_end > extension_end:
        raise ValueError(f'{playlist:05}: truncated extension entry table')
    entries: list[tuple[int, int, int, int]] = []
    for index in range(entry_count):
        entry = extension_start + 12 + index * 12
        entry_type = _read_uint(data, entry, 2, entries_end, playlist)
        entry_version = _read_uint(data, entry + 2, 2, entries_end, playlist)
        entry_start = _read_uint(data, entry + 4, 4, entries_end, playlist)
        entry_length = _read_uint(data, entry + 8, 4, entries_end, playlist)
        absolute_start = extension_start + entry_start
        absolute_end = absolute_start + entry_length
        if absolute_start < entries_end or absolute_end > extension_end:
            raise ValueError(f'{playlist:05}: invalid extension entry bounds')
        entries.append(
            (entry_type, entry_version, absolute_start, absolute_end)
        )
    if any(
        previous[3] > current[2]
        for previous, current in pairwise(
            sorted(entries, key=lambda entry: entry[2])
        )
    ):
        raise ValueError(f'{playlist:05}: overlapping MPLS extension entries')
    subpaths: list[Subpath] = []
    for entry_type, entry_version, absolute_start, absolute_end in entries:
        if (entry_type, entry_version) == (2, 2):
            subpaths.extend(
                _parse_subpath_extension(
                    data, absolute_start, absolute_end, playlist
                )
            )
    return subpaths


def _metadata_tracks(metadata: dict[str, object] | None, key: str) -> object:
    if metadata is None:
        return None
    tracks = metadata.get(key)
    if not isinstance(tracks, list):
        return tracks
    scalar_tracks: list[dict[str, object]] = []
    for track in cast(list[object], tracks):
        if isinstance(track, dict):
            track_data = cast(dict[str, object], track)
            scalar_tracks.append({
                name: value for name, value in track_data.items()
                if value is None or isinstance(value, (str, int, float, bool))
            })
        else:
            scalar_tracks.append({'value': track})
    return scalar_tracks


def variant_differences(
    groups: list[PlaylistGroup],
) -> list[VariantDifferences]:
    reports: list[VariantDifferences] = []
    for group in groups:
        variants = group['variants']
        if len(variants) < 2:
            continue
        values_by_field: dict[str, dict[str, object]] = {
            'base_play_items': {},
            'duration_45khz': {},
            'playlist_subpaths': {},
            'stereoscopic_subpaths': {},
            'chapters': {},
            'angles': {},
            'video_tracks': {},
            'audio_tracks': {},
            'subtitle_tracks': {},
        }
        for variant in variants:
            playlist = variant['playlist']
            metadata_records = variant.get('bluray_info', [])
            metadata = metadata_records[0] if metadata_records else None
            values_by_field['base_play_items'][str(playlist)] = variant[
                'clips'
            ]
            values_by_field['duration_45khz'][str(playlist)] = variant[
                'duration_45khz'
            ]
            values_by_field['playlist_subpaths'][str(playlist)] = [
                subpath
                for subpath in variant['subpaths']
                if subpath['source'] == 'playlist'
            ]
            values_by_field['stereoscopic_subpaths'][str(playlist)] = [
                subpath
                for subpath in variant['subpaths']
                if subpath['type'] == 8
            ]
            chapters = (
                metadata.get('chapters') if metadata is not None else None
            )
            if isinstance(chapters, list):
                chapter_markers: list[dict[str, object]] = []
                for chapter in cast(list[object], chapters):
                    if isinstance(chapter, dict):
                        chapter_data = cast(dict[str, object], chapter)
                        chapter_markers.append(
                            {
                                key: chapter_data[key]
                                for key in (
                                    'chapter',
                                    'start',
                                    'start time',
                                    'length',
                                )
                                if key in chapter_data
                            }
                        )
                    else:
                        chapter_markers.append({'value': chapter})
                values_by_field['chapters'][str(playlist)] = chapter_markers
            else:
                values_by_field['chapters'][str(playlist)] = None
            values_by_field['angles'][str(playlist)] = (
                metadata.get('angles') if metadata else None
            )
            for key, metadata_key in (
                ('video_tracks', 'video'),
                ('audio_tracks', 'audio'),
                ('subtitle_tracks', 'subtitles'),
            ):
                values_by_field[key][str(playlist)] = _metadata_tracks(
                    metadata, metadata_key
                )
        stereo_values = [
            [
                subpath for subpath in variant['subpaths']
                if subpath['type'] == 8
            ]
            for variant in variants
        ]
        shared_stereoscopic_subpaths = (
            stereo_values[0]
            if all(value == stereo_values[0] for value in stereo_values[1:])
            else None
        )
        differences: list[VariantDifference] = []
        for field, values in values_by_field.items():
            if (
                len(
                    {
                        json.dumps(value, sort_keys=True)
                        for value in values.values()
                    }
                )
                > 1
            ):
                differences.append({'field': field, 'values': values})
        if differences or shared_stereoscopic_subpaths:
            reports.append(
                {
                    'playlists': sorted(
                        variant['playlist'] for variant in variants
                    ),
                    'shared_stereoscopic_subpaths': (
                        shared_stereoscopic_subpaths
                    ),
                    'differences': differences,
                }
            )
    return reports


def unique_clip_duration(clips: list[Clip]) -> int:
    """Count referenced ranges once per clip, including partial overlaps."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    for clip in clips:
        ranges.setdefault(clip['clip'], []).append(
            (clip['in_45khz'], clip['out_45khz'])
        )
    total = 0
    for intervals in ranges.values():
        ordered = sorted(intervals)
        start, end = ordered[0]
        for next_start, next_end in ordered[1:]:
            if next_start <= end:
                end = max(end, next_end)
            else:
                total += end - start
                start, end = next_start, next_end
        total += end - start
    return total


def _count_between(values: list[int], lower: int, upper: int) -> int:
    return (
        bisect_right(values, upper) - bisect_left(values, lower)
        if lower <= upper
        else 0
    )


def rank_groups(
    groups: list[PlaylistGroup],
    main_hint: int | None,
    hdmv_referenced: set[int] | None = None,
    differences: list[VariantDifferences] | None = None,
) -> list[RankedGroup]:
    if differences is None:
        differences = variant_differences(groups)
    differences_by_group = {
        tuple(report['playlists']): report for report in differences
    }
    representatives: list[tuple[PlaylistGroup, Playlist, int]] = []
    for group in groups:
        if not group['variants']:
            raise ValueError('Cannot rank a group without playlists')
        measured = [
            (entry, unique_clip_duration(entry['clips']))
            for entry in group['variants']
        ]
        representative, unique = max(
            measured, key=lambda item: (item[1], -item[0]['playlist'])
        )
        representatives.append((group, representative, unique))
    max_unique = max((unique for _, _, unique in representatives), default=0)
    ranked: list[RankedGroup] = []
    for group, representative, unique in representatives:
        numbers = sorted(entry['playlist'] for entry in group['variants'])
        duration = representative['duration_45khz']
        fraction = unique / duration if duration else None
        contains_hint = main_hint is not None and main_hint in numbers
        duration_points = 70 * unique / max_unique if max_unique else 0
        fraction_points = 20 * fraction if fraction is not None else 0
        score_without_hint = round(duration_points + fraction_points, 2)
        score = round(score_without_hint + (10 if contains_hint else 0), 2)
        reasons = [
            (
                f'Playlist {representative["playlist"]:05} has {unique}'
                f' distinct base-clip ticks out of {duration} total (45 kHz).'
            ),
            (
                f'Distinct duration contributes {duration_points:.2f} of 70'
                ' points relative to this disc; the distinct fraction'
                f' contributes {fraction_points:.2f} of 20.'
            ),
        ]
        if contains_hint:
            reasons.append(
                f'Group includes libbluray main-playlist hint {main_hint:05}'
                ' (+10 points).'
            )
        uncertainty = [
            'Experimental score is not a probability or proof of the correct'
            ' feature.'
        ]
        if len(numbers) > 1:
            uncertainty.append(
                'Variants may differ; the score does not choose between them.'
            )
        if fraction is None:
            uncertainty.append(
                'The representative has no positive base-playlist duration.'
            )
        for variant in group['variants']:
            variant_duration = variant['duration_45khz']
            variant_unique = unique_clip_duration(variant['clips'])
            if variant_duration > 0 and variant_unique < variant_duration:
                uncertainty.append(
                    f'Playlist {variant["playlist"]:05} contains repeated clip'
                    ' ranges (distinct fraction:'
                    f' {variant_unique / variant_duration:.4f}); this may be'
                    ' intentional or indicate a decoy.'
                )
        report = differences_by_group.get(tuple(numbers))
        differing_fields: list[str] = []
        if report is not None:
            for difference in report['differences']:
                field = difference['field']
                if field not in (
                    'chapters',
                    'video_tracks',
                    'audio_tracks',
                    'subtitle_tracks',
                ):
                    continue
                known = [
                    cast(list[object], value)
                    for value in difference['values'].values()
                    if isinstance(value, list)
                ]
                values = [
                    len(value) if field == 'chapters' else value
                    for value in known
                ]
                if (
                    len(
                        {json.dumps(value, sort_keys=True) for value in values}
                    )
                    > 1
                ):
                    differing_fields.append(field.replace('_', ' '))
        if differing_fields:
            uncertainty.append(
                'Variants differ in ' + ', '.join(differing_fields)
                + '; inspect their metadata before choosing.'
            )
        hdmv_references = {
            str(number): (
                number in hdmv_referenced
                if hdmv_referenced is not None
                else None
            )
            for number in numbers
        }
        if hdmv_referenced is not None:
            referenced = [
                number for number in numbers if hdmv_references[str(number)]
            ]
            unreferenced = [
                number
                for number in numbers
                if not hdmv_references[str(number)]
            ]
            if referenced:
                reasons.append(
                    'Fixed HDMV navigation references playlist(s) '
                    + ', '.join(f'{number:05}' for number in referenced)
                    + '; this does not establish reachability or the main'
                    ' feature.'
                )
            if unreferenced:
                uncertainty.append(
                    'No fixed HDMV Movie Object reference found for'
                    ' playlist(s) '
                    + ', '.join(f'{number:05}' for number in unreferenced)
                    + '; indirect commands and other navigation may still'
                    ' reference them.'
                )
        ranked.append(
            {
                'playlists': numbers,
                'representative_playlist': representative['playlist'],
                'score': score,
                'signals': {
                    'duration_45khz': duration,
                    'unique_duration_45khz': unique,
                    'unique_fraction': (
                        round(fraction, 4) if fraction is not None else None
                    ),
                    'contains_main_hint': contains_hint,
                    'score_without_main_hint': score_without_hint,
                    'hdmv_fixed_references': hdmv_references,
                },
                'reasons': reasons,
                'uncertainty': uncertainty,
            }
        )
    ranked.sort(key=lambda entry: (-entry['score'], entry['playlists'][0]))
    scores = sorted(round(entry['score'] * 100) for entry in ranked)
    base_scores = {
        hinted: sorted(
            round(entry['signals']['score_without_main_hint'] * 100)
            for entry in ranked
            if entry['signals']['contains_main_hint'] == hinted
        )
        for hinted in (False, True)
    }
    for entry in ranked:
        # Scores are rounded to hundredths; compare those units exactly.
        score = round(entry['score'] * 100)
        base = round(entry['signals']['score_without_main_hint'] * 100)
        opposite = base_scores[not entry['signals']['contains_main_hint']]
        bonus = 0 if entry['signals']['contains_main_hint'] else 1000
        hint_comparisons = _count_between(opposite, base - 500, base + 500)
        overlapping = _count_between(
            opposite,
            max(base - 500, score - 500 - bonus),
            min(base + 500, score + 500 - bonus),
        )
        nearby_comparisons = (
            _count_between(scores, score - 500, score + 500) - 1 - overlapping
        )
        if hint_comparisons:
            entry['uncertainty'].append(
                f'{hint_comparisons} other group(s) have a similar score'
                ' without the main-playlist hint; the hint materially affects'
                ' these comparisons.'
            )
        if nearby_comparisons:
            entry['uncertainty'].append(
                f'{nearby_comparisons} other group(s) are within 5 score'
                ' points; the ordering is inconclusive.'
            )
        entry['uncertainty'] = list(dict.fromkeys(entry['uncertainty']))
    return ranked


def shared_ordered_duration(first: list[Clip], second: list[Clip]) -> int:
    """Find greatest one-to-one ordered overlap of base play-item ranges."""
    if first == second:
        return sum(
            max(0, clip['out_45khz'] - clip['in_45khz']) for clip in first
        )
    if len(second) > len(first):
        first, second = second, first
    previous = [0] * (len(second) + 1)
    for left in first:
        current = [0]
        for index, right in enumerate(second, 1):
            overlap = 0
            if left['clip'] == right['clip']:
                overlap = max(
                    0, min(left['out_45khz'], right['out_45khz'])
                    - max(left['in_45khz'], right['in_45khz']),
                )
            current.append(
                max(
                    previous[index],
                    current[index - 1],
                    previous[index - 1] + overlap,
                )
            )
        previous = current
    return previous[-1]


def group_playlists(
    playlists: list[Playlist],
    threshold_percent: int,
    *,
    include_pairwise: bool = True,
) -> tuple[
    list[PlaylistGroup], list[PlaylistSimilarity], list[RelatedPlaylist]
]:
    if not 1 <= threshold_percent <= 100:
        raise ValueError(
            'Similarity threshold must be between 1 and 100 percent'
        )
    ordered = sorted(playlists, key=lambda entry: entry['playlist'])
    comparison_threshold = min(
        threshold_percent, RELATED_CANDIDATE_THRESHOLD_PERCENT
    )
    if len({entry['playlist'] for entry in ordered}) != len(ordered):
        raise ValueError('Duplicate playlist numbers cannot be grouped')

    clip_ids = {
        entry['playlist']: {clip['clip'] for clip in entry['clips']}
        for entry in ordered
    }

    def overlap(first: Playlist, second: Playlist) -> int:
        first_duration, second_duration = (
            first['duration_45khz'],
            second['duration_45khz'],
        )
        if (
            first_duration <= 0
            or second_duration <= 0
            or min(first_duration, second_duration) * 100
            < max(first_duration, second_duration) * comparison_threshold
            or clip_ids[first['playlist']].isdisjoint(
                clip_ids[second['playlist']]
            )
        ):
            return 0
        return shared_ordered_duration(first['clips'], second['clips'])

    if not include_pairwise:
        def compatible(first: Playlist, second: Playlist) -> bool:
            shared = overlap(first, second)
            return (
                shared > 0
                and shared * 100 >= first['duration_45khz'] * threshold_percent
                and shared * 100
                >= second['duration_45khz'] * threshold_percent
            )

        groups: list[PlaylistGroup] = []
        representatives: list[
            dict[tuple[int, tuple[tuple[str, int, int], ...]], Playlist]
        ] = []
        for entry in ordered:
            key = (
                entry['duration_45khz'],
                tuple(
                    (clip['clip'], clip['in_45khz'], clip['out_45khz'])
                    for clip in entry['clips']
                ),
            )
            for group, variants in zip(groups, representatives, strict=True):
                # Equal timing sequences overlap every group member equally.
                references = (
                    [variants[key]] if key in variants else variants.values()
                )
                if all(compatible(entry, variant) for variant in references):
                    group['variants'].append(entry)
                    variants.setdefault(key, entry)
                    break
            else:
                groups.append({'variants': [entry]})
                representatives.append({key: entry})
        return groups, [], []

    links: dict[tuple[int, int], PlaylistSimilarity] = {}
    related: dict[
        tuple[int, int],
        tuple[int, tuple[int, int], tuple[float, float], bool],
    ] = {}
    for index, first in enumerate(ordered):
        first_duration = first['duration_45khz']
        if first_duration <= 0:
            continue
        for second in ordered[index + 1:]:
            second_duration = second['duration_45khz']
            shared = overlap(first, second)
            if not shared:
                continue
            pair = (first['playlist'], second['playlist'])
            reaches_group_threshold = (
                shared * 100 >= first_duration * threshold_percent
                and shared * 100 >= second_duration * threshold_percent
            )
            reaches_related_threshold = (
                shared * 100
                >= first_duration * RELATED_CANDIDATE_THRESHOLD_PERCENT
                and shared * 100
                >= second_duration * RELATED_CANDIDATE_THRESHOLD_PERCENT
            )
            if reaches_group_threshold:
                links[pair] = {
                    'playlists': pair,
                    'shared_duration_45khz': shared,
                    'playlist_durations_45khz': (
                        first_duration,
                        second_duration,
                    ),
                    'same_base_sequence': first['clips'] == second['clips'],
                }
            elif reaches_related_threshold:
                related[pair] = (
                    shared,
                    (first_duration, second_duration),
                    (
                        round(100 * shared / first_duration, 1),
                        round(100 * shared / second_duration, 1),
                    ),
                    first['clips'] == second['clips'],
                )

    groups = []
    for entry in ordered:
        number = entry['playlist']
        for group in groups:
            if all(
                (variant['playlist'], number) in links
                for variant in group['variants']
            ):
                group['variants'].append(entry)
                break
        else:
            groups.append({'variants': [entry]})
    group_by_playlist = {
        variant['playlist']: min(
            item['playlist'] for item in group['variants']
        )
        for group in groups
        for variant in group['variants']
    }
    related_candidates: list[RelatedPlaylist] = []
    for pair, (
        shared,
        durations,
        percentages,
        same_sequence,
    ) in related.items():
        first, second = pair
        related_candidates.append(
            {
                'playlists': pair,
                'group_representatives': (
                    group_by_playlist[first],
                    group_by_playlist[second],
                ),
                'shared_duration_45khz': shared,
                'playlist_durations_45khz': durations,
                'shared_percentages': percentages,
                'same_base_sequence': same_sequence,
            }
        )
    return groups, list(links.values()), related_candidates


def parse_playlist(data: bytes, playlist: int) -> Playlist:
    if (
        len(data) < 20
        or data[:4] != b'MPLS'
        or data[4:8] not in (b'0100', b'0200', b'0300')
    ):
        raise ValueError(f'{playlist:05}: unsupported or invalid MPLS header')
    start = int.from_bytes(data[8:12], 'big')
    if start < 20 or start + 10 > len(data):
        raise ValueError(f'{playlist:05}: invalid playlist section offset')
    end = start + 4 + int.from_bytes(data[start:start + 4], 'big')
    if end > len(data) or end < start + 10:
        raise ValueError(f'{playlist:05}: invalid playlist section length')
    count = int.from_bytes(data[start + 6:start + 8], 'big')
    subpaths = int.from_bytes(data[start + 8:start + 10], 'big')
    pos = start + 10
    clips: list[Clip] = []
    for _ in range(count):
        if pos + 2 > end:
            raise ValueError(f'{playlist:05}: truncated play item')
        size = int.from_bytes(data[pos:pos + 2], 'big')
        pos += 2
        if size < 20 or pos + size > end:
            raise ValueError(f'{playlist:05}: invalid play item length')
        clip_id = data[pos:pos + 5]
        if (
            not re.fullmatch(rb'[0-9]{5}', clip_id)
            or data[pos + 5 : pos + 9] != b'M2TS'
        ):
            raise ValueError(f'{playlist:05}: unsupported clip reference')
        in_time = int.from_bytes(data[pos + 12:pos + 16], 'big')
        out_time = int.from_bytes(data[pos + 16:pos + 20], 'big')
        if out_time < in_time:
            raise ValueError(f'{playlist:05}: play item ends before it starts')
        clips.append(
            {
                'clip': clip_id.decode('ascii'),
                'in_45khz': in_time,
                'out_45khz': out_time,
            }
        )
        pos += size
    playlist_subpaths = _parse_subpath_records(
        data, pos, end, subpaths, playlist, 'playlist',
    )
    extended_subpaths = _parse_extensions(data, playlist)
    encoded = json.dumps(clips, sort_keys=True, separators=(',', ':')).encode(
        'ascii'
    )
    return {
        'playlist': playlist,
        'mpls_version': data[4:8].decode('ascii'),
        'clips': clips,
        'duration_45khz': sum(
            clip['out_45khz'] - clip['in_45khz'] for clip in clips
        ),
        'subpath_count': subpaths,
        'subpaths': playlist_subpaths + extended_subpaths,
        'clip_timing_sha256': hashlib.sha256(encoded).hexdigest(),
        'mpls_sha256': hashlib.sha256(data).hexdigest(),
    }


def run_command(
    args: list[str], *, max_output_bytes: int | None = None
) -> bytes:
    try:
        data = bytearray()
        # The enclosing analyzer owns the group, so cancellation reaches 7z.
        managed = ManagedProcess(start_new_session=False).run(
            args,
            timeout=60,
            stdout_bytes=data.extend,
            max_stdout_bytes=(
                MAX_ANALYSIS_OUTPUT_BYTES
                if max_output_bytes is None
                else max_output_bytes
            ),
            abandon_unresponsive=True,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise ValueError(f'{args[0]} failed: {error}') from error
    if managed.returncode:
        raise ValueError(
            f'{args[0]} failed (exit {managed.returncode}):'
            f' {managed.stderr.strip()}'
        )
    return bytes(data)


def image_metadata_records(
    source: Path, pattern: str, limit: int
) -> list[tuple[str, int]]:
    listing = run_command(
        ['7z', 'l', '-ssc-', '-slt', '-ba', '--', str(source), pattern]
    )
    records: list[tuple[str, int]] = []
    for block in listing.decode('utf-8', errors='strict').split('\n\n'):
        fields = dict(
            line.split(' = ', 1)
            for line in block.splitlines()
            if ' = ' in line
        )
        path = fields.get('Path', '')
        if not path:
            continue
        try:
            size = int(fields['Size'])
        except (KeyError, ValueError) as error:
            raise ValueError(f'Invalid metadata size for {path}') from error
        if not 0 <= size <= limit:
            raise ValueError(f'Metadata {path} exceeds the supported size')
        records.append((path, size))
    return records


def read_image_metadata(source: Path, path: str, size: int) -> bytes:
    data = run_command(
        ['7z', 'e', '-ssc', '-so', '--', str(source), path],
        max_output_bytes=size,
    )
    if len(data) != size:
        raise ValueError(
            f'Metadata {path} size does not match the image listing'
        )
    return data


def _iter_image_playlists(source: Path) -> Iterator[tuple[int, bytes]]:
    records: list[tuple[int, str, int]] = []
    for path, size in image_metadata_records(
        source, 'BDMV/PLAYLIST/*.mpls', MAX_PLAYLIST_BYTES
    ):
        match = PLAYLIST_PATH.fullmatch(path)
        if match is None:
            raise ValueError(f'Unexpected playlist path in image: {path!r}')
        records.append((int(match.group(1)), path, size))
    if len({number for number, _, _ in records}) != len(records):
        raise ValueError('Image contains duplicate playlist numbers')
    if sum(size for _, _, size in records) > MAX_INVENTORY_BYTES:
        raise ValueError(
            f'Playlist metadata exceeds the {MAX_INVENTORY_BYTES}-byte'
            ' aggregate limit.'
        )
    for number, path, size in sorted(records):
        yield number, read_image_metadata(source, path, size)


def image_playlists(source: Path) -> list[tuple[int, bytes]]:
    return list(_iter_image_playlists(source))


def normalize_bluray_source(source: Path) -> Path:
    return (
        source.parent
        if source.is_dir() and source.name.casefold() == 'bdmv'
        else source
    )


def disc_metadata_path(source: Path, relative_path: str) -> Path:
    path = normalize_bluray_source(source)
    components = relative_path.split('/')
    for index, component in enumerate(components):
        try:
            match = find_entry(
                str(path), component.upper(), lambda _path: True
            )
        except RuntimeError as error:
            raise ValueError(str(error)) from error
        if match is None:
            return path.joinpath(*components[index:])
        path = Path(match)
    return path


def _iter_directory_playlists(source: Path) -> Iterator[tuple[int, bytes]]:
    directory = disc_metadata_path(source, 'BDMV/PLAYLIST')
    if not directory.is_dir():
        raise ValueError(f'Playlist directory not found: {directory}')
    records: list[tuple[int, Path]] = []
    seen: set[int] = set()
    declared_total = 0
    for path in sorted(directory.iterdir()):
        if not re.fullmatch(r'[0-9]{5}\.mpls', path.name, re.IGNORECASE):
            continue
        number = int(path.stem)
        if number in seen:
            raise ValueError('Directory contains duplicate playlist numbers')
        seen.add(number)
        if not path.is_file():
            raise ValueError(f'Invalid or oversized playlist: {path}')
        size = path.stat().st_size
        if size > MAX_PLAYLIST_BYTES:
            raise ValueError(f'Invalid or oversized playlist: {path}')
        declared_total += size
        if declared_total > MAX_INVENTORY_BYTES:
            raise ValueError(
                f'Playlist metadata exceeds the {MAX_INVENTORY_BYTES}-byte'
                ' aggregate limit.'
            )
        records.append((number, path))
    total = 0
    for number, path in records:
        with path.open('rb') as stream:
            data = stream.read(
                min(MAX_PLAYLIST_BYTES, MAX_INVENTORY_BYTES - total) + 1
            )
        if len(data) > MAX_PLAYLIST_BYTES:
            raise ValueError(f'Oversized playlist: {path}')
        total += len(data)
        if total > MAX_INVENTORY_BYTES:
            raise ValueError(
                f'Playlist metadata exceeds the {MAX_INVENTORY_BYTES}-byte'
                ' aggregate limit.'
            )
        yield number, data
        del data


def directory_playlists(source: Path) -> list[tuple[int, bytes]]:
    return list(_iter_directory_playlists(source))


def inventory(
    source: Path,
    threshold_percent: int = 90,
    *,
    hdmv_referenced: set[int] | None = None,
    include_pairwise: bool = True,
) -> Inventory:
    source = normalize_bluray_source(source)
    if source.is_dir():
        playlists = _iter_directory_playlists(source)
    elif source.is_file() or source.is_block_device():
        playlists = _iter_image_playlists(source)
    else:
        raise ValueError(
            'Source is not a disc directory, image file or block device:'
            f' {source}'
        )
    parsed: list[Playlist] = []
    for number, data in playlists:
        parsed.append(parse_playlist(data, number))
        del data
    if not parsed:
        raise ValueError('No MPLS playlists found')
    decoded: object = json.loads(
        run_command(['bluray_info', str(source), '--all', '--json'])
    )
    if not isinstance(decoded, dict):
        raise ValueError('bluray_info returned invalid title metadata')
    metadata = cast(dict[str, object], decoded)
    title_values = metadata.get('titles')
    if not isinstance(title_values, list):
        raise ValueError('bluray_info returned invalid title metadata')
    by_playlist: dict[int, list[dict[str, object]]] = {}
    for value in cast(list[object], title_values):
        if not isinstance(value, dict):
            raise ValueError('bluray_info returned an invalid playlist record')
        title = cast(dict[str, object], value)
        number = title.get('playlist')
        if not isinstance(number, int) or isinstance(number, bool):
            raise ValueError('bluray_info returned an invalid playlist record')
        by_playlist.setdefault(number, []).append(title)
    for entry in parsed:
        entry['bluray_info'] = by_playlist.pop(entry['playlist'], [])
    groups, similarities, related_candidates = group_playlists(
        parsed, threshold_percent, include_pairwise=include_pairwise,
    )
    differences = variant_differences(groups)
    disc = metadata.get('bluray')
    hint = (
        cast(dict[str, object], disc).get('main playlist')
        if isinstance(disc, dict)
        else None
    )
    main_hint = (
        hint if isinstance(hint, int) and not isinstance(hint, bool) else None
    )
    if main_hint is None:
        hint_limitation = (
            'The main-playlist hint is unavailable or invalid; no hint bonus'
            ' was used.'
        )
    elif not any(entry['playlist'] == main_hint for entry in parsed):
        hint_limitation = (
            'The main-playlist hint is not in the inventory; no hint bonus was'
            ' used.'
        )
    else:
        hint_limitation = (
            'The main-playlist hint is advisory and does not establish a'
            ' correct variant.'
        )
    return {
        'source': str(source),
        'disc': disc,
        'playlists': parsed,
        'groups': groups,
        'variant_differences': differences,
        'ranking': rank_groups(
            groups, main_hint, hdmv_referenced, differences
        ),
        'similarities': similarities,
        'related_candidates': related_candidates,
        'similarity_threshold_percent': threshold_percent,
        'related_candidate_threshold_percent': (
            RELATED_CANDIDATE_THRESHOLD_PERCENT
        ),
        'unmatched_bluray_info': [
            title for titles in by_playlist.values() for title in titles
        ],
        'limitations': [
            (
                'Clip timings are from base play items; angle clip references'
                ' and MPLS stream tables are not parsed.'
            ),
            (
                'Playlist subpaths and MPLS extension 2.2 stereoscopic'
                ' subpaths are listed; other extension entries are not parsed.'
            ),
            (
                'Similarity compares ordered base play items one-to-one;'
                ' different segment splits can be missed.'
            ),
            (
                'Groups and fingerprints do not establish playlist equivalence'
                ' or identify the correct title.'
            ),
            (
                'Track and chapter metadata are supplied by bluray_info, not'
                ' parsed from MPLS.'
            ),
            (
                'Ranking is experimental and relative to this disc, not a'
                ' probability or verification of a feature.'
            ),
            (
                'Repeated material can be intentional; short features and'
                ' episode discs may rank poorly.'
            ),
            (
                'Chapter counts, track counts, clip sizes, angles, subpaths'
                ' and 3D are not scored.'
            ),
            hint_limitation,
        ],
    }


def select_playlists(result: Inventory, numbers: list[int]) -> None:
    by_number = {entry['playlist']: entry for entry in result['playlists']}
    missing = sorted(set(numbers) - by_number.keys())
    if missing:
        raise ValueError(
            'Playlists not found:'
            f' {", ".join(f"{number:05}" for number in missing)}'
        )
    result['selected_playlists'] = [
        by_number[number] for number in dict.fromkeys(numbers)
    ]


def authoring_labels(source: Path) -> dict[int, str] | None:
    """Read optional disc-authored labels, not BD-J route metadata."""
    source = normalize_bluray_source(source)
    if source.is_dir():
        try:
            path = disc_metadata_path(source, AUTHORING_INFO_PATH)
            size = path.stat().st_size
        except FileNotFoundError:
            return None
        if not path.is_file() or size > MAX_AUTHORING_INFO_BYTES:
            raise ValueError(
                f'Invalid or oversized authoring metadata: {path}'
            )
        with path.open('rb') as stream:
            data = stream.read(MAX_AUTHORING_INFO_BYTES + 1)
    else:
        records = image_metadata_records(
            source, AUTHORING_INFO_PATH, MAX_AUTHORING_INFO_BYTES
        )
        if not records:
            return None
        if (
            len(records) != 1
            or records[0][0].casefold() != AUTHORING_INFO_PATH.casefold()
        ):
            raise ValueError('Unexpected authoring metadata path in image')
        data = read_image_metadata(source, *records[0])
    if len(data) > MAX_AUTHORING_INFO_BYTES:
        raise ValueError('Authoring metadata exceeds the supported size')
    text = data.decode('iso-8859-1')
    labels: dict[int, str] = {}
    pending: str | None = None
    for line in text.splitlines():
        match = re.fullmatch(r'#\s*Playlist name:\s*(\S.{0,199}|\S)\s*', line)
        if match:
            if pending is not None:
                raise ValueError(
                    'Authoring playlist label has no associated playlist ID'
                )
            pending = match.group(1).strip()
            continue
        if pending is not None:
            match = re.match(r'p([0-9]{1,5})t=', line)
            if match:
                number = int(match.group(1))
                if number in labels:
                    raise ValueError(
                        f'Duplicate authoring label for playlist {number:05}'
                    )
                labels[number] = pending
                pending = None
    if pending is not None:
        raise ValueError(
            'Authoring playlist label has no associated playlist ID'
        )
    return labels


def report_text(value: object) -> str:
    return ''.join(char if char.isprintable() else ' ' for char in str(value))[
        :200
    ]


def _display_duration(ticks: int) -> str:
    seconds = ticks // 45000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}'


def format_analysis(result: Inventory, labels: dict[int, str] | None) -> str:
    by_number = {entry['playlist']: entry for entry in result['playlists']}
    lines = [
        'Playlist analysis',
        '',
        (
            f"Found {len(result['playlists'])} playlists in"
            f" {len(result['ranking'])} groups of similar video."
        ),
        'A playlist is a set of video segments played in order.',
        '',
        'Where to start',
        'The groups below are listed using video length and other clues.',
        (
            'They may include extras or episodes; the first group is not'
            ' necessarily the movie.'
        ),
        (
            'Playlists in the same group may still differ, so check each one'
            ' you are considering.'
        ),
        (
            'Lengths are approximate. Names, when available, come from the'
            ' disc and may be misleading.'
        ),
        '',
    ]
    for index, rank in enumerate(result['ranking'][:5], start=1):
        lines.append(f'Group {index}:')
        for number in rank['playlists']:
            playlist = by_number[number]
            label = (
                f" | disc name: {report_text(labels[number])}"
                if labels is not None and number in labels
                else ''
            )
            lines.append(
                f"  Playlist {number:05} | length about"
                f" {_display_duration(playlist['duration_45khz'])}{label}"
            )
    if len(result['ranking']) > 5:
        lines.append(
            f"  {len(result['ranking']) - 5} more group(s) are listed in the"
            " technical details below."
        )
    lines.extend(
        [
            '',
            'What to do next',
            (
                'Return to the main window and use Inspect if the playlists'
                ' are not listed.'
            ),
            (
                'Select a playlist, then use Inspect Tracks and Preview to'
                ' check it before copying.'
            ),
            (
                'This report cannot tell which playlist the disc menu uses or'
                ' guarantee the correct choice.'
            ),
            (
                'The playlist analysis did not select or copy anything. It'
                ' does not assess 3D.'
            ),
            '',
            'Technical details (all groups)',
            'Experimental 2D playlist analysis (read-only)',
            'Advisory only: no playlist or tracks are selected or copied.',
            (
                'Ranking uses base clips, duration and the libbluray hint; it'
                ' does not score 3D.'
            ),
            (
                'Disc-authored labels are unverified and do not prove menu'
                ' routes or legitimate content.'
            ),
            (
                'BD-J applications are not executed or analyzed; BDJO is not'
                ' parsed.'
            ),
            '',
        ]
    )
    if labels is None:
        lines.append('Disc-authored playlist labels: unavailable.')
    elif not labels:
        lines.append('Disc-authored playlist labels: no recognized entries.')
    else:
        lines.append(
            f'Disc-authored playlist labels: {len(labels)} entries'
            ' (PLsInfo.txt).'
        )
    lines.append(
        f"Playlists: {len(result['playlists'])}; groups:"
        f" {len(result['ranking'])}."
    )
    lines.append('')
    for rank in result['ranking']:
        lines.append(
            f"Score {rank['score']:.2f} | playlists "
            + ', '.join(f'{number:05}' for number in rank['playlists'])
        )
        lines.extend(f'  {reason}' for reason in rank['reasons'])
        lines.extend(
            f'  Warning: {warning}' for warning in rank['uncertainty']
        )
        for number in rank['playlists']:
            playlist = by_number[number]
            label = (
                f' | label: {report_text(labels[number])}'
                if labels is not None and number in labels
                else ''
            )
            clips = ', '.join(
                f"{clip['clip']} ({clip['in_45khz']}-{clip['out_45khz']}"
                " ticks)"
                for clip in playlist['clips']
            )
            lines.append(
                f"  {number:05}{label} | base duration:"
                f" {playlist['duration_45khz'] / 45000:.3f}s | base clips:"
                f" {clips}"
            )
            records = playlist.get('bluray_info', [])
            if not records:
                lines.append('    Track metadata unavailable.')
            for record in records:
                chapters = record.get('chapters')
                lines.append(
                    "    Chapters:"
                    f" {(
                        len(cast(list[object], chapters))
                        if isinstance(chapters, list) else 'unavailable'
                    )}"
                )
                for kind in ('video', 'audio', 'subtitles'):
                    tracks = record.get(kind)
                    if not isinstance(tracks, list):
                        lines.append(f'    {kind}: unavailable')
                        continue
                    rendered: list[str] = []
                    for track in cast(list[object], tracks):
                        if isinstance(track, dict):
                            track_data = cast(dict[str, object], track)
                            rendered.append(
                                ' '.join(
                                    report_text(track_data[key])
                                    for key in (
                                        'stream',
                                        'language',
                                        'codec',
                                        'format',
                                    )
                                    if key in track_data
                                )
                            )
                        else:
                            rendered.append('invalid track metadata')
                    lines.append(
                        f"    {kind}:"
                        f" {(
                            ', '.join(rendered)
                            if rendered else 'none reported'
                        )}"
                    )
            lines.append(f"    MPLS SHA-256: {playlist['mpls_sha256']}")
        lines.append('')
    if result['unmatched_bluray_info']:
        lines.append(
            "Unmatched bluray_info records:"
            f" {len(result['unmatched_bluray_info'])}."
        )
    lines.append(
        'Track data comes from bluray_info; MPLS stream tables and angle clips'
        ' are not parsed.'
    )
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'source', type=Path, help='Blu-ray ISO image or mounted disc root'
    )
    parser.add_argument(
        '--similarity', type=int, default=90, metavar='PERCENT',
        help='Minimum ordered overlap on each playlist (1-100; default: 90)',
    )
    parser.add_argument(
        '--playlist',
        type=int,
        action='append',
        default=[],
        metavar='NUMBER',
        help=(
            'Include an individual playlist in selected_playlists; may be'
            ' repeated'
        ),
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        '--report',
        action='store_true',
        help='Print an advisory 2D-oriented text report',
    )
    output.add_argument(
        '--gui-analysis',
        action='store_true',
        help='Print playlist rankings for GUI inspection',
    )
    args = parser.parse_args()
    try:
        if not 1 <= args.similarity <= 100:
            raise ValueError(
                'Similarity threshold must be between 1 and 100 percent'
            )
        result = inventory(
            args.source,
            args.similarity,
            include_pairwise=not args.gui_analysis,
        )
        if args.playlist:
            select_playlists(result, args.playlist)
        if args.report:
            print(format_analysis(result, authoring_labels(args.source)))
            return 0
        if args.gui_analysis:
            labels = authoring_labels(args.source)
            json.dump(
                {
                    'groups': [
                        rank['playlists'] for rank in result['ranking']
                    ],
                    'labels': {
                        str(number): report_text(label)
                        for number, label in (labels or {}).items()
                    },
                    'repeated_playlists': [
                        playlist['playlist']
                        for playlist in result['playlists']
                        if playlist['duration_45khz']
                        > unique_clip_duration(playlist['clips'])
                    ],
                },
                sys.stdout,
            )
            print()
            return 0
    except (OSError, UnicodeError, ValueError) as error:
        print(f'Blu-ray inventory failed: {error}', file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
