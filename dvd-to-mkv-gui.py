#!/usr/bin/env python3

from __future__ import annotations

import os
import signal
import struct
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk  # type: ignore[reportMissingModuleSource]  # noqa: I001

from dvdvideo.gui_support import ManagedProcess, filename_stem
from dvdvideo.gui_activity import ActivityBuffer, clean_text
from dvdvideo.gui_preferences import (
    DEFAULT_PROFILE_NAME,
    PreferencesStore,
    reusable_source,
)
from dvdvideo.gui_profile_dialog import ProfileDialog
from dvdvideo.gui_widgets import Choice, ResponseWindow
from dvdvideo.gui_dialogs import VerificationDialog, create_copy_summary, create_disc_details
from dvdvideo.gui_view import WindowView
from dvdvideo.gui_listing import Listing
from dvdvideo.gui_listing_view import ListingView
from dvdvideo.gui_listing_workflow import ListingContext, ListingWorkflow, ListingWorkflowHooks
from dvdvideo.gui_operations import (
    WorkerLifecycle,
)
from dvdvideo.gui_operation_controller import (
    DialogHooks, ListingHooks, OperationController, OperationControllerHooks,
    OperationInputs, OperationRuntimeHooks, OperationUiHooks,
)
from dvdvideo.gui_preference_workflow import (
    PreferenceSelectionHooks, PreferenceSettingsHooks, PreferenceWorkflow, ProfileUpdateCallback,
    PreferenceWorkflowHooks, ProfileChoiceHooks,
)
from dvdvideo.gui_selection import SelectionCoordinator
from dvdvideo.gui_source import source_name
from dvdvideo.gui_source_controller import (
    DriveUiHooks, SourceController, SourceControllerHooks, SourceRuntimeHooks, SourceUiHooks,
)
from dvdvideo.gui_source_workflow import SourceWorkflow
from dvdvideo.gui_preferences_io import PreferencesDurabilityError, PreferencesWriter
from dvdvideo.gui_tooltips import TOOLTIPS
from dvdvideo.gui_translation import identity, load_translation
from dvdvideo.optical_drives import OpticalDrive

SCRIPT_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dvd-to-mkv.sh")
MAX_LOG_CHARS: int = 512 * 1024
LOG_FOLLOW_THRESHOLD: int = 16
PREFERENCES_CLOSE_WAIT: float = 5
PREFERENCES_LOAD_WAIT: int = 5
# Inspection output comes from untrusted discs; keep the lists and labels bounded.
MAX_LABEL_CHARS: int = 200
def clean_label(text: str) -> str:
    text = clean_text(text)
    return text if len(text) <= MAX_LABEL_CHARS else text[:MAX_LABEL_CHARS - 1] + "…"


class DvdToMkvWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app, title="DVD to MKV")
        self.set_default_size(1180, 720)
        self.selection = SelectionCoordinator()
        self.source_workflow = SourceWorkflow()
        self.lifecycle = WorkerLifecycle()
        self.suggested_name: str = ""
        self.suggested_output: str = ""
        self.mode_outputs: dict[bool, str] = {False: "", True: ""}
        self.output_mode: bool = False
        self.close_dialog: ResponseWindow | None = None
        self.save_error_dialog: ResponseWindow | None = None
        self.save_wait_dialog: ResponseWindow | None = None
        self.load_wait_dialog: ResponseWindow | None = None
        self.preferences_load_timer: int | None = None
        self.preferences_loading = True
        self.discard_preferences_on_close = False
        self.restart_drive_scan_on_cancel = False
        self.close_timer: int | None = None
        self.preferences_wait_started = 0.0
        self.details_window: Gtk.Window | None = None
        self.activity_buffer = ActivityBuffer()
        self.applying_stream_format: bool = False
        self.preferences_error: str | None = None
        self.translate: Callable[[str], str] = identity
        self.active_gui_language = "en"
        self.preferences_writer = PreferencesWriter(self.log)
        self.preferences: PreferencesStore | None = None
        self.progress_timer: int | None = None

        self.follow_log = True
        self.view = WindowView(self)
        self.listing_view = ListingView(
            self.view.list_box,
            self.view.stream_list_box,
            self.view.streams_label,
            self.on_selection_changed,
            self.on_stream_toggled,
            translate=lambda message: self.translate(message),
            on_visibility_changed=self.update_selection_visibility,
        )
        self.bind_workflows()
        self.set_child(self.view.root)
        self.view.connect_final_signals()
        self.connect("close-request", self.on_close_request)
        self.log_timer: int | None = GLib.timeout_add(100, self.flush_log)
        self.on_mode_changed()
        self.view.root.set_sensitive(False)
        self.view.status_label.set_text("Loading preferences...")
        self.preferences_load_timer = GLib.timeout_add_seconds(PREFERENCES_LOAD_WAIT, self.on_preferences_load_wait)
        try:
            threading.Thread(target=self.load_preferences, daemon=True).start()
        except RuntimeError as error:
            self.finish_preferences_load(None, f"Could not start preferences loader: {error}")

    def load_preferences(self) -> None:
        try:
            store = PreferencesStore(writer=self.preferences_writer.submit)
            error = None
        except (OSError, ValueError) as exception:
            store = None
            error = f"GUI preferences are unavailable: {exception}"
        except Exception:  # noqa: BLE001 - report unexpected failures at the worker boundary
            store = None
            error = "Unexpected preferences loader error:\n" + traceback.format_exc().rstrip()
        translator = None
        translation_error = None
        try:
            translator = load_translation(store.gui_language if store else "system").gettext
        except (OSError, EOFError, ValueError, struct.error) as exception:
            translation_error = f"Could not load GUI translations: {exception}"
        GLib.idle_add(self.finish_preferences_load, store, error, translator, translation_error)

    def finish_preferences_load(
        self, store: PreferencesStore | None, error: str | None,
        translator: Callable[[str], str] | None = None, translation_error: str | None = None,
    ) -> bool:
        if not self.preferences_loading or self.lifecycle.closing:
            return False
        self.preferences_loading = False
        if self.preferences_load_timer is not None:
            GLib.source_remove(self.preferences_load_timer)
            self.preferences_load_timer = None
        if self.load_wait_dialog is not None:
            self.load_wait_dialog.destroy()
            self.load_wait_dialog = None
        self.preferences = store
        self.preferences_error = error
        self.selection.restoring_preferences = True
        try:
            if translation_error:
                self.log(translation_error)
            if translator is not None:
                self.translate = translator
                self.active_gui_language = store.gui_language if store else "system"
                self.view.translate(self.translate)
            self.populate_profile_choice(self.view.profile_combo)
            self.view.gui_language_combo.set_active_id(store.gui_language if store else "system")
        finally:
            self.selection.restoring_preferences = False
        self.view.profile_combo.set_sensitive(store is not None)
        self.view.gui_language_combo.set_sensitive(store is not None)
        self.view.root.set_sensitive(True)
        if error:
            self.log(error)
            self.view.status_label.set_text(self.translate("Preferences unavailable"))
        else:
            self.view.status_label.set_text(self.translate("Ready"))
        GLib.idle_add(self.scan_drives)
        return False

    def on_preferences_load_wait(self) -> bool:
        self.preferences_load_timer = None
        if not self.preferences_loading or self.lifecycle.closing:
            return False
        dialog = ResponseWindow(title="Preferences are still loading", transient_for=self)
        self.load_wait_dialog = dialog
        content = dialog.get_content_area()
        content.set_margin_top(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.append(Gtk.Label(
            label=("Preferences are taking longer than expected to load. Continue without preferences "
                   "to use the application without saving settings for this session."),
            wrap=True, max_width_chars=60, xalign=0,
        ))
        dialog.add_button("Close", Gtk.ResponseType.CLOSE, tooltip=self.translate(TOOLTIPS["close_loading"]))
        dialog.add_button(
            "Continue Without Preferences", Gtk.ResponseType.REJECT, tooltip=self.translate(TOOLTIPS["skip_loading"]),
        )
        dialog.add_button(
            "Wait", Gtk.ResponseType.APPLY, tooltip=self.translate(TOOLTIPS["wait_loading"]), default=True,
        )
        dialog.connect("response", self.on_preferences_load_response)
        dialog.present()
        return False

    def on_preferences_load_response(self, dialog: ResponseWindow, response: int) -> None:
        dialog.destroy()
        self.load_wait_dialog = None
        if response == Gtk.ResponseType.REJECT:
            self.finish_preferences_load(
                None, "Preferences were not loaded; saving is disabled for this session.",
            )
        elif response == Gtk.ResponseType.APPLY:
            self.preferences_load_timer = GLib.timeout_add_seconds(
                PREFERENCES_LOAD_WAIT, self.on_preferences_load_wait,
            )
        else:
            self.begin_close()

    def bind_workflows(self) -> None:
        self.preference_workflow = PreferenceWorkflow(
            self.selection,
            PreferenceWorkflowHooks(
                get_store=lambda: self.preferences,
                get_error=lambda: self.preferences_error,
                is_bluray=lambda: self.view.bluray_button.get_active(),
                profiles=ProfileChoiceHooks(
                    get_profile_choice=lambda: self.view.profile_combo.get_active_id(),
                    set_profile_choice=lambda name: self.view.profile_combo.set_active_id(name),
                    populate_profiles=lambda: self.populate_profile_choice(self.view.profile_combo),
                    present_profile_dialog=self.present_profile_dialog,
                ),
                settings=PreferenceSettingsHooks(
                    get_output_format=lambda: self.view.format_combo.get_active_id() or "auto",
                    set_output_format=lambda value: self.view.format_combo.set_active_id(value),
                    get_minimum_minutes=lambda: self.view.minimum_spin.get_value_as_int(),
                    set_minimum_minutes=lambda value: self.view.minimum_spin.set_value(value),
                ),
                selection=PreferenceSelectionHooks(
                    get_visible_selection=lambda: self.listing_view.active_ids(),
                    set_visible_selection=self.set_profile_selection,
                    clear_hidden_selections=lambda: self.clear_hidden_selections(
                        clear_playlists=False, notify_empty=False,
                    ),
                    update_stream_format=lambda playlist: self.update_stream_format(playlist),
                    reset_visibility=lambda: (
                        None if self.view.bluray_button.get_active()
                        else self.listing_view.set_show_unselected(False)
                    ),
                ),
                log=lambda message: self.log(message),
            ),
        )
        self.operation_controller = OperationController(
            self.lifecycle,
            self.selection,
            OperationControllerHooks(
                get_inputs=self.operation_inputs,
                listing=ListingHooks(
                    clear_rows=self.clear_rows,
                    load_rows=self.load_rows,
                    load_stream_rows=self.load_stream_rows,
                    update_stream_format=self.update_stream_format,
                    load_dvd_titles=self.load_dvd_titles,
                ),
                dialogs=DialogHooks(
                    show_disc_details=self.show_disc_details,
                    present_m2ts_confirmation=lambda playlist: self.confirm_m2ts(playlist),
                    present_verification_dialog=self.present_verification_dialog,
                    present_copy_summary=lambda text, respond: create_copy_summary(
                        self, text, respond, translate=self.translate,
                    ).present(),
                ),
                ui=OperationUiHooks(
                    log_inspect_action=self.log_inspect_action,
                    set_busy=self.set_busy,
                    set_status=self.set_status,
                    invalidate_choosers=self.invalidate_choosers,
                    set_output_path=self.view.output_entry.set_text,
                    notify_completion=self.notify_completion,
                    translate=lambda message: self.translate(message),
                    set_dvd_batch_count=self.set_dvd_batch_count,
                ),
                runtime=OperationRuntimeHooks(
                    cancel_drive_scan=self.source_workflow.cancel_drive_scan,
                    start_worker=self.start_worker,
                    dispatch_ui=self.dispatch_ui,
                    queue_log=self.queue_log,
                    log=self.log,
                    exclude_device_source=self.exclude_device_source,
                    confirm_non_device=lambda source: self.source_controller.confirm_non_device_source(source),
                    is_known_device_source=lambda source: (
                        reusable_source(source) and self.is_known_device_source(source)
                    ),
                ),
            ),
        )
        self.listing_workflow = ListingWorkflow(
            self.selection, self.listing_view, self.listing_context,
            ListingWorkflowHooks(
                clear_rows=self.clear_rows,
                suggest_name=self.suggest_name,
                log=self.log,
                set_status=self.set_status,
                save=self.save_current_source_preferences,
                streams_changed=self.on_stream_toggled,
                update_stream_format=self.update_stream_format,
                clean_label=clean_label,
                translate=lambda message: self.translate(message),
            ),
        )
        self.source_controller = SourceController(
            self.source_workflow,
            SourceControllerHooks(
                ui=SourceUiHooks(
                    get_source=lambda: self.view.source_entry.get_text(),
                    set_source=lambda source: self.view.source_entry.set_text(source),
                    invalidate_choosers=lambda: self.invalidate_choosers(),
                    prepare_source=lambda source: self.prepare_source_change(source),
                    set_status=self.set_status,
                    apply_detected_media=lambda media: self.apply_detected_media(media),
                    exclude_device=self.apply_device_exclusion,
                    confirm_non_device=self.apply_non_device_confirmation,
                    update_title_sensitivity=self.update_title_sensitivity,
                ),
                drives=DriveUiHooks(
                    render_drives=lambda drives, scanning, error: self.render_drives(
                        drives, scanning=scanning, error=error,
                    ),
                    should_clear_device_selections=self.is_known_device_source,
                    clear_device_selections=lambda: self.clear_device_selections(),
                    close_drive_menu=lambda: self.view.drives_popover.popdown(),
                ),
                runtime=SourceRuntimeHooks(
                    is_closing=lambda: self.lifecycle.closing,
                    is_running=lambda: self.lifecycle.running,
                    schedule_detection=lambda source, generation: GLib.timeout_add(
                        400, self.start_detection, source, generation
                    ),
                    remove_timeout=lambda timeout: GLib.source_remove(timeout),
                    start_worker=lambda worker: self.start_worker(worker),
                    dispatch_detection=lambda source, generation, media, error, device: GLib.idle_add(
                        self.finish_detection, source, generation, media, error, device,
                    ),
                    dispatch_exclude_device=lambda source, generation: GLib.idle_add(
                        self.exclude_device_source, source, generation,
                    ),
                    dispatch_drive_scan=lambda operation, drives, error: GLib.idle_add(
                        self.finish_drive_scan, operation, drives, error,
                    ),
                    dispatch_detection_done=lambda operation: GLib.idle_add(
                        self.source_controller.detection_finished, operation,
                    ),
                ),
                log=lambda message: self.log(message),
            ),
        )

    def operation_inputs(self) -> OperationInputs:
        source = self.view.source_entry.get_text()
        return OperationInputs(
            script_path=SCRIPT_PATH,
            source=source,
            is_bluray=self.view.bluray_button.get_active(),
            detecting=self.source_workflow.detecting,
            generation=self.source_workflow.source_generation,
            preferences=self.selection.source_preferences(self.preferences, source),
            output_path=self.view.output_entry.get_text(),
            output_format=self.view.format_combo.get_active_id() or "auto",
            output_name=self.view.name_entry.get_text(),
            minimum_minutes=self.view.minimum_spin.get_value_as_int(),
            selected=tuple(self.listing_view.active_ids()),
            dvd_title=self.selection.dvd_title,
            language=self.active_gui_language,
            profile=(
                self.preferences.get_profile(self.view.profile_combo.get_active_id())
                if self.preferences is not None else None
            ),
        )

    def listing_context(self) -> ListingContext:
        store = self.preferences
        settings = self.selection.source_preferences(store, self.view.source_entry.get_text())
        if settings is not None and settings.dvd_title != self.selection.dvd_title:
            from dataclasses import replace
            settings = replace(settings, dvd_streams=[], dvd_inspected=False)
        return ListingContext(
            is_bluray=self.view.bluray_button.get_active(),
            settings=settings,
            profile=store.get_profile(self.view.profile_combo.get_active_id()) if store is not None else None,
        )

    def log_inspect_action(self) -> None:
        if self.click_diagnostics is not None:
            self.click_diagnostics.log_action("Inspect Tracks activated")

    def setup_click_diagnostics(self, button: Gtk.Button) -> None:
        self.click_diagnostics = None
        if os.environ.get("DVD_BACKUP_DEBUG_CLICKS") == "1":
            from dvd_to_mkv_click_diagnostics import ClickDiagnostics

            self.click_diagnostics = ClickDiagnostics(
                self, button, self.click_diagnostic_state, self.log
            )

    def set_rows_active(self, *, streams: bool = False) -> None:
        if self.selection.restored_source is not None or self.selection.requires_reinspection:
            is_bluray = self.view.bluray_button.get_active()
            playlist = self.selection.stream_playlist if streams else None
            rows = self.listing_view.stream_rows if streams else self.listing_view.rows
            for _row, identifier in rows:
                self.selection.record_selection_edit(identifier, True, is_bluray=is_bluray, playlist=playlist)
            hidden = (
                self.selection.selection_state.omitted_streams.get(playlist, set())
                if streams and playlist is not None else
                self.selection.selection_state.omitted_ids if not streams else set[str]()
            )
            for identifier in hidden:
                self.selection.record_selection_edit(identifier, True, is_bluray=is_bluray, playlist=playlist)
        self.selection.applying_profile = True
        try:
            self.listing_view.set_all_active(streams=streams)
        finally:
            self.selection.applying_profile = False
        if (
            self.selection.stream_playlist is not None
            and self.operation_controller.playlist_format(self.selection.stream_playlist) != "m2ts"
        ):
            self.selection.update_stream_selection(
                self.selection.stream_playlist,
                self.listing_view.active_ids(streams=True),
            )
        self.save_current_source_preferences()

    def select_all_items(self, *_args: Any) -> None:
        count = self.selection.select_all_hidden_ids()
        self.set_rows_active()
        if count:
            self.log(f"Select All included {count} items hidden by the display limit.")

    def select_all_streams(self, *_args: Any) -> None:
        playlist = self.selection.stream_playlist
        if playlist is None or self.operation_controller.playlist_format(playlist) == "m2ts":
            return
        count = self.selection.select_all_hidden_streams(playlist)
        if count:
            self.log(f"Select All Tracks included {count} tracks hidden by the display limit.")
        self.set_rows_active(streams=True)

    def toggle_unselected(self, *_args: object) -> None:
        if self.lifecycle.running or self.lifecycle.closing:
            return
        self.listing_view.set_show_unselected(not self.listing_view.show_unselected)

    def update_selection_visibility(self) -> None:
        listing = self.listing_view
        self.view.show_unselected_button.set_label(self.translate(
            "Hide unselected" if listing.show_unselected else "Show unselected"
        ))
        self.view.show_unselected_button.set_tooltip_text(self.translate(
            TOOLTIPS["hide_unselected" if listing.show_unselected else "show_unselected"]
        ))
        self.view.items_empty_label.set_visible(
            bool(listing.rows) and not listing.show_unselected and not listing.active_ids()
        )
        self.view.tracks_empty_label.set_visible(
            bool(listing.stream_rows) and not listing.show_unselected and not listing.active_ids(streams=True)
        )

    def clear_hidden_selections(
        self, *_args: Any, clear_playlists: bool = True, notify_empty: bool = True,
    ) -> bool:
        changed = self.selection.clear_hidden(
            self.preferences, is_bluray=self.view.bluray_button.get_active(),
            clear_ids=clear_playlists, log=self.log,
        )
        if changed is None:
            return False
        if not changed:
            if notify_empty and (self.selection.restored_source is not None or self.selection.requires_reinspection):
                self.log("No inspected hidden selections to clear.")
            return True
        if notify_empty:
            self.selection.mark_source_edit("selection")
            is_bluray = self.view.bluray_button.get_active()
            if not is_bluray or clear_playlists:
                for identifier in self.selection.selection_state.omitted_ids:
                    self.selection.record_selection_edit(identifier, False, is_bluray=is_bluray)
            if is_bluray:
                for playlist, identifiers in self.selection.selection_state.omitted_streams.items():
                    for identifier in identifiers:
                        self.selection.record_selection_edit(
                            identifier, False, is_bluray=True, playlist=playlist,
                        )
        self.log("Cleared selections hidden by display limits.")
        return True

    def populate_profile_combo(self) -> None:
        self.preference_workflow.populate_profiles()

    def populate_profile_choice(self, combo: Choice) -> None:
        restoring = self.selection.restoring_preferences
        self.selection.restoring_preferences = True
        try:
            combo.remove_all()
            if self.preferences is None:
                combo.append(DEFAULT_PROFILE_NAME, DEFAULT_PROFILE_NAME)
                combo.set_active_id(DEFAULT_PROFILE_NAME)
                combo.set_sensitive(False)
                return
            for name in self.preferences.profile_names:
                combo.append(name, name)
            combo.set_active_id(self.preferences.active_profile)
        finally:
            self.selection.restoring_preferences = restoring

    def show_profile_dialog(self, *_args: Any) -> None:
        self.preference_workflow.show_profile_dialog()

    def present_profile_dialog(
        self,
        store: PreferencesStore,
        on_changed: ProfileUpdateCallback,
        current_profile: str | None,
    ) -> None:
        dialog = ProfileDialog(
            self, store, on_changed, current_profile=current_profile,
            get_current_profile=self.view.profile_combo.get_active_id,
            translate=self.translate,
        )
        dialog.present()

    def on_profiles_updated(
        self, name: str, apply_profile: bool = True, replacement: tuple[str, str] | None = None,
    ) -> tuple[str, bool]:
        return self.preference_workflow.profiles_updated(name, apply_profile, replacement)

    def on_profile_changed(self, *_args: Any) -> None:
        self.preference_workflow.profile_changed()

    def on_gui_language_changed(self, *_args: object) -> None:
        store = self.preferences
        if self.selection.restoring_preferences or store is None:
            return
        language = self.view.gui_language_combo.get_active_id()
        if language is None:
            self.log("Could not save GUI language: no language selected.")
            return
        if language == store.gui_language:
            return
        try:
            store.set_gui_language(language)
        except PreferencesDurabilityError as error:
            self.log(str(error))
        except (OSError, ValueError) as error:
            self.log(f"Could not save GUI language: {error}")
            self.selection.restoring_preferences = True
            try:
                self.view.gui_language_combo.set_active_id(store.gui_language)
            finally:
                self.selection.restoring_preferences = False

    def apply_profile_to_rows(self, apply_settings: bool = True) -> bool:
        return self.preference_workflow.apply_profile_to_rows(apply_settings)

    def set_profile_selection(self, selected: set[str], is_bluray: bool) -> None:
        if is_bluray:
            if self.selection.stream_playlist is not None:
                self.listing_view.set_active_ids(selected, streams=True)
        else:
            self.listing_view.set_active_ids(selected)

    def restore_source_preferences(self, source: str) -> None:
        self.preference_workflow.restore_source_preferences(source)
        saved = self.selection.source_preferences(self.preferences, source)
        self.set_dvd_title_choices([], saved.dvd_title if saved is not None else "1")

    def save_current_source_preferences(self) -> None:
        self.preference_workflow.save_current_source_preferences()

    def on_selection_changed(self, *_args: Any) -> None:
        if not self.selection.applying_profile:
            if _args and (self.selection.restored_source is not None or self.selection.requires_reinspection):
                for row, identifier in self.listing_view.rows:
                    if row is _args[0]:
                        self.selection.record_selection_edit(
                            identifier, row.get_active(), is_bluray=self.view.bluray_button.get_active(),
                        )
                        break
            self.save_current_source_preferences()

    def set_status(self, status: str) -> None:
        self.view.status_label.set_text(self.translate(status))

    def on_status_changed(self, label: Gtk.Label, _param: Any) -> None:
        if self.lifecycle.running:
            self.view.progress_bar.set_text(label.get_text())
        if hasattr(label, "announce"):
            label.announce(label.get_text(), Gtk.AccessibleAnnouncementPriority.MEDIUM)

    def invalidate_choosers(self, *_args: Any) -> None:
        self.source_workflow.chooser_generation += 1

    def on_output_changed(self, *_args: Any) -> None:
        self.invalidate_choosers()
        self.mode_outputs[self.output_mode] = self.view.output_entry.get_text()

    def scan_drives(self, *_args: Any) -> None:
        self.source_controller.scan_drives()

    def finish_drive_scan(
        self, operation: ManagedProcess, drives: list[OpticalDrive], error: str | None,
    ) -> None:
        self.source_controller.finish_drive_scan(operation, drives, error)

    def render_drives(
        self, drives: list[OpticalDrive], *, scanning: bool = False, cancelling: bool = False,
        error: str | None = None,
    ) -> None:
        while (child := self.view.drives_box.get_first_child()) is not None:
            self.view.drives_box.remove(child)
        self.view.drives_button.set_label(
            self.translate("Drives (cancelling...)") if cancelling else
            self.translate("Drives (scanning...)") if scanning else self.translate("Drives")
        )
        if scanning or error or not drives:
            self.view.drives_box.append(Gtk.Label(
                label=self.translate("Cancelling drive scan...") if cancelling else
                self.translate("Scanning drives...") if scanning else
                self.translate("Scan failed; see Activity") if error else self.translate("No optical drives found"),
            ))
        for drive in drives:
            button = Gtk.Button(label=clean_label(f"{drive.path} - {drive.name}: {drive.status}"))
            button.set_tooltip_text(self.translate(TOOLTIPS["select_drive"]))
            button.set_sensitive(drive.media is not None)
            button.connect("clicked", self.select_drive, drive)
            self.view.drives_box.append(button)
        refresh = Gtk.Button(label=self.translate("Refresh drives"))
        refresh.set_tooltip_text(self.translate(TOOLTIPS["refresh_drives"]))
        refresh.set_sensitive(not scanning)
        refresh.connect("clicked", self.scan_drives)
        self.view.drives_box.append(refresh)

    def select_drive(self, _button: Gtk.Button, drive: OpticalDrive) -> None:
        self.source_controller.select_drive(drive)

    def on_mode_changed(self, button: Gtk.ToggleButton | None = None) -> None:
        if button is not None and not button.get_active():
            return
        self.cancel_detection()
        is_bluray: bool = self.view.bluray_button.get_active()
        mode_changed = is_bluray != self.output_mode
        if mode_changed:
            self.mode_outputs[self.output_mode] = self.view.output_entry.get_text()
            self.output_mode: bool = is_bluray
            self.view.output_entry.set_text(self.mode_outputs[is_bluray])
        self.invalidate_choosers()
        self.view.minlength_row.set_visible(is_bluray)
        self.view.inspect_streams_button.set_visible(is_bluray)
        self.view.streams_box.set_visible(is_bluray)
        self.view.select_all_streams_button.set_visible(is_bluray)
        self.view.name_row.set_visible(is_bluray)
        self.view.format_box.set_visible(is_bluray)
        self.view.dvd_title_row.set_visible(not is_bluray)
        self.view.output_entry.set_placeholder_text(
            self.translate("Choose output directory" if is_bluray else "Choose output MKV")
        )
        requires_reinspection = self.selection.requires_reinspection
        self.clear_rows()
        if mode_changed:
            self.listing_view.set_show_unselected(is_bluray)
            if not self.source_workflow.applying_detection:
                self.selection.reinspection_edits = None
        self.selection.requires_reinspection = requires_reinspection
        self.suggest_name()

    def on_manual_mode(self, *_args: Any) -> None:
        if not self.source_workflow.applying_detection:
            self.cancel_detection()
            self.set_status("Media mode selected manually")

    def on_source_changed(self, *_args: Any) -> None:
        self.source_controller.source_changed(self.view.source_entry.get_text())

    def prepare_source_change(self, source: str) -> None:
        self.clear_rows()
        self.selection.reinspection_edits = None
        self.selection.active_source = source
        if source and not reusable_source(source):
            self.log(
                "Device source: saved track choices are not restored or saved because "
                "the inserted disc's identity is unknown. Your language profile still applies."
            )
        self.selection.selection_state.clear()
        self.restore_source_preferences(source)
        self.suggest_name()

    def cancel_detection(self) -> None:
        self.source_controller.cancel_detection()

    def start_detection(self, source: str, generation: int) -> bool:
        return self.source_controller.start_detection(source, generation)

    def exclude_device_source(self, source: str, generation: int | None = None) -> None:
        self.source_controller.exclude_device_source(source, generation)

    def is_known_device_source(self, source: str) -> bool:
        return not reusable_source(source) or self.has_device_classification(source)

    def has_device_classification(self, source: str) -> bool:
        key = PreferencesStore.source_key(source)
        return (
            key in self.source_workflow.device_sources
            or self.preferences is not None and key in self.preferences.device_sources
        )

    def apply_non_device_confirmation(self, source: str) -> None:
        if self.has_device_classification(source) and self.selection.restored_source is None:
            self.source_workflow.source_generation += 1
            self.invalidate_choosers()
            self.clear_rows()
            self.selection.reinspection_edits = None
        self.preference_workflow.confirm_non_device(source)

    def apply_device_exclusion(self, source: str) -> None:
        restored = self.selection.restored_source
        should_clear = self.preference_workflow.exclude_device_source(source)
        if (restored is not None and restored.source == source
                and (restored.has_saved_settings or "selection" in restored.edited)):
            self.source_workflow.source_generation += 1
            self.invalidate_choosers()
        if should_clear:
            self.clear_rows()
            self.log("Saved disc selections were cleared; inspect the current disc again.")
        if restored is not None and restored.source == source and restored.has_saved_settings:
            self.set_dvd_title_choices([], "1")

    def clear_device_selections(self) -> None:
        self.source_workflow.source_generation += 1
        self.invalidate_choosers()
        self.clear_rows()
        self.set_dvd_title_choices([], "1")
        self.selection.reinspection_edits = None
        self.log("Drive refresh cleared disc selections; inspect the current disc again.")
        if self.operation_controller.dvd_batch is not None:
            self.view.dvd_batch_label.set_text(self.translate("DVD batch needs preparation again"))
            self.log("The prepared DVD batch no longer matches the drive. Prepare it again or clear the batch.")

    def apply_detected_media(self, media: str) -> None:
        button: Gtk.ToggleButton = self.view.bluray_button if media == "bluray" else self.view.dvd_button
        button.set_active(True)

    def start_worker(self, worker: Callable[[], None]) -> bool:
        return self.lifecycle.start_worker(worker, self.log)

    def finish_detection(
        self, source: str, generation: int, media: str | None, error: str | None,
        device: bool | None,
    ) -> None:
        self.source_controller.finish_detection(source, generation, media, error, device)

    def suggest_name(self, name: str | None = None) -> None:
        source: str = self.view.source_entry.get_text()
        suggestion: str = filename_stem(name) if name else source_name(source) if source else ""
        if not suggestion and source:
            suggestion: str = source_name(source)
        if self.view.name_entry.get_text() in ("", self.suggested_name):
            self.view.name_entry.set_text(suggestion)
        self.suggested_name: str = suggestion
        if not self.view.bluray_button.get_active():
            output: str = f"{suggestion}.mkv" if suggestion else ""
            if self.view.output_entry.get_text() in ("", self.suggested_output):
                self.view.output_entry.set_text(output)
            self.suggested_output: str = output

    def choose_source_file(self, *_args: Any) -> None:
        self.choose_path("Choose ISO or image file", "open", self.view.source_entry)

    def choose_source_folder(self, *_args: Any) -> None:
        self.choose_path("Choose source folder", "select_folder", self.view.source_entry)

    def choose_output(self, *_args: Any) -> None:
        action: str = "select_folder" if self.view.bluray_button.get_active() else "save"
        self.choose_path("Choose output", action, self.view.output_entry)

    def choose_path(self, title: str, action: str, entry: Gtk.Entry) -> None:
        if self.lifecycle.running or self.lifecycle.closing:
            return
        if Gtk.check_version(4, 10, 0) is None:
            self.choose_modern_path(title, action, entry)
        else:
            self.choose_native_path(title, action, entry)

    def choose_modern_path(self, title: str, action: str, entry: Gtk.Entry) -> None:
        dialog = Gtk.FileDialog(title=self.translate(title))
        path = entry.get_text()
        if path:
            location = Gio.File.new_for_path(os.path.abspath(path))
            if action == "save":
                dialog.set_initial_name(os.path.basename(path))
                dialog.set_initial_folder(location.get_parent())
            elif action == "select_folder":
                dialog.set_initial_folder(location)
            else:
                dialog.set_initial_file(location)
        operations: dict[str, tuple[
            Callable[[Gtk.Window, Gio.Cancellable | None, Callable[[Gtk.FileDialog, Gio.AsyncResult], None]], None],
            Callable[[Gio.AsyncResult], Gio.File],
        ]] = {
            "open": (dialog.open, dialog.open_finish),
            "save": (dialog.save, dialog.save_finish),
            "select_folder": (dialog.select_folder, dialog.select_folder_finish),
        }
        begin, finish = operations[action]
        generation = self.source_workflow.chooser_generation

        def chosen(_dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                selected = finish(result)
            except GLib.Error as error:
                if not any(
                    error.matches(Gtk.DialogError.quark(), code)
                    for code in (Gtk.DialogError.CANCELLED, Gtk.DialogError.DISMISSED)
                ):
                    self.log(f"File chooser failed: {error.message}")
                return
            self.apply_chosen_file(selected, entry, generation)

        begin(self, None, chosen)

    def choose_native_path(self, title: str, action: str, entry: Gtk.Entry) -> None:
        actions: dict[str, Gtk.FileChooserAction] = {
            "open": Gtk.FileChooserAction.OPEN,
            "save": Gtk.FileChooserAction.SAVE,
            "select_folder": Gtk.FileChooserAction.SELECT_FOLDER,
        }
        dialog: Gtk.FileChooserNative = Gtk.FileChooserNative.new(
            self.translate(title), self, actions[action], self.translate("_Select"), self.translate("_Cancel")
        )
        path = entry.get_text()
        if path:
            location: Gio.File = Gio.File.new_for_path(os.path.abspath(path))
            try:
                if action == "save":
                    dialog.set_current_name(os.path.basename(path))
                    initialized: bool = dialog.set_current_folder(location.get_parent())
                elif action == "select_folder":
                    initialized: bool = dialog.set_current_folder(location)
                else:
                    initialized: bool = dialog.set_file(location)
                if not initialized:
                    self.log(f"File chooser could not initialize location: {path}")
            except GLib.Error as error:
                self.log(f"File chooser could not initialize location {path}: {error.message}")
        dialog.connect("response", self.on_native_file_chosen, entry, self.source_workflow.chooser_generation)
        dialog.show()

    def apply_chosen_file(
        self, selected: Gio.File | None, entry: Gtk.Entry, generation: int
    ) -> None:
        self.source_controller.apply_chosen_file(
            selected is not None,
            selected.get_path() if selected is not None else None,
            generation,
            entry.set_text,
        )

    def on_native_file_chosen(
        self, dialog: Gtk.FileChooserNative, response: int, entry: Gtk.Entry, generation: int
    ) -> None:
        try:
            if response == Gtk.ResponseType.ACCEPT:
                self.apply_chosen_file(dialog.get_file(), entry, generation)
            elif response not in (Gtk.ResponseType.CANCEL, Gtk.ResponseType.DELETE_EVENT):
                self.log(f"File chooser failed (response {response}).")
        except GLib.Error as error:
            self.log(f"File chooser failed: {error.message}")
        finally:
            dialog.destroy()

    def clear_rows(self) -> None:
        show_unselected = self.listing_view.show_unselected
        self.selection.selection_state.clear()
        self.listing_view.clear_rows()
        if self.view.bluray_button.get_active():
            self.listing_view.set_show_unselected(show_unselected)
        combo = self.view.track_playlist_combo
        combo.handler_block_by_func(self.on_track_playlist_changed)
        try:
            combo.remove_all()
        finally:
            combo.handler_unblock_by_func(self.on_track_playlist_changed)
        self.clear_stream_rows()
        self.selection.clear()
        self.applying_stream_format = False

    def clear_all(self, *_args: Any) -> None:
        if self.lifecycle.running or self.lifecycle.closing:
            return
        if self.operation_controller.dvd_batch is not None:
            self.operation_controller.clear_dvd_batch()
        self.source_workflow.source_generation += 1
        self.invalidate_choosers()
        self.cancel_detection()
        if self.source_workflow.drive_scan is not None:
            self.source_workflow.cancel_drive_scan(discard=True)
            self.render_drives([], scanning=True, cancelling=True)
        self.view.source_entry.handler_block_by_func(self.on_source_changed)
        try:
            self.view.source_entry.set_text("")
        finally:
            self.view.source_entry.handler_unblock_by_func(self.on_source_changed)
        self.selection.active_source = ""
        pending = self.selection.restored_source
        self.selection.restored_source = None
        self.selection.reinspection_edits = None
        self.clear_rows()
        self.set_dvd_title_choices([], "1")
        self.view.output_entry.set_text("")
        self.mode_outputs = {False: "", True: ""}
        self.view.name_entry.set_text("")
        self.suggested_name = ""
        self.suggested_output = ""
        if self.details_window is not None:
            self.details_window.destroy()
            self.details_window = None
        self.activity_buffer.clear()
        self.view.log_buffer.set_text("")
        if pending is not None and pending.save_pending:
            reason = "the save failed" if pending.confirmed_non_device else "the source was not identified"
            self.log(f"Source settings were not saved because {reason}.")
        self.follow_log = True
        self.set_status("Choose a source")
        self.view.source_entry.grab_focus()

    def on_format_changed(self, *_args: Any) -> None:
        self.selection.mark_source_edit("format")
        self.selection.m2ts_confirmed.clear()
        if self.selection.stream_playlist is not None:
            self.update_stream_format(self.selection.stream_playlist)
        self.save_current_source_preferences()

    def on_source_setting_changed(self, *_args: Any) -> None:
        self.selection.mark_source_edit("minimum")
        self.save_current_source_preferences()

    def update_stream_format(self, playlist: str) -> None:
        all_streams: bool = self.operation_controller.playlist_format(playlist) == "m2ts"
        self.view.select_all_streams_button.set_sensitive(not all_streams and not self.lifecycle.running)
        label = (
            self.translate("Playlist {playlist}: original M2TS preserves ALL streams (filtering disabled)")
            if all_streams else self.translate("Streams for playlist {playlist} — MKV")
        ).format(playlist=playlist)
        self.applying_stream_format = True
        try:
            self.listing_view.update_stream_format(
                label,
                self.selection.playlist_streams.get(playlist, []),
                all_streams=all_streams,
                sensitive=not all_streams and not self.lifecycle.running,
            )
        finally:
            self.applying_stream_format = False

    def clear_stream_rows(self) -> None:
        self.listing_view.clear_stream_rows()
        self.selection.stream_playlist = None

    def append_log(self, text: str) -> None:
        end: Gtk.TextIter = self.view.log_buffer.get_end_iter()
        self.view.log_buffer.insert(end, text)
        excess: int = self.view.log_buffer.get_char_count() - MAX_LOG_CHARS
        if excess > 0:
            self.view.log_buffer.delete(self.view.log_buffer.get_start_iter(), self.view.log_buffer.get_iter_at_offset(excess))
        self.scroll_log_to_end()

    def on_log_scrolled(self, adjustment: Gtk.Adjustment) -> None:
        bottom = adjustment.get_upper() - adjustment.get_page_size()
        self.follow_log = adjustment.get_value() >= bottom - LOG_FOLLOW_THRESHOLD

    def scroll_log_to_end(self, *_args: Any) -> None:
        if self.follow_log:
            self.view.log_adjustment.set_value(
                self.view.log_adjustment.get_upper() - self.view.log_adjustment.get_page_size()
            )

    def log(self, text: str) -> None:
        self.queue_log(f"{text}\n")

    def queue_log(self, text: str) -> None:
        self.activity_buffer.queue(text)

    def flush_log(self) -> bool:
        text, dropped = self.activity_buffer.drain()
        if dropped:
            text = f"[Activity omitted {dropped} older characters to keep the interface responsive.]\n" + text
            # Start the notice on its own line so it cannot extend an unfinished one.
            end: Gtk.TextIter = self.view.log_buffer.get_end_iter()
            if end.backward_char() and end.get_char() != "\n":
                text = "\n" + text
        if text:
            self.append_log(text)
        self.lifecycle.prune()
        return True

    def inspect(self, *_args: Any) -> None:
        self.operation_controller.inspect()

    def discover_dvd_titles(self, *_args: Any) -> None:
        self.operation_controller.discover_dvd_titles()

    def choose_dvd_batch(self, *_args: Any) -> None:
        def discovered(listing: Listing) -> None:
            self.load_dvd_titles(listing)
            inputs = self.operation_inputs()
            if inputs.profile is None or not (
                inputs.profile.audio_languages or inputs.profile.subtitle_languages
            ):
                dialog = ResponseWindow(title=self.translate("Language profile required"), transient_for=self)
                content = dialog.get_content_area()
                content.set_margin_top(12)
                content.set_margin_start(12)
                content.set_margin_end(12)
                content.append(Gtk.Label(
                    label=self.translate(
                        "Choose or create a profile with audio or subtitle languages before preparing a DVD batch."
                    ),
                    wrap=True, xalign=0,
                ))
                dialog.add_button(
                    self.translate("Close"), Gtk.ResponseType.CLOSE, tooltip=self.translate("Close"),
                )
                dialog.connect("response", lambda window, _response: window.destroy())
                dialog.present()
                return
            dialog = ResponseWindow(title=self.translate("Select DVD Titles"), transient_for=self)
            dialog.set_default_size(700, 500)
            content = dialog.get_content_area()
            content.set_margin_top(12)
            content.set_margin_start(12)
            content.set_margin_end(12)
            content.append(Gtk.Label(
                label=self.translate(
                    "Select titles to copy. Each title is inspected separately and uses the active language profile."
                ),
                wrap=True, xalign=0,
            ))
            rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            checkboxes: list[tuple[str, Gtk.CheckButton]] = []
            for identifier, label in listing.rows:
                checkbox = Gtk.CheckButton(label=clean_label(label))
                rows.append(checkbox)
                checkboxes.append((identifier, checkbox))
            scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
            scroll.set_child(rows)
            content.append(scroll)
            select_all = Gtk.Button(label=self.translate("Select All"), halign=Gtk.Align.START)
            def select_titles(_button: Gtk.Button) -> None:
                for _identifier, checkbox in checkboxes:
                    checkbox.set_active(True)

            select_all.connect("clicked", select_titles)
            content.append(select_all)
            dialog.add_button(self.translate("Cancel"), Gtk.ResponseType.CANCEL, tooltip=self.translate("Cancel"))
            dialog.add_button(
                self.translate("Prepare Batch"), Gtk.ResponseType.OK,
                tooltip=self.translate("Inspect the selected titles and prepare the batch."),
            )

            def response(_dialog: ResponseWindow, result: int) -> None:
                if result == Gtk.ResponseType.OK:
                    selected = tuple(identifier for identifier, checkbox in checkboxes if checkbox.get_active())
                    if not selected:
                        self.log("Select at least one DVD title.")
                        return
                    if self.operation_inputs() != inputs:
                        self.log("DVD batch not started because the source or settings changed.")
                        dialog.destroy()
                        return
                    dialog.destroy()
                    self.operation_controller.prepare_dvd_batch(selected)
                else:
                    dialog.destroy()

            dialog.connect("response", response)
            dialog.present()

        self.operation_controller.discover_dvd_titles(discovered)

    def set_dvd_batch_count(self, count: int) -> None:
        self.view.dvd_batch_label.set_text(
            self.translate("DVD batch prepared: {count} title(s)").format(count=count)
        )
        self.view.dvd_batch_row.set_visible(count > 0)

    def clear_dvd_batch(self, *_args: Any) -> None:
        self.operation_controller.clear_dvd_batch()

    def set_dvd_title_choices(self, rows: list[tuple[str, str]], title: str) -> None:
        combo = self.view.dvd_title_combo
        combo.handler_block_by_func(self.on_dvd_title_changed)
        try:
            combo.remove_all()
            identifiers = {identifier for identifier, _label in rows}
            if "1" not in identifiers:
                combo.append("1", self.translate("Title 1 (default)"))
            for identifier, label in rows:
                combo.append(identifier, clean_label(label))
            if title not in identifiers and title != "1":
                combo.append(title, self.translate("Title {title} (previously selected)").format(title=title))
            combo.set_active_id(title)
            self.selection.dvd_title = title
        finally:
            combo.handler_unblock_by_func(self.on_dvd_title_changed)

    def load_dvd_titles(self, listing: Listing) -> None:
        title = self.selection.dvd_title
        self.set_dvd_title_choices(listing.rows, title)
        self.log(
            f"Found {len(listing.rows)} DVD titles. Duration and track counts are hints; "
            "select a different title to inspect its streams automatically."
        )
        if title not in {identifier for identifier, _label in listing.rows}:
            self.log(f"Selected title {title} was not listed; choose a listed title or inspect it manually.")

    def on_dvd_title_changed(self, *_args: Any) -> None:
        title = self.view.dvd_title_combo.get_active_id()
        if title is None or title == self.selection.dvd_title:
            return
        if self.lifecycle.running or self.lifecycle.closing or self.source_workflow.detecting:
            combo = self.view.dvd_title_combo
            combo.handler_block_by_func(self.on_dvd_title_changed)
            try:
                combo.set_active_id(self.selection.dvd_title)
            finally:
                combo.handler_unblock_by_func(self.on_dvd_title_changed)
            self.log("Wait for the current operation before selecting a DVD title.")
            return
        previous_inputs = self.operation_inputs()
        self.source_workflow.source_generation += 1
        self.invalidate_choosers()
        self.clear_rows()
        self.selection.change_dvd_title(title)
        self.operation_controller.preserve_batch_on_title_change(previous_inputs)
        self.save_current_source_preferences()
        self.log(f"DVD title {title} selected; inspecting its streams.")
        self.inspect()

    def preview_selected(self, *_args: Any) -> None:
        self.operation_controller.preview_selected()

    def inspect_disc_details(self, *_args: Any) -> None:
        self.operation_controller.inspect_disc_details()

    def show_disc_details(self, text: str, source: str) -> None:
        if self.details_window is not None:
            self.details_window.destroy()
        dialog = create_disc_details(
            self, clean_label(source), text, self.close_disc_details, translate=self.translate,
        )
        self.details_window = dialog
        dialog.present()

    def close_disc_details(self, dialog: Gtk.Window) -> bool:
        if self.details_window is dialog:
            self.details_window = None
        return False

    def copy_selected(self, *_args: Any) -> None:
        self.operation_controller.copy_selected()

    def confirm_m2ts(self, playlist: str) -> None:
        dialog = ResponseWindow(title=self.translate("Preserve all streams?"), transient_for=self)
        content = dialog.get_content_area()
        content.set_margin_top(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_spacing(8)
        content.append(Gtk.Label(
            label=self.translate("Playlist {playlist}: preserve all streams as M2TS?").format(playlist=playlist),
            wrap=True, xalign=0,
        ))
        content.append(Gtk.Label(
            label=self.translate(
                "Your filtered selection cannot be applied to original M2TS. "
                "Both video views and all audio/subtitles will be copied."
            ),
            wrap=True, xalign=0,
        ))
        dialog.add_button(
            self.translate("Cancel"), Gtk.ResponseType.CANCEL, tooltip=self.translate(TOOLTIPS["keep_selection"]),
        )
        dialog.add_button(self.translate("OK"), Gtk.ResponseType.OK, tooltip=self.translate(TOOLTIPS["preserve_all"]))
        generation: int = self.source_workflow.source_generation

        def response(dialog: ResponseWindow, response: int) -> None:
            dialog.destroy()
            self.operation_controller.confirm_m2ts_response(
                playlist, generation, response == Gtk.ResponseType.OK,
            )

        dialog.connect("response", response)
        dialog.present()

    def click_diagnostic_state(self) -> str:
        selected = self.listing_view.active_ids()
        return (
            f"running={self.lifecycle.running}, detecting={self.source_workflow.detecting}, closing={self.lifecycle.closing}, "
            f"inspect-sensitive={self.view.inspect_streams_button.is_sensitive()}, "
            f"selected={','.join(selected) or 'none'}"
        )

    def inspect_streams(self, *_args: Any) -> None:
        playlist = self.view.track_playlist_combo.get_active_id()
        if playlist is None:
            self.log_inspect_action()
            self.log("Choose a playlist to inspect its video, audio, and subtitles.")
            return
        self.operation_controller.inspect_playlist(playlist)

    def on_track_playlist_changed(self, *_args: Any) -> None:
        playlist = self.view.track_playlist_combo.get_active_id()
        if playlist is None:
            self.clear_stream_rows()
            return
        if playlist == self.selection.stream_playlist:
            return
        if self.lifecycle.running or self.lifecycle.closing or self.source_workflow.detecting:
            self.log("Wait for the current operation before choosing a playlist.")
            return
        self.clear_stream_rows()
        self.operation_controller.inspect_playlist(playlist)

    def show_verify_dialog(self, *_args: Any) -> None:
        self.operation_controller.show_verify_dialog()

    def present_verification_dialog(
        self, source: str, bluray: bool, title: str, streams: list[str], generation: int,
    ) -> None:
        dialog = VerificationDialog(
            self, bluray=bluray, title=title, streams=streams,
            choose_copy=lambda entry: self.choose_path("Choose existing copy", "open", entry),
            translate=self.translate,
        )

        def response(dialog: VerificationDialog, result: int) -> None:
            close = self.operation_controller.verification_response(
                source,
                bluray,
                generation,
                accepted=result == Gtk.ResponseType.OK,
                copy_path=dialog.copy_entry.get_text(),
                title=dialog.title_entry.get_text(),
                streams_text=dialog.stream_entry.get_text(),
            )
            if close:
                dialog.destroy()

        dialog.connect("response", response)
        dialog.present()

    def set_busy(self, busy: bool) -> None:
        if busy:
            self.activity_buffer.start_progress()
            self.view.progress_bar.set_text(self.view.status_label.get_text())
            self.view.progress_bar.set_visible(True)
            self.view.progress_bar.pulse()
            if self.progress_timer is None:
                self.progress_timer = GLib.timeout_add(100, self.pulse_progress)
        else:
            self.activity_buffer.stop_progress()
            if self.progress_timer is not None:
                GLib.source_remove(self.progress_timer)
                self.progress_timer = None
            self.view.progress_bar.set_fraction(0)
            self.view.progress_bar.set_text("")
            self.view.progress_bar.set_visible(False)
        for widget in (
            self.view.inspect_button, self.view.copy_button, self.view.inspect_streams_button,
            self.view.source_entry.get_parent(), self.view.output_entry.get_parent(),
            self.view.name_row, self.view.minlength_row, self.view.profile_row, self.view.list_box, self.view.stream_list_box,
            self.view.dvd_title_row,
            self.view.dvd_batch_row,
            self.view.track_playlist_combo,
            self.view.dvd_button, self.view.bluray_button, self.view.select_all_items_button,
            self.view.show_unselected_button,
            self.view.select_all_streams_button,
            self.view.format_box,
            self.view.details_button,
            self.view.preview_button,
            self.view.clear_all_button,
            self.view.verify_button,
        ):
            if widget is not None:
                widget.set_sensitive(not busy)
        self.view.cancel_button.set_sensitive(busy)
        self.update_title_sensitivity()
        if self.selection.stream_playlist is not None:
            self.update_stream_format(self.selection.stream_playlist)

    def update_title_sensitivity(self) -> None:
        self.view.dvd_title_row.set_sensitive(
            not self.source_workflow.detecting and not self.lifecycle.running and not self.lifecycle.closing
        )

    @staticmethod
    def dispatch_ui(action: Callable[[], object]) -> int:
        def once() -> bool:
            action()
            return False

        return GLib.idle_add(once)

    def pulse_progress(self) -> bool:
        progress = self.activity_buffer.progress()
        phase = self.view.status_label.get_text()
        operation = self.lifecycle.operation
        if progress is None or (operation is not None and operation.cancelled.is_set()):
            self.view.progress_bar.set_text(
                self.translate("{phase} (percentage and ETA unavailable)").format(phase=phase)
            )
            self.view.progress_bar.pulse()
        else:
            fraction, eta = progress
            remaining = (
                f"{eta // 3600:02}:{eta // 60 % 60:02}:{eta % 60:02}"
                if eta is not None else self.translate("unavailable")
            )
            self.view.progress_bar.set_fraction(fraction)
            self.view.progress_bar.set_text(
                self.translate("{phase} {fraction:.0%} - phase ETA {remaining}").format(
                    phase=phase, fraction=fraction, remaining=remaining,
                )
            )
        return True

    def cancel_operation(self, *_args: Any) -> None:
        if self.lifecycle.operation is not None:
            self.lifecycle.operation.cancel()
            self.view.cancel_button.set_sensitive(False)
            self.set_status("Cancelling...")

    def notify_completion(self, kind: str, status: str) -> None:
        if self.lifecycle.closing or self.is_active():
            return
        app = self.get_application()
        if app is None:
            self.log("Cannot send completion notification: the window has no application.")
            return
        failed = status == "Failed"
        if kind == "Verification":
            title = self.translate("Verification failed") if failed else self.translate("Verification completed")
        else:
            title = self.translate("Copy failed") if failed else self.translate("Copy completed")
        notification = Gio.Notification.new(title)
        notification.set_body(
            self.translate("The operation could not be completed. Open Activity for details.")
            if failed else
            self.translate("The copy passed the requested verification checks. Open Activity for the report.")
            if status == "Verified" else
            self.translate("The selected copy operation completed. Open Activity for details.")
        )
        notification.set_icon(Gio.ThemedIcon.new("media-optical"))
        notification.set_default_action("app.show-window")
        try:
            app.send_notification("operation-completion", notification)
        except GLib.Error as error:
            self.log(f"Could not send completion notification: {error.message}")

    def on_close_request(self, *_args: Any) -> bool:
        if self.lifecycle.closing:
            return True
        pending = self.selection.restored_source
        unsaved = pending is not None and pending.save_pending
        if self.lifecycle.running or unsaved:
            if self.close_dialog is None:
                self.close_dialog = ResponseWindow(title=self.translate("Close DVD to MKV?"), transient_for=self)
                content = self.close_dialog.get_content_area()
                content.set_margin_top(12)
                content.set_margin_start(12)
                content.set_margin_end(12)
                message = (self.translate("Stop the current operation and close?")
                           if self.lifecycle.running else self.translate("Close?"))
                if pending is not None and pending.save_pending:
                    message += "\n" + (
                        self.translate("Source settings could not be saved. Closing now will discard those changes.")
                        if pending.confirmed_non_device else self.translate(
                            "Source settings have not been saved because the source was not identified. "
                            "Closing now will discard those changes."
                        )
                    )
                content.append(Gtk.Label(label=message, wrap=True, xalign=0))
                self.close_dialog.add_button(
                    self.translate("No"), Gtk.ResponseType.NO, tooltip=self.translate(TOOLTIPS["stay_open"]),
                )
                if pending is not None and pending.save_pending and pending.confirmed_non_device:
                    self.close_dialog.add_button(
                        self.translate("Retry Save"), Gtk.ResponseType.APPLY,
                        tooltip=self.translate(TOOLTIPS["retry_save"]),
                    )
                self.close_dialog.add_button(
                    self.translate("Yes"), Gtk.ResponseType.YES, tooltip=self.translate(TOOLTIPS["confirm_close"]),
                )
                self.close_dialog.connect("response", self.on_close_response)
                self.close_dialog.present()
            return True
        self.begin_close()
        return True

    def on_close_response(self, dialog: ResponseWindow, response: int) -> None:
        dialog.destroy()
        self.close_dialog = None
        if response == Gtk.ResponseType.YES:
            self.begin_close()
        elif response == Gtk.ResponseType.APPLY:
            pending = self.selection.restored_source
            if pending is not None and pending.confirmed_non_device:
                self.preference_workflow.confirm_non_device(pending.source)

    def begin_close(self) -> None:
        if self.lifecycle.closing:
            return
        self.lifecycle.closing = True
        self.preferences_loading = False
        if self.preferences_load_timer is not None:
            GLib.source_remove(self.preferences_load_timer)
            self.preferences_load_timer = None
        if self.load_wait_dialog is not None:
            self.load_wait_dialog.destroy()
            self.load_wait_dialog = None
        self.discard_preferences_on_close = False
        self.preferences_wait_started = time.monotonic()
        self.restart_drive_scan_on_cancel = self.source_workflow.drive_scan is not None
        self.invalidate_choosers()
        if self.close_dialog is not None:
            self.close_dialog.destroy()
            self.close_dialog = None
        self.cancel_detection()
        self.cancel_operation()
        if self.source_workflow.drive_scan is not None:
            self.source_workflow.cancel_drive_scan()
        self.set_sensitive(False)
        self.close_timer = GLib.timeout_add(100, self.finish_close)

    def finish_close(self) -> bool:
        if self.log_timer is None:
            return False
        # Polled every 100 ms until all workers and their subprocesses have stopped.
        if self.lifecycle.busy():
            return True
        if self.preferences_writer.busy and not self.discard_preferences_on_close:
            if (self.save_wait_dialog is None
                    and time.monotonic() - self.preferences_wait_started >= PREFERENCES_CLOSE_WAIT):
                dialog = ResponseWindow(title=self.translate("Preferences are still saving"), transient_for=self)
                self.save_wait_dialog = dialog
                content = dialog.get_content_area()
                content.set_margin_top(12)
                content.set_margin_start(12)
                content.set_margin_end(12)
                content.append(Gtk.Label(
                    label=self.translate(
                        "The preferences save is taking longer than expected. Closing anyway may lose "
                        "pending changes; the in-progress write may still complete after closing, and "
                        "its durability is not confirmed. A temporary file may remain."
                    ),
                    wrap=True, max_width_chars=60, xalign=0,
                ))
                dialog.add_button(
                    self.translate("Cancel Closing"), Gtk.ResponseType.CANCEL, tooltip=self.translate(TOOLTIPS["stay_open"]),
                )
                dialog.add_button(
                    self.translate("Close Anyway"), Gtk.ResponseType.REJECT, tooltip=self.translate(TOOLTIPS["close_anyway"]),
                )
                dialog.add_button(
                    self.translate("Wait"), Gtk.ResponseType.APPLY,
                    tooltip=self.translate(TOOLTIPS["wait_saving"]), default=True,
                )
                dialog.connect("response", self.on_save_wait_response)
                dialog.present()
            return True
        if self.save_wait_dialog is not None:
            self.save_wait_dialog.destroy()
            self.save_wait_dialog = None
        save_error = self.preferences_writer.error
        if save_error and not self.discard_preferences_on_close:
            self.close_timer = None
            if self.save_error_dialog is None:
                dialog = ResponseWindow(title=self.translate("Preferences were not safely saved"), transient_for=self)
                self.save_error_dialog = dialog
                content = dialog.get_content_area()
                content.set_margin_top(12)
                content.set_margin_start(12)
                content.set_margin_end(12)
                content.append(Gtk.Label(
                    label=clean_text(save_error), wrap=True, max_width_chars=60, xalign=0,
                ))
                dialog.add_button(
                    self.translate("Cancel"), Gtk.ResponseType.CANCEL, tooltip=self.translate(TOOLTIPS["stay_open"]),
                )
                dialog.add_button(
                    self.translate("Close Without Saving"), Gtk.ResponseType.REJECT,
                    tooltip=self.translate(TOOLTIPS["discard_settings"]),
                )
                dialog.add_button(
                    self.translate("Retry"), Gtk.ResponseType.APPLY,
                    tooltip=self.translate(TOOLTIPS["retry_save"]), default=True,
                )
                dialog.connect("response", self.on_save_close_response)
                dialog.present()
            return False
        self.flush_log()
        GLib.source_remove(self.log_timer)
        self.log_timer = None
        self.close_timer = None
        self.listing_view.close()
        self.destroy()
        return False

    def cancel_close(self) -> None:
        self.lifecycle.closing = False
        self.set_sensitive(True)
        self.update_title_sensitivity()
        self.set_status("Preferences not saved")
        if self.restart_drive_scan_on_cancel:
            self.scan_drives()

    def on_save_wait_response(self, dialog: ResponseWindow, response: int) -> None:
        dialog.destroy()
        self.save_wait_dialog = None
        if response == Gtk.ResponseType.REJECT:
            self.preferences_writer.abandon()
            self.discard_preferences_on_close = True
        elif response == Gtk.ResponseType.APPLY:
            self.preferences_wait_started = time.monotonic()
        else:
            if self.close_timer is not None:
                GLib.source_remove(self.close_timer)
                self.close_timer = None
            self.cancel_close()

    def on_save_close_response(self, dialog: ResponseWindow, response: int) -> None:
        dialog.destroy()
        self.save_error_dialog = None
        if response == Gtk.ResponseType.REJECT:
            self.discard_preferences_on_close = True
        elif response == Gtk.ResponseType.APPLY:
            try:
                self.preferences_writer.retry()
            except OSError as error:
                self.log(str(error))
        else:
            self.cancel_close()
            return
        self.close_timer = GLib.timeout_add(100, self.finish_close)

    def load_rows(self, output: str | Listing) -> None:
        listing = self.listing_workflow.load_rows(output)
        if self.view.bluray_button.get_active():
            combo = self.view.track_playlist_combo
            combo.handler_block_by_func(self.on_track_playlist_changed)
            try:
                combo.remove_all()
                for identifier, label in listing.rows:
                    combo.append(identifier, clean_label(label))
                combo.set_active_id(listing.longest_playlist)
            finally:
                combo.handler_unblock_by_func(self.on_track_playlist_changed)

    def load_stream_rows(
        self, playlist: str, output: str | Listing, selected: list[str] | None = None
    ) -> None:
        self.listing_workflow.load_stream_rows(playlist, output, selected)
        if self.selection.stream_playlist == playlist:
            combo = self.view.track_playlist_combo
            combo.handler_block_by_func(self.on_track_playlist_changed)
            try:
                combo.set_active_id(playlist)
            finally:
                combo.handler_unblock_by_func(self.on_track_playlist_changed)

    def on_stream_toggled(self, *_args: Any) -> None:
        if self.applying_stream_format or self.selection.applying_profile:
            return
        if self.selection.stream_playlist is not None:
            if _args and (self.selection.restored_source is not None or self.selection.requires_reinspection):
                for row, identifier in self.listing_view.stream_rows:
                    if row is _args[0]:
                        self.selection.record_selection_edit(
                            identifier, row.get_active(), is_bluray=True,
                            playlist=self.selection.stream_playlist,
                        )
                        break
            self.selection.update_stream_selection(
                self.selection.stream_playlist,
                self.listing_view.active_ids(streams=True),
            )
            self.save_current_source_preferences()

    def add_row(self, identifier: str, label: str, active: bool = False) -> None:
        self.listing_view.add_row(identifier, label, active)


class DvdToMkvApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="org.local.dvd_to_mkv")
        show_window = Gio.SimpleAction.new("show-window", None)
        show_window.connect("activate", lambda *_args: self.activate())
        self.add_action(show_window)

    def do_activate(self) -> None:
        self.withdraw_notification("operation-completion")
        window = self.props.active_window
        if not isinstance(window, DvdToMkvWindow):
            window = DvdToMkvWindow(self)
        window.present()

    def request_shutdown(self) -> bool:
        windows = [
            window for window in self.get_windows()
            if isinstance(window, DvdToMkvWindow)
        ]
        if not windows:
            self.quit()
            return True
        for window in windows:
            if window.lifecycle.closing:
                continue
            pending = window.selection.restored_source
            if pending is not None and pending.save_pending:
                window.on_close_request()
            else:
                window.begin_close()
        return True


if __name__ == "__main__":
    application: DvdToMkvApplication = DvdToMkvApplication()
    # Ctrl+C and SIGTERM go through the normal close path so subprocesses are stopped.
    signal_sources: list[int] = [
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, application.request_shutdown)
        for sig in (signal.SIGINT, signal.SIGTERM)
    ]
    try:
        application.run()
    finally:
        for source in signal_sources:
            GLib.source_remove(source)
