"""Formatting helpers for the command-line DVD inspection reports."""

# JSON schema violations use ValueError, as in Blu-ray inspection helpers.
# ruff: noqa: TRY004

import json
import re
import sys
from typing import cast

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def _clean(value: object) -> str:
    if isinstance(value, str):
        return _CONTROL_CHARS.sub(" ", value)
    return str(value) if type(value) is int else ""


def _json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"expected an FFprobe {label} object")
    # JSON object keys are strings; field values are still unvalidated.
    return cast(dict[str, object], value)


def format_dvd_streams(output: str) -> str:
    """Convert FFprobe stream JSON to the CLI's stable TSV format."""
    data = _json_object(json.loads(output), "metadata")
    streams = data.get("streams")
    if not isinstance(streams, list):
        raise ValueError("expected an FFprobe streams array")
    lines = ["INDEX\tTYPE\tCODEC\tPROFILE\tCH\tLAYOUT\tLANG\tTITLE"]
    seen_indexes: set[int] = set()
    for item in cast(list[object], streams):
        stream = _json_object(item, "stream")
        index = stream.get("index")
        if type(index) is not int or not 0 <= index <= 9999:
            raise ValueError("stream index must be an integer from 0 to 9999")
        if index in seen_indexes:
            raise ValueError(f"duplicate stream index {index}")
        seen_indexes.add(index)
        codec_type = stream.get("codec_type")
        if not isinstance(codec_type, str) or not _clean(codec_type).strip():
            raise ValueError("FFprobe stream is missing a valid codec type")
        tags = _json_object(stream.get("tags", {}), "stream tags")
        profile = stream.get("profile")
        lines.append(
            "\t".join(
                map(
                    _clean,
                    (
                        index,
                        codec_type,
                        stream.get("codec_name"),
                        "" if profile == "unknown" else profile,
                        stream.get("channels"),
                        stream.get("channel_layout"),
                        tags.get("language"),
                        tags.get("title"),
                    ),
                )
            )
        )
    return "\n".join(lines)


def main() -> None:
    try:
        listing = format_dvd_streams(
            sys.stdin.buffer.read().decode("utf-8")
        ).encode("utf-8")
    except ValueError as exc:
        sys.exit(f"Invalid DVD stream metadata: {exc}")
    sys.stdout.buffer.write(listing)


if __name__ == "__main__":
    main()
