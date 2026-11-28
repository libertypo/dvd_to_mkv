"""Interactively investigate BD-J menus in an isolated VLC session."""

import argparse
import os
import shutil
import sys
from pathlib import Path

from dvdvideo.bluray_disc_type import detect_disc_type
from dvdvideo.hdmv_menu_probe import LIBBLURAY_DEBUG_MASK, trace_player

JAVA_HOME = Path("/usr/lib/jvm/java-17-openjdk")
JAVA_OPTIONS = (
    "-Xmx512m -XX:MaxMetaspaceSize=256m -XX:ReservedCodeCacheSize=128m "
    "-XX:CompressedClassSpaceSize=128m -XX:ActiveProcessorCount=2"
)


def sandbox_command(
    source: Path, display_socket: Path, player: str, bubblewrap: str
) -> list[str]:
    runtime = f"/run/user/{os.getuid()}"
    sandbox_source = "/disc/source.iso" if source.is_file() else "/disc/source"
    java_config = (JAVA_HOME / "conf").resolve(strict=True)
    if not (
        java_config.is_relative_to(JAVA_HOME)
        or java_config.is_relative_to("/usr")
        or java_config.is_relative_to("/etc")
    ):
        raise RuntimeError(
            "The Java configuration must be installed under /usr or /etc."
        )
    java_mount = (
        ["--ro-bind", str(java_config), str(java_config)]
        if java_config.is_relative_to("/etc")
        else []
    )
    command = [
        bubblewrap,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib",
        "/lib64",
        "--dir",
        "/etc",
        "--ro-bind-try",
        "/etc/ld.so.cache",
        "/etc/ld.so.cache",
        "--ro-bind-try",
        "/etc/fonts",
        "/etc/fonts",
        *java_mount,
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--size",
        "268435456",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/tmp/home",
        "--dir",
        "/tmp/home/config",
        "--dir",
        "/tmp/home/cache",
        "--dir",
        "/tmp/home/data",
        "--size",
        "1048576",
        "--tmpfs",
        "/home",
        "--size",
        "1048576",
        "--tmpfs",
        "/run",
        "--dir",
        runtime,
        "--chmod",
        "0700",
        runtime,
        "--ro-bind",
        str(display_socket),
        f"{runtime}/wayland-0",
        "--size",
        "134217728",
        "--tmpfs",
        "/persistent",
        "--size",
        "134217728",
        "--tmpfs",
        "/buda",
        "--dir",
        "/disc",
        "--ro-bind",
        str(source),
        sandbox_source,
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin",
        "--setenv",
        "HOME",
        "/tmp/home",
        "--setenv",
        "XDG_CONFIG_HOME",
        "/tmp/home/config",
        "--setenv",
        "XDG_CACHE_HOME",
        "/tmp/home/cache",
        "--setenv",
        "XDG_DATA_HOME",
        "/tmp/home/data",
        "--setenv",
        "XDG_RUNTIME_DIR",
        runtime,
        "--setenv",
        "WAYLAND_DISPLAY",
        "wayland-0",
        "--setenv",
        "QT_QPA_PLATFORM",
        "wayland",
        "--setenv",
        "JAVA_HOME",
        str(JAVA_HOME),
        "--setenv",
        "JAVA_TOOL_OPTIONS",
        JAVA_OPTIONS,
        "--setenv",
        "BD_DEBUG_MASK",
        LIBBLURAY_DEBUG_MASK,
        "--",
        "/usr/bin/timeout",
        "--signal=TERM",
        "--kill-after=5s",
        "30m",
        "/usr/bin/prlimit",
        "--as=4294967296",
        "--nofile=512",
        "--",
        player,
        "--ignore-config",
        "--no-one-instance",
        "--no-one-instance-when-started-from-file",
        "--no-qt-privacy-ask",
        "--no-metadata-network-access",
        "--no-audio",
        "--vout=wl_shm",
        "--bluray-menu",
        "--play-and-exit",
        "--intf",
        "qt",
        "--",
        "bluray://" + sandbox_source,
    ]
    return command


def run_probe(source: Path) -> int:
    source = source.expanduser().resolve(strict=True)
    if not (source.is_file() or source.is_dir()):
        raise ValueError(
            "The source must be a Blu-ray ISO image or mounted disc directory."
        )
    if detect_disc_type(source) == "HDMV":
        raise ValueError(
            "This test requires a BD-J or mixed disc; use hdmv_menu_probe for"
            " HDMV-only discs."
        )
    bubblewrap = shutil.which("bwrap")
    player = shutil.which("vlc")
    if bubblewrap is None or player is None:
        raise RuntimeError(
            "This test requires bubblewrap and VLC with libbluray menu"
            " support."
        )
    player_path = Path(player).resolve(strict=True)
    if not player_path.is_relative_to("/usr"):
        raise RuntimeError(
            "The isolated test requires VLC installed under /usr."
        )
    if not (JAVA_HOME / "lib/server/libjvm.so").is_file():
        raise RuntimeError(f"This test requires Java 17 at {JAVA_HOME}.")
    if not (JAVA_HOME / "conf/security/java.security").is_file():
        raise RuntimeError(
            f"Java 17 security configuration is missing under {JAVA_HOME}."
        )
    runtime, display = os.environ.get("XDG_RUNTIME_DIR"), os.environ.get(
        "WAYLAND_DISPLAY"
    )
    if (
        not runtime
        or not display
        or Path(display).name != display
        or display in (".", "..")
    ):
        raise RuntimeError(
            "This isolated interactive test requires a Wayland desktop"
            " session."
        )
    display_socket = Path(runtime) / display
    if not display_socket.is_socket():
        raise RuntimeError(
            f"Wayland display socket is unavailable: {display_socket}"
        )
    print(
        "Experimental BD-J test: VLC has a read-only disc, an isolated network"
        " namespace, temporary storage, and a 30-minute limit. Audio is"
        " disabled.",
        flush=True,
    )
    print(
        "The shared Wayland connection is not a complete isolation boundary.",
        flush=True,
    )
    return trace_player(
        source,
        sandbox_command(source, display_socket, str(player_path), bubblewrap),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source",
        type=Path,
        help="BD-J or mixed Blu-ray ISO image or mounted disc directory",
    )
    args = parser.parse_args()
    try:
        return run_probe(args.source)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"BD-J menu test failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
