# DVD and Blu-ray to MKV

Copy the bits you want from a DVD or Blu-ray into a video file, without
re-encoding. Pick the movie, audio, and subtitles, and leave the rest behind.

## What you'll need

This runs on Linux. Before starting, make sure you have:

- **Bash, FFmpeg and FFprobe** for copying and checking video.
- **Python 3, PyGObject and GTK 4** for the windowed app.
- **For DVDs:** an FFmpeg build with the `dvdvideo` input format.
  Not every packaged version includes it.
- **For Blu-rays:** `bluray_info`, `bluray_copy` and `jq`.
- **For automatic 3D detection:** FFprobe with Blu-ray protocol support.
- **For automatic disc detection:** `7z` or `7zz`, plus `prlimit` from
  util-linux. Without these, choose DVD or Blu-ray yourself.

Linux's `/proc` filesystem also needs to be available for safe copying.
Package names vary between distributions.

## Get started

Keep the included `dvdvideo/` folder beside the scripts, then run:

```sh
./dvd-to-mkv-gui.py
```

1. Choose your disc image, drive, or disc folder.
2. Let the app detect DVD or Blu-ray, or choose it yourself.
3. Inspect the disc and pick what to copy.
4. Choose where to save it and start copying.

The Activity pane tells you what's happening and shows any errors.
Existing output files won't be overwritten.

**Optical drives:** The app does not automatically detect when the tray is
opened or the disc is changed, so the displayed selections may no longer
match the disc. After opening the tray or changing discs, use **Drives >
Refresh drives**, select the drive again or choose the media type manually,
then inspect the disc again and review your selections before previewing or
copying. Do not change discs while an operation is in progress.

## Pick your tracks once

Save a profile with your favourite audio and subtitle languages, Blu-ray
output format, and minimum playlist length.

The app also remembers your choices for each source, so opening the same
disc again doesn't mean starting from scratch. You'll still need to choose
where to save the copy.

New lists start with nothing selected unless you have saved choices or your
profile picks matching tracks. Use **Select All** or **Select All Tracks**
when you want everything.

## Copy several DVD titles

For an episode disc, choose a profile with audio or subtitle languages and
an output MKV filename, then select **Batch Titles**. Title discovery requires
`lsdvd`. Choose the titles and select **Prepare Batch**; this only prepares
them. Select **Copy Selected** to review the tracks and output paths, then
**Start Copy** to begin. Cancelling the confirmation keeps the batch.

Titles are inspected separately and copied sequentially. Titles that fail
are reported; the others continue. Cancelling stops the batch, keeps completed
copies and leaves uncompleted prepared titles available for another attempt.
When the copier confirms that a title was published before cancellation during
cleanup, it is counted as completed; a forced stop can leave publication
uncertain. Existing files are never overwritten.

Space estimates are per title, not for the entire batch, and are checked
again before each copy. **Clear Batch** returns to single-title copying.
Changing the source, output filename or profile requires preparing the batch
again or clearing it. Refreshing an optical drive invalidates its batch; disc
changes are not detected automatically, so refresh after changing discs and
select the drive again or choose the media type manually.

## MKV or M2TS?

For regular 2D video, MKV is the usual choice. Blu-rays offer three options:

- **Automatic:** uses MKV normally and keeps original M2TS when it detects
  Blu-ray 3D video.
- **MKV:** lets you choose which tracks to keep.
- **Original M2TS:** keeps all tracks, including both views of a 3D movie.
  You can't leave individual tracks out in this mode.

For now, 3D stays in M2TS while we wait for FFmpeg/mkvmerge to catch up with
reliable Blu-ray 3D copying to MKV in this workflow. Choosing MKV yourself
doesn't add 3D support.

Automatic detection only checks a sample. If you know a playlist is 3D but
it isn't detected, choose **Original M2TS** yourself. You'll need a player
that supports Blu-ray 3D; full-movie 3D playback hasn't been verified here.

## Chapters come along too

Blu-ray MKVs keep the playlist's chapter markers. M2TS copies get a separate
chapter file beside the video instead. Keep the two together, but bear in
mind that not every player reads that file, and seeking may be off on
playlists made from several clips.

## A few things worth knowing

- The suggested main movie is a hint, not a promise. The longest playlist
  can sometimes be a menu.
- Protected Blu-rays may need a local AACS key database. If you have **lawfully
  obtained one**, you can place it at `~/.config/aacs/KEYDB.cfg` (or
  `~/.config/aacs/keydb.cfg`), or specify its path with `--keydb`. Keep it
  private: recommended folder permissions are `700` and file permissions
  are `600`. Lawful possession alone does not authorize decryption or copying;
  see the [legal notice](./LEGAL-NOTICE.md).
- Save into a folder you own. Unsafe shared output folders are refused.
- If the app rejects a shortcut to a disc image or folder, choose the real
  file or folder instead. Drive links such as `/dev/cdrom` are fine.

## Prefer the terminal?

Start with:

```sh
./dvd-to-mkv.sh --help
```

## More details

- [What's new](./WHATSNEW.md)

## Standards and primary references

These are useful places to look when working on the disc formats, APIs, or
system features the app uses. They're references, not a claim that the app
supports every part of every specification.

### DVD, UDF, and media formats

- [ECMA-167](https://ecma-international.org/publications-and-standards/standards/ecma-167/)
  explains the volume and file structures behind UDF. Read it alongside the
  UDF version you're working with.
- [ECMA TR/112: Universal Disk Format](https://ecma-international.org/publications-and-standards/technical-reports/ecma-tr-112/)
  is the Ecma reference for UDF. Part 1 covers the general format, and Part 7
  covers UDF 1.02. Check the relevant part before claiming support for another
  version. The repository also includes a UDF reader in `dvdvideo/udf/`: it
  handles a bounded subset and explicitly rejects unsupported allocation forms.
  The MKV app itself relies on external tools to read disc filesystems.
- [ECMA-267](https://ecma-international.org/publications-and-standards/standards/ecma-267/)
  explains the physical and data format of 120 mm DVD-ROM discs. For video
  menus and navigation, you'll need the separate DVD-Video specification.
- **DVD-Video application layer:** DVD Forum, *DVD Specifications for
  Read-Only Disc, Part 3: Video Specifications*. This covers IFO/VOB
  navigation and DVD-Video structures, but access is controlled. ECMA-167
  and ECMA-267 aren't substitutes for it.
- **Blu-ray Disc formats:** the relevant Blu-ray Disc Association
  specifications are licensed. Consult them before making claims about
  BD-ROM, playlists, transport streams, or MVC conformance. The app relies on
  `bluray_info`, `bluray_copy`, and FFmpeg for inspection and copying; its MVC
  probe checks a limited sample and set of NAL units, not the whole disc.
- [ISO/IEC 13818-1:2025 (MPEG-2 Systems)](https://www.iso.org/standard/91403.html)
  covers the MPEG transport and program streams used on DVDs and Blu-rays.
  For AVC/MVC bitstreams, see
  [ISO/IEC 14496-10:2025](https://www.iso.org/standard/87574.html). The app
  relies on FFmpeg rather than implementing full decoders itself.

### GUI, runtime, and data interchange

- [GTK 4 API reference](https://docs.gtk.org/gtk4/) and [Gtk.Accessible.announce](https://docs.gtk.org/gtk4/method.Accessible.announce.html)
  cover the widgets and accessibility APIs. `announce()` arrived in GTK 4.14,
  so keep older supported GTK versions in mind when using it.
- [GLib main event loop](https://docs.gtk.org/glib/main-loop.html) and
  [GMainContext](https://docs.gtk.org/glib/struct.MainContext.html) explain
  how events, callbacks, and contexts work. They're useful when changing
  GTK-thread dispatch, timers, or worker completion notifications.
- For Python typing, see [PEP 484](https://peps.python.org/pep-0484/)
  (type hints), [PEP 544](https://peps.python.org/pep-0544/)
  (protocols/structural subtyping), and
  [PEP 695](https://peps.python.org/pep-0695/) (type-parameter syntax).
  These help with type checking; they don't validate data at runtime.
- [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259) describes JSON, which the
  app uses for preferences and messages between helper processes.
- [RFC 3629](https://www.rfc-editor.org/rfc/rfc3629) describes UTF-8, used for
  JSON and for paths or messages explicitly encoded as UTF-8. UDF compressed
  Unicode identifiers follow ECMA TR/112 instead.
- [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir/latest/)
  explains `$XDG_CONFIG_HOME`, which the GUI uses to find its preferences.

### Linux and filesystem behavior

- [POSIX.1-2024 / The Open Group Base Specifications Issue 8](https://pubs.opengroup.org/onlinepubs/9799919799/)
  covers portable file and process interfaces used where the app relies on
  POSIX behavior.
- For Linux-specific features, see the
  [Linux man-pages](https://man7.org/linux/man-pages/), including `/proc`
  descriptor paths, `memfd_create`, block-device ioctls, and `fsync`. Output
  pinning, block-device sizing, and subprocess argument spooling deliberately
  rely on Linux rather than POSIX alone.
