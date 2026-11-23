"""Synthetic copier tools for the GUI batch cancellation integration test."""

import os
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("dvd-to-mkv.sh")


class CopyFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.device = self.root / "disc.iso"
        self.device.write_bytes(b"disc fixture")
        self.env = dict(os.environ, HOME=str(self.root), PATH=f"{self.bin}:{os.environ['PATH']}")

    def add_tool(self, name, contents):
        tool = self.bin / name
        tool.write_text(contents)
        tool.chmod(0o755)

    def add_recording_copy_tools(self):
        self.add_tool("ffmpeg", """#!/usr/bin/env python3
import pathlib, sys
if sys.argv[1:2] == ["-hide_banner"]:
    print(" D  dvdvideo        DVD-Video")
    raise SystemExit
pathlib.Path(sys.argv[-1]).write_bytes(b"mkv")
""")
        self.add_tool("ffprobe", """#!/usr/bin/env python3
import json, sys
if "-show_entries" in sys.argv and sys.argv[sys.argv.index("-show_entries") + 1] == "stream=id,codec_type":
    print(json.dumps({"streams": [{"id": "0x1011", "codec_type": "video"}]}))
""")
