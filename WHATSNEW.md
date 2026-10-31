# What's new

## 2026-10-31

### Bug fixes

- **A failed copy is never published.** A `--retries` value with a leading
  zero (such as `08`) used to break the retry check, so a failed or partial
  copy could be saved as the finished MKV with a success exit status.
  `--retries`, `--stream` and `--playlist` are now read as decimal numbers,
  so `010` means 10 and `01` is the same as `1`.
- **Duplicate selections are caught.** `--stream 1 --stream 01` and two
  spellings of the same PID (`0:0x1011` and `0:0x01011`) are reported as
  duplicates instead of mapping one stream twice.
- **Paths starting with a dash work.** A DVD input folder or `--keydb` file
  named like `-disc` is no longer mistaken for an option.
- **GUI detection and workers.** A detection scheduled for a source you have
  already changed no longer starts. Background workers are tracked from the
  moment they are created, so closing the window always waits for them.
- **Stream selection is kept.** If inspecting a playlist's streams fails, the
  previous track selection is kept instead of being cleared.
- **Clearer errors.** A broken symbolic link as the source now says so,
  instead of "Source does not exist." Unexpected errors are logged in Activity
  with a traceback.

### Safety and robustness

- **Media detection** requires the marker files `BDMV/index.bdmv` or
  `VIDEO_TS/VIDEO_TS.IFO` (case-insensitive), not just a folder name.
- **The 7z probe** for images and devices now needs `prlimit` (util-linux),
  which caps its virtual address space at 512 MiB. Without it, choose DVD or
  Blu-ray manually.
- **Symbolic links** are rejected as GUI sources, disc folders and marker
  files. Links to optical devices such as `/dev/cdrom` are still accepted.
- **Disc metadata is treated as untrusted.** Playlist and stream IDs must be
  short ASCII numbers, PIDs must be within `0x0000`–`0x1fff`, lists are
  capped (2000 playlists or DVD streams, 512 Blu-ray streams), labels are
  shortened, and control or bidirectional-override characters are removed.
- **The Activity pane** breaks lines longer than 1024 characters, so output
  without newlines can no longer exhaust memory. It only scrolls to new
  output while you are at the bottom.

### Command-line changes

- `--stream` accepts 0–9999, `--playlist` 0–99999 and `--retries` 0–9999.
- `--stream-id` requires a PID from `0x0000` to `0x1fff`.
