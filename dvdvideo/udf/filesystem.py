"""
Parses UDF file sets, entries, and directories, resolving file names and
allocation extents while validating descriptor bounds and integrity.

@copyright: 2009 Bastian Blank <waldi@debian.org>
@license: GNU GPL-3

Modified 2026-11-11 by libertypo: modernized Python 3.14 cached metadata,
validated allocation and directory bounds, and checked each entry's tag.
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

import struct
from functools import cached_property
from typing import TYPE_CHECKING

from .general import (
    DescriptorTag,
    LongAD,
    MalformedUdfError,
    OSTACompressedUnicode,
    ShortAD,
    UnsupportedUdfError,
)

if TYPE_CHECKING:
    from .media import Media
    from .volume import Volume


class LongADPartition(LongAD):
    def __init__(self, buf: bytes, volume: Volume) -> None:
        super().__init__(buf)
        try:
            partition = volume.partitions[self.partition]
        except KeyError as error:
            raise MalformedUdfError(
                f'UDF allocation references unknown partition {self.partition}'
            ) from error
        if self.flags != 0:
            raise UnsupportedUdfError(
                'Unrecorded or chained UDF extents are unsupported'
            )
        if self.location * 2048 + self.length > partition.length * 2048:
            raise MalformedUdfError(
                'Long allocation extent exceeds partition bounds'
            )
        self.location_absolute = partition.location + self.location


class ShortADPartition(ShortAD):
    def __init__(self, buf: bytes, volume: Volume, partition_nr: int) -> None:
        super().__init__(buf)
        try:
            partition = volume.partitions[partition_nr]
        except KeyError as error:
            raise MalformedUdfError(
                f'UDF allocation references unknown partition {partition_nr}'
            ) from error
        if self.flags != 0:
            raise UnsupportedUdfError(
                'Unrecorded or chained UDF extents are unsupported'
            )
        if self.location * 2048 + self.length > partition.length * 2048:
            raise MalformedUdfError(
                'Short allocation extent exceeds partition bounds'
            )
        self.location_absolute = partition.location + self.location


class ICBTag:
    _struct = struct.Struct('<8x3xB6xH')

    def __init__(self, buf: bytes) -> None:
        if len(buf) != self._struct.size:
            raise MalformedUdfError('Invalid ICB tag length')
        data = self._struct.unpack(buf)
        self.filetype, self.flags = data

    def __repr__(self) -> str:
        return (
            f'<ICBTag with filetype: {self.filetype:d}; flags: {self.flags:x}>'
        )


class FileSet:
    _struct = struct.Struct('<16x384x16s')

    def __init__(
        self,
        media: Media,
        volume: Volume,
        partition_nr: int,
        tag: DescriptorTag,
        buf: bytes,
    ) -> None:
        self._media = media
        self._volume = volume
        self._partition_nr = partition_nr
        self.tag = tag
        if tag.identifier != 256:
            raise MalformedUdfError('Invalid file-set descriptor tag')

        if len(buf) < self._struct.size:
            raise MalformedUdfError('Truncated file-set descriptor')
        tag.require_crc_length(self._struct.size - 16)
        data = self._struct.unpack(buf[:416])
        self.root_icb = LongADPartition(data[0], volume)

    @cached_property
    def root(self) -> FileEntry:
        location = self.root_icb.location_absolute
        length = self.root_icb.length

        buf = self._media.read_sector(location, length)[:length]
        tag = DescriptorTag(buf, self.root_icb.location)
        return FileEntry(
            self._media, self._volume, self.root_icb.partition, tag, buf
        )


class FileEntry:
    _struct = struct.Struct('<16x20s20xQ104xII')

    def __init__(
        self,
        media: Media,
        volume: Volume,
        partition_nr: int,
        tag: DescriptorTag,
        buf: bytes,
    ) -> None:
        self._media = media
        self._volume = volume
        self._partition_nr = partition_nr
        self.tag = tag
        if tag.identifier != 261:
            raise MalformedUdfError('Invalid file-entry descriptor tag')

        if len(buf) < self._struct.size:
            raise MalformedUdfError('Truncated file-entry descriptor')
        data = self._struct.unpack(buf[:176])
        icb, self.length, length_ea, length_ad = data

        self.icb = ICBTag(icb)

        ad: list[ShortADPartition] = []

        if 176 + length_ea + length_ad > len(buf):
            raise MalformedUdfError(
                'Allocation descriptors exceed file-entry length'
            )
        tag.require_crc_length(160 + length_ea + length_ad)
        buf = buf[176 + length_ea:176 + length_ea + length_ad]

        desc_type = self.icb.flags & 7
        if desc_type != 0:
            raise UnsupportedUdfError(
                f'Unsupported UDF allocation descriptor type: {desc_type}'
            )
        if len(buf) % ShortAD.size:
            raise MalformedUdfError('Truncated short allocation descriptor')
        cur = 0
        while cur < len(buf):
            ad.append(
                ShortADPartition(
                    buf[cur : cur + ShortAD.size],
                    self._volume,
                    self._partition_nr,
                )
            )
            cur += ShortAD.size

        self.ad = tuple(ad)

    def __repr__(self) -> str:
        return f'<FileEntry with icbtag: {self.icb!r}; ad: {self.ad!r}>'

    @cached_property
    def tree(self) -> dict[str, FileIdentifier]:
        if self.icb.filetype != 4:
            raise MalformedUdfError('Expected a UDF directory entry')
        if self.length > self._media.MAX_METADATA_BYTES:
            raise MalformedUdfError(
                'UDF directory exceeds the 16 MiB metadata limit'
            )
        tree: dict[str, FileIdentifier] = {}
        remaining = self.length
        chunks: list[bytes] = []
        extents: list[tuple[int, int, int]] = []
        total = 0
        for ad in self.ad:
            length = min(ad.length, remaining)
            if not length:
                continue
            chunks.append(
                self._media.read_sector(ad.location_absolute, length)[:length]
            )
            extents.append((total, total + length, ad.location))
            total += length
            remaining -= length
        if remaining:
            raise MalformedUdfError(
                'Directory length exceeds its allocation extents'
            )
        buf = b''.join(chunks)
        cur = 0
        extent_index = 0
        while cur < len(buf):
            while cur >= extents[extent_index][1]:
                extent_index += 1
            start, end, location = extents[extent_index]
            block_end = min(end, start + ((cur - start) // 2048 + 1) * 2048)
            if not any(buf[cur:block_end]):
                cur = block_end
                continue
            tag = DescriptorTag(
                memoryview(buf)[cur:], location + (cur - start) // 2048
            )
            f = FileIdentifier(
                self._media,
                self._volume,
                self._partition_nr,
                tag,
                buf,
                offset=cur,
            )
            cur += f.descriptor_length
            if f.characteristics & 4:
                continue
            if f.name in tree:
                raise MalformedUdfError(
                    f'Duplicate UDF directory name: {f.name!r}'
                )
            if len(tree) >= 100_000:
                raise MalformedUdfError(
                    'UDF directory exceeds the 100000-entry limit'
                )
            tree[f.name] = f
        return tree


class FileIdentifier:
    _struct = struct.Struct('<16x2xBB16sH')

    def __init__(
        self, media: Media, volume: Volume, partition_nr: int,
        tag: DescriptorTag, buf: bytes, offset: int = 0,
    ) -> None:
        self._media = media
        self._volume = volume
        self._partition_nr = partition_nr
        self.tag = tag
        if tag.identifier != 257:
            raise MalformedUdfError(
                f'Invalid file-identifier tag: {tag.identifier}'
            )

        if offset < 0 or len(buf) - offset < self._struct.size:
            raise MalformedUdfError('Truncated file-identifier descriptor')
        data = self._struct.unpack_from(buf, offset)
        self.characteristics, length_fi, icb, length_iu = data

        # From libdvdread
        self.descriptor_length = 4 * ((38 + length_fi + length_iu + 3) // 4)
        if self.descriptor_length > len(buf) - offset:
            raise MalformedUdfError(
                'File-identifier name exceeds descriptor bounds'
            )
        if tag.crc_length > self.descriptor_length - 16:
            raise MalformedUdfError(
                'File-identifier CRC exceeds descriptor bounds'
            )
        tag.require_crc_length(22 + length_iu + length_fi)

        self._icb = icb
        if self.characteristics & 4:
            self.name = ''
        else:
            _ = self.icb
            self.name = (
                OSTACompressedUnicode(
                    buf[
                        offset
                        + 38
                        + length_iu : offset
                        + 38
                        + length_iu
                        + length_fi
                    ]
                )
                if length_fi
                else ''
            )

    def __repr__(self) -> str:
        return f'<FileIdentifier with name: {self.name!r}>'

    @cached_property
    def icb(self) -> LongADPartition:
        if self.characteristics & 4:
            raise MalformedUdfError(
                'Deleted file identifier has no usable ICB'
            )
        return LongADPartition(self._icb, self._volume)

    @cached_property
    def entry(self) -> FileEntry:
        location = self.icb.location_absolute
        length = self.icb.length

        buf = self._media.read_sector(location, length)[:length]
        tag = DescriptorTag(buf, self.icb.location)
        return FileEntry(
            self._media, self._volume, self.icb.partition, tag, buf
        )
