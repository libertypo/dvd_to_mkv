"""
Defines shared UDF descriptor and allocation types, validates descriptor
integrity, and decodes OSTA compressed Unicode names.

@copyright: 2009 Bastian Blank <waldi@debian.org>
@license: GNU GPL-3

Modified 2026-11-11 by libertypo: modernized Python 3.14 descriptors,
validated lengths, and corrected OSTA compressed Unicode decoding.
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

import binascii
import struct


class MalformedUdfError(RuntimeError):
    """A UDF descriptor or extent is malformed."""


class UnsupportedUdfError(NotImplementedError):
    """A UDF feature is not supported by this reader."""


class DescriptorTag:
    _struct = struct.Struct('<HHBxHHHI')

    def __init__(
        self,
        buf: bytes | bytearray | memoryview,
        expected_location: int | None = None,
    ) -> None:
        if len(buf) < self._struct.size:
            raise MalformedUdfError('Truncated UDF descriptor tag')
        data = self._struct.unpack(buf[:16])
        (
            self.identifier,
            self.version,
            self.checksum,
            _serial_number,
            crc,
            self.crc_length,
            self.location,
        ) = data
        if (sum(buf[:4]) + sum(buf[5:16])) & 0xFF != self.checksum:
            raise MalformedUdfError('Invalid UDF descriptor tag checksum')
        if self.version not in (2, 3):
            raise MalformedUdfError(
                f'Unsupported UDF descriptor version: {self.version}'
            )
        if (
            expected_location is not None
            and self.location != expected_location
        ):
            raise MalformedUdfError(
                f'UDF descriptor tag location {self.location} does not match'
                f' expected {expected_location}'
            )
        if len(buf) < 16 + self.crc_length:
            raise MalformedUdfError('Truncated UDF descriptor CRC data')
        if binascii.crc_hqx(buf[16:16 + self.crc_length], 0) != crc:
            raise MalformedUdfError('Invalid UDF descriptor CRC')

    def require_crc_length(self, length: int) -> None:
        if self.crc_length < length:
            raise MalformedUdfError(
                'UDF descriptor CRC does not cover required fields'
            )

    def __repr__(self) -> str:
        return (
            f'<DescriptorTag with identifier: {self.identifier:d}; '
            f'version: {self.version:d}; location: {self.location:d}>'
        )


class OSTACompressedUnicode(str):
    def __new__(cls, buf: bytes) -> str:
        if not buf:
            raise MalformedUdfError(
                'Missing OSTA Unicode compression identifier'
            )
        stype = buf[0]

        if stype == 8:
            return buf[1:].decode('latin-1')
        elif stype == 16:
            if (len(buf) - 1) % 2:
                raise MalformedUdfError('Truncated OSTA 16-bit Unicode string')
            try:
                return buf[1:].decode('utf-16be')
            except UnicodeDecodeError as error:
                raise MalformedUdfError(
                    'Invalid OSTA 16-bit Unicode string'
                ) from error
        raise MalformedUdfError(
            f'Unsupported OSTA Unicode compression identifier: {stype}'
        )


class ExtentAD:
    _struct = struct.Struct('<II')
    size = _struct.size

    def __init__(self, buf: bytes) -> None:
        if len(buf) != self.size:
            raise MalformedUdfError('Invalid extent descriptor length')
        self.length, self.location = self._struct.unpack(buf)

    def __repr__(self) -> str:
        return (
            f'<ExtentAD with location: {self.location:d}; length:'
            f' {self.length:d}>'
        )


class LongAD:
    _struct = struct.Struct('<IIH6x')
    size = _struct.size

    def __init__(self, buf: bytes) -> None:
        if len(buf) != self.size:
            raise MalformedUdfError(
                'Invalid long allocation descriptor length'
            )
        data = self._struct.unpack(buf)
        length, self.location, self.partition = data
        self.length = length & 0x3fffffff
        self.flags = length >> 30

    def __repr__(self) -> str:
        return (
            f'<LongAD with location: {self.location:d}; length:'
            f' {self.length:d}; flags: {self.flags:d}; partition:'
            f' {self.partition:d}>'
        )


class ShortAD:
    _struct = struct.Struct('<II')
    size = _struct.size

    def __init__(self, buf: bytes) -> None:
        if len(buf) != self.size:
            raise MalformedUdfError(
                'Invalid short allocation descriptor length'
            )
        data = self._struct.unpack(buf)
        length, self.location = data
        self.length = length & 0x3fffffff
        self.flags = length >> 30

    def __repr__(self) -> str:
        return (
            f'<ShortAD with location: {self.location:d}; length:'
            f' {self.length:d}; flags: {self.flags:x}>'
        )
