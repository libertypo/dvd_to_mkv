"""Identify MVC payload in a bounded sample of a Blu-ray transport stream."""

import collections
import re
import sys

SAMPLE_BYTES = 16 * 1024 * 1024


def _has_mvc(body: bytearray) -> bool:
    units = [
        match[1] for match in re.finditer(rb"(?=\x00\x00\x01(.{4}))", body, re.DOTALL)
    ]
    subset_sps = any(
        not unit[0] & 128 and unit[0] & 31 == 15 and unit[1] in (118, 128)
        for unit in units
    )
    mvc_slice = any(
        not unit[0] & 128 and unit[0] & 31 == 20 and not unit[1] & 128
        for unit in units
    )
    return subset_sps and mvc_slice


def mvc_pids(data: bytes) -> list[int]:
    if len(data) < 192:
        raise ValueError("Blu-ray probe returned no complete M2TS packets.")
    payloads: dict[int, bytearray] = collections.defaultdict(bytearray)
    headers: dict[int, bytearray] = {}
    video_pids: set[int] = set()
    verified_pids: set[int] = set()
    previous_packets: dict[int, tuple[int, bytes]] = {}
    for offset in range(0, len(data) - 191, 192):
        packet = data[offset + 4:offset + 192]
        if packet[0] != 0x47:
            raise ValueError("Blu-ray probe returned invalid M2TS packet alignment.")
        if packet[1] & 0x80:
            raise ValueError("Blu-ray probe returned a transport-error packet.")
        if packet[3] & 0xC0:
            raise ValueError("Blu-ray probe returned a scrambled transport packet.")
        pid = ((packet[1] & 31) << 8) | packet[2]
        if pid == 0x1FFF:
            continue
        control = (packet[3] >> 4) & 3
        start = 4 if control == 1 else 5 + packet[4]
        if control in (2, 3) and start > len(packet):
            raise ValueError("Blu-ray probe returned an invalid adaptation field.")
        discontinuity = control in (2, 3) and packet[4] > 0 and bool(packet[5] & 0x80)
        if discontinuity:
            completed = payloads.pop(pid, None)
            if completed is not None and _has_mvc(completed):
                verified_pids.add(pid)
            previous_packets.pop(pid, None)
            headers.pop(pid, None)
            video_pids.discard(pid)
        if control not in (1, 3):
            continue
        counter = packet[3] & 15
        previous = previous_packets.get(pid)
        if previous is not None:
            if previous == (counter, packet):
                continue
            if counter != (previous[0] + 1) % 16:
                raise ValueError(f"Blu-ray probe returned a continuity-counter gap on PID 0x{pid:04x}.")
        previous_packets[pid] = (counter, packet)
        body = packet[start:]
        if packet[1] & 64:
            headers[pid] = bytearray()
            video_pids.discard(pid)
        if pid in headers:
            header = headers[pid]
            header.extend(body)
            if len(header) < 4:
                continue
            if not header.startswith(b"\x00\x00\x01") or not 0xE0 <= header[3] <= 0xEF:
                del headers[pid]
                continue
            if len(header) < 9 or len(header) < 9 + header[8]:
                continue
            video_pids.add(pid)
            body = bytes(header[9 + header[8]:])
            del headers[pid]
        if pid in video_pids:
            payloads[pid].extend(body)
    for pid, body in payloads.items():
        if _has_mvc(body):
            verified_pids.add(pid)
    return sorted(verified_pids)


if __name__ == "__main__":
    try:
        for pid in mvc_pids(sys.stdin.buffer.read(SAMPLE_BYTES)):
            print(f"0x{pid:04x}")
    except (OSError, ValueError) as error:
        print(f"MVC probe failed: {error}", file=sys.stderr)
        sys.exit(1)
