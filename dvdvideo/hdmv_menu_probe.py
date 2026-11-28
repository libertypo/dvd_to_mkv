"""Interactively observe HDMV playlist selections using VLC and libbluray."""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from dvdvideo.bluray_disc_type import detect_disc_type, read_disc_metadata
from dvdvideo.bluray_inventory import MAX_PLAYLIST_BYTES, parse_playlist

PLAYLIST_SELECTION = re.compile(
    r"(?:^|[/\\])bluray\.c:\d+:\s+Title\s+([0-9]{5})\.mpls selected\b"
)
LIBBLURAY_DEBUG_MASK = str(0x40 | 0x800)


def playlist_duration(source: Path, number: int) -> str:
    data = read_disc_metadata(
        source, f"BDMV/PLAYLIST/{number:05}.mpls", MAX_PLAYLIST_BYTES
    )
    ticks = parse_playlist(data, number)["duration_45khz"]
    milliseconds = (ticks * 1000 + 22500) // 45000
    seconds, milliseconds = divmod(milliseconds, 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"


def trace_player(
    source: Path,
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
) -> int:
    print(
        "Navigate the disc menu in VLC. Close VLC when finished.", flush=True
    )
    print(
        "Selections include startup and menu playlists, not just features.",
        flush=True,
    )
    process = subprocess.Popen(
        command,
        env=environment,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        bufsize=1,
    )
    observed = False
    durations: dict[int, str | None] = {}
    try:
        assert process.stderr is not None
        with process.stderr:
            for line in process.stderr:
                match = PLAYLIST_SELECTION.search(line)
                if match:
                    observed = True
                    number = int(match.group(1))
                    if number not in durations:
                        try:
                            durations[number] = playlist_duration(
                                source, number
                            )
                        except (OSError, ValueError) as error:
                            durations[number] = None
                            print(
                                f"Could not read playlist {number:05}"
                                f" duration: {error}",
                                file=sys.stderr,
                            )
                    duration = durations[number]
                    detail = (
                        f"duration {duration}"
                        if duration is not None
                        else "duration unavailable"
                    )
                    print(
                        f"Playlist {number:05} selected by libbluray"
                        f" ({detail}).",
                        flush=True,
                    )
                else:
                    sys.stderr.write(line)
        status = process.wait()
    except KeyboardInterrupt:
        print("\nMenu test cancelled.", file=sys.stderr)
        return 130
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    if status != 0:
        print(f"Player process exited with status {status}.", file=sys.stderr)
        return 1
    if not observed:
        print(
            "No playlist selections were observed; check the VLC diagnostics.",
            file=sys.stderr,
        )
        return 1
    if any(duration is None for duration in durations.values()):
        return 1
    return 0


def run_probe(source: Path) -> int:
    source = source.expanduser().resolve(strict=True)
    if not (source.is_file() or source.is_dir()):
        raise ValueError(
            "The source must be a Blu-ray ISO image or mounted disc directory."
        )
    if detect_disc_type(source) != "HDMV":
        raise ValueError(
            "This test supports HDMV-only discs, not BD-J or mixed discs."
        )
    player = shutil.which("vlc")
    if player is None:
        raise RuntimeError(
            "This test requires VLC with libbluray and Blu-ray menu support."
        )

    with tempfile.TemporaryDirectory(prefix="hdmv-menu-probe-") as directory:
        environment = os.environ.copy()
        environment.update({
            "HOME": directory,
            "XDG_CONFIG_HOME": directory,
            "XDG_CACHE_HOME": directory,
            "XDG_DATA_HOME": directory,
            "BD_DEBUG_MASK": LIBBLURAY_DEBUG_MASK,
        })
        environment.pop("BD_DEBUG_FILE", None)
        uri = "bluray://" + quote(str(source), safe="/")
        command = [
            player,
            "--ignore-config",
            "--no-one-instance",
            "--no-one-instance-when-started-from-file",
            "--no-qt-privacy-ask",
            "--no-metadata-network-access",
            "--bluray-menu",
            "--play-and-exit",
            "--intf",
            "qt",
            "--",
            uri,
        ]
        return trace_player(source, command, environment=environment)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source",
        type=Path,
        help="HDMV-only Blu-ray ISO image or mounted disc directory",
    )
    args = parser.parse_args()
    try:
        return run_probe(args.source)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"HDMV menu test failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
