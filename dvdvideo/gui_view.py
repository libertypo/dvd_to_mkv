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
from typing import Protocol

import gi

# GTK's version must be selected before GTK-dependent imports.
# ruff: noqa: E402
gi.require_version("Gtk", "4.0")

from gi.repository import Gtk, Pango  # type: ignore[reportMissingModuleSource]

from dvdvideo.gui_tooltips import TOOLTIPS
from dvdvideo.gui_widgets import Choice, add_field_label, add_path_row


class SourceCallbacks(Protocol):

    def on_mode_changed(
        self, button: Gtk.ToggleButton | None = None
    ) -> None:
        ...

    def on_manual_mode(self, *args: object) -> None:
        ...

    def choose_source_file(self, *args: object) -> None:
        ...

    def choose_source_folder(self, *args: object) -> None:
        ...

    def on_source_changed(self, *args: object) -> None:
        ...

    def invalidate_choosers(self, *args: object) -> None:
        ...


class OperationCallbacks(Protocol):
    def inspect(self, *args: object) -> None:
        ...

    def copy_selected(self, *args: object) -> None:
        ...

    def inspect_disc_details(self, *args: object) -> None:
        ...

    def show_verify_dialog(self, *args: object) -> None:
        ...

    def preview_selected(self, *args: object) -> None:
        ...

    def inspect_streams(self, *args: object) -> None:
        ...

    def discover_dvd_titles(self, *args: object) -> None:
        ...

    def choose_dvd_batch(self, *args: object) -> None:
        ...

    def clear_dvd_batch(self, *args: object) -> None:
        ...

    def on_dvd_title_changed(self, *args: object) -> None:
        ...

    def on_track_playlist_changed(self, *args: object) -> None:
        ...

    def cancel_operation(self, *args: object) -> None:
        ...

    def choose_output(self, *args: object) -> None:
        ...

    def on_output_changed(self, *args: object) -> None:
        ...


class SelectionCallbacks(Protocol):
    def on_format_changed(self, *args: object) -> None:
        ...

    def on_source_setting_changed(self, *args: object) -> None:
        ...

    def on_profile_changed(self, *args: object) -> None:
        ...

    def on_gui_language_changed(self, *args: object) -> None:
        ...

    def show_profile_dialog(self, *args: object) -> None:
        ...

    def select_all_items(self, *args: object) -> None:
        ...

    def select_all_streams(self, *args: object) -> None:
        ...

    def toggle_unselected(self, *args: object) -> None:
        ...

    def toggle_hidden_playlists(self, *args: object) -> None:
        ...

    def clear_all(self, *args: object) -> None:
        ...

    def populate_profile_choice(self, combo: Choice) -> None:
        ...


class ActivityCallbacks(Protocol):

    def on_status_changed(
        self, label: Gtk.Label, param: object,
        /,
    ) -> None:
        ...

    def scroll_log_to_end(self, *args: object) -> None:
        ...

    def on_log_scrolled(self, adjustment: Gtk.Adjustment) -> None:
        ...

    def setup_click_diagnostics(self, button: Gtk.Button) -> None:
        ...

    def show_legal_notice(self, *args: object) -> None:
        ...


class WindowCallbacks(
    SourceCallbacks,
    OperationCallbacks,
    SelectionCallbacks,
    ActivityCallbacks,
    Protocol,
):
    pass


class WindowView:
    """Construct the main window's widgets and connect their callbacks."""

    def __init__(self, callbacks: WindowCallbacks) -> None:
        self.callbacks = callbacks
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(18)
        root.set_margin_end(18)
        workflow_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        workflow_scroll.set_child(root)

        root.append(
            Gtk.Label(label="Source", xalign=0, css_classes=["heading"])
        )
        self.source_entry: Gtk.Entry = self.add_source_row(root)
        mode_format_box = Gtk.Box(spacing=8)
        mode_box = Gtk.Box(spacing=8)
        mode_box.set_hexpand(True)
        mode_format_box.append(mode_box)
        mode_box.append(Gtk.Label(label="Media", xalign=0))
        self.dvd_button = Gtk.ToggleButton(label="DVD", active=True)
        self.bluray_button = Gtk.ToggleButton(
            label="Blu-ray", group=self.dvd_button
        )
        self.dvd_button.set_tooltip_text(TOOLTIPS["dvd"])
        self.bluray_button.set_tooltip_text(TOOLTIPS["bluray"])
        self.dvd_button.connect("toggled", self.callbacks.on_mode_changed)
        self.bluray_button.connect("toggled", self.callbacks.on_mode_changed)
        self.dvd_button.connect("clicked", self.callbacks.on_manual_mode)
        self.bluray_button.connect("clicked", self.callbacks.on_manual_mode)
        mode_box.append(self.dvd_button)
        mode_box.append(self.bluray_button)
        self.format_box = Gtk.Box(spacing=8)
        self.format_combo = Choice(allow_none=False)
        for identifier, label in (
            ("auto", "Automatic (sample-based)"),
            ("mkv", "MKV (2D / selected streams)"),
            ("m2ts", "Original M2TS (unfiltered output)"),
        ):
            self.format_combo.append(identifier, label)
        self.format_combo.set_active_id("auto")
        add_field_label(self.format_box, "_Blu-ray output", self.format_combo)
        self.format_box.append(self.format_combo)
        self.format_combo.set_hexpand(True)
        root.append(mode_format_box)
        self.format_combo.connect("changed", self.callbacks.on_format_changed)

        self.dvd_title_row = Gtk.Box(spacing=8)
        self.dvd_title_combo = Choice(allow_none=False)
        self.dvd_title_combo.append("1", "Title 1 (default)")
        self.dvd_title_combo.set_active_id("1")
        add_field_label(self.dvd_title_row, "DVD _title", self.dvd_title_combo)
        self.dvd_title_row.append(self.dvd_title_combo)
        self.discover_titles_button = Gtk.Button(label="Discover Titles")
        self.discover_titles_button.set_tooltip_text(
            TOOLTIPS["discover_titles"]
        )
        self.discover_titles_button.connect(
            "clicked", self.callbacks.discover_dvd_titles
        )
        self.dvd_title_row.append(self.discover_titles_button)
        self.dvd_batch_button = Gtk.Button(label="Batch Titles")
        self.dvd_batch_button.set_tooltip_text(
            "Discover and select DVD titles to copy sequentially using the"
            " active language profile."
        )
        self.dvd_batch_button.connect(
            "clicked", self.callbacks.choose_dvd_batch
        )
        self.dvd_title_row.append(self.dvd_batch_button)
        root.append(self.dvd_title_row)
        self.dvd_batch_row = Gtk.Box(spacing=8, visible=False)
        self.dvd_batch_label = Gtk.Label(xalign=0, hexpand=True)
        self.dvd_batch_row.append(self.dvd_batch_label)
        self.clear_dvd_batch_button = Gtk.Button(label="Clear Batch")
        self.clear_dvd_batch_button.set_tooltip_text(
            "Discard the prepared DVD batch."
        )
        self.clear_dvd_batch_button.connect(
            "clicked", self.callbacks.clear_dvd_batch
        )
        self.dvd_batch_row.append(self.clear_dvd_batch_button)
        root.append(self.dvd_batch_row)

        output_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        output_box.append(
            Gtk.Label(label="Output", xalign=0, css_classes=["heading"])
        )
        output_box.append(self.format_box)
        self.output_entry: Gtk.Entry = add_path_row(
            output_box,
            "Output",
            "Choose output MKV or directory",
            self.callbacks.choose_output,
            tooltip=TOOLTIPS["output"],
        )
        self.name_row = Gtk.Box(spacing=8)
        self.name_entry = Gtk.Entry(
            hexpand=True, placeholder_text="Suggested name without .mkv"
        )
        add_field_label(self.name_row, "_Filename", self.name_entry)
        self.name_row.append(self.name_entry)
        output_box.append(self.name_row)

        root.append(
            Gtk.Label(label="Preferences", xalign=0, css_classes=["heading"])
        )
        self.minlength_row = Gtk.Box(spacing=8)
        settings_box = Gtk.Box(spacing=16)
        self.minimum_spin: Gtk.SpinButton = Gtk.SpinButton.new_with_range(
            0, 600, 1
        )
        add_field_label(
            self.minlength_row, "_Minimum minutes", self.minimum_spin
        )
        self.minimum_spin.set_value(5)
        self.minimum_spin.connect(
            "value-changed", self.callbacks.on_source_setting_changed
        )
        self.minlength_row.append(self.minimum_spin)

        self.profile_row = Gtk.Box(spacing=8)
        settings_box.append(self.profile_row)
        settings_box.append(self.minlength_row)
        self.profile_combo = Choice(allow_none=False)
        self.callbacks.populate_profile_choice(self.profile_combo)
        self.profile_combo.connect(
            "changed", self.callbacks.on_profile_changed
        )
        add_field_label(self.profile_row, "_Profile", self.profile_combo)
        self.profile_row.append(self.profile_combo)
        manage_profiles = Gtk.Button(label="Manage Profiles")
        manage_profiles.set_tooltip_text(TOOLTIPS["manage_profiles"])
        manage_profiles.connect("clicked", self.callbacks.show_profile_dialog)
        self.profile_row.append(manage_profiles)
        root.append(settings_box)

        language_row = Gtk.Box(spacing=8)
        self.gui_language_combo = Choice(allow_none=False)
        for identifier, label in (
            ("system", "System default"),
            ("en", "English"),
            ("it_IT", "Italiano"),
            ("ro_RO", "Română"),
        ):
            self.gui_language_combo.append(identifier, label)
        self.gui_language_combo.set_active_id("system")
        self.gui_language_combo.connect(
            "changed", self.callbacks.on_gui_language_changed
        )
        add_field_label(language_row, "GUI _language", self.gui_language_combo)
        language_row.append(self.gui_language_combo)
        language_notice = Gtk.Label(
            label=(
                "Restart to apply the language. Some messages remain in"
                " English."
            ),
            xalign=0,
            wrap=True,
        )
        language_row.append(language_notice)
        root.append(language_row)

        self.details_button = Gtk.Button(label="Disc Details")
        self.details_button.set_tooltip_text(TOOLTIPS["details"])
        self.details_button.connect(
            "clicked", self.callbacks.inspect_disc_details
        )
        mode_format_box.append(self.details_button)
        self.verify_button = Gtk.Button(label="Verify Copy")
        self.verify_button.set_tooltip_text(TOOLTIPS["verify_copy"])
        self.verify_button.connect(
            "clicked", self.callbacks.show_verify_dialog
        )
        mode_format_box.append(self.verify_button)

        self.inspect_button: Gtk.Button = Gtk.Button.new_with_mnemonic(
            "_Inspect"
        )
        self.inspect_button.set_tooltip_text(TOOLTIPS["inspect"])
        self.inspect_button.connect("clicked", self.callbacks.inspect)
        mode_format_box.insert_child_after(self.inspect_button, mode_box)
        self.copy_button: Gtk.Button = Gtk.Button.new_with_mnemonic(
            "_Copy Selected"
        )
        self.copy_button.set_tooltip_text(TOOLTIPS["copy"])
        self.copy_button.connect("clicked", self.callbacks.copy_selected)
        self.copy_button.add_css_class("suggested-action")
        self.select_all_items_button = Gtk.Button(label="Select All")
        self.select_all_items_button.set_tooltip_text(TOOLTIPS["select_all"])
        self.select_all_items_button.connect(
            "clicked", self.callbacks.select_all_items
        )
        self.show_unselected_button = Gtk.Button(label="Show unselected")
        self.show_unselected_button.set_tooltip_text(
            TOOLTIPS["show_unselected"]
        )
        self.show_unselected_button.connect(
            "clicked", self.callbacks.toggle_unselected
        )
        self.preview_button = Gtk.Button(label="Preview")
        self.preview_button.set_tooltip_text(TOOLTIPS["preview"])
        self.preview_button.connect("clicked", self.callbacks.preview_selected)
        self.inspect_streams_button = Gtk.Button(label="Inspect Tracks")
        self.inspect_streams_button.set_tooltip_text(
            TOOLTIPS["inspect_tracks"]
        )
        self.inspect_streams_button.connect(
            "clicked", self.callbacks.inspect_streams
        )
        self.callbacks.setup_click_diagnostics(self.inspect_streams_button)
        self.select_all_streams_button = Gtk.Button(label="Select All Tracks")
        self.select_all_streams_button.set_tooltip_text(
            TOOLTIPS["select_all_tracks"]
        )
        self.select_all_streams_button.connect(
            "clicked", self.callbacks.select_all_streams
        )
        self.cancel_button: Gtk.Button = Gtk.Button.new_with_mnemonic(
            "C_ancel"
        )
        self.cancel_button.set_tooltip_text(TOOLTIPS["cancel_operation"])
        self.cancel_button.set_sensitive(False)
        self.cancel_button.connect("clicked", self.callbacks.cancel_operation)
        self.clear_all_button = Gtk.Button.new_with_mnemonic("Clear _All")
        self.clear_all_button.set_tooltip_text(TOOLTIPS["clear_all"])
        self.clear_all_button.connect("clicked", self.callbacks.clear_all)
        self.status_label = Gtk.Label(label="Ready", xalign=0)
        self.status_label.set_hexpand(True)
        self.status_label.set_max_width_chars(28)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
            [
                "Operation status",
                "Current media detection, inspection, preview, or copy status",
            ],
        )
        self.status_label.connect(
            "notify::label", self.callbacks.on_status_changed
        )
        action_status_box = Gtk.Box(spacing=8)
        action_status_box.append(self.status_label)
        action_status_box.append(self.clear_all_button)
        action_status_box.append(self.cancel_button)
        action_status_box.append(self.copy_button)
        output_box.append(action_status_box)

        playlist_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        playlist_header = Gtk.Box(spacing=8)
        playlist_header.append(
            Gtk.Label(
                label="Streams or playlists",
                xalign=0,
                hexpand=True,
                css_classes=["heading"],
            )
        )
        playlist_header.append(self.select_all_items_button)
        playlist_header.append(self.show_unselected_button)
        playlist_header.append(self.preview_button)
        playlist_box.append(playlist_header)
        self.show_hidden_playlists_check = Gtk.CheckButton(
            label="Show dummy, top menu, and repeated playlists"
        )
        self.show_hidden_playlists_check.set_tooltip_text(
            "Show disc-labelled dummy and top menu playlists, and playlists"
            " with repeated video. Labels and repetition do not prove a"
            " playlist is unusable."
        )
        self.show_hidden_playlists_check.connect(
            "toggled", self.callbacks.toggle_hidden_playlists
        )
        self.show_hidden_playlists_check.set_visible(False)
        playlist_box.append(self.show_hidden_playlists_check)
        self.items_empty_label = Gtk.Label(
            label="No selected items. Use Show unselected to choose.",
            xalign=0, wrap=True,
        )
        self.items_empty_label.set_visible(False)
        playlist_box.append(self.items_empty_label)
        self.list_box = Gtk.ListView()
        self.list_box.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["DVD streams or Blu-ray playlists"],
        )
        list_scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=72)
        list_scroll.set_child(self.list_box)
        playlist_box.append(list_scroll)

        self.streams_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8
        )
        track_header = Gtk.Box(spacing=8)
        track_header.append(
            Gtk.Label(
                label="Tracks", xalign=0, hexpand=True, css_classes=["heading"]
            )
        )
        self.track_playlist_combo = Choice(none_label="Choose a playlist")
        self.track_playlist_combo.set_hexpand(True)
        self.track_playlist_combo.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Playlist for track inspection"]
        )
        self.track_playlist_combo.set_tooltip_text(
            "Playlist for track inspection"
        )
        track_header.append(self.track_playlist_combo)
        track_header.append(self.inspect_streams_button)
        track_header.append(self.select_all_streams_button)
        self.streams_box.append(track_header)
        self.streams_label = Gtk.Label(
            label="Choose a playlist to inspect its tracks", xalign=0
        )
        self.streams_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.streams_box.append(self.streams_label)
        self.tracks_empty_label = Gtk.Label(
            label="No selected tracks. Use Show unselected to choose.",
            xalign=0, wrap=True,
        )
        self.tracks_empty_label.set_visible(False)
        self.streams_box.append(self.tracks_empty_label)
        self.stream_list_box = Gtk.ListView()
        self.stream_list_box.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
            [
                "Blu-ray streams",
                (
                    "Check the video, audio, and subtitle tracks to include in"
                    " the MKV"
                ),
            ],
        )
        stream_scroll = Gtk.ScrolledWindow(
            vexpand=True, min_content_height=100
        )
        stream_scroll.set_child(self.stream_list_box)
        self.streams_box.append(stream_scroll)
        self.selection_panes = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        # Thin GTK handles extend their drag hit area over adjacent controls.
        self.selection_panes.set_wide_handle(True)
        self.selection_panes.set_start_child(playlist_box)
        self.selection_panes.set_end_child(self.streams_box)
        self.selection_panes.set_position(110)
        self.selection_panes.set_shrink_start_child(False)
        self.selection_panes.set_shrink_end_child(False)

        activity_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        activity_header = Gtk.Box(spacing=8)
        activity_header.append(
            Gtk.Label(label="Activity", xalign=0, hexpand=True)
        )
        self.legal_button = Gtk.Button(label="Legalese Pleasantries")
        self.legal_button.set_tooltip_text("Read the full legal notice.")
        self.legal_button.connect("clicked", self.callbacks.show_legal_notice)
        activity_header.append(self.legal_button)
        activity_box.append(activity_header)
        self.progress_bar = Gtk.ProgressBar(show_text=True)
        self.progress_bar.set_text("")
        self.progress_bar.set_visible(False)
        self.progress_bar.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
            [
                "Operation progress",
                (
                    "Current phase progress and estimated time remaining, when"
                    " available"
                ),
            ],
        )
        activity_box.append(self.progress_bar)
        self.log_buffer = Gtk.TextBuffer()
        self.log_buffer.set_enable_undo(False)
        log_view = Gtk.TextView(
            buffer=self.log_buffer, editable=False, monospace=True
        )
        log_view.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Activity output"]
        )
        log_scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hexpand=True,
            min_content_height=90,
            min_content_width=240,
        )
        log_scroll.set_child(log_view)
        self.log_adjustment: Gtk.Adjustment = log_scroll.get_vadjustment()
        self.log_adjustment.connect(
            "changed", self.callbacks.scroll_log_to_end
        )
        self.log_adjustment.connect(
            "value-changed", self.callbacks.on_log_scrolled
        )
        activity_box.append(log_scroll)
        self.activity_panes = Gtk.Paned(
            orientation=Gtk.Orientation.HORIZONTAL, vexpand=True
        )
        self.activity_panes.set_wide_handle(True)
        content_output_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        content_output_box.append(self.selection_panes)
        content_output_box.append(output_box)
        root.append(content_output_box)
        activity_box.set_margin_top(12)
        activity_box.set_margin_bottom(12)
        activity_box.set_margin_start(12)
        activity_box.set_margin_end(12)
        self.activity_panes.set_start_child(workflow_scroll)
        self.activity_panes.set_end_child(activity_box)
        self.activity_panes.set_shrink_start_child(False)
        self.activity_panes.set_shrink_end_child(False)
        self.activity_panes.set_resize_start_child(True)
        self.activity_panes.set_resize_end_child(False)
        self.activity_panes.set_position(820)
        self.root = self.activity_panes

    def translate(self, gettext: Callable[[str], str]) -> None:
        def translate_widget(widget: Gtk.Widget) -> None:
            if isinstance(
                widget,
                (Gtk.Label, Gtk.Button, Gtk.MenuButton, Gtk.CheckButton),
            ):
                label = widget.get_label()
                if label:
                    widget.set_label(gettext(label))
            elif isinstance(widget, Gtk.Entry):
                placeholder = widget.get_placeholder_text()
                if placeholder:
                    widget.set_placeholder_text(gettext(placeholder))
            tooltip = widget.get_tooltip_text()
            if tooltip:
                widget.set_tooltip_text(gettext(tooltip))
            child = widget.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                translate_widget(child)
                child = next_child

        translate_widget(self.root)
        self.track_playlist_combo.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [gettext("Playlist for track inspection")],
        )
        for choice in (
            self.format_combo,
            self.dvd_title_combo,
            self.gui_language_combo,
            self.track_playlist_combo,
        ):
            model = choice.get_model()
            if isinstance(model, Gtk.StringList):
                selected = choice.get_active_id()
                labels: list[str] = []
                for index in range(model.get_n_items()):
                    label = model.get_string(index)
                    if label is None:
                        raise ValueError(
                            f"Missing choice label at index {index}"
                        )
                    labels.append(gettext(label) if label else label)
                model.splice(0, model.get_n_items(), labels)
                choice.set_active_id(selected)

    def connect_final_signals(self) -> None:
        self.dvd_title_combo.connect(
            "changed", self.callbacks.on_dvd_title_changed
        )
        self.track_playlist_combo.connect(
            "changed", self.callbacks.on_track_playlist_changed
        )
        self.source_entry.connect("changed", self.callbacks.on_source_changed)
        self.output_entry.connect("changed", self.callbacks.on_output_changed)
        self.name_entry.connect("changed", self.callbacks.invalidate_choosers)

    def add_source_row(self, parent: Gtk.Box) -> Gtk.Entry:
        row = Gtk.Box(spacing=8)
        entry = Gtk.Entry(
            hexpand=True, placeholder_text="Enter DVD device, ISO, or folder"
        )
        add_field_label(row, "_Source", entry)
        row.append(entry)
        file_button = Gtk.Button(label="File")
        file_button.set_tooltip_text(TOOLTIPS["source_file"])
        file_button.connect("clicked", self.callbacks.choose_source_file)
        row.append(file_button)
        folder_button = Gtk.Button(label="Folder")
        folder_button.set_tooltip_text(TOOLTIPS["source_folder"])
        folder_button.connect("clicked", self.callbacks.choose_source_folder)
        row.append(folder_button)
        self.drives_button = Gtk.MenuButton(label="Drives")
        self.drives_button.set_tooltip_text(TOOLTIPS["drives"])
        self.drives_popover = Gtk.Popover()
        self.drives_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        self.drives_box.set_margin_top(8)
        self.drives_box.set_margin_bottom(8)
        self.drives_box.set_margin_start(8)
        self.drives_box.set_margin_end(8)
        self.drives_popover.set_child(self.drives_box)
        self.drives_button.set_popover(self.drives_popover)
        row.append(self.drives_button)
        parent.append(row)
        return entry
