"""
Parses DVD-Video VMG and VTS IFO headers and exposes their declared data for
DVD image backups.

@copyright: 2009 Bastian Blank <waldi@debian.org>
@license: GNU GPL-3

Modified 2026-11-11 by libertypo: modernized Python 3.14 types, repaired
the shared IFO contract, and reported malformed headers explicitly.
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
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .volume import FileSetUdf


class MalformedIfoHeaderError(Exception):
    pass


class VmgIfoHeader:
    _struct = struct.Struct(
        '>12s I 12x IHI 24x H 32x Q 24x II 56x IIIIIIII 32x 1792x'
    )

    def __init__(self, buf: bytes) -> None:
        if len(buf) != self._struct.size:
            raise MalformedIfoHeaderError(
                'VMG IFO header must contain exactly 2048 bytes'
            )
        data = self._struct.unpack(buf)

        (
            id,
            self.part_bup_end,
            self.part_ifo_end,
            _version,
            _category,
            self.number_titlesets,
            _pos,
            _vmgi_mat_end,
            _fp_pgc_start,
            self.part_menu_vob_start,
            _vmgm1_start,
            _vmgm2_start,
            _vmgm3_start,
            _vmgm4_start,
            _vmgm5_start,
            _vmgm6_start,
            _vmgm7_start,
        ) = data

        if id != b'DVDVIDEO-VMG':
            raise MalformedIfoHeaderError('Invalid VMG IFO identifier')


class VtsIfoHeader:
    _struct = struct.Struct('>12s I 12x IH 94x I 60x IIIIIIIIII 24x 1792x')

    def __init__(self, buf: bytes) -> None:
        if len(buf) != self._struct.size:
            raise MalformedIfoHeaderError(
                'VTS IFO header must contain exactly 2048 bytes'
            )
        data = self._struct.unpack(buf)

        (
            id,
            self.part_bup_end,
            self.part_ifo_end,
            _version,
            _vts_mat_end,
            self.part_menu_vob_start,
            self.part_title_vob_start,
            _vtsm1_start,
            _vtsm2_start,
            _vtsm3_start,
            _vtsm4_start,
            _vtsm5_start,
            _vtsm6_start,
            _vtsm7_start,
            _vtsm8_start,
        ) = data

        if id != b'DVDVIDEO-VTS':
            raise MalformedIfoHeaderError('Invalid VTS IFO identifier')


class _Ifo[Header: VmgIfoHeader | VtsIfoHeader]:
    _file: FileSetUdf.File
    header: Header

    def dump(self) -> tuple[Iterator[bytes]]:
        return self._file.dump(self.header.part_ifo_end + 1),


class VmgIfo(_Ifo[VmgIfoHeader]):
    def __init__(self, file: FileSetUdf.File) -> None:
        self._file = file

        self.header = VmgIfoHeader(self._file.read_sector(0))
        if self.header.part_ifo_end >= self._file.length:
            raise MalformedIfoHeaderError(
                'VMG IFO length exceeds its file extent'
            )


class VtsIfo(_Ifo[VtsIfoHeader]):
    def __init__(self, file: FileSetUdf.File) -> None:
        self._file = file

        self.header = VtsIfoHeader(self._file.read_sector(0))
        if self.header.part_ifo_end >= self._file.length:
            raise MalformedIfoHeaderError(
                'VTS IFO length exceeds its file extent'
            )
