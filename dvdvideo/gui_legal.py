# @copyright: 2026 libertypo
# @license: GNU GPL-3

"""First-launch and full legal-notice dialogs."""

from __future__ import annotations

from collections.abc import Callable

import gi

# GTK's version must be selected before GTK-dependent imports.
# ruff: noqa: E402
gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # type: ignore[reportMissingModuleSource]

from dvdvideo.gui_widgets import ResponseWindow
from dvdvideo.legal_notice import NOTICE

SUMMARY = (
    "Use this software only where copying and any necessary decryption are"
    " lawful. The software is provided without warranty."
)


def show_full_notice(
    parent: Gtk.Window, translate: Callable[[str], str]
) -> None:
    dialog = ResponseWindow(
        title=translate("Legalese Pleasantries"), transient_for=parent
    )
    dialog.set_default_size(700, 560)
    content = dialog.get_content_area()
    content.set_vexpand(True)
    content.set_margin_top(12)
    content.set_margin_start(12)
    content.set_margin_end(12)
    text = NOTICE.replace("**", "").replace("<u>", "").replace("</u>", "")
    text = text.replace(
        "[GNU General Public License](./LICENSE)",
        "GNU General Public License (LICENSE)",
    )
    view = Gtk.TextView(
        editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR
    )
    view.get_buffer().set_text(text)
    scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
    scroll.set_child(view)
    content.append(scroll)
    dialog.add_button(
        translate("Close"),
        Gtk.ResponseType.CLOSE,
        tooltip=translate("Close the legal notice."),
        default=True,
    )

    def close(window: ResponseWindow, _response: int) -> None:
        window.destroy()

    dialog.connect("response", close)
    dialog.present()


def show_first_notice(
    parent: Gtk.Window,
    translate: Callable[[str], str],
    dismissed: Callable[[], None],
) -> None:
    dialog = ResponseWindow(
        title=translate("Legal notice"), transient_for=parent
    )
    content = dialog.get_content_area()
    content.set_margin_top(12)
    content.set_margin_start(12)
    content.set_margin_end(12)
    content.append(
        Gtk.Label(
            label=translate(SUMMARY), wrap=True, max_width_chars=60, xalign=0
        )
    )
    dialog.add_button(
        translate("Legalese Pleasantries"), Gtk.ResponseType.APPLY,
        tooltip=translate("Read the full legal notice."),
    )
    dialog.add_button(
        translate("Close"), Gtk.ResponseType.CLOSE,
        tooltip=translate("Close the legal notice."), default=True,
    )

    def response(window: ResponseWindow, result: int) -> None:
        if result == Gtk.ResponseType.APPLY:
            show_full_notice(window, translate)
        else:
            window.destroy()
            dismissed()

    dialog.connect("response", response)
    dialog.present()
