"""
Opens DVD images or devices, parses their UDF filesystem, and exposes bounded
sector reads and DVD-Video files.

@copyright: 2009 Bastian Blank <waldi@debian.org>
@license: GNU GPL-3

Modified 2026-11-11 by libertypo: modernized Python 3.14 media access,
added explicit resource ownership, validated sector operations, and
refused scrambled video from raw readers.
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

import logging
import os
import stat
from collections.abc import Iterator, Mapping
from types import TracebackType
from typing import Self

from .css import check_decrypted
from .ifo import MalformedIfoHeaderError
from .output_space import source_bytes
from .udf.filesystem import FileIdentifier
from .udf.general import MalformedUdfError, UnsupportedUdfError
from .volume import MalformedVolumePartError, VmgUdf, VtsUdf

logger = logging.getLogger(__name__)


class MediaUdf(Mapping[str, FileIdentifier]):
    class File:
        def __init__(self, filename: str) -> None:
            self._file = open(filename, 'rb')  # noqa: SIM115 - this context-manager backend owns the file
            try:
                info = os.fstat(self._file.fileno())
                self._regular = stat.S_ISREG(info.st_mode)
                self.size = (
                    source_bytes(filename)
                    if stat.S_ISBLK(info.st_mode)
                    else info.st_size
                )
            except BaseException:
                self._file.close()
                raise

        def close(self) -> None:
            self._file.close()

        def __enter__(self) -> Self:
            if self._file.closed:
                raise ValueError('I/O operation on closed image source')
            return self

        def __exit__(
            self, exc_type: type[BaseException] | None,
            exc: BaseException | None, traceback: TracebackType | None,
        ) -> None:
            self.close()

        def read(self, count: int) -> bytes:
            if count < 0 or self.tell() + count > self.size:
                raise ValueError('Read exceeds source bounds')
            data = self._file.read(count)
            if (
                len(data) < count
                and self._regular
                and os.fstat(self._file.fileno()).st_size < self.size
            ):
                raise ValueError('Source image was truncated during reading')
            return data

        def seek(self, offset: int) -> None:
            if offset < 0 or offset > self.size:
                raise ValueError('Seek exceeds source bounds')
            self._file.seek(offset)

        def tell(self) -> int:
            return self._file.tell()

        def read_sector(self, count: int, encrypted: bool = False) -> bytes:
            sector = self.tell_sector()
            data = self.read(count * 2048)
            if encrypted:
                check_decrypted(data, sector)
            return data

        def seek_sector(
            self, offset: int, start_encrypted: bool = False
        ) -> None:
            self.seek(offset * 2048)

        def tell_sector(self) -> int:
            return self.tell() // 2048

    def __init__(self, filename: str) -> None:
        from .udf.media import Media

        s = os.stat(filename)
        if stat.S_ISREG(s.st_mode):
            f = self.File(filename)
        elif stat.S_ISBLK(s.st_mode):
            try:
                from .libdvdcss import DvdCssFile
            except ImportError as error:
                logger.warning(
                    'libdvdcss unavailable; encrypted DVDs cannot be'
                    ' decrypted: %s',
                    error,
                )
                DvdCssFile = self.File
            f = DvdCssFile(filename)
        else:
            raise ValueError(
                'Source must be a regular image file or block device:'
                f' {filename}'
            )

        self._file = f
        try:
            self.udf = Media(f)
            volume = self.udf.volume

            def read_video_dir() -> dict[str, FileIdentifier]:
                try:
                    video_dir = (
                        volume.partitions[0]
                        .fileset.root.tree['VIDEO_TS']
                        .entry.tree
                    )
                    self.video_dir = video_dir
                    VmgUdf(self)
                    return video_dir
                except (
                    KeyError,
                    MalformedVolumePartError,
                    MalformedIfoHeaderError,
                ) as error:
                    raise MalformedUdfError(
                        f'Invalid DVD video directory or VMG: {error}'
                    ) from error

            self.video_dir = volume.read_with_fallback(read_video_dir)
        except BaseException:
            f.close()
            raise

    @property
    def size(self) -> int:
        return self._file.size

    def read(self, count: int, encrypted: bool = False) -> bytes:
        return self._file.read_sector(count, encrypted=encrypted)

    def seek(self, offset: int, start_encrypted: bool = False) -> None:
        self._file.seek_sector(offset, start_encrypted=start_encrypted)

    def tell(self) -> int:
        return self._file.tell_sector()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Self:
        self.tell()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None,
        exc: BaseException | None, traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __getitem__(self, name: str) -> FileIdentifier:
        f = self.video_dir[name]
        if f.entry.icb.filetype != 5:
            raise MalformedUdfError(
                f'DVD file is not a regular UDF file: {name}'
            )
        if not f.entry.ad:
            raise MalformedUdfError(
                f'DVD file has no allocation extent: {name}'
            )
        if len(f.entry.ad) > 1:
            raise UnsupportedUdfError(
                f'Multi-extent DVD files are unsupported: {name}'
            )
        return f

    def __iter__(self) -> Iterator[str]:
        return iter(self.video_dir)

    def __len__(self) -> int:
        return len(self.video_dir)

    def vmg(self) -> VmgUdf:
        return VmgUdf(self)

    def vts(self, titleset: int) -> VtsUdf:
        return VtsUdf(self, titleset)
