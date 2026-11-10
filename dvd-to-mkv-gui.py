#!/usr/bin/env python3

from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import traceback
from _thread import lock
from collections import deque
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk, Pango  # type: ignore[reportMissingModuleSource]  # noqa: I001

from dvdvideo.gui_support import ManagedProcess, OperationCancelled, filename_stem, validate_output_paths
from dvdvideo.gui_preferences import (
    DEFAULT_PROFILE_NAME,
    PreferencesStore,
    Profile,
    TrackInfo,
    select_tracks,
)
from dvdvideo.gui_profile_dialog import ProfileDialog
from dvdvideo.gui_listing import (
    MAX_INSPECTION_ROWS, MAX_STREAM_ROWS,
    parse_playlists, parse_dvd_streams, parse_bluray_streams,
)
from dvdvideo.gui_selection import SelectionState
from dvdvideo.gui_preferences_io import PreferencesWriter

SCRIPT_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dvd-to-mkv.sh")
MAX_LOG_CHARS: int = 512 * 1024
MAX_PENDING_CHARS: int = 256 * 1024
# GTK lays out each line as a whole; unbroken output would grow memory without bound.
MAX_LOG_LINE_CHARS: int = 1024
LOG_FOLLOW_THRESHOLD: int = 16
DISC_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("BDMV", "INDEX.BDMV", "bluray"), ("VIDEO_TS", "VIDEO_TS.IFO", "dvd")
)
# Caps the 7z probe's virtual address space (RLIMIT_AS), not its resident memory.
PROBE_ADDRESS_SPACE_LIMIT: int = 512 * 1024 * 1024
# Inspection output comes from untrusted discs; keep the lists and labels bounded.
MAX_LABEL_CHARS: int = 200
# C0/C1 controls (except tab and newline) and bidirectional overrides can disguise external text.
UNSAFE_TEXT: re.Pattern[str] = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ud800-\udfff]")
SOURCE_SYMLINK_ERROR: str = "Symbolic links are not accepted as sources; choose the real file or folder."


def clean_text(text: str) -> str:
    return UNSAFE_TEXT.sub("", text)


def clean_label(text: str) -> str:
    text = clean_text(text)
    return text if len(text) <= MAX_LABEL_CHARS else text[:MAX_LABEL_CHARS - 1] + "…"


def symlinked_source(source: str) -> bool:
    # normpath drops a trailing slash, so lstat sees the link rather than its target.
    try:
        if not stat.S_ISLNK(os.lstat(os.path.normpath(source)).st_mode):
            return False
    except (OSError, ValueError):
        return False
    # Device aliases such as /dev/cdrom are symlinks managed by the system.
    try:
        return not stat.S_ISBLK(os.stat(source).st_mode)
    except OSError:
        return True


def is_plain_directory(path: str) -> bool:
    return stat.S_ISDIR(os.lstat(path).st_mode)


def is_plain_file(path: str) -> bool:
    return stat.S_ISREG(os.lstat(path).st_mode)


def validate_source_links(source: str) -> None:
    if symlinked_source(source):
        raise RuntimeError(SOURCE_SYMLINK_ERROR)
    if not os.path.isdir(source):
        return
    source_folder = os.path.basename(os.path.normpath(source)).upper()
    for folder, marker, _kind in DISC_MARKERS:
        directories = (
            [source] if source_folder == folder else
            [os.path.join(source, entry) for entry in os.listdir(source) if entry.upper() == folder]
        )
        for directory in directories:
            if os.path.islink(directory):
                raise RuntimeError(SOURCE_SYMLINK_ERROR)
            if os.path.isdir(directory):
                for entry in os.listdir(directory):
                    if entry.upper() == marker and os.path.islink(os.path.join(directory, entry)):
                        raise RuntimeError(SOURCE_SYMLINK_ERROR)


def source_name(source: str) -> str:
    path = os.path.normpath(source)
    if os.path.basename(path).upper() in ("BDMV", "VIDEO_TS"):
        path = os.path.dirname(path)
    name = os.path.basename(path)
    if name.lower().endswith((".iso", ".img")):
        name = os.path.splitext(name)[0]
    return filename_stem(name) or "disc"


def find_entry(directory: str, name: str, accept: Callable[[str], bool]) -> str | None:
    # Disc folders are matched case-insensitively; copies often use lowercase names.
    for entry in os.listdir(directory):
        path = os.path.join(directory, entry)
        if entry.upper() == name and accept(path):
            return path
    return None


def detect_media(source: str, probe: ManagedProcess | None = None) -> str:
    """Return "dvd" or "bluray" from the disc structure; images and devices are listed with 7z."""
    if symlinked_source(source):
        raise RuntimeError(SOURCE_SYMLINK_ERROR)
    source_stat: os.stat_result = os.stat(source)
    if stat.S_ISDIR(source_stat.st_mode):
        source_folder = os.path.basename(os.path.normpath(source)).upper()
        kinds: set[str] = set()
        for folder, marker, kind in DISC_MARKERS:
            folder_path = source if source_folder == folder else find_entry(source, folder, is_plain_directory)
            if folder_path and find_entry(folder_path, marker, is_plain_file):
                kinds.add(kind)
    elif stat.S_ISREG(source_stat.st_mode) or stat.S_ISBLK(source_stat.st_mode):
        tool: str | None = shutil.which("7zz") or shutil.which("7z")
        if not tool:
            raise RuntimeError("Automatic image/device detection requires 7zz or 7z. Choose DVD or Blu-ray manually.")
        limiter: str | None = shutil.which("prlimit")
        if not limiter:
            raise RuntimeError(
                "Automatic image/device detection requires prlimit (util-linux) to cap 7z address space. "
                "Choose DVD or Blu-ray manually."
            )
        command = [
            limiter, f"--as={PROBE_ADDRESS_SPACE_LIMIT}", "--", tool, "l", "-slt", "-ba", "--", source,
        ]
        result = (probe or ManagedProcess()).run(command, timeout=30)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
            raise RuntimeError(f"Could not inspect the source filesystem: {detail}")
        paths = {
            line.removeprefix("Path = ").replace("\\", "/").strip("/").upper()
            for line in result.stdout.splitlines() if line.startswith("Path = ")
        }
        kinds: set[str] = {kind for folder, marker, kind in DISC_MARKERS if f"{folder}/{marker}" in paths}
    else:
        raise RuntimeError("Source must be a disc image, optical device, or disc folder.")
    if len(kinds) != 1:
        raise RuntimeError(
            "Source has no unique BDMV/index.bdmv or VIDEO_TS/VIDEO_TS.IFO structure. "
            "Choose DVD or Blu-ray manually."
        )
    return kinds.pop()


class DvdToMkvWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app, title="DVD to MKV")
        self.set_default_size(860, 720)
        self.rows: list[tuple[Gtk.CheckButton, str]] = []
        self.stream_rows: list[tuple[Gtk.CheckButton, str]] = []
        self.playlist_streams: dict[str, list[str]] = {}
        self.stream_playlist: str | None = None
        self.running: bool = False
        self.suggested_name: str = ""
        self.suggested_output: str = ""
        self.detection_generation: int = 0  # Invalidates detection on source changes or cancellation.
        self.detection_timeout: int | None = None
        self.detecting: bool = False
        self.detection_probe: ManagedProcess | None = None
        self.jobs: list[threading.Thread] = []
        self.operation: ManagedProcess | None = None
        self.source_generation: int = 0  # Invalidates inspection/copy results on source changes.
        self.chooser_generation: int = 0
        self.mode_outputs: dict[bool, str] = {False: "", True: ""}
        self.output_mode: bool = False
        self.closing: bool = False
        self.close_dialog: Gtk.MessageDialog | None = None
        self.close_timer: int | None = None
        self.applying_detection: bool = False
        self.log_lock: lock = threading.Lock()
        self.pending_log: deque[str] = deque()
        self.pending_log_chars: int = 0
        self.dropped_log_chars: int = 0
        self.log_column: int = 0
        self.playlist_mvc: dict[str, bool] = {}
        self.playlist_all_streams: dict[str, list[str]] = {}
        self.m2ts_confirmed: set[str] = set()
        self.applying_stream_format: bool = False
        self.applying_profile: bool = False
        self.restoring_preferences: bool = False
        self.active_source: str = ""
        self.selection_state = SelectionState()
        self.playlist_track_info: dict[str, list[TrackInfo]] = {}
        self.dvd_track_info: list[TrackInfo] = []
        self.preferences_error: str | None = None
        self.preferences_writer = PreferencesWriter(self.log)
        try:
            self.preferences: PreferencesStore | None = PreferencesStore(
                writer=self.preferences_writer.submit
            )
        except (OSError, ValueError) as error:
            self.preferences = None
            self.preferences_error = f"GUI preferences are unavailable: {error}"

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(18)
        root.set_margin_end(18)
        self.set_child(root)

        mode_format_box = Gtk.Box(spacing=16)
        mode_box = Gtk.Box(spacing=8)
        mode_format_box.append(mode_box)
        mode_box.append(Gtk.Label(label="Media", xalign=0))
        self.dvd_button = Gtk.ToggleButton(label="DVD", active=True)
        self.bluray_button = Gtk.ToggleButton(label="Blu-ray", group=self.dvd_button)
        self.dvd_button.connect("toggled", self.on_mode_changed)
        self.bluray_button.connect("toggled", self.on_mode_changed)
        self.dvd_button.connect("clicked", self.on_manual_mode)
        self.bluray_button.connect("clicked", self.on_manual_mode)
        mode_box.append(self.dvd_button)
        mode_box.append(self.bluray_button)
        self.format_box = Gtk.Box(spacing=8)
        self.format_combo = Gtk.ComboBoxText()
        for identifier, label in (
            ("auto", "Automatic (MVC → original M2TS)"),
            ("mkv", "MKV (2D / selected streams)"),
            ("m2ts", "Original M2TS (all streams)"),
        ):
            self.format_combo.append(identifier, label)
        self.format_combo.set_active_id("auto")
        self.add_field_label(self.format_box, "_Blu-ray output", self.format_combo)
        self.format_box.append(self.format_combo)
        self.format_combo.set_hexpand(True)
        mode_format_box.append(self.format_box)
        root.append(mode_format_box)
        self.format_combo.connect("changed", self.on_format_changed)

        self.source_entry: Gtk.Entry = self.add_source_row(root)
        self.output_entry: Gtk.Entry = self.add_path_row(root, "Output", "Choose output MKV or directory", self.choose_output)
        self.name_row = Gtk.Box(spacing=8)
        self.name_entry = Gtk.Entry(hexpand=True, placeholder_text="Suggested name without .mkv")
        self.add_field_label(self.name_row, "_Filename", self.name_entry)
        self.name_row.append(self.name_entry)
        root.append(self.name_row)

        self.minlength_row = Gtk.Box(spacing=8)
        settings_box = Gtk.Box(spacing=16)
        settings_box.append(self.minlength_row)
        self.minimum_spin: Gtk.SpinButton = Gtk.SpinButton.new_with_range(0, 600, 1)
        self.add_field_label(self.minlength_row, "_Minimum minutes", self.minimum_spin)
        self.minimum_spin.set_value(2)
        self.minimum_spin.connect("value-changed", self.on_source_setting_changed)
        self.minlength_row.append(self.minimum_spin)

        self.profile_row = Gtk.Box(spacing=8)
        settings_box.append(self.profile_row)
        self.profile_combo = Gtk.ComboBoxText()
        self.populate_profile_combo()
        self.profile_combo.connect("changed", self.on_profile_changed)
        self.add_field_label(self.profile_row, "_Profile", self.profile_combo)
        self.profile_row.append(self.profile_combo)
        manage_profiles = Gtk.Button(label="Manage Profiles")
        manage_profiles.connect("clicked", self.show_profile_dialog)
        self.profile_row.append(manage_profiles)
        root.append(settings_box)

        buttons = Gtk.Box(spacing=8)
        self.inspect_button: Gtk.Button = Gtk.Button.new_with_mnemonic("_Inspect")
        self.inspect_button.connect("clicked", self.inspect)
        self.copy_button: Gtk.Button = Gtk.Button.new_with_mnemonic("_Copy Selected")
        self.copy_button.connect("clicked", self.copy_selected)
        self.select_all_items_button = Gtk.Button(label="Select All")
        self.select_all_items_button.connect("clicked", self.select_all_items)
        buttons.append(self.select_all_items_button)
        buttons.append(self.inspect_button)
        self.inspect_streams_button = Gtk.Button(label="Inspect Streams")
        self.inspect_streams_button.connect("clicked", self.inspect_streams)
        buttons.append(self.inspect_streams_button)
        self.select_all_streams_button = Gtk.Button(label="Select All Tracks")
        self.select_all_streams_button.connect("clicked", self.select_all_streams)
        buttons.append(self.select_all_streams_button)
        buttons.append(self.copy_button)
        self.cancel_button: Gtk.Button = Gtk.Button.new_with_mnemonic("C_ancel")
        self.cancel_button.set_sensitive(False)
        self.cancel_button.connect("clicked", self.cancel_operation)
        buttons.append(self.cancel_button)
        self.status_label = Gtk.Label(label="Ready", xalign=0)
        self.status_label.set_hexpand(True)
        self.status_label.set_max_width_chars(28)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
            ["Operation status", "Current media detection, inspection, or copy status"],
        )
        self.status_label.connect("notify::label", self.on_status_changed)
        action_status_box = Gtk.Box(spacing=8)
        action_status_box.append(buttons)
        action_status_box.append(self.status_label)
        root.append(action_status_box)

        playlist_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        playlist_box.append(Gtk.Label(label="Streams or playlists", xalign=0))
        self.list_box = Gtk.ListBox()
        self.list_box.update_property(
            [Gtk.AccessibleProperty.LABEL], ["DVD streams or Blu-ray playlists"]
        )
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=72)
        list_scroll.set_child(self.list_box)
        playlist_box.append(list_scroll)

        self.streams_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.streams_label = Gtk.Label(label="Select one playlist and click Inspect Streams", xalign=0)
        self.streams_box.append(self.streams_label)
        self.stream_list_box = Gtk.ListBox()
        self.stream_list_box.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
            ["Blu-ray streams", "Check the video, audio, and subtitle tracks to include in the MKV"],
        )
        self.stream_list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        stream_scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=100)
        stream_scroll.set_child(self.stream_list_box)
        self.streams_box.append(stream_scroll)
        self.selection_panes = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.selection_panes.set_start_child(playlist_box)
        self.selection_panes.set_end_child(self.streams_box)
        self.selection_panes.set_position(110)
        self.selection_panes.set_shrink_start_child(False)
        self.selection_panes.set_shrink_end_child(False)

        activity_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        activity_box.append(Gtk.Label(label="Activity", xalign=0))
        self.log_buffer = Gtk.TextBuffer()
        self.log_buffer.set_enable_undo(False)
        log_view = Gtk.TextView(buffer=self.log_buffer, editable=False, monospace=True)
        log_view.update_property([Gtk.AccessibleProperty.LABEL], ["Activity output"])
        log_scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=90)
        log_scroll.set_child(log_view)
        self.log_adjustment: Gtk.Adjustment = log_scroll.get_vadjustment()
        self.follow_log = True
        self.log_adjustment.connect("changed", self.scroll_log_to_end)
        self.log_adjustment.connect("value-changed", self.on_log_scrolled)
        activity_box.append(log_scroll)
        self.activity_panes = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.activity_panes.set_start_child(self.selection_panes)
        self.activity_panes.set_end_child(activity_box)
        self.activity_panes.set_shrink_start_child(False)
        self.activity_panes.set_shrink_end_child(False)
        self.activity_panes.set_position(280)
        root.append(self.activity_panes)
        self.source_entry.connect("changed", self.on_source_changed)
        self.output_entry.connect("changed", self.on_output_changed)
        self.name_entry.connect("changed", self.invalidate_choosers)
        self.connect("close-request", self.on_close_request)
        self.log_timer: int | None = GLib.timeout_add(100, self.flush_log)
        self.on_mode_changed()
        if self.preferences_error:
            self.log(self.preferences_error)
            self.status_label.set_text("Preferences unavailable")

    def set_rows_active(self, rows: list[tuple[Gtk.CheckButton, str]]) -> None:
        self.applying_profile = True
        try:
            for checkbox, _identifier in rows:
                checkbox.set_active(True)
        finally:
            self.applying_profile = False
        if self.stream_playlist is not None and self.playlist_format(self.stream_playlist) != "m2ts":
            self.playlist_streams[self.stream_playlist] = [
                identifier for checkbox, identifier in self.stream_rows if checkbox.get_active()
            ]
            self.m2ts_confirmed.discard(self.stream_playlist)
        save = getattr(self, "save_current_source_preferences", None)
        if save is not None:
            save()

    def select_all_items(self, *_args: Any) -> None:
        DvdToMkvWindow.set_rows_active(self, self.rows)

    def select_all_streams(self, *_args: Any) -> None:
        DvdToMkvWindow.set_rows_active(self, self.stream_rows)

    def populate_profile_combo(self) -> None:
        self.profile_combo.remove_all()
        if self.preferences is None:
            self.profile_combo.append(DEFAULT_PROFILE_NAME, DEFAULT_PROFILE_NAME)
            self.profile_combo.set_active_id(DEFAULT_PROFILE_NAME)
            self.profile_combo.set_sensitive(False)
            return
        for name in self.preferences.profile_names:
            self.profile_combo.append(name, name)
        self.profile_combo.set_active_id(self.preferences.active_profile)

    def show_profile_dialog(self, *_args: Any) -> None:
        if self.preferences is None:
            self.log(self.preferences_error or "GUI preferences are unavailable.")
            return
        dialog = ProfileDialog(
            self, self.preferences, self.on_profiles_updated,
            current_profile=self.profile_combo.get_active_id(),
        )
        dialog.connect("response", self.close_profile_dialog)
        dialog.present()

    def close_profile_dialog(self, dialog: Gtk.Dialog, _response: int) -> None:
        dialog.destroy()

    def on_profiles_updated(self, name: str, apply_profile: bool = True) -> None:
        self.restoring_preferences = True
        try:
            self.populate_profile_combo()
            self.profile_combo.set_active_id(name)
        finally:
            self.restoring_preferences = False
        if apply_profile:
            self.apply_profile_to_rows()
        self.save_current_source_preferences()

    def on_profile_changed(self, *_args: Any) -> None:
        if self.restoring_preferences or self.preferences is None:
            return
        name = self.profile_combo.get_active_id()
        if name is None:
            return
        try:
            self.preferences.set_active_profile(name)
        except (OSError, ValueError) as error:
            self.log(f"Could not save active profile: {error}")
            return
        self.apply_profile_to_rows()
        self.save_current_source_preferences()

    def apply_profile_to_rows(self, apply_settings: bool = True) -> None:
        if self.preferences is None:
            return
        profile = self.preferences.get_profile(self.profile_combo.get_active_id())
        if apply_settings:
            self.format_combo.set_active_id(profile.output_format)
            self.minimum_spin.set_value(profile.minimum_minutes)
        self.applying_profile = True
        try:
            if self.bluray_button.get_active():
                for playlist, tracks in self.playlist_track_info.items():
                    self.playlist_streams[playlist] = select_tracks(profile, tracks)
                    self.m2ts_confirmed.discard(playlist)
                if self.stream_playlist is not None:
                    selected = set(self.playlist_streams.get(self.stream_playlist, []))
                    for checkbox, identifier in self.stream_rows:
                        checkbox.set_active(identifier in selected)
            else:
                selected = set(select_tracks(profile, self.dvd_track_info))
                for checkbox, identifier in self.rows:
                    checkbox.set_active(identifier in selected)
        finally:
            self.applying_profile = False
        if self.stream_playlist is not None:
            self.update_stream_format(self.stream_playlist)
        self.save_current_source_preferences()

    def restore_source_preferences(self, source: str) -> None:
        if self.preferences is None:
            return
        settings = self.preferences.get_source(source)
        name = settings.profile_name if settings else self.preferences.active_profile
        self.restoring_preferences = True
        try:
            self.profile_combo.set_active_id(name)
            if settings:
                self.format_combo.set_active_id(settings.output_format)
                self.minimum_spin.set_value(settings.minimum_minutes)
            self.apply_profile_to_rows(apply_settings=settings is None)
        finally:
            self.restoring_preferences = False

    def save_current_source_preferences(self) -> None:
        store = getattr(self, "preferences", None)
        active_source = getattr(self, "active_source", "")
        if (
            store is None or not active_source
            or getattr(self, "restoring_preferences", False)
        ):
            return
        profile_combo = getattr(self, "profile_combo", None)
        name = (
            profile_combo.get_active_id() if profile_combo is not None else None
        ) or store.active_profile
        preferences = self.selection_state.snapshot(
            store.get_source(active_source),
            is_bluray=self.bluray_button.get_active(),
            selected=[identifier for checkbox, identifier in self.rows if checkbox.get_active()],
            inspected_streams=self.playlist_streams,
            profile_name=name,
            output_format=self.format_combo.get_active_id() or "auto",
            minimum_minutes=self.minimum_spin.get_value_as_int(),
        )
        try:
            store.save_source(active_source, preferences)
        except (OSError, ValueError) as error:
            self.log(f"Could not save source preferences: {error}")

    def on_selection_changed(self, *_args: Any) -> None:
        if not self.applying_profile:
            save = getattr(self, "save_current_source_preferences", None)
            if save is not None:
                save()

    def on_status_changed(self, label: Gtk.Label, _param: Any) -> None:
        if hasattr(label, "announce"):
            label.announce(label.get_text(), Gtk.AccessibleAnnouncementPriority.MEDIUM)

    def invalidate_choosers(self, *_args: Any) -> None:
        self.chooser_generation += 1

    def on_output_changed(self, *_args: Any) -> None:
        self.invalidate_choosers()
        self.mode_outputs[self.output_mode] = self.output_entry.get_text()

    def add_field_label(self, row: Gtk.Box, text: str, widget: Gtk.Widget) -> None:
        label = Gtk.Label(label=text, use_underline=True, xalign=0)
        label.set_mnemonic_widget(widget)
        row.append(label)

    def add_path_row(
        self, parent: Gtk.Box, label: str, placeholder: str, callback: Callable[..., None]
    ) -> Gtk.Entry:
        row = Gtk.Box(spacing=8)
        entry = Gtk.Entry(hexpand=True, placeholder_text=placeholder)
        self.add_field_label(row, f"_{label}", entry)
        row.append(entry)
        button = Gtk.Button(label="Browse")
        button.connect("clicked", callback)
        row.append(button)
        parent.append(row)
        return entry

    def add_source_row(self, parent: Gtk.Box) -> Gtk.Entry:
        row = Gtk.Box(spacing=8)
        entry = Gtk.Entry(hexpand=True, placeholder_text="Enter DVD device, ISO, or folder")
        self.add_field_label(row, "_Source", entry)
        row.append(entry)
        file_button = Gtk.Button(label="File")
        file_button.connect("clicked", self.choose_source_file)
        row.append(file_button)
        folder_button = Gtk.Button(label="Folder")
        folder_button.connect("clicked", self.choose_source_folder)
        row.append(folder_button)
        parent.append(row)
        return entry

    def on_mode_changed(self, button: Gtk.ToggleButton | None = None) -> None:
        if button is not None and not button.get_active():
            return
        self.cancel_detection()
        is_bluray: bool = self.bluray_button.get_active()
        if is_bluray != self.output_mode:
            self.mode_outputs[self.output_mode] = self.output_entry.get_text()
            self.output_mode: bool = is_bluray
            self.output_entry.set_text(self.mode_outputs[is_bluray])
        self.invalidate_choosers()
        self.minlength_row.set_visible(is_bluray)
        self.inspect_streams_button.set_visible(is_bluray)
        self.streams_box.set_visible(is_bluray)
        self.select_all_streams_button.set_visible(is_bluray)
        self.name_row.set_visible(is_bluray)
        self.format_box.set_visible(is_bluray)
        self.output_entry.set_placeholder_text("Choose output directory" if is_bluray else "Choose output MKV")
        self.clear_rows()
        self.suggest_name()

    def on_manual_mode(self, *_args: Any) -> None:
        if not self.applying_detection:
            self.cancel_detection()
            self.status_label.set_text("Media mode selected manually")

    def on_source_changed(self, *_args: Any) -> None:
        save = getattr(self, "save_current_source_preferences", None)
        if save is not None:
            save()
        self.source_generation += 1
        self.invalidate_choosers()
        self.cancel_detection()
        self.clear_rows()
        source: str = self.source_entry.get_text()
        self.active_source = source
        self.selection_state.clear()
        restore = getattr(self, "restore_source_preferences", None)
        if restore is not None:
            restore(source)
        self.suggest_name()
        if source:
            self.detecting = True
            self.status_label.set_text("Detecting media type...")
            self.detection_timeout = GLib.timeout_add(
                400, self.start_detection, source, self.detection_generation
            )
        else:
            self.status_label.set_text("Choose a source")

    def cancel_detection(self) -> None:
        self.detection_generation += 1
        if self.detection_timeout is not None:
            GLib.source_remove(self.detection_timeout)
            self.detection_timeout = None
        self.detecting = False
        if self.detection_probe is not None:
            self.detection_probe.cancel()
            self.detection_probe = None

    def start_detection(self, source: str, generation: int) -> bool:
        if self.closing or generation != self.detection_generation or source != self.source_entry.get_text():
            return False
        self.detection_timeout = None
        probe = ManagedProcess()
        self.detection_probe = probe

        def worker() -> None:
            try:
                probe.check_cancelled()
                media = detect_media(source, probe)
                probe.check_cancelled()
            except OperationCancelled:
                return
            except FileNotFoundError:
                GLib.idle_add(self.finish_detection, source, generation, None, "Source does not exist.")
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                GLib.idle_add(self.finish_detection, source, generation, None, str(error))
            except Exception as error:  # noqa: BLE001 - worker boundary reports unexpected errors to Activity
                self.log("Unexpected media detection error:\n" + traceback.format_exc().rstrip())
                GLib.idle_add(
                    self.finish_detection, source, generation, None,
                    f"Unexpected error: {type(error).__name__}: {error}",
                )
            else:
                GLib.idle_add(self.finish_detection, source, generation, media, None)

        if not self.start_worker(worker):
            self.finish_detection(
                source, generation, None, "Could not start background worker. See Activity for details."
            )
        return False

    def start_worker(self, worker: Callable[[], None]) -> bool:
        thread = None
        try:
            # Non-daemon on purpose: closing waits for workers so no subprocess is left behind.
            thread = threading.Thread(target=worker)
            # Register before start() so even a worker that finishes at once is tracked.
            self.jobs.append(thread)
            thread.start()
        except RuntimeError as error:
            if thread is not None:
                self.jobs.remove(thread)
            self.log(f"Could not start background worker: {error}")
            return False
        return True

    def finish_detection(
        self, source: str, generation: int, media: str | None, error: str | None
    ) -> None:
        if self.closing or generation != self.detection_generation or source != self.source_entry.get_text():
            return
        self.detecting = False
        self.detection_probe = None
        if error:
            self.log(f"Media detection failed: {error}")
            self.status_label.set_text("Detection failed - select DVD or Blu-ray manually")
            return
        button: Gtk.ToggleButton = self.bluray_button if media == "bluray" else self.dvd_button
        self.applying_detection = True
        try:
            button.set_active(True)
        finally:
            self.applying_detection = False
        self.status_label.set_text(f"Detected {'Blu-ray' if media == 'bluray' else 'DVD'}")
        self.log(f"Detected {'Blu-ray' if media == 'bluray' else 'DVD'} source.")

    def suggest_name(self, name: str | None = None) -> None:
        source: str = self.source_entry.get_text()
        suggestion: str = filename_stem(name) if name else source_name(source) if source else ""
        if not suggestion and source:
            suggestion: str = source_name(source)
        if self.name_entry.get_text() in ("", self.suggested_name):
            self.name_entry.set_text(suggestion)
        self.suggested_name: str = suggestion
        if not self.bluray_button.get_active():
            output: str = f"{suggestion}.mkv" if suggestion else ""
            if self.output_entry.get_text() in ("", self.suggested_output):
                self.output_entry.set_text(output)
            self.suggested_output: str = output

    def choose_source_file(self, *_args: Any) -> None:
        self.choose_path("Choose ISO or image file", "open", self.source_entry)

    def choose_source_folder(self, *_args: Any) -> None:
        self.choose_path("Choose source folder", "select_folder", self.source_entry)

    def choose_output(self, *_args: Any) -> None:
        action: str = "select_folder" if self.bluray_button.get_active() else "save"
        self.choose_path("Choose output", action, self.output_entry)

    def choose_path(self, title: str, action: str, entry: Gtk.Entry) -> None:
        if self.running or self.closing:
            return
        actions: dict[str, Gtk.FileChooserAction] = {
            "open": Gtk.FileChooserAction.OPEN,
            "save": Gtk.FileChooserAction.SAVE,
            "select_folder": Gtk.FileChooserAction.SELECT_FOLDER,
        }
        dialog: Gtk.FileChooserNative = Gtk.FileChooserNative.new(
            title, self, actions[action], "_Select", "_Cancel"
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
        dialog.connect("response", self.on_native_file_chosen, entry, self.chooser_generation)
        dialog.show()

    def apply_chosen_file(
        self, selected: Gio.File | None, entry: Gtk.Entry, generation: int
    ) -> None:
        if self.running or self.closing or generation != self.chooser_generation:
            self.log("Ignored file selection because the source or operation changed.")
            return
        if selected is None:
            self.log("File chooser did not return a selection.")
            return
        path = selected.get_path()
        if path:
            entry.set_text(path)
        else:
            self.log("The selected location is not a local path.")

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
        self.selection_state.clear()
        while (child := self.list_box.get_first_child()) is not None:
            self.list_box.remove(child)
        self.rows = []
        self.clear_stream_rows()
        self.playlist_streams = {}
        self.playlist_mvc = {}
        self.playlist_all_streams = {}
        self.m2ts_confirmed = set()
        self.applying_stream_format = False
        self.playlist_track_info = {}
        self.dvd_track_info = []

    def playlist_format(self, playlist: str) -> str:
        requested: str | None = self.format_combo.get_active_id()
        if requested == "auto":
            return "m2ts" if self.playlist_mvc.get(playlist, False) else "mkv"
        return requested or "mkv"

    def on_format_changed(self, *_args: Any) -> None:
        self.m2ts_confirmed.clear()
        if self.stream_playlist is not None:
            self.update_stream_format(self.stream_playlist)
        self.save_current_source_preferences()

    def on_source_setting_changed(self, *_args: Any) -> None:
        self.save_current_source_preferences()

    def update_stream_format(self, playlist: str) -> None:
        all_streams: bool = self.playlist_format(playlist) == "m2ts"
        self.streams_label.set_text(
            f"Playlist {playlist}: original M2TS preserves ALL streams (filtering disabled)"
            if all_streams else f"Streams for playlist {playlist} — MKV"
        )
        self.applying_stream_format = True
        try:
            for checkbox, pid in self.stream_rows:
                checkbox.set_active(all_streams or pid in self.playlist_streams.get(playlist, []))
                checkbox.set_sensitive(not all_streams and not self.running)
        finally:
            self.applying_stream_format = False

    def clear_stream_rows(self) -> None:
        while (child := self.stream_list_box.get_first_child()) is not None:
            self.stream_list_box.remove(child)
        self.stream_rows = []
        self.stream_playlist = None
        self.streams_label.set_text("Select one playlist and click Inspect Streams")

    def append_log(self, text: str) -> None:
        end: Gtk.TextIter = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, text)
        excess: int = self.log_buffer.get_char_count() - MAX_LOG_CHARS
        if excess > 0:
            self.log_buffer.delete(self.log_buffer.get_start_iter(), self.log_buffer.get_iter_at_offset(excess))
        self.scroll_log_to_end()

    def on_log_scrolled(self, adjustment: Gtk.Adjustment) -> None:
        bottom = adjustment.get_upper() - adjustment.get_page_size()
        self.follow_log = adjustment.get_value() >= bottom - LOG_FOLLOW_THRESHOLD

    def scroll_log_to_end(self, *_args: Any) -> None:
        if self.follow_log:
            self.log_adjustment.set_value(
                self.log_adjustment.get_upper() - self.log_adjustment.get_page_size()
            )

    def log(self, text: str) -> None:
        self.queue_log(f"{text}\n")

    def break_long_lines(self, text: str) -> str:
        # Called with log_lock held; log_column carries the open line across chunks.
        pieces: list[str] = []
        for index, part in enumerate(text.split("\n")):
            if index:
                pieces.append("\n")
                self.log_column = 0
            while self.log_column + len(part) > MAX_LOG_LINE_CHARS:
                room: int = MAX_LOG_LINE_CHARS - self.log_column
                pieces += [part[:room], "\n"]
                part = part[room:]
                self.log_column = 0
            pieces.append(part)
            self.log_column += len(part)
        return "".join(pieces)

    def queue_log(self, text: str) -> None:
        text = clean_text(text)
        if not text:
            return
        with self.log_lock:
            text = self.break_long_lines(text)
            if len(text) > MAX_PENDING_CHARS:
                self.dropped_log_chars += len(text) - MAX_PENDING_CHARS
                text = text[-MAX_PENDING_CHARS:]
            self.pending_log.append(text)
            self.pending_log_chars += len(text)
            while self.pending_log_chars > MAX_PENDING_CHARS:
                removed = self.pending_log.popleft()
                self.pending_log_chars -= len(removed)
                self.dropped_log_chars += len(removed)

    def flush_log(self) -> bool:
        with self.log_lock:
            text = "".join(self.pending_log)
            dropped: int = self.dropped_log_chars
            self.pending_log.clear()
            self.pending_log_chars = 0
            self.dropped_log_chars = 0
        if dropped:
            text = f"[Activity omitted {dropped} older characters to keep the interface responsive.]\n" + text
            # Start the notice on its own line so it cannot extend an unfinished one.
            end: Gtk.TextIter = self.log_buffer.get_end_iter()
            if end.backward_char() and end.get_char() != "\n":
                text = "\n" + text
        if text:
            self.append_log(text)
        self.jobs = [job for job in self.jobs if job.is_alive()]
        return True

    def output_snapshot(
        self, command: list[str]
    ) -> tuple[str, bool, str, list[str], dict[str, str]]:
        return (
            command[-1], self.bluray_button.get_active(), self.name_entry.get_text().strip(),
            [pid for checkbox, pid in self.rows if checkbox.get_active()],
            {pid: self.playlist_format(pid) for checkbox, pid in self.rows if checkbox.get_active()}
            if self.bluray_button.get_active() else {},
        )

    def check_output_snapshot(
        self, snapshot: tuple[str, bool, str, list[str], dict[str, str]], operation: ManagedProcess
    ) -> bool:
        try:
            validate_output_paths(
                snapshot[0], snapshot[1], snapshot[2], snapshot[3], operation, formats=snapshot[4]
            )
        except (OSError, ValueError) as error:
            self.log(f"Cannot validate output filename: {error}")
            return False
        return True

    def command(self, inspect: bool = False) -> list[str] | None:
        source: str = self.source_entry.get_text()
        if not source:
            self.log("Choose a source first.")
            return None
        # Keep a relative path starting with a dash from being read as an option.
        if source.startswith("-"):
            source: str = "./" + source
        if self.bluray_button.get_active():
            command: list[str] = [SCRIPT_PATH, "--bluray", "--device", source]
            if inspect:
                return command + ["--minlength", str(self.minimum_spin.get_value_as_int()), "--list-playlists"]
            selected = [identifier for checkbox, identifier in self.rows if checkbox.get_active()]
            if not selected:
                self.log("Select at least one playlist.")
                return None
            output: str = self.output_entry.get_text() or "."
            for identifier in selected:
                command += ["--playlist", identifier]
                if self.playlist_format(identifier) == "m2ts":
                    continue
                # No saved entry copies every stream; an empty list means nothing was chosen.
                if identifier in self.playlist_streams:
                    streams = self.playlist_streams[identifier]
                    if not streams:
                        self.log(f"Select at least one stream for playlist {identifier}.")
                        return None
                    for stream in streams:
                        command += ["--stream-id", f"{identifier}:{stream}"]
            return command + ["--output-format", self.format_combo.get_active_id() or "auto", "--", output]
        if inspect:
            return [SCRIPT_PATH, "--list-streams-tsv", "--", source]
        selected = [identifier for checkbox, identifier in self.rows if checkbox.get_active()]
        if not selected:
            self.log("Select at least one stream.")
            return None
        output: str = self.output_entry.get_text()
        if not output:
            self.log("Choose an output MKV first.")
            return None
        command: list[str] = [SCRIPT_PATH]
        for identifier in selected:
            command += ["--stream", identifier]
        return command + ["--", source, output]

    def inspect(self, *_args: Any) -> None:
        if self.running or self.detecting:
            self.log("Wait for the current operation before inspecting.")
            return
        command: list[str] | None = self.command(inspect=True)
        if command:
            self.clear_rows()
            self.run(command, self.load_rows)

    def copy_selected(self, *_args: Any) -> None:
        if self.running or self.detecting or self.closing:
            self.log("Wait for the current operation before copying.")
            return
        if self.bluray_button.get_active():
            selected = [pid for checkbox, pid in self.rows if checkbox.get_active()]
            for playlist in selected:
                if self.format_combo.get_active_id() != "m2ts" and playlist not in self.playlist_mvc:
                    source: str = self.source_entry.get_text()
                    if source.startswith("-"):
                        source: str = "./" + source
                    command = [SCRIPT_PATH, "--bluray", "--device", source, "--playlist", playlist, "--list-streams"]

                    def inspected(output: str, playlist: str = playlist) -> None:
                        self.load_stream_rows(playlist, output)
                        if playlist in self.playlist_mvc:
                            self.copy_selected()

                    self.run(command, inspected)
                    return
            for playlist in selected:
                if self.playlist_format(playlist) == "m2ts" and playlist not in self.m2ts_confirmed:
                    choices = self.playlist_streams.get(playlist)
                    if choices is None and self.preferences is not None:
                        saved = self.preferences.get_source(self.source_entry.get_text())
                        if saved is not None:
                            choices = saved.playlist_streams.get(playlist)
                    if choices is not None:
                        all_pids = self.playlist_all_streams.get(playlist)
                        if all_pids is None or set(choices) != set(all_pids):
                            self.confirm_m2ts(playlist)
                            return
                    self.m2ts_confirmed.add(playlist)
        command: list[str] | None = self.command()
        if command:
            self.run(command, output_snapshot=self.output_snapshot(command))

    def confirm_m2ts(self, playlist: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"Playlist {playlist}: preserve all streams as M2TS?",
            secondary_text="Your filtered selection cannot be applied to original M2TS. Both video views and all audio/subtitles will be copied.",
        )
        generation: int = self.source_generation

        def response(dialog: Gtk.MessageDialog, response: int) -> None:
            dialog.destroy()
            if generation != self.source_generation or self.closing or self.running:
                return
            if response == Gtk.ResponseType.OK:
                self.m2ts_confirmed.add(playlist)
                self.copy_selected()
            else:
                self.log("M2TS copy cancelled; filtered selection retained.")

        dialog.connect("response", response)
        dialog.show()

    def inspect_streams(self, *_args: Any) -> None:
        if self.running or self.detecting:
            self.log("Wait for the current operation before inspecting streams.")
            return
        source: str = self.source_entry.get_text()
        selected = [identifier for checkbox, identifier in self.rows if checkbox.get_active()]
        if not source:
            self.log("Choose a source first.")
            return
        if source.startswith("-"):
            source: str = "./" + source
        if len(selected) != 1:
            self.log("Select exactly one playlist to inspect its streams.")
            return
        playlist = selected[0]
        previous = self.playlist_streams.get(playlist)
        if self.preferences is not None:
            saved = self.preferences.get_source(self.source_entry.get_text())
            if saved is not None:
                saved_streams = saved.playlist_streams.get(playlist)
                if previous is None:
                    previous = saved_streams
                elif saved_streams is not None:
                    omitted = self.selection_state.omitted_streams.get(playlist, set())
                    previous = list(previous) + [
                        pid for pid in saved_streams if pid in omitted and pid not in previous
                    ]
        if previous is None:
            # Without a saved selection, a failed inspection must not fall back to copying all streams.
            self.playlist_streams[playlist] = []
        command = [SCRIPT_PATH, "--bluray", "--device", source, "--playlist", playlist, "--list-streams"]
        self.run(command, lambda output: self.load_stream_rows(playlist, output, previous, refresh=True))

    def run(
        self,
        command: list[str],
        on_success: Callable[[str], None] | None = None,
        output_snapshot: tuple[str, bool, str, list[str], dict[str, str]] | None = None,
    ) -> None:
        if self.closing:
            return
        if self.detecting:
            self.log("Detecting media type; wait a moment or choose DVD/Blu-ray manually.")
            return
        if self.running:
            self.log("An operation is already running.")
            return
        self.running = True
        self.invalidate_choosers()
        operation = ManagedProcess()
        self.operation = operation
        generation: int = self.source_generation
        source: str = self.source_entry.get_text()
        is_bluray: bool = self.bluray_button.get_active()
        self.set_busy(True)
        self.status_label.set_text(
            "Validating output..." if output_snapshot else "Inspecting..." if on_success else "Copying..."
        )

        def worker() -> None:
            process_command = command
            output: str = ""
            success_callback = None
            status = "Failed"
            try:
                # Check again: the source may have been replaced after detection.
                if source:
                    validate_source_links(source)
                if output_snapshot:
                    output_path, bluray_output, name, playlists, formats = output_snapshot
                    directory_output = os.path.isdir(output_path)
                    if bluray_output and not directory_output and len(playlists) == 1:
                        extension = "." + formats[playlists[0]]
                        if os.path.splitext(output_path)[1].lower() != extension:
                            output_path = os.path.splitext(output_path)[0] + extension
                            process_command = command[:-1] + [output_path]
                            GLib.idle_add(self.update_output_path, operation, output_path)
                    snapshot = (output_path, bluray_output, name, playlists, formats)
                    if not self.check_output_snapshot(snapshot, operation):
                        return
                    if bluray_output and name and directory_output:
                        # Insert before the trailing "--" and output path.
                        process_command = process_command[:-2] + ["--output-name", name] + process_command[-2:]
                    GLib.idle_add(self.show_copying_status, operation)
                self.log("$ " + " ".join(process_command))
                # Only inspections need stdout; ManagedProcess caps what it collects at 8 MiB.
                result: subprocess.CompletedProcess[str] = operation.run(process_command, collect=on_success is not None, log=self.queue_log)
                output: str = result.stdout
                if result.returncode == 0:
                    success_callback = on_success
                    status = "Completed"
                self.log(f"Exit status: {result.returncode}")
            except OperationCancelled:
                status = "Cancelled"
                self.log("Operation cancelled; subprocesses stopped.")
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                self.log(f"Operation failed: {error}")
            except Exception:  # noqa: BLE001 - report the traceback and always release the busy state
                self.log("Unexpected worker error:\n" + traceback.format_exc().rstrip())
            finally:
                GLib.idle_add(
                    self.finish_run, success_callback, output, generation, source, is_bluray, status
                )

        if not self.start_worker(worker):
            self.finish_run(status="Failed")

    def show_copying_status(self, operation: ManagedProcess) -> None:
        if self.operation is operation and not self.closing and not operation.cancelled.is_set():
            self.status_label.set_text("Copying...")

    def update_output_path(self, operation: ManagedProcess, path: str) -> None:
        if self.operation is operation and not self.closing:
            self.output_entry.set_text(path)
            self.log(f"Output format changed filename to: {path}")

    def set_busy(self, busy: bool) -> None:
        for widget in (
            self.inspect_button, self.copy_button, self.inspect_streams_button,
            self.source_entry.get_parent(), self.output_entry.get_parent(),
            self.name_row, self.minlength_row, self.profile_row, self.list_box, self.stream_list_box,
            self.dvd_button, self.bluray_button, self.select_all_items_button,
            self.select_all_streams_button,
            self.format_box,
        ):
            if widget is not None:
                widget.set_sensitive(not busy)
        self.cancel_button.set_sensitive(busy)
        if self.stream_playlist is not None:
            self.update_stream_format(self.stream_playlist)

    def finish_run(
        self,
        on_success: Callable[[str], None] | None = None,
        output: str = "",
        generation: int | None = None,
        source: str | None = None,
        is_bluray: bool | None = None,
        status: str = "Completed",
    ) -> None:
        if self.operation is not None and self.operation.cancelled.is_set():
            if status == "Completed":
                self.log("Cancellation received after subprocess completion; ignored pending results.")
                status = "Cancelled"
            on_success = None
        self.running = False
        self.operation = None
        self.set_busy(False)
        self.status_label.set_text(status)
        if self.closing:
            return
        # Drop results that belong to an earlier source or media mode.
        if generation is not None and (
            generation != self.source_generation or source != self.source_entry.get_text()
            or is_bluray != self.bluray_button.get_active()
        ):
            self.log("Ignored stale operation results because the source or media mode changed.")
            return
        if on_success:
            on_success(output)

    def cancel_operation(self, *_args: Any) -> None:
        if self.operation is not None:
            self.operation.cancel()
            self.cancel_button.set_sensitive(False)
            self.status_label.set_text("Cancelling...")

    def on_close_request(self, *_args: Any) -> bool:
        if self.closing:
            return True
        if self.running:
            if self.close_dialog is None:
                self.close_dialog = Gtk.MessageDialog(
                    transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                    buttons=Gtk.ButtonsType.YES_NO,
                    text="Stop the current operation and close?",
                )
                self.close_dialog.connect("response", self.on_close_response)
                self.close_dialog.present()
            return True
        self.begin_close()
        return True

    def on_close_response(self, dialog: Gtk.MessageDialog, response: int) -> None:
        dialog.destroy()
        self.close_dialog = None
        if response == Gtk.ResponseType.YES:
            self.begin_close()

    def begin_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.invalidate_choosers()
        if self.close_dialog is not None:
            self.close_dialog.destroy()
            self.close_dialog = None
        self.cancel_detection()
        self.cancel_operation()
        self.set_sensitive(False)
        self.close_timer = GLib.timeout_add(100, self.finish_close)

    def finish_close(self) -> bool:
        if self.log_timer is None:
            return False
        # Polled every 100 ms until all workers and their subprocesses have stopped.
        if self.running or any(job.is_alive() for job in self.jobs) or self.preferences_writer.busy:
            return True
        self.flush_log()
        GLib.source_remove(self.log_timer)
        self.log_timer = None
        self.close_timer = None
        self.destroy()
        return False

    def load_rows(self, output: str) -> None:
        self.clear_rows()
        self.dvd_track_info = []
        self.playlist_track_info = {}
        store = getattr(self, "preferences", None)
        profile_combo = getattr(self, "profile_combo", None)
        settings = (
            store.get_source(self.source_entry.get_text())
            if store is not None else None
        )
        profile = (
            store.get_profile(profile_combo.get_active_id() if profile_combo else None)
            if store is not None else Profile()
        )
        if self.bluray_button.get_active():
            listing = parse_playlists(output)
            self.selection_state.omitted_ids = listing.omitted_ids
            if listing.disc_name is not None:
                self.suggest_name(listing.disc_name)
            for warning in listing.warnings:
                self.log(clean_label(warning))
            for playlist, label in listing.rows:
                self.add_row(
                    playlist, clean_label(label),
                    active=settings is not None and playlist in settings.playlists,
                )
            if listing.truncated:
                self.log(f"Inspection listed too many playlists; showing the first {MAX_INSPECTION_ROWS}.")
            if listing.invalid or listing.duplicates:
                self.log(
                    f"Playlist inspection ignored {listing.invalid} malformed and "
                    f"{listing.duplicates} duplicate records."
                )
            self.selection_state.loaded_mode = True if self.rows else None
            if settings:
                missing = (
                    set(settings.playlists) - {identifier for _, identifier in self.rows}
                    - listing.omitted_ids
                )
                if missing:
                    self.log(f"Saved playlist selections no longer exist: {', '.join(sorted(missing))}.")
            elif self.rows:
                save = getattr(self, "save_current_source_preferences", None)
                if save is not None:
                    save()
            if not self.rows:
                self.log("No Blu-ray playlists found in the inspection output. See Activity for details.")
                self.status_label.set_text("No playlists found")
            else:
                self.log("Select one playlist and click Inspect Streams to list its video, audio, and subtitles.")
        else:
            listing = parse_dvd_streams(output)
            self.selection_state.omitted_ids = listing.omitted_ids
            self.dvd_track_info = listing.tracks
            if listing.truncated:
                self.log(f"Inspection listed too many streams; showing the first {MAX_INSPECTION_ROWS}.")
            if listing.invalid or listing.duplicates:
                self.log(
                    f"DVD inspection ignored {listing.invalid} malformed and "
                    f"{listing.duplicates} duplicate records."
                )
            if settings is not None and settings.dvd_inspected:
                selected = set(settings.dvd_streams)
                missing = selected - {track.identifier for track in self.dvd_track_info} - listing.omitted_ids
                if missing:
                    self.log(f"Saved DVD streams no longer exist: {', '.join(sorted(missing))}.")
            else:
                selected = set(select_tracks(profile, self.dvd_track_info))
            for identifier, label in listing.rows:
                self.add_row(
                    identifier, clean_label(label), active=identifier in selected,
                )
            self.selection_state.loaded_mode = False if self.rows else None
            if not self.rows:
                self.log("No DVD streams found in the inspection output. See Activity for details.")
                self.status_label.set_text("No streams found")
            else:
                save = getattr(self, "save_current_source_preferences", None)
                if save is not None:
                    save()

    def load_stream_rows(
        self, playlist: str, output: str, selected: list[str] | None = None, refresh: bool = False
    ) -> None:
        listing = parse_bluray_streams(output)
        streams = listing.rows
        if not hasattr(self, "playlist_track_info"):
            self.playlist_track_info = {}
        for warning in listing.warnings:
            self.log(clean_label(warning))
        if listing.truncated:
            self.log(f"Playlist {playlist} listed too many streams; showing the first {MAX_STREAM_ROWS}.")
        if listing.invalid or listing.duplicates:
            self.log(
                f"Stream inspection ignored {listing.invalid} malformed and {listing.duplicates} duplicate records "
                f"for playlist {playlist}."
            )
        if not streams:
            self.log(f"No streams found for playlist {playlist}. See Activity for details.")
            self.status_label.set_text("Stream inspection failed")
            return
        self.selection_state.omitted_streams[playlist] = listing.omitted_ids
        self.playlist_track_info[playlist] = listing.tracks
        # A saved choice wins; otherwise only explicit profile language matches are selected.
        if selected is None:
            store = getattr(self, "preferences", None)
            saved = store.get_source(self.source_entry.get_text()) if store is not None else None
            if saved is not None and playlist in saved.playlist_streams:
                selected = saved.playlist_streams[playlist]
            elif playlist in self.playlist_streams and not (
                refresh and not self.playlist_streams[playlist]
            ):
                selected = self.playlist_streams[playlist]
            elif (store := getattr(self, "preferences", None)) is not None:
                profile_combo = getattr(self, "profile_combo", None)
                profile = store.get_profile(
                    profile_combo.get_active_id() if profile_combo else None
                )
                selected = select_tracks(profile, self.playlist_track_info[playlist])
            else:
                selected = []
            if selected is None:
                selected = []
        all_stream_ids = {identifier for identifier, _ in streams}
        missing = set(selected) - all_stream_ids - listing.omitted_ids
        if missing:
            self.log(
                f"Saved streams for playlist {playlist} no longer exist: "
                f"{', '.join(sorted(missing))}."
            )
            selected = [identifier for identifier in selected if identifier in all_stream_ids]
        self.clear_stream_rows()
        self.stream_playlist = playlist
        self.streams_label.set_text(f"Streams for playlist {playlist}")
        for identifier, details in streams:
            checkbox = Gtk.CheckButton(label=clean_label(f"{identifier}  {details}"))
            checkbox.set_active(identifier in selected)
            self.stream_list_box.append(checkbox)
            self.stream_rows.append((checkbox, identifier))
            checkbox.connect("toggled", self.on_stream_toggled)
        self.on_stream_toggled()
        self.playlist_mvc[playlist] = listing.mvc_verified
        self.playlist_all_streams[playlist] = [
            identifier for identifier, _ in streams
        ] + sorted(listing.omitted_ids)
        self.update_stream_format(playlist)
        save = getattr(self, "save_current_source_preferences", None)
        if save is not None:
            save()

    def on_stream_toggled(self, *_args: Any) -> None:
        if self.applying_stream_format or getattr(self, "applying_profile", False):
            return
        if self.stream_playlist is not None:
            self.m2ts_confirmed.discard(self.stream_playlist)
            self.playlist_streams[self.stream_playlist] = [
                identifier for checkbox, identifier in self.stream_rows if checkbox.get_active()
            ]
            save = getattr(self, "save_current_source_preferences", None)
            if save is not None:
                save()

    def add_row(self, identifier: str, label: str, active: bool = False) -> None:
        checkbox = Gtk.CheckButton(label=label)
        checkbox.set_active(active)
        checkbox.set_margin_top(4)
        checkbox.set_margin_bottom(4)
        self.list_box.append(checkbox)
        self.rows.append((checkbox, identifier))
        checkbox.connect(
            "toggled",
            lambda *_args: DvdToMkvWindow.on_selection_changed(self),
        )


class DvdToMkvApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="org.local.dvd_to_mkv")

    def do_activate(self) -> None:
        window = self.props.active_window
        if not isinstance(window, DvdToMkvWindow):
            window = DvdToMkvWindow(self)
        window.present()

    def request_shutdown(self) -> bool:
        window = self.props.active_window
        if isinstance(window, DvdToMkvWindow):
            if not window.closing:
                window.begin_close()
        else:
            self.quit()
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
