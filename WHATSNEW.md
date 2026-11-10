# What's new

## 2026-11-10

Saved profiles, remembered track choices, and better handling of Blu-ray
chapters and 3D discs, along with a good round of fixes.

### Less setting things up from scratch

- Save profiles with your preferred audio and subtitle languages, Blu-ray
  output format, and minimum playlist length.
- The app remembers your profile and track choices for each disc or source,
  so you don't have to pick everything again next time. Output locations
  aren't saved.
- New lists start with nothing selected unless you've saved choices or your
  profile matches tracks by language. Use Select All when you want everything.
- Refreshing a Blu-ray's track list now brings back saved choices that
  were hidden because the list was too long. Tracks you deliberately
  unchecked stay unchecked.

### Blu-ray chapters and 3D

- Blu-ray MKVs keep the playlist's chapter markers, even when you only copy
  some tracks. Original M2TS copies get a separate chapter file alongside
  the video; whether it works depends on your player.
- Choose Automatic, MKV, or original M2TS for Blu-rays. Automatic keeps
  original M2TS when it detects the disc's 3D video, rather than losing
  the extra view. Original M2TS copies include all tracks.
- For now, 3D stays in M2TS while we wait for FFmpeg/mkvmerge to catch up
  with reliable Blu-ray 3D (MVC) remuxing to MKV in this workflow.
- Fixed a case where a normal timing reset on a 3D Blu-ray could make
  Automatic output choose MKV and lose the 3D part. Converting these
  playlists to 3D MKV isn't supported yet.
- Switching profiles now asks again before copying all tracks to original
  M2TS, rather than silently ignoring your track choices.
- That warning also catches tracks hidden by the display limit, even if
  you've selected every track you can see.

### Safer copying

- Fixed a security issue in shared output folders: someone with access to
  such a folder could redirect a copy and cause another file to be overwritten.
  Copying now stays tied to the folder it started in, and folders with unsafe
  ownership or permissions are refused. This needs Linux with `/proc` available.
- Gave the copier and its supporting code another security check. No new
  vulnerabilities were confirmed, and all tests and extra security checks
  passed, including checks with GTK and the Avatar disc image. That isn't a
  guarantee: attacks from a separate user account, other filesystem types,
  and security bugs in external tools weren't tested.

## 2026-10-31

A bit of tidying up and quite a few fixes. No big new features this time.

### Things that should behave better now

- Fixed a nasty one in the command-line script: `--retries 08` could leave
  you with a failed copy saved as if everything had worked. Leading zeros
  are now handled properly, and very long retry values are rejected.
- Selecting the same stream twice using different spellings, such as `1`
  and `01`, is now caught. The same goes for playlists and Blu-ray PIDs.
- DVD folders and key database files with names starting with a dash no
  longer get mistaken for command options.
- Changing the source quickly no longer starts detection for the old one.
  Closing the window also waits for the background workers to finish stopping.
- If inspecting a playlist's tracks fails, your previous selection stays put.
- The Activity pane no longer pulls you back to the bottom while you're
  reading earlier output. Very long lines are broken up to stop GTK from
  eating memory.
- Error messages are a little clearer, and unexpected errors leave more
  detail in Activity.

### A few extra checks

- Detection now looks for the actual DVD or Blu-ray marker files, rather
  than trusting a folder name.
- Added limits on track lists and labels, stricter checks on stream IDs,
  and filtering for control characters and text-direction tricks.
- The GUI won't accept symbolic links as sources or disc marker files.
  Device links such as `/dev/cdrom` are still fine.

### Worth knowing

- A reminder for encrypted Blu-rays: keep `~/.config/aacs/` at `700` and
  `KEYDB.cfg` at `600`. The script checks the file's permissions even when
  it finds it automatically, not just when you use `--keydb`. The folder
  permissions are a recommendation, not a check. This isn't new behaviour,
  just something worth spelling out.
- Automatic detection of images and devices now needs `prlimit`, from
  util-linux, as well as `7z` or `7zz`. It limits the probe's virtual address
  space to 512 MiB, not its physical RAM use. Without these tools, you can
  still choose DVD or Blu-ray yourself.
- For command-line use, stream indexes and retry counts accept 1-4 digits
  (0-9999), and playlist IDs accept 1-5 digits (0-99999). Leading zeros are
  fine within those lengths; the numbers are read as decimal.
- Blu-ray PIDs must be between `0x0000` and `0x1fff`.
