"""Selection snapshots independent of GTK widgets and persistence."""

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

from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace

from dvdvideo.gui_listing import Listing
from dvdvideo.gui_preferences import (
    PreferencesStore,
    Profile,
    SourcePreferences,
    TrackInfo,
    reusable_source,
    select_tracks,
)
from dvdvideo.gui_preferences_io import PreferencesDurabilityError


@dataclass
class SelectionState:
    loaded_mode: bool | None = None
    omitted_ids: set[str] = field(default_factory=set[str])
    omitted_streams: dict[str, set[str]] = field(
        default_factory=dict[str, set[str]]
    )
    selected_hidden_ids: set[str] = field(default_factory=set[str])
    selected_hidden_streams: dict[str, set[str]] = field(
        default_factory=dict[str, set[str]]
    )
    pending_streams: set[str] = field(default_factory=set[str])
    ignore_saved_playlists: bool = False

    def clear(self) -> None:
        self.loaded_mode = None
        self.omitted_ids.clear()
        self.omitted_streams.clear()
        self.selected_hidden_ids.clear()
        self.selected_hidden_streams.clear()
        self.pending_streams.clear()
        self.ignore_saved_playlists = False

    def selected_ids(
        self,
        previous: SourcePreferences | None,
        *,
        is_bluray: bool,
        selected: list[str],
    ) -> list[str]:
        saved: list[str] = []
        if previous is not None:
            if is_bluray and not self.ignore_saved_playlists:
                saved = previous.playlists
            elif not is_bluray:
                saved = previous.dvd_streams
        return list(dict.fromkeys(list(selected) + [
            identifier for identifier in saved
            if identifier in self.omitted_ids
        ] + sorted(self.selected_hidden_ids & self.omitted_ids, key=int)))

    def selected_streams(
        self,
        previous: SourcePreferences | None,
        playlist: str,
        selected: list[str],
    ) -> list[str]:
        saved = previous.playlist_streams.get(playlist, []) if previous else []
        return list(
            dict.fromkeys(
                list(selected)
                + [
                    identifier
                    for identifier in saved
                    if identifier in self.omitted_streams.get(playlist, set())
                ]
                + sorted(
                    self.selected_hidden_streams.get(playlist, set())
                    & self.omitted_streams.get(playlist, set())
                )
            )
        )

    def clear_selected_hidden(
        self, *, is_bluray: bool, clear_ids: bool
    ) -> bool:
        changed = False
        if not is_bluray or clear_ids:
            changed = bool(self.selected_hidden_ids)
            self.selected_hidden_ids.clear()
        if is_bluray:
            changed = changed or any(self.selected_hidden_streams.values())
            self.selected_hidden_streams.clear()
        return changed

    def without_hidden(
        self,
        previous: SourcePreferences,
        *,
        is_bluray: bool,
        clear_ids: bool = True,
    ) -> SourcePreferences:
        if not is_bluray:
            return replace(
                previous,
                dvd_streams=[
                    identifier
                    for identifier in previous.dvd_streams
                    if identifier not in self.omitted_ids
                ],
            )
        return replace(
            previous,
            playlists=[
                identifier
                for identifier in previous.playlists
                if not clear_ids or identifier not in self.omitted_ids
            ],
            playlist_streams={
                playlist: [
                    identifier
                    for identifier in identifiers
                    if identifier
                    not in self.omitted_streams.get(playlist, set())
                ]
                for playlist, identifiers in previous.playlist_streams.items()
            },
        )

    def snapshot(
        self, previous: SourcePreferences | None, *, is_bluray: bool,
        selected: list[str], inspected_streams: dict[str, list[str]],
        profile_name: str, output_format: str, minimum_minutes: int,
        dvd_title: str = "1",
    ) -> SourcePreferences:
        listing_loaded = (
            self.loaded_mode is not None and self.loaded_mode == is_bluray
        )
        previous = previous or SourcePreferences(dvd_inspected=False)
        if not is_bluray and previous.dvd_title != dvd_title:
            previous = replace(
                previous,
                dvd_streams=[],
                dvd_inspected=False,
                dvd_title=dvd_title,
            )
        selected = self.selected_ids(
            previous, is_bluray=is_bluray, selected=selected
        )
        streams = {
            key: list(value)
            for key, value in previous.playlist_streams.items()
        }
        if listing_loaded and is_bluray:
            for key, value in inspected_streams.items():
                streams[key] = self.selected_streams(previous, key, value)
        return SourcePreferences(
            profile_name=profile_name,
            output_format=output_format,
            minimum_minutes=minimum_minutes,
            dvd_streams=(
                list(selected)
                if listing_loaded and not is_bluray
                else list(previous.dvd_streams)
            ),
            playlists=(
                list(selected)
                if listing_loaded and is_bluray
                else list(previous.playlists)
            ),
            playlist_streams=streams,
            dvd_inspected=previous.dvd_inspected
            or (listing_loaded and not is_bluray),
            dvd_title=previous.dvd_title if is_bluray else dvd_title,
        )


@dataclass
class RestoredSourceSettings:
    source: str
    edited: set[str] = field(default_factory=set[str])
    confirmed_non_device: bool = False
    save_pending: bool = False
    row_edits: dict[tuple[bool, str], bool] = field(
        default_factory=dict[tuple[bool, str], bool]
    )
    stream_edits: dict[tuple[str, str], bool] = field(
        default_factory=dict[tuple[str, str], bool]
    )
    has_saved_settings: bool = True
    pending_preferences: SourcePreferences | None = None


@dataclass
class SelectionCoordinator:
    """Own source selections, inspection metadata, and profile state."""

    selection_state: SelectionState = field(default_factory=SelectionState)
    active_source: str = ""
    dvd_title: str = "1"
    stream_playlist: str | None = None
    playlist_streams: dict[str, list[str]] = field(
        default_factory=dict[str, list[str]]
    )
    playlist_mvc: dict[str, bool] = field(default_factory=dict[str, bool])
    playlist_all_streams: dict[str, list[str]] = field(
        default_factory=dict[str, list[str]]
    )
    m2ts_confirmed: set[str] = field(default_factory=set[str])
    playlist_track_info: dict[str, list[TrackInfo]] = field(
        default_factory=dict[str, list[TrackInfo]]
    )
    stale_track_info: set[str] = field(default_factory=set[str])
    dvd_track_info: list[TrackInfo] = field(default_factory=list[TrackInfo])
    applying_profile: bool = False
    restoring_preferences: bool = False
    restored_source: RestoredSourceSettings | None = None
    reinspection_edits: RestoredSourceSettings | None = None
    requires_reinspection: bool = False

    def clear(self) -> None:
        self.selection_state.clear()
        self.stream_playlist = None
        self.playlist_streams.clear()
        self.playlist_mvc.clear()
        self.playlist_all_streams.clear()
        self.m2ts_confirmed.clear()
        self.playlist_track_info.clear()
        self.stale_track_info.clear()
        self.dvd_track_info.clear()
        self.requires_reinspection = False

    def change_dvd_title(self, title: str) -> None:
        from dvdvideo.gui_commands import validate_dvd_title

        validate_dvd_title(title)
        self.clear()
        self.reinspection_edits = None
        if self.restored_source is not None:
            self.restored_source.row_edits = {
                key: value
                for key, value in self.restored_source.row_edits.items()
                if key[0]
            }
        self.dvd_title = title
        self.mark_source_edit("selection")

    def source_preferences(
        self, store: PreferencesStore | None, source: str
    ) -> SourcePreferences | None:
        if store is None or not source:
            return None
        restored = self.restored_source
        if (
            restored is not None
            and restored.source == source
            and self.active_source == source
            and restored.pending_preferences is not None
            and reusable_source(source)
            and store.source_key(source) not in store.device_sources
        ):
            return restored.pending_preferences
        return store.get_source(source)

    def mark_source_edit(self, *fields: str) -> None:
        if self.restored_source is not None and not self.restoring_preferences:
            self.restored_source.edited.update(fields)

    def record_selection_edit(
        self,
        identifier: str,
        active: bool,
        *,
        is_bluray: bool,
        playlist: str | None = None,
    ) -> None:
        edits = self.restored_source or (
            self.reinspection_edits if self.requires_reinspection else None
        )
        if edits is None or self.restoring_preferences:
            return
        edits.edited.add("selection")
        if playlist is None:
            edits.row_edits[is_bluray, identifier] = active
        else:
            edits.stream_edits[playlist, identifier] = active

    def clear_profile_selection_edits(self, *, is_bluray: bool) -> None:
        edits = self.restored_source or self.reinspection_edits
        if edits is None:
            return
        if is_bluray:
            edits.stream_edits.clear()
        else:
            for mode, identifier in list(edits.row_edits):
                if not mode:
                    del edits.row_edits[mode, identifier]

    def update_stream_selection(
        self, playlist: str, selected: list[str]
    ) -> None:
        self.m2ts_confirmed.discard(playlist)
        self.selection_state.pending_streams.discard(playlist)
        self.playlist_streams[playlist] = selected

    def select_all_hidden_ids(self, excluded: Collection[str] = ()) -> int:
        included = self.selection_state.omitted_ids - set(excluded)
        self.selection_state.selected_hidden_ids.update(included)
        return len(included)

    def select_all_hidden_streams(self, playlist: str) -> int:
        omitted = self.selection_state.omitted_streams.get(playlist, set())
        self.selection_state.selected_hidden_streams.setdefault(
            playlist, set()
        ).update(omitted)
        return len(omitted)

    def record_listing_metadata(
        self, listing: Listing, *, is_bluray: bool
    ) -> None:
        self.selection_state.omitted_ids = listing.omitted_ids
        if not is_bluray:
            self.dvd_track_info = listing.tracks

    def choose_dvd_streams(
        self, saved: SourcePreferences | None, profile: Profile,
    ) -> set[str]:
        if saved is not None and saved.dvd_inspected:
            selected = set(saved.dvd_streams)
        else:
            selected = set(select_tracks(profile, self.dvd_track_info))
        self.selection_state.selected_hidden_ids = (
            selected & self.selection_state.omitted_ids
        )
        return selected

    def stream_inspection_failed(self, playlist: str) -> None:
        self.playlist_all_streams.pop(playlist, None)
        self.m2ts_confirmed.discard(playlist)

    def record_stream_metadata(self, playlist: str, listing: Listing) -> None:
        self.selection_state.omitted_streams[playlist] = listing.omitted_ids
        self.playlist_track_info[playlist] = listing.tracks
        self.stale_track_info.discard(playlist)

    def complete_stream_inspection(
        self, playlist: str, listing: Listing
    ) -> None:
        self.playlist_mvc[playlist] = listing.mvc_verified
        if listing.invalid or listing.duplicates:
            self.playlist_all_streams.pop(playlist, None)
        else:
            self.playlist_all_streams[playlist] = [
                identifier for identifier, _ in listing.rows
            ] + sorted(listing.omitted_ids)

    def requires_m2ts_confirmation(
        self, playlist: str, saved: SourcePreferences | None,
    ) -> bool:
        edits = self.reinspection_edits
        if edits is not None and any(
            key[0] == playlist for key in edits.stream_edits
        ):
            return True
        choices = self.playlist_streams.get(playlist)
        if choices is not None:
            choices = self.selection_state.selected_streams(
                saved, playlist, choices
            )
        elif saved is not None:
            choices = saved.playlist_streams.get(playlist)
        if choices is None:
            return False
        all_pids = self.playlist_all_streams.get(playlist)
        return all_pids is None or set(choices) != set(all_pids)

    def begin_stream_inspection(
        self, playlist: str, saved: SourcePreferences | None,
    ) -> list[str] | None:
        previous = (
            None if playlist in self.selection_state.pending_streams
            else self.playlist_streams.get(playlist)
        )
        if previous is None and saved is not None:
            previous = saved.playlist_streams.get(playlist)
        if previous is not None:
            previous = self.selection_state.selected_streams(
                saved, playlist, previous
            )
        else:
            # A failed first inspection must not copy all streams.
            self.playlist_streams[playlist] = []
            self.selection_state.pending_streams.add(playlist)
        self.playlist_all_streams.pop(playlist, None)
        self.m2ts_confirmed.discard(playlist)
        self.playlist_mvc.pop(playlist, None)
        if self.playlist_track_info.pop(playlist, None) is not None:
            self.stale_track_info.add(playlist)
        return previous

    def choose_streams(
        self, playlist: str, selected: list[str] | None,
        saved: SourcePreferences | None, profile: Callable[[], Profile] | None,
    ) -> list[str]:
        if selected is not None:
            return selected
        if saved is not None and playlist in saved.playlist_streams:
            return saved.playlist_streams[playlist]
        if (
            playlist in self.playlist_streams
            and playlist not in self.selection_state.pending_streams
        ):
            return self.playlist_streams[playlist]
        if profile is not None:
            return select_tracks(profile(), self.playlist_track_info[playlist])
        return []

    def apply_profile(self, profile: Profile, *, is_bluray: bool) -> set[str]:
        if is_bluray:
            for playlist, tracks in self.playlist_track_info.items():
                selected = select_tracks(profile, tracks)
                omitted = self.selection_state.omitted_streams.get(
                    playlist, set()
                )
                self.selection_state.selected_hidden_streams[playlist] = (
                    set(selected) & omitted
                )
                self.playlist_streams[playlist] = [
                    identifier
                    for identifier in selected
                    if identifier not in omitted
                ]
                self.m2ts_confirmed.discard(playlist)
            return (
                set(self.playlist_streams.get(self.stream_playlist, []))
                if self.stream_playlist is not None
                else set()
            )
        selected = set(select_tracks(profile, self.dvd_track_info))
        self.selection_state.selected_hidden_ids = (
            selected & self.selection_state.omitted_ids
        )
        return selected

    def restore_profile_choice(
        self, name: str, set_choice: Callable[[str], None]
    ) -> None:
        self.restoring_preferences = True
        try:
            set_choice(name)
        finally:
            self.restoring_preferences = False

    def change_profile(
        self, store: PreferencesStore, name: str, *,
        apply: Callable[[], bool], set_choice: Callable[[str], None],
        save: Callable[[], None], log: Callable[[str], None],
    ) -> None:
        previous_active = store.active_profile
        previous_source = self.source_preferences(store, self.active_source)
        previous_name = (
            previous_source.profile_name
            if previous_source
            else previous_active
        )
        try:
            store.set_active_profile(name)
        except PreferencesDurabilityError as error:
            log(str(error))
        except (OSError, ValueError) as error:
            log(f"Could not save active profile: {error}")
            self.restore_profile_choice(previous_name, set_choice)
            return
        if not apply():
            try:
                store.set_active_profile(previous_active)
            except (OSError, ValueError) as error:
                log(f"Could not restore active profile: {error}")
            self.restore_profile_choice(previous_name, set_choice)
            return
        save()

    def profiles_updated(
        self,
        store: PreferencesStore | None,
        name: str,
        apply_profile: bool,
        *,
        get_choice: Callable[[], str | None],
        set_choice: Callable[[str], None],
        populate: Callable[[], None],
        apply: Callable[[], bool],
        save: Callable[[], None],
        log: Callable[[str], None],
    ) -> tuple[str, bool]:
        previous_name = get_choice()
        self.restoring_preferences = True
        try:
            populate()
            set_choice(name)
        finally:
            self.restoring_preferences = False
        if apply_profile and not apply():
            if (
                store is not None
                and previous_name is not None
                and previous_name in store.profiles
            ):
                try:
                    store.set_active_profile(previous_name)
                except (OSError, ValueError) as error:
                    log(f"Could not restore active profile: {error}")
                self.restore_profile_choice(previous_name, set_choice)
            return get_choice() or name, False
        if apply_profile:
            save()
        return get_choice() or name, True

    def clear_hidden(
        self,
        store: PreferencesStore | None,
        *,
        is_bluray: bool,
        clear_ids: bool,
        log: Callable[[str], None],
    ) -> bool | None:
        if (
            not self.active_source
            or self.selection_state.loaded_mode != is_bluray
        ):
            return False
        previous = (
            store.get_source(self.active_source) if store is not None else None
        )
        updated = (
            self.selection_state.without_hidden(
                previous, is_bluray=is_bluray, clear_ids=clear_ids
            )
            if previous is not None
            else None
        )
        if (
            self.restored_source is not None
            and self.restored_source.source == self.active_source
            and not self.restored_source.confirmed_non_device
            and updated != previous
        ):
            log("Identify the source before changing saved hidden selections.")
            return None
        if store is not None and updated is not None and updated != previous:
            try:
                store.save_source(self.active_source, updated)
            except PreferencesDurabilityError as error:
                log(str(error))
            except (OSError, ValueError) as error:
                log(f"Could not clear hidden selections: {error}")
                return None
        restored = self.restored_source
        pending_changed = False
        if (
            restored is not None
            and restored.source == self.active_source
            and restored.pending_preferences is not None
        ):
            pending = restored.pending_preferences
            restored.pending_preferences = self.selection_state.without_hidden(
                pending, is_bluray=is_bluray, clear_ids=clear_ids,
            )
            pending_changed = restored.pending_preferences != pending
        changed = self.selection_state.clear_selected_hidden(
            is_bluray=is_bluray, clear_ids=clear_ids
        )
        if not changed and not pending_changed and updated == previous:
            return False
        self.m2ts_confirmed.clear()
        return True

    def source_snapshot(
        self,
        previous: SourcePreferences | None,
        *,
        is_bluray: bool,
        selected: list[str],
        profile_name: str,
        output_format: str,
        minimum_minutes: int,
    ) -> SourcePreferences:
        return self.selection_state.snapshot(
            previous,
            is_bluray=is_bluray,
            selected=selected,
            inspected_streams={
                key: value
                for key, value in self.playlist_streams.items()
                if key not in self.selection_state.pending_streams
            },
            profile_name=profile_name,
            output_format=output_format,
            minimum_minutes=minimum_minutes,
            dvd_title=self.dvd_title,
        )

    def persist_source(
        self,
        store: PreferencesStore,
        preferences: SourcePreferences,
        *,
        log: Callable[[str], None],
    ) -> bool:
        try:
            store.save_source(self.active_source, preferences)
        except PreferencesDurabilityError as error:
            log(str(error))
        except (OSError, ValueError) as error:
            log(f"Could not save source preferences: {error}")
            return False
        return True

    def save_source(
        self,
        store: PreferencesStore | None,
        *,
        is_bluray: bool,
        selected: list[str],
        profile_name: str | None,
        output_format: str,
        minimum_minutes: int,
        log: Callable[[str], None],
    ) -> None:
        if (
            store is None
            or not self.active_source
            or self.restoring_preferences
        ):
            return
        preferences = self.source_snapshot(
            store.get_source(self.active_source),
            is_bluray=is_bluray,
            selected=selected,
            profile_name=profile_name or store.active_profile,
            output_format=output_format,
            minimum_minutes=minimum_minutes,
        )
        self.persist_source(store, preferences, log=log)
