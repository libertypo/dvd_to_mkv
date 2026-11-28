"""Coordinate profile and source-preference UI without importing GTK."""

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

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from dvdvideo.gui_preferences import PreferencesStore, reusable_source
from dvdvideo.gui_selection import RestoredSourceSettings, SelectionCoordinator

ProfileUpdateCallback = Callable[
    [str, bool, tuple[str, str] | None], tuple[str, bool]
]
PresentProfileDialog = Callable[
    [PreferencesStore, ProfileUpdateCallback, str | None],
    None,
]


@dataclass(frozen=True)
class ProfileChoiceHooks:
    get_profile_choice: Callable[[], str | None]
    set_profile_choice: Callable[[str], None]
    populate_profiles: Callable[[], None]
    present_profile_dialog: PresentProfileDialog


@dataclass(frozen=True)
class PreferenceSettingsHooks:
    get_output_format: Callable[[], str]
    set_output_format: Callable[[str], None]
    get_minimum_minutes: Callable[[], int]
    set_minimum_minutes: Callable[[int], None]


@dataclass(frozen=True)
class PreferenceSelectionHooks:
    get_visible_selection: Callable[[], list[str]]
    set_visible_selection: Callable[[set[str], bool], None]
    clear_hidden_selections: Callable[[], bool]
    update_stream_format: Callable[[str], None]
    reset_visibility: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class PreferenceWorkflowHooks:
    get_store: Callable[[], PreferencesStore | None]
    get_error: Callable[[], str | None]
    is_bluray: Callable[[], bool]
    profiles: ProfileChoiceHooks
    settings: PreferenceSettingsHooks
    selection: PreferenceSelectionHooks
    log: Callable[[str], None]


class PreferenceWorkflow:
    def __init__(
        self, selection: SelectionCoordinator, hooks: PreferenceWorkflowHooks,
    ) -> None:
        self.selection = selection
        self.hooks = hooks

    def populate_profiles(self) -> None:
        self.hooks.profiles.populate_profiles()

    def show_profile_dialog(self) -> None:
        store = self.hooks.get_store()
        if store is None:
            self.hooks.log(
                self.hooks.get_error() or "GUI preferences are unavailable."
            )
            return
        self.hooks.profiles.present_profile_dialog(
            store,
            self.profiles_updated,
            self.hooks.profiles.get_profile_choice(),
        )

    def profiles_updated(
        self,
        name: str,
        apply_profile: bool = True,
        replacement: tuple[str, str] | None = None,
    ) -> tuple[str, bool]:
        restored = self.selection.restored_source
        if (
            replacement is not None and restored is not None
            and restored.source == self.selection.active_source
            and restored.pending_preferences is not None
            and restored.pending_preferences.profile_name == replacement[0]
        ):
            restored.pending_preferences = replace(
                restored.pending_preferences, profile_name=replacement[1]
            )
        result = self.selection.profiles_updated(
            self.hooks.get_store(),
            name,
            apply_profile,
            get_choice=self.hooks.profiles.get_profile_choice,
            set_choice=self.hooks.profiles.set_profile_choice,
            populate=self.populate_profiles,
            apply=self.apply_profile_to_rows,
            save=self.save_current_source_preferences,
            log=self.hooks.log,
        )
        if result[1] and apply_profile:
            self.selection.mark_source_edit("profile", "format", "minimum")
        return result

    def profile_changed(self) -> None:
        store = self.hooks.get_store()
        if self.selection.restoring_preferences or store is None:
            return
        name = self.hooks.profiles.get_profile_choice()
        if name is None:
            return
        self.selection.change_profile(
            store,
            name,
            apply=self.apply_profile_to_rows,
            set_choice=self.hooks.profiles.set_profile_choice,
            save=self.save_current_source_preferences,
            log=self.hooks.log,
        )
        if (
            store.active_profile == name
            and self.hooks.profiles.get_profile_choice() == name
        ):
            self.selection.mark_source_edit("profile", "format", "minimum")

    def apply_profile_to_rows(self, apply_settings: bool = True) -> bool:
        store = self.hooks.get_store()
        if store is None:
            return False
        is_bluray = self.hooks.is_bluray()
        if (
            is_bluray
            and self.selection.stale_track_info
            and not self.selection.restoring_preferences
        ):
            playlists = ", ".join(
                sorted(self.selection.stale_track_info, key=int)
            )
            self.hooks.log(
                "Profile was not applied; inspect the affected playlists"
                f" successfully and try again: {playlists}."
            )
            return False
        if (
            not self.selection.restoring_preferences
            and not self.hooks.selection.clear_hidden_selections()
        ):
            self.hooks.log(
                "Profile was not applied; existing selections were retained."
            )
            return False
        profile = store.get_profile(self.hooks.profiles.get_profile_choice())
        if apply_settings:
            self.hooks.settings.set_output_format(profile.output_format)
            self.hooks.settings.set_minimum_minutes(profile.minimum_minutes)
        self.selection.applying_profile = True
        try:
            selected = self.selection.apply_profile(
                profile, is_bluray=is_bluray
            )
            self.hooks.selection.set_visible_selection(selected, is_bluray)
        finally:
            self.selection.applying_profile = False
        if not self.selection.restoring_preferences:
            self.selection.clear_profile_selection_edits(is_bluray=is_bluray)
        if self.selection.stream_playlist is not None:
            self.hooks.selection.update_stream_format(
                self.selection.stream_playlist
            )
        self.hooks.selection.reset_visibility()
        self.save_current_source_preferences()
        return True

    def restore_source_preferences(self, source: str) -> None:
        store = self.hooks.get_store()
        previous = self.selection.restored_source
        if previous is not None and previous.save_pending:
            reason = (
                "the save failed"
                if previous.confirmed_non_device
                else "the source was not identified"
            )
            self.hooks.log(f"Source settings were not saved because {reason}.")
        self.selection.restored_source = None
        if store is None:
            return
        settings = store.get_source(source)
        if source and reusable_source(source):
            self.selection.restored_source = RestoredSourceSettings(
                source, has_saved_settings=settings is not None
            )
        name = settings.profile_name if settings else store.active_profile
        self.selection.restoring_preferences = True
        try:
            self.hooks.profiles.set_profile_choice(name)
            if settings:
                self.hooks.settings.set_output_format(settings.output_format)
                self.hooks.settings.set_minimum_minutes(
                    settings.minimum_minutes
                )
            self.apply_profile_to_rows(apply_settings=settings is None)
        finally:
            self.selection.restoring_preferences = False

    def confirm_non_device(self, source: str) -> None:
        restored = self.selection.restored_source
        store = self.hooks.get_store()
        if (
            self.selection.active_source != source
            or store is None
            or not reusable_source(source)
        ):
            return
        key = store.source_key(source)
        previously_device = key in store.device_sources
        store.device_sources.discard(key)
        if restored is None:
            self.restore_source_preferences(source)
            restored = self.selection.restored_source
        if restored is None or restored.source != source:
            return
        previous = store.get_source(source) if previously_device else None
        if previous is not None:
            restored.has_saved_settings = True
            self.selection.restoring_preferences = True
            try:
                if "profile" not in restored.edited:
                    self.hooks.profiles.set_profile_choice(
                        previous.profile_name
                    )
                if "format" not in restored.edited:
                    self.hooks.settings.set_output_format(
                        previous.output_format
                    )
                if "minimum" not in restored.edited:
                    self.hooks.settings.set_minimum_minutes(
                        previous.minimum_minutes
                    )
            finally:
                self.selection.restoring_preferences = False
            if restored.save_pending:
                if "profile" in restored.edited:
                    previous = self.selection.selection_state.without_hidden(
                        previous,
                        is_bluray=self.hooks.is_bluray(),
                        clear_ids=False,
                    )
                previous = replace(
                    previous,
                    dvd_streams=[
                        identifier
                        for identifier in previous.dvd_streams
                        if restored.row_edits.get((False, identifier))
                        is not False
                    ],
                    playlists=[
                        identifier
                        for identifier in previous.playlists
                        if restored.row_edits.get((True, identifier))
                        is not False
                    ],
                    playlist_streams={
                        playlist: [
                            identifier
                            for identifier in identifiers
                            if restored.stream_edits.get(
                                (playlist, identifier)
                            )
                            is not False
                        ]
                        for playlist, identifiers in (
                            previous.playlist_streams.items()
                        )
                    },
                )
                restored.pending_preferences = self.selection.source_snapshot(
                    previous,
                    is_bluray=self.hooks.is_bluray(),
                    selected=self.hooks.selection.get_visible_selection(),
                    profile_name=self.hooks.profiles.get_profile_choice()
                    or store.active_profile,
                    output_format=self.hooks.settings.get_output_format(),
                    minimum_minutes=self.hooks.settings.get_minimum_minutes(),
                )
        restored.confirmed_non_device = True
        if (
            restored.save_pending
            and restored.pending_preferences is not None
            and self.selection.persist_source(
                store, restored.pending_preferences, log=self.hooks.log
            )
        ):
            restored.save_pending = False
            restored.pending_preferences = None

    def exclude_device_source(self, source: str) -> bool:
        store = self.hooks.get_store()
        if store is None:
            return False
        store.exclude_device_source(source)
        restored = self.selection.restored_source
        if (
            restored is None
            or restored.source != source
            or self.selection.active_source != source
        ):
            return False
        self.selection.restored_source = None
        if restored.has_saved_settings:
            self.selection.restoring_preferences = True
            try:
                if "profile" not in restored.edited:
                    self.hooks.profiles.set_profile_choice(
                        store.active_profile
                    )
                profile = store.get_profile(
                    self.hooks.profiles.get_profile_choice()
                )
                if "format" not in restored.edited:
                    self.hooks.settings.set_output_format(
                        profile.output_format
                    )
                if "minimum" not in restored.edited:
                    self.hooks.settings.set_minimum_minutes(
                        profile.minimum_minutes
                    )
            finally:
                self.selection.restoring_preferences = False
        loaded = self.selection.selection_state.loaded_mode is not None
        if "selection" in restored.edited:
            self.selection.reinspection_edits = restored
            self.selection.requires_reinspection = True
            self.hooks.log(
                "The source is a block device; inspect the current disc again"
                " before using the existing selections."
            )
        return (
            loaded
            and restored.has_saved_settings
            and "selection" not in restored.edited
        )

    def save_current_source_preferences(self) -> None:
        store = self.hooks.get_store()
        source = self.selection.active_source
        if store is None or not source or self.selection.restoring_preferences:
            return
        restored = self.selection.restored_source
        if (
            restored is None
            and store.source_key(source) in store.device_sources
        ):
            return
        previous = (
            restored.pending_preferences
            if restored is not None
            and restored.source == source
            and not restored.confirmed_non_device
            and restored.pending_preferences is not None
            else self.selection.source_preferences(store, source)
        )
        preferences = self.selection.source_snapshot(
            previous,
            is_bluray=self.hooks.is_bluray(),
            selected=self.hooks.selection.get_visible_selection(),
            profile_name=self.hooks.profiles.get_profile_choice()
            or store.active_profile,
            output_format=self.hooks.settings.get_output_format(),
            minimum_minutes=self.hooks.settings.get_minimum_minutes(),
        )
        if (
            restored is not None
            and restored.source == source
            and not restored.confirmed_non_device
        ):
            if not restored.save_pending:
                self.hooks.log(
                    "Source settings will be saved after the source is"
                    " identified."
                )
            restored.save_pending = True
            restored.pending_preferences = preferences
            return
        saved = self.selection.persist_source(
            store, preferences, log=self.hooks.log
        )
        if restored is not None and restored.source == source:
            restored.save_pending = not saved
            restored.pending_preferences = None if saved else preferences
