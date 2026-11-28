"""DVD sector decryption checks shared by raw and native readers."""

# @copyright: 2026 libertypo <libertypo@proton.me>
# @license: GNU GPL-3
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.


class DecryptionError(OSError):
    """CSS decryption failed; damaged-sector padding cannot recover it."""


def check_decrypted(data: bytes, sector: int) -> None:
    for offset in range(0, len(data) - 2047, 2048):
        if data[offset:offset + 4] != b'\x00\x00\x01\xba':
            continue
        end = offset + 2048
        pos = offset + 14 + (data[offset + 13] & 7)
        while pos + 6 <= end and data[pos:pos + 3] == b'\x00\x00\x01':
            stream_id = data[pos + 3]
            length = int.from_bytes(data[pos + 4:pos + 6], 'big')
            if (
                (stream_id == 0xbd or 0xc0 <= stream_id <= 0xef)
                and pos + 9 <= end and data[pos + 6] & 0xc0 == 0x80
                and data[pos + 6] & 0x30
            ):
                raise DecryptionError(
                    'DVD video remains CSS-scrambled at sector'
                    f' {sector + offset // 2048}; a raw reader cannot decrypt'
                    ' it, or libdvdcss could not obtain a valid title key'
                )
            if not length:
                break
            pos += 6 + length
