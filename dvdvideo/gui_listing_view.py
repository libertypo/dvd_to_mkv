"""Virtualized GTK presentation for playlist and stream listing rows."""

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

from collections.abc import Callable, Collection, Iterable

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, GObject, Gtk  # type: ignore[reportMissingModuleSource]

from dvdvideo.gui_tooltips import TOOLTIPS
from dvdvideo.gui_translation import identity

RENDER_BATCH_ROWS = 32


class ListingRow:
    def __init__(self, label: str, active: bool, changed: Callable[..., object]) -> None:
        self.label = label
        self.active = active
        self.sensitive = True
        self.changed = changed
        self.widget: Gtk.CheckButton | None = None

    def get_active(self) -> bool:
        return self.active

    def set_active(self, active: bool) -> None:
        if self.active != active:
            self.active = active
            if self.widget is not None:
                self.widget.set_active(active)
            self.changed(self)

    def get_sensitive(self) -> bool:
        return self.sensitive

    def set_sensitive(self, sensitive: bool) -> None:
        self.sensitive = sensitive
        if self.widget is not None:
            self.widget.set_sensitive(sensitive)


class _RowItem(GObject.Object):
    def __init__(self, row: ListingRow) -> None:
        super().__init__()
        self.row = row


class _RowModel(GObject.Object, Gio.ListModel):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[ListingRow, str]] = []
        self.items: dict[int, _RowItem] = {}
        self.published = 0
        self.publication: int | None = None

    def do_get_item_type(self) -> GObject.GType:
        return _RowItem.__gtype__

    def do_get_n_items(self) -> int:
        return self.published

    def do_get_item(self, position: int) -> _RowItem | None:
        if position >= self.published:
            return None
        if position not in self.items:
            self.items[position] = _RowItem(self.rows[position][0])
        return self.items[position]

    def replace(self, rows: list[tuple[ListingRow, str]]) -> None:
        self.cancel_publication()
        previous = self.published
        prefix = 0
        shared = min(len(self.rows), len(rows))
        while prefix < shared and self.rows[prefix] == rows[prefix]:
            prefix += 1
        suffix = 0
        while (
            suffix < shared - prefix
            and self.rows[-suffix - 1] == rows[-suffix - 1]
        ):
            suffix += 1
        removed = len(self.rows) - prefix - suffix
        added = len(rows) - prefix - suffix
        published_prefix = min(prefix, previous)
        published_added = min(RENDER_BATCH_ROWS, added) if prefix <= previous else 0
        published_suffix = max(0, previous - prefix - removed) if added <= RENDER_BATCH_ROWS else 0
        published = published_prefix + published_added + published_suffix
        cached = {item.row: item for item in self.items.values()}
        self.rows = rows
        self.items = {
            index: cached[row] for index, (row, _identifier) in enumerate(rows[:published])
            if row in cached
        }
        # GTK preallocates a viewport cache; publish it in small main-loop batches too.
        self.published = published
        removed = previous - published_prefix - published_suffix
        added = published - published_prefix - published_suffix
        if removed or added:
            self.items_changed(published_prefix, removed, added)
        if self.published < len(rows):
            self.publication = GLib.idle_add(self._publish)

    def _publish(self) -> bool:
        previous = self.published
        self.published = min(previous + RENDER_BATCH_ROWS, len(self.rows))
        self.items_changed(previous, 0, self.published - previous)
        if self.published == len(self.rows):
            self.publication = None
            return False
        return True

    def cancel_publication(self) -> None:
        if self.publication is not None:
            GLib.source_remove(self.publication)
            self.publication = None

    def append(self, row: ListingRow, identifier: str) -> None:
        position = len(self.rows)
        self.rows.append((row, identifier))
        if self.publication is None:
            self.published += 1
            self.items_changed(position, 0, 1)


class ListingView:
    def __init__(
        self,
        list_box: Gtk.ListView,
        stream_list_box: Gtk.ListView,
        streams_label: Gtk.Label,
        on_selection_changed: Callable[..., object],
        on_stream_toggled: Callable[..., object],
        *,
        translate: Callable[[str], str] = identity,
        on_visibility_changed: Callable[[], None] = lambda: None,
    ) -> None:
        self.list_box = list_box
        self.stream_list_box = stream_list_box
        self.streams_label = streams_label
        self.on_selection_changed = on_selection_changed
        self.on_stream_toggled = on_stream_toggled
        self.translate = translate
        self.show_unselected = False
        self.on_visibility_changed = on_visibility_changed
        self._visibility_refresh: int | None = None
        self._closed = False
        self.rows: list[tuple[ListingRow, str]] = []
        self.stream_rows: list[tuple[ListingRow, str]] = []
        self._bound_rows: dict[Gtk.ListItem, tuple[ListingRow, Gtk.CheckButton, int]] = {}
        self._model = self._configure(list_box)
        self._stream_model = self._configure(stream_list_box)

    def _configure(self, view: Gtk.ListView) -> _RowModel:
        model = _RowModel()
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup)
        factory.connect("bind", self._bind)
        factory.connect("unbind", self._unbind)
        view.set_factory(factory)
        view.set_model(Gtk.NoSelection.new(model))
        return model

    def _setup(self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        checkbox = Gtk.CheckButton(margin_top=4, margin_bottom=4, hexpand=True)
        item.set_child(checkbox)

    def _bind(self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        model_item, checkbox = item.get_item(), item.get_child()
        if not isinstance(model_item, _RowItem) or not isinstance(checkbox, Gtk.CheckButton):
            raise TypeError("Listing items require a row model and a checkbox")
        row = model_item.row
        checkbox.set_label(row.label)
        checkbox.set_tooltip_text(self.translate(TOOLTIPS["select_item"]))
        checkbox.set_active(row.get_active())
        checkbox.set_sensitive(row.get_sensitive())
        row.widget = checkbox
        handler = checkbox.connect("toggled", self._toggled, row)
        self._bound_rows[item] = row, checkbox, handler

    def _unbind(self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        row, checkbox, handler = self._bound_rows.pop(item)
        checkbox.disconnect(handler)
        if row.widget is checkbox:
            row.widget = None

    def _toggled(self, checkbox: Gtk.CheckButton, row: ListingRow) -> None:
        row.set_active(checkbox.get_active())

    def _row(self, label: str, active: bool, *, streams: bool = False) -> ListingRow:
        callback = self.on_stream_toggled if streams else self.on_selection_changed

        def changed(row: ListingRow) -> None:
            try:
                callback(row)
            finally:
                self._schedule_visibility_refresh()

        return ListingRow(label, active, changed)

    def set_show_unselected(self, show: bool) -> None:
        self.show_unselected = show
        self._schedule_visibility_refresh()

    def _schedule_visibility_refresh(self) -> None:
        if not self._closed and self._visibility_refresh is None:
            self._visibility_refresh = GLib.idle_add(self._refresh_visibility)

    def _refresh_visibility(self) -> bool:
        self._visibility_refresh = None
        for rows, model in (
            (self.rows, self._model),
            (self.stream_rows, self._stream_model),
        ):
            visible = [
                (row, identifier) for row, identifier in rows
                if self.show_unselected or row.get_active()
            ]
            # Keep existing bindings and keyboard focus when membership did not change.
            if visible != model.rows:
                focused = next(
                    (index for index, (row, _identifier) in enumerate(model.rows)
                     if row.widget is not None and row.widget.has_focus()),
                    None,
                )
                visible_rows = {row for row, _identifier in visible}
                focus_position = (
                    sum(row in visible_rows for row, _identifier in model.rows[:focused])
                    if focused is not None else None
                )
                model.replace(visible)
                if focus_position is not None and visible:
                    row = visible[min(focus_position, len(visible) - 1)][0]
                    if row.widget is not None:
                        row.widget.grab_focus()
        self.on_visibility_changed()
        return False

    def render_rows(self, rows: Iterable[tuple[str, str, bool]]) -> None:
        self.rows = [(self._row(label, active), identifier) for identifier, label, active in rows]
        self.show_unselected = False
        self._model.replace([])
        self._schedule_visibility_refresh()

    def add_row(self, identifier: str, label: str, active: bool = False) -> None:
        row = self._row(label, active)
        self.rows.append((row, identifier))
        self._schedule_visibility_refresh()

    def clear_rows(self) -> None:
        self.rows.clear()
        self._model.replace([])
        self.show_unselected = False
        self._schedule_visibility_refresh()

    def close(self) -> None:
        self._closed = True
        if self._visibility_refresh is not None:
            GLib.source_remove(self._visibility_refresh)
            self._visibility_refresh = None
        self._model.cancel_publication()
        self._stream_model.cancel_publication()
        for view in (self.list_box, self.stream_list_box):
            view.set_model(None)
            view.set_factory(None)

    def active_ids(self, *, streams: bool = False) -> list[str]:
        rows = self.stream_rows if streams else self.rows
        return [identifier for row, identifier in rows if row.get_active()]

    def set_all_active(self, *, streams: bool = False) -> None:
        rows = self.stream_rows if streams else self.rows
        for row, _identifier in rows:
            row.set_active(True)

    def set_active_ids(self, selected: Collection[str], *, streams: bool = False) -> None:
        rows = self.stream_rows if streams else self.rows
        for row, identifier in rows:
            row.set_active(identifier in selected)

    def render_stream_rows(
        self, playlist: str, rows: Iterable[tuple[str, str]], selected: Collection[str],
    ) -> None:
        self.stream_rows = [
            (self._row(label, identifier in selected, streams=True), identifier)
            for identifier, label in rows
        ]
        self._stream_model.replace([])
        self._schedule_visibility_refresh()
        self.streams_label.set_text(self.translate("Streams for playlist {playlist}").format(playlist=playlist))

    def clear_stream_rows(self) -> None:
        self.stream_rows.clear()
        self._stream_model.replace([])
        self.streams_label.set_text(self.translate("Choose a playlist to inspect its tracks"))
        self._schedule_visibility_refresh()

    def update_stream_format(
        self,
        label: str,
        selected: Collection[str],
        *,
        all_streams: bool,
        sensitive: bool,
    ) -> None:
        self.streams_label.set_text(label)
        for row, identifier in self.stream_rows:
            row.set_active(all_streams or identifier in selected)
            row.set_sensitive(sensitive)
