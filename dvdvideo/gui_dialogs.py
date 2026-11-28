# @copyright: 2026 libertypo
# @license: GNU GPL-3

"""GTK dialogs for disc details, copy confirmation, and verification."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import gi

# GTK's version must be selected before GTK-dependent imports.
# ruff: noqa: E402
gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[reportMissingModuleSource]

from dvdvideo.gui_activity import clean_text
from dvdvideo.gui_tooltips import TOOLTIPS
from dvdvideo.gui_translation import identity
from dvdvideo.gui_widgets import ResponseWindow, add_field_label, add_path_row


def create_copy_summary(
    parent: Gtk.Window, summary: str, respond: Callable[[bool], None],
    *, translate: Callable[[str], str] = identity,
) -> ResponseWindow:
    dialog = ResponseWindow(
        title=translate("Copy Summary"), transient_for=parent
    )
    dialog.set_default_size(700, 520)
    content = dialog.get_content_area()
    view = Gtk.TextView(
        editable=False, cursor_visible=False, monospace=True,
        wrap_mode=Gtk.WrapMode.WORD_CHAR,
    )
    view.get_buffer().set_text(clean_text(summary))
    scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
    scroll.set_child(view)
    content.append(scroll)
    dialog.add_button(
        translate("Cancel"),
        Gtk.ResponseType.CANCEL,
        tooltip=translate(TOOLTIPS["cancel_copy"]),
        default=True,
    )
    start = dialog.add_button(
        translate("Start Copy"),
        Gtk.ResponseType.OK,
        tooltip=translate(TOOLTIPS["start_copy"]),
    )
    start.add_css_class("suggested-action")

    def response(_dialog: ResponseWindow, result: int) -> None:
        dialog.destroy()
        respond(result == Gtk.ResponseType.OK)

    dialog.connect("response", response)
    return dialog


def create_disc_details(
    parent: Gtk.Window,
    source: str,
    text: str,
    on_close: Callable[[Gtk.Window], bool],
    *,
    translate: Callable[[str], str] = identity,
) -> Gtk.Window:
    dialog = Gtk.Window(
        title=translate("Disc Details"),
        transient_for=parent,
        destroy_with_parent=True,
    )
    dialog.set_default_size(700, 560)
    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    root.set_margin_top(12)
    root.set_margin_bottom(12)
    root.set_margin_start(12)
    root.set_margin_end(12)
    root.append(Gtk.Label(label=source, xalign=0, selectable=True, wrap=True))
    view = Gtk.TextView(
        editable=False,
        cursor_visible=False,
        monospace=True,
        wrap_mode=Gtk.WrapMode.WORD_CHAR,
    )
    view.get_buffer().set_text(text)
    scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
    scroll.set_child(view)
    root.append(scroll)
    close = Gtk.Button(label=translate("Close"), halign=Gtk.Align.END)
    close.set_tooltip_text(translate(TOOLTIPS["close_details"]))

    def close_clicked(_button: Gtk.Button) -> None:
        dialog.close()

    close.connect("clicked", close_clicked)
    root.append(close)
    dialog.set_child(root)
    dialog.connect("close-request", on_close)
    return dialog


class VerificationDialog(ResponseWindow):

    def __init__(
        self,
        parent: Gtk.Window,
        *,
        bluray: bool,
        title: str,
        streams: list[str],
        choose_copy: Callable[[Gtk.Entry], None],
        translate: Callable[[str], str] = identity,
    ) -> None:
        super().__init__(
            title=translate("Verify Existing Copy"), transient_for=parent
        )
        self.add_button(
            translate("Cancel"),
            Gtk.ResponseType.CANCEL,
            tooltip=translate(TOOLTIPS["cancel_verify"]),
        )
        self.add_button(
            translate("Verify"),
            Gtk.ResponseType.OK,
            tooltip=translate(TOOLTIPS["start_verify"]),
            default=True,
        )
        content = self.get_content_area()
        content.set_spacing(8)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.append(
            Gtk.Label(
                label=translate(
                    "Compare an existing copy with the selected source. "
                    "This rereads the source and needs temporary disk space."
                ),
                wrap=True,
                xalign=0,
            )
        )
        title_row = Gtk.Box(spacing=8)
        self.title_entry = Gtk.Entry(text=title, hexpand=True)
        self.title_entry.set_activates_default(True)
        add_field_label(
            title_row,
            translate("_Playlist") if bluray else translate("_DVD title"),
            self.title_entry,
        )
        title_row.append(self.title_entry)
        content.append(title_row)
        stream_row = Gtk.Box(spacing=8)
        self.stream_entry = Gtk.Entry(text=", ".join(streams), hexpand=True)
        self.stream_entry.set_activates_default(True)
        add_field_label(
            stream_row, translate("_Stream IDs"), self.stream_entry
        )
        stream_row.append(self.stream_entry)
        content.append(stream_row)
        content.append(
            Gtk.Label(
                label=translate(
                    "Enter the original stream indexes (DVD) or PIDs (Blu-ray)"
                    " in copy order, separated by commas. Leave empty for"
                    " original M2TS."
                ),
                wrap=True,
                xalign=0,
            )
        )

        def browse_copy(_button: Gtk.Button) -> None:
            choose_copy(self.copy_entry)

        self.copy_entry = add_path_row(
            content,
            "Copy",
            translate("Choose existing MKV or M2TS"),
            browse_copy,
            tooltip=TOOLTIPS["existing_copy"],
            translate=translate,
        )
        saved_streams = self.stream_entry.get_text()
        m2ts_selected = False

        def copy_changed(_entry: Gtk.Entry) -> None:
            nonlocal saved_streams, m2ts_selected
            is_m2ts = (
                bluray
                and Path(self.copy_entry.get_text()).suffix.lower() == ".m2ts"
            )
            if is_m2ts != m2ts_selected:
                if is_m2ts:
                    saved_streams = self.stream_entry.get_text()
                    self.stream_entry.set_text("")
                else:
                    self.stream_entry.set_text(saved_streams)
                self.stream_entry.set_sensitive(not is_m2ts)
                m2ts_selected = is_m2ts

        self.copy_entry.connect("changed", copy_changed)
