"""
Reads DVD sectors through libdvdcss, optionally decrypting CSS-protected data.

@copyright: 2009 Bastian Blank <waldi@debian.org>
@license: GNU GPL-3

Modified 2026-11-11 by libertypo: modernized Python 3.14 I/O, corrected
native cleanup, validated sector reads and seeks, and enforced CSS title-key
checks and decrypted video output.
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

import os
from ctypes import (
    CDLL,
    CFUNCTYPE,
    c_char_p,
    c_int,
    c_void_p,
    create_string_buffer,
)
from types import TracebackType
from typing import Self

from .css import DecryptionError, check_decrypted
from .output_space import source_bytes

try:
    _libdvdcss = CDLL('libdvdcss.so.2')
except OSError as error:
    raise ImportError('Could not load libdvdcss.so.2') from error

_dvdcss_open = CFUNCTYPE(c_void_p, c_char_p)(('dvdcss_open', _libdvdcss))
_dvdcss_close = CFUNCTYPE(c_int, c_void_p)(('dvdcss_close', _libdvdcss))
_dvdcss_seek = CFUNCTYPE(c_int, c_void_p, c_int, c_int)(
    ('dvdcss_seek', _libdvdcss)
)
_dvdcss_read = CFUNCTYPE(c_int, c_void_p, c_void_p, c_int, c_int)(
    ('dvdcss_read', _libdvdcss)
)

DVDCSS_READ_DECRYPT = 1 << 0
DVDCSS_SEEK_KEY = 1 << 1


class DvdCssFile:
    def __init__(self, filename: str) -> None:
        self.size = source_bytes(filename)
        self._handle = _dvdcss_open(os.fsencode(filename))
        if not self._handle:
            raise OSError(f'Could not open DVD source: {filename}')
        self._cur: int = 0

    def close(self) -> None:
        handle = self._handle
        if handle is not None:
            self._handle = None
            if _dvdcss_close(handle) < 0:
                raise OSError('Could not close DVD source')

    def __enter__(self) -> Self:
        self._check_open()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None,
        exc: BaseException | None, traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _check_open(self) -> None:
        if self._handle is None:
            raise ValueError('I/O operation on closed DVD source')

    def read(self, count: int) -> bytes:
        if count < 0 or count % 2048:
            raise ValueError(
                'DVD byte reads require a nonnegative multiple of 2048'
            )
        return self.read_sector(count // 2048)

    def read_sector(self, count: int, encrypted: bool = False) -> bytes:
        self._check_open()
        if count < 0 or (self._cur + count) * 2048 > self.size:
            raise ValueError('DVD read exceeds source bounds')
        chunks: list[bytes] = []
        while count:
            chunk = min(count, 512)
            buf = create_string_buffer(chunk * 2048)
            ret = int(
                _dvdcss_read(
                    self._handle,
                    buf,
                    chunk,
                    DVDCSS_READ_DECRYPT if encrypted else 0,
                )
            )
            if ret < 0 or ret > chunk:
                raise OSError('Invalid sector count returned by libdvdcss')
            sector = self._cur
            self._cur += ret
            data = buf.raw[:ret * 2048]
            if encrypted:
                check_decrypted(data, sector)
            chunks.append(data)
            if ret != chunk:
                # libdvdcss may move its cursor after a short read.
                self.seek_sector(self._cur)
                break
            count -= ret
        return b''.join(chunks)

    def seek(self, offset: int) -> None:
        if offset < 0 or offset % 2048:
            raise ValueError(
                'DVD byte seeks require a nonnegative multiple of 2048'
            )
        self.seek_sector(offset // 2048)

    def seek_sector(self, offset: int, start_encrypted: bool = False) -> None:
        self._check_open()
        if offset < 0 or offset * 2048 > self.size or offset > 0x7fffffff:
            raise ValueError('DVD seek exceeds source bounds')
        ret = int(
            _dvdcss_seek(
                self._handle, offset, DVDCSS_SEEK_KEY if start_encrypted else 0
            )
        )
        if ret < 0 or ret != offset:
            if start_encrypted:
                raise DecryptionError(
                    f'Could not check DVD title key at sector {offset}'
                )
            raise OSError(f'Could not seek DVD source to sector {offset}')
        self._cur = ret

    def tell(self) -> int:
        return self.tell_sector() * 2048

    def tell_sector(self) -> int:
        self._check_open()
        return self._cur
