# @copyright: 2026 libertypo
# @license: GNU GPL-3

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

import gi

# GTK's version must be selected before GTK-dependent imports.
# ruff: noqa: E402
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import (
    Gdk,  # type: ignore[reportMissingModuleSource]
    GObject,  # type: ignore[reportMissingModuleSource]
    Gtk,  # type: ignore[reportMissingModuleSource]
)

from dvdvideo.gui_translation import identity


class Choice(Gtk.DropDown):
    """A drop-down with stable identifiers separate from displayed labels."""

    __gsignals__: ClassVar[
        dict[str, tuple[GObject.SignalFlags, None, tuple[type, ...]]]
    ] = {
        "changed": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(
        self, *, allow_none: bool = True, none_label: str = ""
    ) -> None:
        # Gtk.DropDown cannot deselect a nonempty model; row zero means none.
        self._offset = 1 if allow_none else 0
        self._labels = Gtk.StringList.new([none_label] if allow_none else [])
        self._ids: list[str] = []
        super().__init__(model=self._labels)
        self.connect("notify::selected", lambda *_args: self.emit("changed"))

    def append(self, identifier: str, label: str) -> None:
        self._ids.append(identifier)
        self._labels.append(label)

    def remove_all(self) -> None:
        if self._offset:
            self.set_selected(0)
        self._ids.clear()
        self._labels.splice(
            self._offset, self._labels.get_n_items() - self._offset, []
        )

    def set_active_id(self, identifier: str | None) -> None:
        index = (
            self._ids.index(identifier) + self._offset
            if identifier in self._ids
            else (0 if self._offset else Gtk.INVALID_LIST_POSITION)
        )
        self.set_selected(index)

    def get_active_id(self) -> str | None:
        selected = self.get_selected()
        index = selected - self._offset
        return self._ids[index] if 0 <= index < len(self._ids) else None


class ResponseWindow(Gtk.Window):
    __gsignals__: ClassVar[
        dict[str, tuple[GObject.SignalFlags, None, tuple[type, ...]]]
    ] = {
        "response": (GObject.SignalFlags.RUN_LAST, None, (int,)),
    }

    def __init__(
        self, *, title: str, transient_for: Gtk.Window, modal: bool = True
    ) -> None:
        super().__init__(
            title=title,
            transient_for=transient_for,
            modal=modal,
            destroy_with_parent=True,
        )
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self._buttons.set_margin_top(12)
        self._buttons.set_margin_bottom(12)
        self._buttons.set_margin_start(12)
        self._buttons.set_margin_end(12)
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        layout.append(self._content)
        layout.append(self._buttons)
        self.set_child(layout)
        self.connect("close-request", self._close_requested)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)

    def _close_requested(self, *_args: object) -> bool:
        self.response(Gtk.ResponseType.CANCEL)
        return True

    def _key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.response(Gtk.ResponseType.CANCEL)
            return True
        return False

    def add_button(
        self, label: str, response: int, *, tooltip: str, default: bool = False
    ) -> Gtk.Button:
        button = Gtk.Button(label=label, tooltip_text=tooltip)
        button.connect("clicked", lambda *_args: self.response(response))
        self._buttons.append(button)
        if default:
            self.set_default_widget(button)
        return button

    def get_content_area(self) -> Gtk.Box:
        return self._content

    def response(self, response: int) -> None:
        self.emit("response", response)


def add_field_label(row: Gtk.Box, text: str, widget: Gtk.Widget) -> None:
    label = Gtk.Label(label=text, use_underline=True, xalign=0)
    label.set_mnemonic_widget(widget)
    row.append(label)


def add_path_row(
    parent: Gtk.Box,
    label: str,
    placeholder: str,
    callback: Callable[..., object],
    *,
    tooltip: str,
    translate: Callable[[str], str] = identity,
) -> Gtk.Entry:
    row = Gtk.Box(spacing=8)
    entry = Gtk.Entry(hexpand=True, placeholder_text=placeholder)
    entry.set_activates_default(True)
    add_field_label(row, translate(f"_{label}"), entry)
    row.append(entry)
    button = Gtk.Button(
        label=translate("Browse"), tooltip_text=translate(tooltip)
    )
    button.connect("clicked", callback)
    row.append(button)
    parent.append(row)
    return entry
