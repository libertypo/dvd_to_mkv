"""
Provides a stream wrapper and progress meter for reporting DVD image-copy
progress.

@copyright: 2009 Bastian Blank <waldi@debian.org>
@license: GNU GPL-3

Modified 2026-11-11 by libertypo: modernized Python 3.14 progress streams
and handled zero totals and out-of-range counts.
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

from typing import TextIO


class ProgressMeter:

    def __init__(
        self, stream: ProgressStream, total: int, count: int = 0
    ) -> None:
        if total < 0:
            raise ValueError('Progress total must be nonnegative')
        self.stream = stream
        self.total = total
        self.count = max(0, min(count, total))

        self.meter_ticks = 60

        stream.set_meter(self)

    def clear(self) -> None:
        self.stream.write_real(chr(27) + '[M' + chr(27) + '[G')

    def display(self) -> None:
        fraction = self.count / self.total if self.total else 1.0
        value = int(fraction * self.meter_ticks)

        meter = [
            '[{}>{}]'.format('-' * value, ' ' * (self.meter_ticks - value))
        ]
        meter.append(f'{self.count:d}/{self.total:d} {int(fraction * 100):d}%')

        self.stream.write_real(' '.join(meter))
        self.stream.flush()

    def set(self, count: int) -> None:
        self.count = max(0, min(count, self.total))
        self.clear()
        self.display()

    def update(self, count: int) -> None:
        self.set(self.count + count)

    def write(self, str: str) -> None:
        self.clear()
        self.stream.write_real(str)
        self.display()


class ProgressStream:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream

        self._meter: ProgressMeter | None = None

    def clear_meter(self) -> None:
        self.set_meter(None)

    def flush(self) -> None:
        self.stream.flush()

    def set_meter(self, meter: ProgressMeter | None) -> None:
        if self._meter:
            self._meter.clear()
        self._meter = meter
        if self._meter:
            self._meter.display()

    def write(self, str: str) -> None:
        if self._meter:
            self._meter.write(str)
        else:
            self.stream.write(str)

    def write_real(self, str: str) -> int:
        return self.stream.write(str)
