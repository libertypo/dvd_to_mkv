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

## Pick your tracks once

Save a profile with your favourite audio and subtitle languages, Blu-ray
output format, and minimum playlist length.

The app also remembers your choices for each source, so opening the same
disc again doesn't mean starting from scratch. You'll still need to choose
where to save the copy.

New lists start with nothing selected unless you have saved choices or your
profile picks matching tracks. Use **Select All** or **Select All Tracks**
when you want everything.

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
- Protected Blu-rays may need a local key database. Put `KEYDB.cfg` in
  `~/.config/aacs/` and keep it private: folder permissions `700`, file
  permissions `600`.
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
