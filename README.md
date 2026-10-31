# DVD and Blu-ray to MKV

Scripts for selecting and remuxing DVD streams or Blu-ray playlists to MKV.

## Requirements

- Linux
- Bash, FFmpeg and FFprobe
- For DVD input, FFmpeg built with the `dvdvideo` input format
- For Blu-ray input, `bluray_info`, `bluray_copy` and `jq`
- For the graphical interface, Python with GTK 4 bindings and
  `dvdvideo.gui_support`
- For automatic detection of image files or devices, `7z` or `7zz` and
  `prlimit` (util-linux), which caps the probe's virtual address space at
  512 MiB (this limits allocations, not resident RAM); without
  them, choose DVD or Blu-ray manually

Protected Blu-ray discs may require a local AACS key database. Keep it private.

## Graphical interface

Run from the directory containing both scripts:

```sh
./dvd-to-mkv-gui.py
```

Enter or browse to a disc image, device, or disc folder. Choose DVD or Blu-ray,
inspect the available streams or playlists, select what to include, and choose
an output path. The Activity pane reports progress and errors.

Media detection is debounced while editing the source. Pending detection for
an obsolete source or a manually selected media mode is discarded. Disc folders
and images are recognized by `BDMV/index.bdmv` or `VIDEO_TS/VIDEO_TS.IFO`,
matched case-insensitively.

The GUI rejects sources, disc folders, and marker files that are symbolic
links; choose the real path instead. Symbolic links to optical devices, such
as `/dev/cdrom`, are accepted. The command-line script still follows links.

Inspection output is treated as untrusted: playlist and stream identifiers
must be short ASCII numbers, lists are capped at 2000 playlists or DVD streams
and 512 Blu-ray streams, and control or bidirectional-override characters are
removed from labels, Activity output, and suggested filenames.

Closing cancels active operations and waits for background workers to stop
their subprocesses before exiting. A failed stream inspection keeps the
previous track selection. The Activity pane follows new output only while it
is scrolled to the bottom. It keeps at most the last 512K characters and breaks
lines longer than 1024 characters, so a flood of output without newlines cannot
exhaust memory.

## Command line

List DVD streams and copy selected streams:

```sh
./dvd-to-mkv.sh --list-streams INPUT
./dvd-to-mkv.sh --stream INDEX --stream INDEX INPUT OUTPUT
```

List Blu-ray playlists or tracks and copy a playlist:

```sh
./dvd-to-mkv.sh --bluray --device DEVICE --list-playlists
./dvd-to-mkv.sh --bluray --device DEVICE --playlist ID --list-streams
./dvd-to-mkv.sh --bluray --device DEVICE --playlist ID OUTPUT
```

Use `--stream-id ID:PID` to select Blu-ray tracks; PIDs range from `0x0000`
to `0x1fff`. Stream indexes (0–9999), playlist IDs (0–99999) and
`--retries` (0–9999) are read as decimal numbers, so `01` is the same as `1`.
Multiple playlists can be
selected by repeating `--playlist`; provide an existing output directory when
copying more than one. Options go before positional arguments; use `--` before
paths that begin with a dash, which are then treated as relative files (for
example `-disc` becomes `./-disc`). A `--device` value beginning with a dash is
rejected; write it as `./-disc`. Run either script with `--help` for its
full usage.

## Output safety

Existing output files are not overwritten. Copies are prepared in temporary
files and published only after validation.
