"""Thread-safe, bounded Activity output buffering independent of GTK."""

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

import math
import re
import threading
import time
from collections import deque

MAX_PENDING_CHARS = 256 * 1024
MAX_FLUSH_CHARS = 16 * 1024
# GTK lays out each line as a whole; unbroken output would grow memory
# without bound.
MAX_LOG_LINE_CHARS = 1024
# C0/C1 controls (except tab and newline) and bidirectional overrides can
# disguise external text.
UNSAFE_TEXT = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e"
    "\u2066-\u2069\ud800-\udfff]"
)
MEDIA_TIME = r"(\d{1,6}):([0-5]\d):([0-5]\d(?:\.\d+)?)"
DURATION = re.compile(r"^\s*Duration: " + MEDIA_TIME)
POSITION = re.compile(r"\btime=" + MEDIA_TIME)
SPEED = re.compile(r"\bspeed=\s*(\d+(?:\.\d+)?)x")
BLURAY_PROGRESS = re.compile(
    r"^Progress:\s*\d+(?:\.\d+)?/\d+(?:\.\d+)? MBs \((\d+(?:\.\d+)?)%\)$"
)
BLURAY_PHASE = re.compile(r"^Playlist \d+: (extracting|extraction complete)\b")


def media_seconds(match: re.Match[str]) -> float:
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


class MediaProgress:
    """Track FFmpeg or Blu-ray extraction, not invented overall progress."""

    def __init__(self) -> None:
        self.pending = ""
        self.duration: float | None = None
        self.position: float | None = None
        self.speed: float | None = None
        self.started = time.monotonic()
        self.bluray = False
        self.baseline = 0.0

    def feed(self, text: str) -> None:
        for part in text.replace("\r", "\n").splitlines(keepends=True):
            self.pending = (self.pending + part)[-4096:]
            if not self.pending.endswith("\n"):
                continue
            line = self.pending.strip()
            self.pending = ""
            if line.startswith(
                ("Input #0,", "Comparing ", "Retrying ", "Copying DVD title ")
            ) or BLURAY_PHASE.match(line):
                self.duration = self.position = self.speed = None
                self.started = time.monotonic()
                self.bluray = False
            elif match := BLURAY_PROGRESS.fullmatch(line):
                position = float(match.group(1))
                if position > 100:
                    continue
                if not self.bluray or (
                    self.position is not None and position < self.position
                ):
                    self.started = time.monotonic()
                    self.baseline = position
                self.bluray = True
                self.duration = 100
                self.position = position
                self.speed = None
            elif match := DURATION.match(line):
                if self.duration is None:
                    self.duration = media_seconds(match) or None
            elif line.startswith(("frame=", "size=")) and (
                match := POSITION.search(line)
            ):
                self.position = media_seconds(match)
                speed = SPEED.search(line)
                self.speed = float(speed.group(1)) if speed else None
            elif line.startswith("video:"):
                self.duration = self.position = self.speed = None

    def snapshot(self) -> tuple[float, int | None] | None:
        if self.duration is None or self.position is None:
            return None
        fraction = min(self.position / self.duration, 1.0)
        speed = self.speed
        elapsed = time.monotonic() - self.started
        if speed is None and elapsed >= 1:
            speed = (
                self.position - self.baseline if self.bluray else self.position
            ) / elapsed
        remaining = (
            max(0, self.duration - self.position) / speed
            if speed and speed > 0 and math.isfinite(speed) else None
        )
        eta = (
            round(remaining)
            if remaining is not None and math.isfinite(remaining)
            else None
        )
        return fraction, eta


def clean_text(text: str) -> str:
    return UNSAFE_TEXT.sub("", text)


class ActivityBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: deque[str] = deque()
        self._pending_chars = 0
        self._dropped_chars = 0
        self._column = 0
        self._progress: MediaProgress | None = None

    def start_progress(self) -> None:
        with self._lock:
            self._progress = MediaProgress()

    def stop_progress(self) -> None:
        with self._lock:
            self._progress = None

    def progress(self) -> tuple[float, int | None] | None:
        with self._lock:
            return (
                self._progress.snapshot()
                if self._progress is not None
                else None
            )

    def _break_long_lines(self, text: str) -> str:
        pieces: list[str] = []
        for index, part in enumerate(text.split("\n")):
            if index:
                pieces.append("\n")
                self._column = 0
            start = 0
            while self._column + len(part) - start > MAX_LOG_LINE_CHARS:
                room = MAX_LOG_LINE_CHARS - self._column
                pieces += [part[start:start + room], "\n"]
                start += room
                self._column = 0
            pieces.append(part[start:])
            self._column += len(part) - start
        return "".join(pieces)

    def queue(self, text: str) -> None:
        dropped = max(0, len(text) - MAX_PENDING_CHARS)
        text = clean_text(text[-MAX_PENDING_CHARS:].replace("\r", "\n"))
        if not text and not dropped:
            return
        with self._lock:
            if self._progress is not None:
                self._progress.feed(text)
            self._dropped_chars += dropped
            if not text:
                return
            text = self._break_long_lines(text)
            if len(text) > MAX_PENDING_CHARS:
                self._dropped_chars += len(text) - MAX_PENDING_CHARS
                text = text[-MAX_PENDING_CHARS:]
            self._pending.append(text)
            self._pending_chars += len(text)
            while self._pending_chars > MAX_PENDING_CHARS:
                removed = self._pending.popleft()
                self._pending_chars -= len(removed)
                self._dropped_chars += len(removed)

    def drain(self) -> tuple[str, int]:
        with self._lock:
            text = "".join(self._pending)
            dropped = self._dropped_chars
            self._pending.clear()
            self._pending_chars = 0
            self._dropped_chars = 0
        if len(text) > MAX_FLUSH_CHARS:
            dropped += len(text) - MAX_FLUSH_CHARS
            text = text[-MAX_FLUSH_CHARS:]
        return text, dropped

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()
            self._pending_chars = 0
            self._dropped_chars = 0
            self._column = 0
