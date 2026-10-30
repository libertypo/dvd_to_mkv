# DVD and Blu-ray to MKV

Scripts for selecting and remuxing DVD streams or Blu-ray playlists to MKV.

## Requirements

- Linux
- Bash, FFmpeg and FFprobe
- For DVD input, FFmpeg built with the `dvdvideo` input format
- For Blu-ray input, `bluray_info`, `bluray_copy` and `jq`
- For the graphical interface, Python with GTK 4 bindings and
  `dvdvideo.gui_support`
- For automatic detection of image files or devices, `7z` or `7zz`

Protected Blu-ray discs may require a local AACS key database. Keep it private.

## Graphical interface

Run from the directory containing both scripts:

```sh
./dvd-to-mkv-gui.py
```

Enter or browse to a disc image, device, or disc folder. Choose DVD or Blu-ray,
inspect the available streams or playlists, select what to include, and choose
an output path. The Activity pane reports progress and errors.

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

Use `--stream-id ID:PID` to select Blu-ray tracks. Multiple playlists can be
selected by repeating `--playlist`; provide an existing output directory when
copying more than one. Options go before positional arguments. Run either
script with `--help` for its full usage.

## Output safety

Existing output files are not overwritten. Copies are prepared in temporary
files and published only after validation.
