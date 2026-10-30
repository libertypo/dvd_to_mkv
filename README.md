**Better projects exist than this one. This script exists only to suit the needs of a single user**

# Stream-copy to MKV

`dvd-to-mkv.sh` remuxes selected DVD streams with FFmpeg or selected Blu-ray
playlists with `bluray_copy`. DVD support requires an `ffmpeg` build with the
`dvdvideo` input format; `libdvdnav` alone is not sufficient. Listing Blu-ray playlists requires
`bluray_info` and `jq`; copying requires `bluray_copy` and `ffmpeg` to remux
the copied M2TS playlist to MKV. A protected disc requires an AACS `KEYDB.cfg`
file. Keep a local key database private, for example with `chmod 600`.

## Usage synopsis

```
dvd-to-mkv.sh --list-streams INPUT [TITLE_OR_PLAYLIST]
dvd-to-mkv.sh [--stream INDEX]... INPUT OUTPUT [TITLE_OR_PLAYLIST]
dvd-to-mkv.sh --bluray [--device DEVICE] [--keydb FILE] [--minlength MINUTES] --list-playlists
dvd-to-mkv.sh --bluray [--device DEVICE] [--keydb FILE] --playlist ID --list-streams
dvd-to-mkv.sh --bluray [--device DEVICE] [--keydb FILE] [--playlist ID]... [--stream-id ID:PID]... [OUTPUT]
```

**Options must come before the positional arguments** (`INPUT`, `OUTPUT`,
`TITLE_OR_PLAYLIST`). For example `dvd-to-mkv.sh backup.iso --list-streams`
does *not* work; write `dvd-to-mkv.sh --list-streams backup.iso` instead.

## DVD walkthrough

1. List the streams on a title (defaults to title `1`):

   ```
   bash dvd-to-mkv.sh --list-streams backup.iso
   ```

   This prints an aligned table, one row per stream:

   ```
   INDEX  TYPE      CODEC         PROFILE  CH  LAYOUT     LANG  TITLE
   0      video     mpeg2video
   1      audio     ac3               6   5.1(side)  eng
   2      audio     ac3               2   stereo     ger
   4      subtitle  dvd_subtitle                     eng
   ```

   - `INDEX` is the number to pass to `--stream` below.
   - `TYPE`/`CODEC` identify the kind of stream and its codec.
   - `CH`/`LAYOUT` only apply to audio streams (channel count and layout).
   - `LANG` is the stream's declared language tag, when present.
   - `PROFILE`/`TITLE` are blank when ffprobe has nothing useful to report
     (DVD streams almost never set a codec profile).

2. Pick the streams you want to keep and pass each one with a separate
   `--stream INDEX`. Both `INPUT` and `OUTPUT` are required and always come
   after all `--stream` options:

   ```
   bash dvd-to-mkv.sh --stream 0 --stream 1 --stream 4 backup.iso movie.mkv
   ```

   If `--stream` is omitted entirely, every stream is copied (`-map 0 -c copy`).

3. To remux a different title, add the title number at the end:

   ```
   bash dvd-to-mkv.sh --stream 0 --stream 1 backup.iso movie.mkv 2
   ```

DVD titles default to `1`. The script overwrites only its own private
temporary file while working and refuses to overwrite an existing final
output file.

## Blu-ray walkthrough

For Blu-ray, first list playlists at or above a minimum duration, then select
one or more IDs. `--device` defaults to `/dev/sr0`; `--keydb` defaults to
`~/.config/aacs/KEYDB.cfg` or `~/.config/aacs/keydb.cfg` when either exists.
Listing prints the playlist ID, duration reported by `bluray_info`, and chapter
count. `--device` may also name a Blu-ray image or BDMV directory.

```
bash dvd-to-mkv.sh --bluray --device /dev/sr0 --minlength 20 --list-playlists
bash dvd-to-mkv.sh --bluray --device /path/to/disc/BDMV --list-playlists
bash dvd-to-mkv.sh --bluray --device /path/to/disc --playlist 0 movie.mkv
bash dvd-to-mkv.sh --bluray --playlist 1 --playlist 5 /path/to/output-directory
```

Each completed Blu-ray output is printed. Results are written to temporary files
and renamed only after a successful copy. If a later playlist fails, rerun the
command with only the remaining playlist IDs; existing outputs are protected.
`--stream` is DVD-only and cannot be combined with `--bluray`.

To select individual Blu-ray tracks, inspect one playlist's streams, then pass
its stream PIDs with `--stream-id PLAYLIST:PID`:

```
bash dvd-to-mkv.sh --bluray --device disc.iso --playlist 0 --list-streams
bash dvd-to-mkv.sh --bluray --device disc.iso --playlist 0 --stream-id 0:0x1011 --stream-id 0:0x1100 movie.mkv
```

Listing streams requires `bluray_info` and `jq` and reports PID, type, codec,
language, and format when available. PIDs are not DVD-style stream indexes.
Selections apply only to the named playlist; playlists with no stream
selections still copy all tracks. A requested PID missing from the copied
playlist causes remuxing to fail rather than silently copying other tracks.


# GTK interface(work in progress)

On Linux with GTK4 Python bindings installed, run:

```
./dvd-to-mkv-gui.py
```

Choosing or entering an existing source automatically detects DVD versus Blu-ray
from its `VIDEO_TS` or `BDMV` structure and activates the matching mode.
Folders need no extra detection tools; images and device paths require `7zz`
or `7z` (7-Zip with UDF/ISO support). Detection runs in the background without
mounting or extracting the source. Unrecognized or ambiguous sources and probe
errors are reported in Activity; the DVD/Blu-ray buttons remain available for
manual selection, which overrides any pending detection. Optical drives are
not discovered automatically.

Clicking either media button cancels pending automatic detection, including
clicking the already-selected mode. Obsolete image/device probes are stopped
and reaped, not merely ignored.

The GUI runs `dvd-to-mkv.sh`, lets you inspect and select streams or playlists,
and shows the tool output in its activity pane, which automatically scrolls to
the latest output during inspection and extraction. In DVD mode, **Inspect** fills
the **Streams or playlists** pane with selectable stream indexes and their
reported details. Select the streams to keep, choose an output MKV, and click
**Copy Selected**.

In Blu-ray mode, **Inspect** lists playlists. If there is only one playlist,
it is selected automatically and its video, audio, and subtitle tracks are
loaded immediately. With multiple playlists, check exactly one playlist and
click **Inspect Streams** to show its video, audio, and subtitle tracks.
Tracks initially start selected; uncheck those you do not want. Repeat for
other playlists as needed: choices are remembered separately for each playlist.
Finally, check the playlists to copy, choose an output directory, and click
**Copy Selected**. Uninspected playlists copy all tracks; an inspected playlist
must have at least one track selected. Changing the source, media mode, or
refreshing the playlist list clears stored stream choices.

### Suggested filenames

The Blu-ray **Filename** field is filled from the disc name, then the UDF volume
title, falling back to the source file/folder name when neither is available.
It is editable and excludes the `.mkv` extension. Unsafe filename characters
in suggestions are replaced with underscores. User-edited names and output
paths are not replaced by later suggestions.
DVD and Blu-ray output locations are remembered separately while the GUI is
open, so switching media modes does not turn a Blu-ray directory into a DVD
output filename. Each source change resets the status immediately, even when
the new source is empty or does not exist.

For an output directory, one selected playlist is saved as `Sintel.mkv`;
multiple selected playlists use `Sintel-playlist-0.mkv`, etc. An explicit MKV
output path takes precedence; the Filename field is ignored and not validated
when saving to an explicit file. DVD mode suggests an editable output MKV path
based on the source name. Existing files are never overwritten.

The CLI retains its existing default names unless `--output-name NAME` is
provided for Blu-ray copies to a directory:

```
bash dvd-to-mkv.sh --bluray --device disc.iso --playlist 0 --output-name Sintel /path/to/output-directory
```

Playlist listings include a `# disc-name` metadata line when a disc name or
volume title is available; numeric playlist rows keep their existing format.

### Operation safety and diagnostics

- The status indicator shows detection, inspection, copying, completion,
  failure, and cancellation. Source browsing, output settings, media mode,
  and stream selections are locked while a command runs.
  If a background worker cannot start, the error is reported in Activity and
  the controls are restored so the operation can be retried or the window closed.
- **Cancel** stops the operation's process group, giving the script time to run
  its cleanup traps before forcibly stopping unresponsive processes. Previously
  completed MKVs are kept; the script never publishes a partially copied MKV.
  Forced termination can leave private temporary files if cleanup cannot finish.
  Cancelling after a subprocess exits but before its results are displayed
  discards those results and prevents chained stream inspections. It cannot
  undo a copy that has already finished.
- Closing during an operation asks for confirmation, then waits for worker and
  subprocess shutdown. SIGINT/SIGTERM also request coordinated shutdown.
  Repeated shutdown requests are safe and share one completion timer.
- Results are accepted only for the source and mode that launched the operation.
  Starting an inspection invalidates its previous selections; a failed refresh
  cannot silently reuse old stream choices.
- Tool output uses tolerant UTF-8 decoding. Invalid bytes are replaced rather
  than breaking Activity reporting. Nonzero exit statuses and worker failures
  are reported explicitly. Standard output and diagnostics are read separately:
  both appear in Activity, but only standard output is used for inspection
  results. Remaining processes in the command's process group are stopped even
  when the parent command has already exited successfully.
  Blu-ray stream PIDs are validated and normalized; malformed and duplicate
  records are omitted with an Activity notice rather than producing invalid
  copy arguments. Positional paths are separated from command options so
  manually entered relative filenames beginning with `-` work as paths.
- Activity updates are batched every 100 ms. Its visible text is capped at
  512 Ki characters, with at most 256 Ki characters pending. If output arrives
  faster than it can be displayed, Activity reports how much older text was
  omitted. Copies do not keep a second complete output transcript in memory;
  inspection results are limited to 8 MiB. Activity has undo disabled so deleted
  log text is not retained in an invisible undo history.
- Suggested stems are capped at 160 UTF-8 bytes. Final names, including playlist
  suffixes and `.mkv`, are checked against the destination filesystem's filename
  limit before copying, along with existing-output checks.
  Filesystem detection and output preflight checks run on worker threads and
  check cancellation before launching a copy. Cancellation cannot interrupt a
  filesystem operation already blocked inside the OS; the GUI remains responsive
  and waits for that call to return before closing.
- File browsing uses `Gtk.FileChooserNative`, without requiring GTK 4.10's
  `Gtk.FileDialog`. Source and output paths are used literally, including
  leading or trailing whitespace in filenames. Only an empty field is treated
  as unset. Browse dialogs start from the current file or folder; Save uses
  the displayed output's parent directory and filename. Initialization failures
  are reported in Activity while leaving the chooser available.
  Cancellation is quiet; unexpected responses and backend
  errors are reported in Activity. Form labels have associated keyboard
  mnemonics: Alt+S for Source, Alt+O for Output, Alt+F for Filename, and Alt+M
  for Minimum minutes. Inspect, Copy and Cancel use Alt+I, Alt+C and Alt+A.
  Lists have accessible labels/descriptions; status changes are announced when
  the installed GTK version supports accessibility announcements.
- The default window is 720 pixels tall to fit a 768-pixel-high display with
  room for window decorations. Resizable dividers separate playlist,
  stream-selection, and Activity panes, with minimum usable viewport heights.
  Compact form spacing also accommodates larger text such as Sans 16.
  File chooser responses are discarded after source/mode changes, newer output
  edits, or an operation starting, rather than overwriting newer choices.

Process supervision and filesystem output validation are kept in the
GTK-independent `dvdvideo/gui_support.py` module so they can be tested without
constructing a window.

Run the focused regression suite, including actual GTK widgets and managed
subprocess cancellation, on a machine with GTK4 bindings:

```
xvfb-run -a /usr/bin/python -m unittest test_dvd_to_mkv_gui test_dvd_to_mkv_cli test_dvd_to_mkv_lifecycle
```
