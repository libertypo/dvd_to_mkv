"""
Provides bounded, sector-aligned reads of UDF metadata from DVD media sources.

@copyright: 2009 Bastian Blank <waldi@debian.org>
@license: GNU GPL-3

Modified 2026-11-11 by libertypo: modernized Python 3.14 media access,
bounded metadata reads, and validated source extents.
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

from functools import cached_property
from typing import TYPE_CHECKING

from .general import MalformedUdfError
from .volume import Volume

if TYPE_CHECKING:
    from ..libdvdcss import DvdCssFile
    from ..media import MediaUdf


class Media:
    MAX_METADATA_BYTES = 16 * 1024 * 1024

    def __init__(self, file: MediaUdf.File | DvdCssFile) -> None:
        self._file = file
        self._sector_size = 2048

    @property
    def size(self) -> int:
        return self._file.size

    def read(self, count: int) -> bytes:
        if count < 0 or count > self.MAX_METADATA_BYTES:
            raise MalformedUdfError(
                'UDF metadata read exceeds the 16 MiB limit'
            )
        count_align = (count + self._sector_size - 1) & ~(
            self._sector_size - 1
        )
        if self._file.tell() + count_align > self._file.size:
            raise MalformedUdfError('UDF metadata read exceeds source bounds')
        data = self._file.read(count_align)
        if len(data) != count_align:
            raise MalformedUdfError('Truncated UDF metadata read')
        return data

    def validate_extent(self, offset: int, length: int) -> None:
        if (
            offset < 0
            or length < 0
            or offset * self._sector_size + length > self._file.size
        ):
            raise MalformedUdfError(
                f'UDF extent outside source: sector {offset}, {length} bytes'
            )

    def read_sector(self, offset: int, count: int = 2048) -> bytes:
        self.validate_extent(offset, count)
        offset_bytes = offset * self._sector_size
        self._file.seek(offset_bytes)
        return self.read(count)

    @cached_property
    def volume(self) -> Volume:
        return Volume(self)
