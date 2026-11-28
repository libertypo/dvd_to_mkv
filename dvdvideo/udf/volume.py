"""
Finds and validates UDF volume descriptors, partitions, and file sets within
bounded reads of DVD media.

@copyright: 2009 Bastian Blank <waldi@debian.org>
@license: GNU GPL-3

Modified 2026-11-11 by libertypo: modernized Python 3.14 cached metadata,
corrected reserve extents, and scanned partitions in bounded sector reads.
"""
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations

import logging
import struct
from collections.abc import Callable, Iterator
from functools import cached_property
from typing import TYPE_CHECKING

from ..css import DecryptionError
from .general import (
    DescriptorTag,
    ExtentAD,
    MalformedUdfError,
    UnsupportedUdfError,
)

MAX_FILESET_SCAN_SECTORS = 8192
MAX_VOLUME_SEQUENCE_SECTORS = 8192
logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .filesystem import FileSet
    from .media import Media


class AnchorVolumeDescriptorPointer:
    _struct = struct.Struct('<16x8s8s')

    def __init__(self, tag: DescriptorTag, buf: bytes) -> None:
        self.tag = tag
        if tag.identifier != 2:
            raise MalformedUdfError('Invalid anchor volume descriptor tag')

        if len(buf) < self._struct.size:
            raise MalformedUdfError('Truncated anchor volume descriptor')
        tag.require_crc_length(self._struct.size - 16)
        data = self._struct.unpack(buf[:self._struct.size])
        self.main_descriptor_extent = ExtentAD(data[0])
        self.reserve_descriptor_extent = ExtentAD(data[1])


def _DescriptorSequence(
    media: Media,
    location: int,
    length: int,
    *,
    tag_location: int | None = None,
    require_terminator: bool = False,
    max_sectors: int | None = None,
) -> Iterator[tuple[DescriptorTag, bytes]]:
    media.validate_extent(location, length)
    if not length or length % 2048:
        raise MalformedUdfError(
            'Descriptor sequence length must be whole sectors'
        )
    for cur in range(0, length, 2048):
        if max_sectors is not None and cur // 2048 >= max_sectors:
            raise MalformedUdfError(
                'UDF volume descriptor sequence exceeds the'
                f' {max_sectors}-sector scan limit'
            )
        buf = media.read_sector(location + cur // 2048)
        tag = DescriptorTag(
            buf,
            (location if tag_location is None else tag_location) + cur // 2048,
        )
        if tag.identifier == 8:
            return
        if tag.identifier not in {
            *range(1, 10),
            256,
            257,
            258,
            259,
            260,
            261,
            262,
            263,
            264,
            265,
            266,
        }:
            raise MalformedUdfError(
                f'Invalid UDF descriptor identifier: {tag.identifier}'
            )
        yield tag, buf
    if require_terminator:
        raise MalformedUdfError(
            'UDF volume descriptor sequence has no terminating descriptor'
        )


class Volume:
    def __init__(self, media: Media) -> None:
        self._media = media
        self.recovery_used = False

        try:
            buf = media.read_sector(256)
            self.anchor = AnchorVolumeDescriptorPointer(
                DescriptorTag(buf, 256), buf
            )
            self._anchor_sector = 256
            return
        except DecryptionError:
            raise
        except (MalformedUdfError, OSError) as primary_error:
            errors = [f'sector 256: {primary_error}']

        sectors = media.size // 2048
        for location in dict.fromkeys((sectors - 257, sectors - 1)):
            if location < 0 or location == 256:
                continue
            try:
                buf = media.read_sector(location)
                self.anchor = AnchorVolumeDescriptorPointer(
                    DescriptorTag(buf, location), buf
                )
            except DecryptionError:
                raise
            except (MalformedUdfError, OSError) as error:
                errors.append(f'sector {location}: {error}')
            else:
                self._anchor_sector = location
                self.recovery_used = True
                logger.warning(
                    'Primary UDF anchor unavailable; using backup at'
                    ' sector %d',
                    location,
                )
                return
        raise MalformedUdfError('No valid UDF anchor: ' + '; '.join(errors))

    def _read_descriptors(self, extent: ExtentAD) -> dict[int, Partition]:
        seq = _DescriptorSequence(
            self._media, extent.location, extent.length,
            require_terminator=True, max_sectors=MAX_VOLUME_SEQUENCE_SECTORS,
        )

        descriptors: dict[int, tuple[int, DescriptorTag, bytes]] = {}

        for tag, buf in seq:
            if tag.identifier == 5:
                tag.require_crc_length(180)
                sequence, number = Partition.descriptor_order(buf)
                previous = descriptors.get(number)
                if previous is None or sequence >= previous[0]:
                    descriptors[number] = (sequence, tag, buf)
            elif tag.identifier == 6:
                # DVD UDF uses the physical partition mapping.
                pass

        if 0 not in descriptors:
            raise MalformedUdfError(
                'No UDF partition descriptor for DVD partition 0 found'
            )
        return {
            number: Partition(self._media, self, tag, buf)
            for number, (_, tag, buf) in descriptors.items()
        }

    @cached_property
    def partitions(self) -> dict[int, Partition]:
        return self._find_partitions(lambda: None)[0]

    def read_with_fallback[T](self, read: Callable[[], T]) -> T:
        return self._find_partitions(read)[1]

    def _find_partitions[T](
        self, read: Callable[[], T]
    ) -> tuple[dict[int, Partition], T]:
        errors: list[str] = []

        def read_anchor(
            anchor: AnchorVolumeDescriptorPointer, location: int
        ) -> tuple[dict[int, Partition], T] | None:
            for name, extent in (
                ('main', anchor.main_descriptor_extent),
                ('reserve', anchor.reserve_descriptor_extent),
            ):
                try:
                    partitions = self._read_descriptors(extent)
                    self.__dict__['partitions'] = partitions
                    read_ok = False
                    try:
                        result = read()
                        read_ok = True
                    finally:
                        if not read_ok:
                            self.__dict__.pop('partitions', None)
                except DecryptionError:
                    raise
                except (
                    MalformedUdfError,
                    UnsupportedUdfError,
                    OSError,
                ) as error:
                    errors.append(
                        f'anchor {location} {name} sequence: {error}'
                    )
                else:
                    if name == 'reserve':
                        self.recovery_used = True
                        logger.warning(
                            'Main UDF descriptor sequence unavailable; using'
                            ' reserve at anchor %d',
                            location,
                        )
                    return partitions, result
            return None

        current_sector = self._anchor_sector
        data = read_anchor(self.anchor, current_sector)
        if data is not None:
            return data
        sectors = self._media.size // 2048
        for location in dict.fromkeys((sectors - 257, sectors - 1)):
            if location < 0 or location in (256, current_sector):
                continue
            try:
                buf = self._media.read_sector(location)
                anchor = AnchorVolumeDescriptorPointer(
                    DescriptorTag(buf, location), buf
                )
            except DecryptionError:
                raise
            except (MalformedUdfError, OSError) as error:
                errors.append(f'anchor {location}: {error}')
                continue
            data = read_anchor(anchor, location)
            if data is not None:
                self.anchor = anchor
                self._anchor_sector = location
                self.recovery_used = True
                logger.warning(
                    'UDF descriptor sequences unavailable; using backup anchor'
                    ' at sector %d',
                    location,
                )
                return data
        raise MalformedUdfError(
            'No readable UDF descriptor sequence: ' + '; '.join(errors)
        )


class Partition:
    _struct = struct.Struct('<16xIHH164xII316x')

    @classmethod
    def descriptor_order(cls, buf: bytes) -> tuple[int, int]:
        if len(buf) < cls._struct.size:
            raise MalformedUdfError('Truncated partition descriptor')
        data = cls._struct.unpack(buf[:512])
        return data[0], data[2]

    def __init__(
        self, media: Media, volume: Volume, tag: DescriptorTag, buf: bytes
    ) -> None:
        self._media = media
        self._volume = volume

        self.tag = tag
        if tag.identifier != 5:
            raise MalformedUdfError('Invalid partition descriptor tag')

        if len(buf) < self._struct.size:
            raise MalformedUdfError('Truncated partition descriptor')
        tag.require_crc_length(180)
        data = self._struct.unpack(buf[:512])
        (
            self.sequence_number,
            self.flags,
            self.number,
            self.location,
            self.length,
        ) = data
        media.validate_extent(self.location, self.length * 2048)

    @cached_property
    def fileset(self) -> FileSet:
        from .filesystem import FileSet

        scan_length = min(self.length, MAX_FILESET_SCAN_SECTORS) * 2048
        seq = _DescriptorSequence(
            self._media, self.location, scan_length, tag_location=0
        )

        for tag, buf in seq:
            if tag.identifier == 256:
                return FileSet(
                    self._media, self._volume, self.number, tag, buf
                )
        if self.length > MAX_FILESET_SCAN_SECTORS:
            raise MalformedUdfError(
                'File-set descriptor not found within scan limit in partition'
                f' {self.number}'
            )
        raise MalformedUdfError(
            f'No file-set descriptor in partition {self.number}'
        )
