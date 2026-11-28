"""
Provides iterators for copying DVD menu and title VOB file data.

@copyright: 2009 Bastian Blank <waldi@debian.org>
@license: GNU GPL-3

Modified 2026-11-11 by libertypo: added type annotations for VOB files
and grouped dump iterators.
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

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .volume import FileSetUdf


class MenuVob:
    def __init__(self, file: FileSetUdf.File) -> None:
        self._file = file

    def dump(self) -> tuple[Iterator[bytes]]:
        return self._file.dump(),


class TitleVob:
    def __init__(self, files: Sequence[FileSetUdf.File]) -> None:
        self._files = files

    def dump(self) -> Iterator[Iterator[bytes]]:
        for file in self._files:
            yield file.dump()
