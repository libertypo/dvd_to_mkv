# What's new

## 2026-11-21

- Inspect Tracks now always uses the playlist in the tracks drop-down, without
  changing or depending on playlists checked for copying.
- Failed copy preflight no longer changes the output filename in the GUI.
- Fixed translation catalog metadata appearing in the empty playlist chooser.
- Blu-ray inspection now shows playlists and automatically inspects tracks for
  the longest candidate without selecting it for copying. Choose another
  playlist in the tracks drop-down to inspect it.
- Blu-ray lists initially show unselected items; the manual Show/Hide choice
  persists during track inspection and profile application. Saved choices are
  retained, and profiles continue to select tracks rather than playlists.

## 2026-11-20

- DVD stream listings now read FFprobe JSON, preserve Unicode and multiline
  titles, and reject duplicate or out-of-range stream indexes. Formatted
  listings fall back to tabs when UTF-8 alignment is unavailable.
- DVD copies check for Python and `setsid` before preparing output. Blu-ray
  copies check for required tools before sampling the disc to select a format.
- Blu-ray stream listings replace control characters in display text and
  check for MVC tools before probing 3D streams with FFprobe.
- Media inspection reports confirmed validation timeouts with exit status
  124. Copy cancellation stops lingering helpers, and publication failures
  attempt rollback before cleaning up temporary files.

## 2026-11-19

- Replaced Clear Hidden with Show unselected / Hide unselected for DVD streams,
  Blu-ray playlists, and Blu-ray tracks. Inspection and profile application show
  selected items by default, without changing copy selections when toggling.
- Added empty-list guidance and Italian/Romanian translations for the filter.
- Filtering out a focused item now moves keyboard focus to the next remaining
  item (or the previous one at the end), instead of jumping to the first row.
- Cancelling shutdown after a slow or failed preferences save now restores DVD
  title selection.
- Legacy file choosers now translate their titles and action buttons using the
  selected GUI language. Cleared remaining GUI typing diagnostics.
- Selecting a different DVD title in the GUI now automatically inspects its
  streams. Title discovery and saved-title restoration do not start inspection.
- Disabled DVD title selection during media detection so automatic inspection
  cannot be skipped by an active probe.
- The GUI rejects video extensions in Blu-ray filename stems before copy
  confirmation.
- Replaced inferred output-argument positions with command-builder metadata,
  centralized DVD title state changes, and tightened GTK callback typing.
- Updated GUI regression expectations for translation-aware dialogs, save
  failures, and explicit DVD title inspection arguments.

## 2026-11-18

- Added Italian and Romanian tooltips for application buttons in the main
  window and dialogs, with wording kept in one shared module.
- Expanded Italian and Romanian GUI translations to cover Copy Summary,
  Disc Details, verification and profile dialogs, operation status and progress, and
  completion notifications.
- Removed filename templates and their preview from the GUI. Standard output
  naming and Copy Summary remain available.

## 2026-11-16

- Optional filename templates for DVD and Blu-ray with previews, automatic
  playlist suffixes for generated-name collisions, and overwrite protection.
- Optional DVD title discovery with duration, chapter and track hints; choose
  one title for inspection, preview, copying and verification. Title 1 remains
  available without `lsdvd`.
- Blu-ray playlist labels also identify feature-length durations and the
  longest listed duration. All hints remain advisory.

## 2026-11-15

- Background desktop notifications for completed or failed copies and verification;
  clicking a notification returns to the application.
- Copy Summary confirmation before extraction, showing source, playlists/tracks,
  resolved output filenames, and estimated peak space alongside available space.
- Fixed missing language labels in newly copied Blu-ray MKVs: audio and subtitle
  tracks now keep the disc's three-letter language codes, matched by stream PID.
  This works with selected tracks, copy-all, and shared-PID companion tracks.
- Missing disc language codes produce a warning rather than guessed labels.
  Invalid language metadata stops the copy instead of repeatedly recopying it.
  Existing MKVs are not repaired, and original M2TS output is unchanged.
- The Manjaro/Arch package has been rebuilt as `2026.11.15-1`.

## 2026-11-12

- Optional verification of an existing copy against its original source.

## 2026-11-11

- Bugs squashed.
- Clear All workspace reset.
- Complete local Manjaro/Arch package and application-menu entry.
- Optical-drive discovery and selection.
- Optional direct-disc Preview.
- Development GTK mockup with sample Blu-ray content in the source checkout.

## 2026-11-10

- Saved language profiles and remembered source selections.
- Disc Details overview.
- Resizable side-by-side Activity and workflow layout.
- Blu-ray chapter preservation.
- Automatic, MKV, and original M2TS Blu-ray output choices.
- Free-space checks before copying.
