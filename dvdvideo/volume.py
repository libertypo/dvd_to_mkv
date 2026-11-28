"""
Locates and validates DVD-Video files in UDF volumes and provides their
sector streams for image backup, including recoverable read-error handling.

@copyright: 2009 Bastian Blank <waldi@debian.org>
@license: GNU GPL-3

Modified 2026-11-11 by libertypo: modernized Python 3.14 file access,
validated physical extents, and preserved VOB decryption and read recovery.
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
import re
from collections.abc import Iterator, Sequence
from itertools import pairwise
from typing import TYPE_CHECKING

from .css import DecryptionError
from .ifo import MalformedIfoHeaderError, VmgIfo, VtsIfo
from .vob import MenuVob, TitleVob

if TYPE_CHECKING:
    from .media import MediaUdf
    from .udf.filesystem import FileIdentifier

logger = logging.getLogger(__name__)


class MalformedVolumePartError(Exception):
    pass


def recover_read(
    media: MediaUdf, sector: int, count: int, *, encrypted: bool = False
) -> tuple[bytes, int]:
    def read(length: int) -> bytes:
        return (
            media.read(length, encrypted=True)
            if encrypted
            else media.read(length)
        )

    try:
        data = read(count)
        if data or count == 0:
            return data, 0
    except DecryptionError:
        raise
    except OSError:
        if count == 1:
            logger.warning(
                'Read error at sector %d; padding 1 sector with zeroes', sector
            )
            media.seek(sector + 1)
            return bytes(2048), 1
    logger.warning(
        'Read made no progress or failed at sector %d; retrying %d sectors'
        ' individually',
        sector,
        count,
    )

    media.seek(sector)
    chunks: list[bytes] = []
    padded = 0
    for current in range(sector, sector + count):
        try:
            data = read(1)
        except DecryptionError:
            raise
        except OSError:
            logger.warning(
                'Read error at sector %d; padding 1 sector with zeroes',
                current,
            )
            media.seek(current + 1)
            data = bytes(2048)
            padded += 1
        if not data:
            logger.warning(
                'Read made no progress at sector %d; padding 1 sector with'
                ' zeroes',
                current,
            )
            media.seek(current + 1)
            data = bytes(2048)
            padded += 1
        if len(data) != 2048:
            raise OSError(
                f'Incomplete sector while recovering sector {current}'
            )
        chunks.append(data)
    return b''.join(chunks), padded


def check_backup_ifo(
    primary: FileSetUdf.File, backup: FileSetUdf.File
) -> None:
    if primary.length != backup.length:
        raise MalformedIfoHeaderError(
            'Backup IFO length differs from the primary'
        )
    for offset in range(0, primary.length, 512):
        count = min(512, primary.length - offset)
        primary_data = primary.read_sector(offset, count)
        backup_data = backup.read_sector(offset, count)
        if (
            len(primary_data) != count * 2048
            or len(backup_data) != count * 2048
        ):
            raise OSError(
                'Incomplete read while comparing primary and backup IFO'
            )
        if primary_data != backup_data:
            raise MalformedIfoHeaderError(
                'Backup IFO differs from the primary'
            )


def check_declared_vob(
    ifo: FileSetUdf.File,
    vob: FileSetUdf.File | None,
    start: int,
    description: str,
) -> None:
    if not start:
        if vob is not None and vob.length:
            raise MalformedVolumePartError(
                f'{description} is present without an IFO start sector'
            )
        return
    if vob is None:
        raise MalformedVolumePartError(f'{description} is missing')
    if not vob.length or vob.location != ifo.location + start:
        raise MalformedVolumePartError(
            f'{description} has an invalid extent or IFO start sector'
        )


def check_declared_end(
    ifo: FileSetUdf.File, bup: FileSetUdf.File, end: int
) -> None:
    if bup.location + bup.length - 1 != ifo.location + end:
        raise MalformedVolumePartError(
            'Backup IFO end differs from the IFO-declared end sector'
        )


class VmgUdf:
    def __init__(self, media: MediaUdf) -> None:
        try:
            file_ifo = media['VIDEO_TS.IFO']
            file_bup = media['VIDEO_TS.BUP']
        except KeyError as e:
            raise MalformedVolumePartError(f'Missing file {e.args[0]}') from e

        file_vob = media.get('VIDEO_TS.VOB')

        self.fileset = FileSetUdf(media, file_ifo, file_bup, file_vob)

        self.ifo = VmgIfo(self.fileset.ifo)
        check_declared_vob(
            self.fileset.ifo,
            self.fileset.menu_vob,
            self.ifo.header.part_menu_vob_start,
            'VMG declares a menu VOB but VIDEO_TS.VOB',
        )
        check_declared_end(
            self.fileset.ifo, self.fileset.bup, self.ifo.header.part_bup_end
        )
        try:
            self.bup = VmgIfo(self.fileset.bup)
            check_backup_ifo(self.fileset.ifo, self.fileset.bup)
        except DecryptionError:
            raise
        except (MalformedIfoHeaderError, OSError) as error:
            logger.warning('Invalid backup VMG IFO: %s', error)
            self.bup = None
        count = self.ifo.header.number_titlesets
        if count > 99:
            raise MalformedVolumePartError(
                'VMG declares more than 99 titlesets'
            )
        for name in media:
            match = re.fullmatch(
                r'VTS_([0-9]{2})_[0-9]\.(?:IFO|BUP|VOB)', name
            )
            if match and not 1 <= int(match.group(1)) <= count:
                raise MalformedVolumePartError(
                    f'DVD file is outside the VMG titleset count: {name}'
                )
        self.menu_vob = (
            MenuVob(self.fileset.menu_vob)
            if self.fileset.menu_vob is not None
            else None
        )

    def dump(self) -> Iterator[FileSetUdf.File]:
        return iter(self.fileset)


class VtsUdf:
    def __init__(self, media: MediaUdf, titleset: int) -> None:
        prefix = f'VTS_{titleset:02d}'

        try:
            file_ifo = media[f'{prefix}_0.IFO']
            file_bup = media[f'{prefix}_0.BUP']
        except KeyError as e:
            raise MalformedVolumePartError(f'Missing file {e.args[0]}') from e

        file_menu_vob = media.get(f'{prefix}_0.VOB')

        file_title_vob: list[FileIdentifier] = []
        missing_vob = False
        for i in range(1, 10):
            try:
                vob = media[f'{prefix}_{i:d}.VOB']
            except KeyError:
                missing_vob = True
                continue
            if missing_vob:
                raise MalformedVolumePartError(
                    f'Missing earlier VOB in titleset {titleset}'
                )
            file_title_vob.append(vob)

        self.fileset = FileSetUdf(
            media, file_ifo, file_bup, file_menu_vob, file_title_vob
        )

        try:
            self.ifo = VtsIfo(self.fileset.ifo)
        except DecryptionError:
            raise
        except (MalformedIfoHeaderError, OSError) as error:
            raise MalformedVolumePartError(
                f'Invalid IFO for titleset {titleset}: {error}'
            ) from error
        check_declared_vob(
            self.fileset.ifo,
            self.fileset.menu_vob,
            self.ifo.header.part_menu_vob_start,
            f'Titleset {titleset} declares a menu VOB but it',
        )
        check_declared_vob(
            self.fileset.ifo,
            self.fileset.title_vob[0] if self.fileset.title_vob else None,
            self.ifo.header.part_title_vob_start,
            f'Titleset {titleset} declares a title VOB but it',
        )
        if not self.ifo.header.part_title_vob_start and any(
            vob.length for vob in self.fileset.title_vob
        ):
            raise MalformedVolumePartError(
                f'Titleset {titleset} title VOB is present without an IFO'
                ' start sector'
            )
        check_declared_end(
            self.fileset.ifo, self.fileset.bup, self.ifo.header.part_bup_end
        )
        try:
            self.bup = VtsIfo(self.fileset.bup)
            check_backup_ifo(self.fileset.ifo, self.fileset.bup)
        except DecryptionError:
            raise
        except (MalformedIfoHeaderError, OSError) as error:
            logger.warning(
                'Invalid backup IFO for titleset %d: %s', titleset, error
            )
            self.bup = None

        if self.fileset.menu_vob is not None:
            self.menu_vob = MenuVob(self.fileset.menu_vob)
        else:
            self.menu_vob = None

        self.title_vob = TitleVob(self.fileset.title_vob)

    def dump(self) -> Iterator[FileSetUdf.File]:
        return iter(self.fileset)


class FileSetUdf:
    class File:

        def __init__(
            self,
            media: MediaUdf,
            name: str,
            location: int,
            length: int,
            *,
            allocation_length: int | None = None,
        ) -> None:
            self._media, self.name, self.location, self.length = (
                media,
                name,
                location,
                length,
            )
            self.allocation_length = (
                length if allocation_length is None else allocation_length
            )
            self._position = 0

        def __iter__(self) -> Iterator[bytes]:
            return self.dump()

        def dump(self, count: int | None = None) -> Iterator[bytes]:
            length = self.length if count is None else count
            if length < 0 or length > self.length:
                raise ValueError('Dump length exceeds file extent')
            cur = 0
            self.seek(cur)
            while cur < length:
                if self._position != cur:
                    self.seek(cur)
                r = self.read(min(512, length - cur))
                if not r or len(r) % 2048:
                    raise OSError(
                        'Incomplete sector while dumping ' + self.name
                    )
                cur += len(r) // 2048
                yield r

        def __repr__(self) -> str:
            return (
                f'<{self.__class__.__name__} with name: {self.name!r}; '
                f'location: {self.location:d}; length: {self.length:d}>'
            )

        def read(self, count: int = 1) -> bytes:
            return self._read(count)

        def read_sector(self, offset: int, count: int = 1) -> bytes:
            self.seek(offset)
            return self.read(count)

        def seek(self, offset: int) -> None:
            self._seek(offset)

        def _read(self, count: int) -> bytes:
            self._prepare_read(count)
            data = self._media.read(count)
            self._position += len(data) // 2048
            return data

        def _prepare_read(self, count: int) -> None:
            if count < 0:
                raise ValueError('Sector count must be nonnegative')
            if self._position + count > self.length:
                raise ValueError('Read exceeds file extent')
            if self._media.tell() != self.location + self._position:
                self.seek(self._position)

        def _seek(self, offset: int, **kw: bool) -> None:
            if offset < 0 or offset > self.length:
                raise ValueError('Seek exceeds file extent')
            self._media.seek(self.location + offset, **kw)
            self._position = offset

    class FileIfo(File):
        pass

    class FileVob(File):
        def read(self, count: int = 1) -> bytes:
            if count < 0:
                raise ValueError('Sector count must be nonnegative')
            if self._position + count > self.length:
                raise ValueError('Read exceeds file extent')
            self.seek(self._position)
            sector = self.location + self._position
            data, _padded = recover_read(
                self._media, sector, count, encrypted=True
            )
            self._position += len(data) // 2048
            return data

        def seek(self, offset: int) -> None:
            if offset < 0 or offset > self.length:
                raise ValueError('Seek exceeds VOB extent')
            self._seek(0, start_encrypted=True)
            self._seek(offset)

    def __init__(
        self,
        media: MediaUdf,
        ifo: FileIdentifier,
        bup: FileIdentifier,
        menu_vob: FileIdentifier | None,
        title_vob: Sequence[FileIdentifier] = (),
    ) -> None:
        input = [ifo, bup]

        if menu_vob:
            input.insert(1, menu_vob)

        input[-1:-1] = title_vob
        locations: list[int] = []
        for entry in input:
            if len(entry.entry.ad) != 1:
                raise MalformedVolumePartError(
                    f'DVD file must have one extent: {entry.name}'
                )
            locations.append(entry.entry.ad[0].location_absolute)
        if any(right < left for left, right in pairwise(locations)):
            raise MalformedVolumePartError(
                'DVD file extents are out of physical order'
            )
        for entry, next_location in zip(input, locations[1:], strict=False):
            allocation = entry.entry.ad[0]
            if (
                allocation.location_absolute * 2048 + allocation.length
                > next_location * 2048
            ):
                raise MalformedVolumePartError(
                    f'DVD file allocation overlaps the next file: {entry.name}'
                )

        for entry in (ifo, bup):
            size = entry.entry.length
            if size <= 0 or size % 2048 or size > entry.entry.ad[0].length:
                raise MalformedVolumePartError(
                    f'Invalid DVD IFO file length: {entry.name}'
                )
        for entry in input[1:-1]:
            size = entry.entry.length
            if size < 0 or size % 2048 or size > entry.entry.ad[0].length:
                raise MalformedVolumePartError(
                    f'Invalid DVD VOB file length: {entry.name}'
                )

        i = input[0]
        ad_i = i.entry.ad[0]

        location = ad_i.location_absolute
        length = i.entry.length // 2048

        files: list[FileSetUdf.File] = [
            self.FileIfo(
                media,
                i.name,
                location,
                length,
                allocation_length=(ad_i.length + 2047) // 2048,
            )
        ]

        for i in input[1:-1]:
            ad_i = i.entry.ad[0]

            location = ad_i.location_absolute
            length = i.entry.length // 2048

            files.append(
                self.FileVob(
                    media,
                    i.name,
                    location,
                    length,
                    allocation_length=(ad_i.length + 2047) // 2048,
                )
            )

        i = input[-1]
        ad_i = i.entry.ad[0]
        location = ad_i.location_absolute
        length = i.entry.length // 2048

        files.append(
            self.FileIfo(
                media,
                i.name,
                location,
                length,
                allocation_length=(ad_i.length + 2047) // 2048,
            )
        )
        if any(
            file.location < 0
            or (file.location + file.length) * 2048 > media.size
            for file in files
        ):
            raise MalformedVolumePartError(
                'DVD file extent exceeds source bounds'
            )

        self.list = files[:]

        self.ifo = files.pop(0)
        self.bup = files.pop(-1)
        if menu_vob:
            self.menu_vob = files.pop(0)
        else:
            self.menu_vob = None
        self.title_vob = files

    def __iter__(self) -> Iterator[FileSetUdf.File]:
        return iter(self.list)
