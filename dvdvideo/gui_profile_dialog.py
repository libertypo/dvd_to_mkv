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

import gi

# GTK's version must be selected before GTK-dependent imports.
# ruff: noqa: E402
gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[reportMissingModuleSource]

from dvdvideo.gui_preference_workflow import ProfileUpdateCallback
from dvdvideo.gui_preferences import (
    DEFAULT_PROFILE_NAME,
    PreferencesStore,
    Profile,
    parse_language_codes,
)
from dvdvideo.gui_preferences_io import PreferencesDurabilityError
from dvdvideo.gui_tooltips import TOOLTIPS
from dvdvideo.gui_translation import identity
from dvdvideo.gui_widgets import Choice, ResponseWindow


class ProfileDialog(ResponseWindow):
    def __init__(
        self,
        parent: Gtk.Window,
        preferences: PreferencesStore,
        on_changed: ProfileUpdateCallback,
        current_profile: str | None = None,
        *,
        get_current_profile: Callable[[], str | None],
        translate: Callable[[str], str] = identity,
    ) -> None:
        super().__init__(
            title=translate("Manage profiles"),
            transient_for=parent,
            modal=True,
        )
        self.translate = translate
        self.preferences = preferences
        self.on_changed = on_changed
        self.get_current_profile = get_current_profile
        self.current_profile = current_profile or preferences.active_profile
        self.set_default_size(420, 300)
        self.add_button(
            translate("Close"),
            Gtk.ResponseType.CLOSE,
            tooltip=translate(TOOLTIPS["close_profiles"]),
        )
        save_button = self.add_button(
            translate("Save"),
            Gtk.ResponseType.APPLY,
            tooltip=translate(TOOLTIPS["save_profile"]),
            default=True,
        )
        save_button.add_css_class("suggested-action")
        self.connect("response", self.on_dialog_response)

        content = self.get_content_area()
        content.set_spacing(8)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        self.profile_combo = Choice()
        for name in preferences.profile_names:
            self.profile_combo.append(name, name)
        self.profile_combo.set_active_id(self.current_profile)
        self.profile_combo.connect("changed", self.on_profile_selected)
        profile_row = Gtk.Box(spacing=8)
        profile_label = Gtk.Label(
            label=translate("_Profile"), xalign=0, use_underline=True
        )
        profile_label.set_mnemonic_widget(self.profile_combo)
        profile_row.append(profile_label)
        self.profile_combo.set_hexpand(True)
        profile_row.append(self.profile_combo)
        new_button = Gtk.Button(label=translate("New"))
        new_button.set_tooltip_text(translate(TOOLTIPS["new_profile"]))
        new_button.connect("clicked", self.on_new_profile)
        profile_row.append(new_button)
        self.delete_button = Gtk.Button(label=translate("Delete"))
        self.delete_button.set_tooltip_text(
            translate(TOOLTIPS["delete_profile"])
        )
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.connect("clicked", self.on_delete_profile)
        profile_row.append(self.delete_button)
        content.append(profile_row)
        self.name_entry = Gtk.Entry()
        self.audio_entry = Gtk.Entry(
            placeholder_text=translate("e.g. eng, jpn")
        )
        self.subtitle_entry = Gtk.Entry(placeholder_text=translate("e.g. eng"))
        for entry in (self.name_entry, self.audio_entry, self.subtitle_entry):
            entry.set_activates_default(True)
        self.format_combo = Choice(allow_none=False)
        for identifier, label in (
            ("auto", translate("Automatic")),
            ("mkv", "MKV"),
            ("m2ts", translate("Original M2TS")),
        ):
            self.format_combo.append(identifier, label)
        self.minimum_spin = Gtk.SpinButton.new_with_range(0, 600, 1)
        self.error_label = Gtk.Label(xalign=0, wrap=True)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        fields = (
            (translate("_Name"), self.name_entry),
            (translate("_Audio languages"), self.audio_entry),
            (translate("_Subtitle languages"), self.subtitle_entry),
            (translate("_Blu-ray output"), self.format_combo),
            (translate("_Minimum minutes"), self.minimum_spin),
        )
        for row, (label, widget) in enumerate(fields):
            field_label = Gtk.Label(label=label, xalign=0, use_underline=True)
            field_label.set_mnemonic_widget(widget)
            grid.attach(field_label, 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
        content.append(grid)
        content.append(self.error_label)

        self.on_profile_selected()

    def on_dialog_response(
        self, _dialog: ResponseWindow, response: int
    ) -> None:
        if response == Gtk.ResponseType.APPLY:
            self.on_save_profile()
        else:
            self.destroy()

    def on_profile_selected(self, *_args: object) -> None:
        profile = self.preferences.get_profile(
            self.profile_combo.get_active_id()
        )
        self.delete_button.set_sensitive(
            self.profile_combo.get_active_id()
            not in (None, DEFAULT_PROFILE_NAME)
        )
        self.name_entry.set_text(profile.name)
        self.audio_entry.set_text(", ".join(profile.audio_languages))
        self.subtitle_entry.set_text(", ".join(profile.subtitle_languages))
        self.format_combo.set_active_id(profile.output_format)
        self.minimum_spin.set_value(profile.minimum_minutes)
        self.error_label.set_text("")

    def on_new_profile(self, *_args: object) -> None:
        self.profile_combo.set_active_id(None)
        self.name_entry.set_text("")
        self.audio_entry.set_text("")
        self.subtitle_entry.set_text("")
        self.format_combo.set_active_id("auto")
        self.minimum_spin.set_value(5)
        self.error_label.set_text("")

    def on_save_profile(self, *_args: object) -> None:
        try:
            name = self.name_entry.get_text().strip()
            selected = self.profile_combo.get_active_id()
            if name in self.preferences.profiles and name != selected:
                raise ValueError(
                    self.translate(
                        "A profile with that name already exists; select it to"
                        " edit it."
                    )
                )
            profile = Profile(
                name=name,
                audio_languages=parse_language_codes(
                    self.audio_entry.get_text()
                ),
                subtitle_languages=parse_language_codes(
                    self.subtitle_entry.get_text()
                ),
                output_format=self.format_combo.get_active_id() or "auto",
                minimum_minutes=self.minimum_spin.get_value_as_int(),
            )
        except ValueError as error:
            self.error_label.set_text(str(error))
            return
        warning = None
        try:
            self.preferences.save_profile(
                profile, previous_name=selected, activate=True
            )
        except PreferencesDurabilityError as error:
            warning = str(error)
        except (OSError, ValueError) as error:
            self.error_label.set_text(str(error))
            return
        name = profile.name.strip()
        replacement = (
            (selected, name)
            if selected is not None and selected != name
            else None
        )
        self.current_profile, applied = self.on_changed(
            name, True, replacement
        )
        self.refresh_profiles(name)
        if not applied:
            message = self.translate(
                "Profile saved, but not applied; see Activity for details."
            )
            self.error_label.set_text(
                f"{message}\n{warning}" if warning else message
            )
            return
        self.error_label.set_text(
            warning
            or (
                self.translate("Profile updated; save queued.")
                if self.preferences.writer
                else self.translate("Profile saved.")
            )
        )

    def on_delete_profile(self, *_args: object) -> None:
        name = self.profile_combo.get_active_id()
        if name is None or name == DEFAULT_PROFILE_NAME:
            self.error_label.set_text(
                self.translate("The Default profile cannot be deleted.")
            )
            return
        warning = None
        current_profile = (
            self.get_current_profile() or self.preferences.active_profile
        )
        was_active = name == current_profile
        try:
            self.preferences.delete_profile(name)
        except PreferencesDurabilityError as error:
            warning = str(error)
        except (OSError, ValueError) as error:
            self.error_label.set_text(str(error))
            return
        self.current_profile = (
            DEFAULT_PROFILE_NAME if was_active else current_profile
        )
        self.refresh_profiles(self.current_profile)
        self.current_profile, applied = self.on_changed(
            self.current_profile, was_active, (name, DEFAULT_PROFILE_NAME),
        )
        if not applied:
            message = self.translate(
                "Profile deleted, but the replacement was not applied; see"
                " Activity."
            )
            self.error_label.set_text(
                f"{message}\n{warning}" if warning else message
            )
            return
        self.error_label.set_text(
            warning
            or (
                self.translate("Profile deleted; save queued.")
                if self.preferences.writer
                else self.translate("Profile deleted.")
            )
        )

    def refresh_profiles(self, selected: str) -> None:
        self.profile_combo.remove_all()
        for name in self.preferences.profile_names:
            self.profile_combo.append(name, name)
        self.profile_combo.set_active_id(selected)
