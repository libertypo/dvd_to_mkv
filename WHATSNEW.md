# What's new

## 2026-11-28

- DVD title discovery offers advisory menu and possible commentary hints.
- Blu-ray Inspect presents advisory playlist rankings and disc labels when
  available. You choose the playlists; none are selected automatically.
  Dummy, top-menu and repeated playlists are hidden by default, with an
  option to show them.
- DVD title discovery and Blu-ray Inspect exclude titles or playlists shorter
  than five minutes.
- Disc previews now use mpv instead of FFplay.
- For an ambiguous single-playlist Blu-ray output path, you can choose
  whether it is a file or directory.
- A legal notice appears on first launch and is available from the GUI.
- Bug fixing.

## 2026-11-23

- Prepare several DVD titles with a language profile, review their individual
  tracks and filenames, then copy them one at a time. Cancelling confirmation
  keeps the prepared batch.
- Finished titles stay finished when a batch is cancelled. Unfinished titles
  remain available for another attempt, and existing files aren't overwritten.
- Refreshing a drive clears its old disc selections and invalidates its
  prepared batch. Disc changes still need a manual refresh.
- Fixed reinspection state when switching media types and made shutdown find
  application windows even when another window has focus.
- Added Italian and Romanian text for the DVD batch controls.

## 2026-11-21

- Blu-ray inspection now shows your playlists and opens the tracks for the
  longest one automatically. It's a helpful starting point, not necessarily
  the main movie, and it won't be selected for copying unless you choose it.
- Want to see another playlist's tracks? Pick it from the drop-down. The
  Inspect Tracks button also follows that choice, independently of the
  playlists you've checked for copying.
- Unselected Blu-ray items are visible when you start. Show or hide them as
  you like: your choice stays put while you inspect tracks or apply a profile.
  Your saved choices are still respected.
- Fixed some strange translation text that could appear in an empty playlist
  chooser.
- If an output check fails, the filename you entered now stays put.

## 2026-11-20

- DVD track listings now handle accented and multiline titles better and reject
  invalid or duplicate track numbers. If neat column alignment isn't available,
  you'll still get a complete tab-separated list.
- DVD copies now check for Python and `setsid` before preparing output.
  Blu-ray copies check for required tools before choosing a format.
- Blu-ray track listings clean up unusual control characters and check for the
  tools needed to inspect 3D streams.
- When a media check times out, the command reports it with exit status 124.
  Cancelling a copy also stops lingering helpers, and failed output publication
  attempts a rollback before cleaning up.

## 2026-11-19

- Use Show unselected and Hide unselected to tidy up the lists without changing
  what you've chosen to copy. Helpful messages explain when a filtered list is
  empty, in English, Italian or Romanian.
- Keyboard focus now moves to a nearby item when the focused item is hidden,
  rather than jumping back to the start.
- If you cancel closing the app while preferences are saving or after a save
  fails, your DVD title choice is restored.
- Older file choosers now use the selected language for their titles and buttons.
- Pick a different DVD title and its tracks are inspected automatically. Finding
  titles or restoring a saved choice doesn't start an inspection.
- DVD title selection waits until media detection is finished, so that automatic
  inspection doesn't get skipped.
- The app now warns you before copy confirmation if a Blu-ray filename stem
  includes a video extension.
- Tidied up the GUI's command handling and added checks for dialogs, save
  failures and DVD title inspection.

## 2026-11-18

- More buttons now have helpful Italian and Romanian tooltips.
- More of the app speaks Italian and Romanian, including Copy Summary, Disc
  Details, profiles, verification, progress and notifications.
- Removed filename templates and their preview from the app. You can still use
  standard output names and check them in Copy Summary.

## 2026-11-16

- Added optional filename templates with previews for DVD and Blu-ray, with
  help avoiding name clashes and overwriting. (These were later removed from
  the GUI.)
- Discover DVD titles and see their duration, chapters and tracks before
  choosing one. Title 1 is still available if `lsdvd` isn't installed.
- Blu-ray playlist labels now point out longer titles and the longest listed
  duration. They're clues, not guarantees about which one is the movie.

## 2026-11-15

- The app can let you know when a copy or verification finishes or fails.
  Click the notification to return to the window.
- Before copying, check the source, selected items, filenames and estimated
  space needed in the new Copy Summary.
- Newly copied Blu-ray MKVs now keep the disc's audio and subtitle language
  labels, including when you copy all tracks or tracks share an ID.
- If a language code is missing, you'll see a warning instead of a guessed
  label. Invalid language information stops the copy. Existing MKVs aren't
  repaired, and original M2TS copies are unchanged.
- Rebuilt the Manjaro/Arch package as `2026.11.15-1`.

## 2026-11-12

- You can now check an existing copy against its original source.

## 2026-11-11

- Squashed some bugs.
- Clear All gives you a fresh workspace.
- Added a local Manjaro/Arch package and an application-menu entry.
- Find and select optical drives from the app.
- Preview directly from the disc if you like.
- Added a development GTK mockup with sample Blu-ray content.

## 2026-11-10

- Save language profiles and remember your choices for each source.
- Take a closer look with Disc Details.
- Resize the side-by-side Activity and workflow panes.
- Keep Blu-ray chapters in your copies.
- Choose Automatic, MKV or original M2TS for Blu-ray output.
- Check free space before copying.
