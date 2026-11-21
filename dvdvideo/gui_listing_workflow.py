"""Reconcile inspection listings and saved selections before rendering."""

# @copyright: 2026 libertypo <libertypo@proton.me>
# @license: GNU GPL-3
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import Protocol

from dvdvideo.gui_listing import (
    MAX_INSPECTION_ROWS,
    MAX_STREAM_ROWS,
    Listing,
    parse_bluray_streams,
    parse_dvd_streams,
    parse_playlists,
)
from dvdvideo.gui_preferences import Profile, SourcePreferences
from dvdvideo.gui_selection import SelectionCoordinator
from dvdvideo.gui_translation import identity


class ListingPresenter(Protocol):
    show_unselected: bool

    def render_rows(self, rows: Iterable[tuple[str, str, bool]]) -> None: ...

    def set_show_unselected(self, show: bool) -> None: ...

    def render_stream_rows(
        self, playlist: str, rows: Iterable[tuple[str, str]], selected: Collection[str],
    ) -> None: ...


@dataclass(frozen=True)
class ListingContext:
    is_bluray: bool
    settings: SourcePreferences | None
    profile: Profile | None


@dataclass(frozen=True)
class ListingWorkflowHooks:
    clear_rows: Callable[[], None]
    suggest_name: Callable[[str], None]
    log: Callable[[str], None]
    set_status: Callable[[str], None]
    save: Callable[[], None]
    streams_changed: Callable[[], None]
    update_stream_format: Callable[[str], None]
    clean_label: Callable[[str], str]
    translate: Callable[[str], str] = identity


class ListingWorkflow:
    def __init__(
        self, selection: SelectionCoordinator, presenter: ListingPresenter,
        get_context: Callable[[], ListingContext], hooks: ListingWorkflowHooks,
    ) -> None:
        self.selection = selection
        self.presenter = presenter
        self.get_context = get_context
        self.hooks = hooks

    def load_rows(self, output: str | Listing) -> Listing:
        show_unselected = self.presenter.show_unselected
        requires_reinspection = self.selection.requires_reinspection
        try:
            self.hooks.clear_rows()
        finally:
            self.selection.requires_reinspection = requires_reinspection
        context = self.get_context()
        settings = context.settings
        if isinstance(output, str):
            listing = (parse_playlists(output, translate=self.hooks.translate) if context.is_bluray
                       else parse_dvd_streams(output))
        else:
            listing = output
        self.selection.record_listing_metadata(listing, is_bluray=context.is_bluray)
        if context.is_bluray:
            selected = set(settings.playlists) if settings is not None else set[str]()
            if listing.disc_name is not None:
                self.hooks.suggest_name(listing.disc_name)
            for warning in listing.warnings:
                self.hooks.log(self.hooks.clean_label(warning))
        else:
            selected = self.selection.choose_dvd_streams(settings, context.profile or Profile())
        edits = self.selection.reinspection_edits
        if edits is not None and listing.rows:
            available = {identifier for identifier, _label in listing.rows} | listing.omitted_ids
            missing = 0
            for (is_bluray, identifier), active in list(edits.row_edits.items()):
                if is_bluray == context.is_bluray and identifier in available:
                    if active:
                        selected.add(identifier)
                    else:
                        selected.discard(identifier)
                else:
                    missing += 1
                del edits.row_edits[is_bluray, identifier]
            for playlist, identifier in list(edits.stream_edits):
                if not context.is_bluray or playlist not in available:
                    del edits.stream_edits[playlist, identifier]
                    missing += 1
            self.selection.selection_state.selected_hidden_ids = selected & listing.omitted_ids
            if missing:
                self.hooks.log(f"Could not restore {missing} explicitly changed selection(s) on the current disc.")
            if not edits.stream_edits:
                self.selection.reinspection_edits = None
        rows = [
            (identifier, self.hooks.clean_label(label), identifier in selected)
            for identifier, label in listing.rows
        ]
        self.presenter.render_rows(rows)
        if context.is_bluray:
            self.presenter.set_show_unselected(show_unselected)
        if rows:
            self.selection.requires_reinspection = False
        kind = "playlists" if context.is_bluray else "streams"
        if listing.truncated:
            self.hooks.log(f"Inspection listed too many {kind}; showing the first {MAX_INSPECTION_ROWS}.")
        if listing.invalid or listing.duplicates:
            prefix = "Playlist" if context.is_bluray else "DVD"
            self.hooks.log(
                f"{prefix} inspection ignored {listing.invalid} malformed and "
                f"{listing.duplicates} duplicate records."
            )
        self.selection.selection_state.loaded_mode = context.is_bluray if rows else None
        if settings is not None and (context.is_bluray or settings.dvd_inspected):
            hidden = selected & listing.omitted_ids
            if hidden:
                name = "playlists" if context.is_bluray else "DVD streams"
                self.hooks.log(
                    f"{len(hidden)} saved {name} are hidden by the display limit and will be copied."
                )
            missing = selected - {identifier for identifier, _label in listing.rows} - listing.omitted_ids
            if missing:
                name = "playlist selections" if context.is_bluray else "DVD streams"
                self.hooks.log(f"Saved {name} no longer exist: {', '.join(sorted(missing))}.")
        if not rows:
            name = "Blu-ray playlists" if context.is_bluray else "DVD streams"
            self.hooks.log(f"No {name} found in the inspection output. See Activity for details.")
            if context.is_bluray:
                self.hooks.set_status("No playlists found")
            else:
                self.hooks.set_status("No streams found")
        elif context.is_bluray:
            if settings is None:
                self.hooks.save()
            if listing.longest_playlist is None:
                self.hooks.log("Choose a playlist to inspect its video, audio, and subtitles.")
        else:
            self.hooks.save()
        return listing

    def load_stream_rows(
        self, playlist: str, output: str | Listing, selected: list[str] | None = None,
    ) -> None:
        listing = parse_bluray_streams(output) if isinstance(output, str) else output
        for warning in listing.warnings:
            self.hooks.log(self.hooks.clean_label(warning))
        if listing.truncated:
            self.hooks.log(f"Playlist {playlist} listed too many streams; showing the first {MAX_STREAM_ROWS}.")
        if listing.invalid or listing.duplicates:
            self.hooks.log(
                f"Stream inspection ignored {listing.invalid} malformed and {listing.duplicates} duplicate records "
                f"for playlist {playlist}."
            )
        if not listing.rows:
            self.selection.stream_inspection_failed(playlist)
            self.hooks.log(f"No streams found for playlist {playlist}. See Activity for details.")
            self.hooks.set_status("Stream inspection failed")
            return
        self.selection.record_stream_metadata(playlist, listing)
        if selected is None:
            context = self.get_context()
            profile = context.profile
            selected = self.selection.choose_streams(
                playlist, None, context.settings,
                (lambda: profile) if profile is not None else None,
            )
        edits = self.selection.reinspection_edits
        if edits is not None:
            selected_set = set(selected)
            available = {identifier for identifier, _details in listing.rows} | listing.omitted_ids
            missing = 0
            for (saved_playlist, identifier), active in list(edits.stream_edits.items()):
                if saved_playlist == playlist:
                    if identifier in available:
                        if active:
                            selected_set.add(identifier)
                        else:
                            selected_set.discard(identifier)
                    else:
                        missing += 1
                    del edits.stream_edits[saved_playlist, identifier]
            selected = sorted(selected_set)
            if missing:
                self.hooks.log(f"Could not restore {missing} explicitly changed track(s) in playlist {playlist}.")
            if not edits.stream_edits and not edits.row_edits:
                self.selection.reinspection_edits = None
        all_ids = {identifier for identifier, _details in listing.rows}
        hidden = set(selected) & listing.omitted_ids
        self.selection.selection_state.selected_hidden_streams[playlist] = hidden
        if hidden:
            self.hooks.log(
                f"{len(hidden)} saved streams for playlist {playlist} are hidden by the display limit "
                "and will be copied."
            )
        missing = set(selected) - all_ids - listing.omitted_ids
        if missing:
            self.hooks.log(
                f"Saved streams for playlist {playlist} no longer exist: {', '.join(sorted(missing))}."
            )
            selected = [identifier for identifier in selected if identifier in all_ids]
        self.selection.stream_playlist = playlist
        self.presenter.render_stream_rows(
            playlist,
            (
                (identifier, self.hooks.clean_label(f"{identifier}  {details}"))
                for identifier, details in listing.rows
            ),
            selected,
        )
        self.selection.selection_state.pending_streams.discard(playlist)
        self.hooks.streams_changed()
        self.selection.complete_stream_inspection(playlist, listing)
        self.hooks.update_stream_format(playlist)
        self.hooks.save()
