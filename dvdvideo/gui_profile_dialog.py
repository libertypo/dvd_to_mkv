from __future__ import annotations

from collections.abc import Callable

from gi.repository import Gtk  # type: ignore[reportMissingModuleSource]

from dvdvideo.gui_preferences import (
    DEFAULT_PROFILE_NAME,
    PreferencesStore,
    Profile,
    parse_language_codes,
)


class ProfileDialog(Gtk.Dialog):
    def __init__(
        self,
        parent: Gtk.Window,
        preferences: PreferencesStore,
        on_changed: Callable[[str, bool], None],
        current_profile: str | None = None,
    ) -> None:
        super().__init__(title="Manage profiles", transient_for=parent, modal=True)
        self.preferences = preferences
        self.on_changed = on_changed
        self.current_profile = current_profile or preferences.active_profile
        self.set_default_size(420, 300)
        self.add_button("Close", Gtk.ResponseType.CLOSE)

        content = self.get_content_area()
        content.set_spacing(8)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        self.profile_combo = Gtk.ComboBoxText()
        for name in preferences.profile_names:
            self.profile_combo.append(name, name)
        self.profile_combo.set_active_id(self.current_profile)
        self.profile_combo.connect("changed", self.on_profile_selected)
        self.name_entry = Gtk.Entry()
        self.audio_entry = Gtk.Entry(placeholder_text="e.g. eng, jpn")
        self.subtitle_entry = Gtk.Entry(placeholder_text="e.g. eng")
        self.format_combo = Gtk.ComboBoxText()
        for identifier, label in (
            ("auto", "Automatic"),
            ("mkv", "MKV"),
            ("m2ts", "Original M2TS"),
        ):
            self.format_combo.append(identifier, label)
        self.minimum_spin = Gtk.SpinButton.new_with_range(0, 600, 1)
        self.error_label = Gtk.Label(xalign=0, wrap=True)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        fields = (
            ("Profile", self.profile_combo),
            ("Name", self.name_entry),
            ("Audio languages", self.audio_entry),
            ("Subtitle languages", self.subtitle_entry),
            ("Blu-ray output", self.format_combo),
            ("Minimum minutes", self.minimum_spin),
        )
        for row, (label, widget) in enumerate(fields):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
        content.append(grid)
        content.append(self.error_label)

        buttons = Gtk.Box(spacing=8)
        new_button = Gtk.Button(label="New")
        new_button.connect("clicked", self.on_new_profile)
        save_button = Gtk.Button(label="Save")
        save_button.connect("clicked", self.on_save_profile)
        delete_button = Gtk.Button(label="Delete")
        delete_button.connect("clicked", self.on_delete_profile)
        buttons.append(new_button)
        buttons.append(save_button)
        buttons.append(delete_button)
        content.append(buttons)
        self.on_profile_selected()

    def on_profile_selected(self, *_args: object) -> None:
        profile = self.preferences.get_profile(self.profile_combo.get_active_id())
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
        self.minimum_spin.set_value(2)
        self.error_label.set_text("")

    def on_save_profile(self, *_args: object) -> None:
        try:
            profile = Profile(
                name=self.name_entry.get_text(),
                audio_languages=parse_language_codes(self.audio_entry.get_text()),
                subtitle_languages=parse_language_codes(self.subtitle_entry.get_text()),
                output_format=self.format_combo.get_active_id() or "auto",
                minimum_minutes=self.minimum_spin.get_value_as_int(),
            )
            self.preferences.save_profile(profile)
            self.preferences.set_active_profile(profile.name.strip())
        except (OSError, ValueError) as error:
            self.error_label.set_text(str(error))
            return
        self.current_profile = profile.name.strip()
        self.refresh_profiles(self.current_profile)
        self.on_changed(self.current_profile, True)
        self.error_label.set_text(
            "Profile updated; save queued." if self.preferences.writer else "Profile saved."
        )

    def on_delete_profile(self, *_args: object) -> None:
        name = self.profile_combo.get_active_id()
        if name is None or name == DEFAULT_PROFILE_NAME:
            self.error_label.set_text("The Default profile cannot be deleted.")
            return
        try:
            was_active = name == self.current_profile
            self.preferences.delete_profile(name)
        except (OSError, ValueError) as error:
            self.error_label.set_text(str(error))
            return
        if was_active:
            self.current_profile = DEFAULT_PROFILE_NAME
        self.refresh_profiles(self.current_profile)
        self.on_changed(self.current_profile, was_active)
        self.error_label.set_text(
            "Profile deleted; save queued." if self.preferences.writer else "Profile deleted."
        )

    def refresh_profiles(self, selected: str) -> None:
        self.profile_combo.remove_all()
        for name in self.preferences.profile_names:
            self.profile_combo.append(name, name)
        self.profile_combo.set_active_id(selected)
