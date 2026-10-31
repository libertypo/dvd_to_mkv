#!/usr/bin/env python3

from __future__ import annotations

from collections import deque
import os
import re
import signal
import shutil
import stat
import subprocess
import threading
import traceback

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk  # noqa: I001

from dvdvideo.gui_support import ManagedProcess, OperationCancelled, filename_stem, validate_output_paths

SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dvd-to-mkv.sh")
MAX_LOG_CHARS = 512 * 1024
MAX_PENDING_CHARS = 256 * 1024
# GTK lays out each line as a whole; unbroken output would grow memory without bound.
MAX_LOG_LINE_CHARS = 1024
LOG_FOLLOW_THRESHOLD = 16
DISC_MARKERS = (("BDMV", "INDEX.BDMV", "bluray"), ("VIDEO_TS", "VIDEO_TS.IFO", "dvd"))
# Caps the 7z probe's virtual address space (RLIMIT_AS), not its resident memory.
PROBE_ADDRESS_SPACE_LIMIT = 512 * 1024 * 1024
# Inspection output comes from untrusted discs; keep the lists and labels bounded.
MAX_INSPECTION_ROWS = 2000
MAX_STREAM_ROWS = 512
MAX_LABEL_CHARS = 200
PLAYLIST_ID = re.compile(r"[0-9]{1,5}")
DVD_STREAM_INDEX = re.compile(r"[0-9]{1,4}")
STREAM_PID = re.compile(r"0x0{0,4}[0-9a-fA-F]{1,4}")
MAX_STREAM_PID = 0x1FFF  # MPEG-TS PIDs are 13 bits.
# C0/C1 controls (except tab and newline) and bidirectional overrides can disguise external text.
UNSAFE_TEXT = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
SOURCE_SYMLINK_ERROR = "Symbolic links are not accepted as sources; choose the real file or folder."


def clean_text(text):
    return UNSAFE_TEXT.sub("", text)


def clean_label(text):
    text = clean_text(text)
    return text if len(text) <= MAX_LABEL_CHARS else text[:MAX_LABEL_CHARS - 1] + "…"


def symlinked_source(source):
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


def is_plain_directory(path):
    return stat.S_ISDIR(os.lstat(path).st_mode)


def is_plain_file(path):
    return stat.S_ISREG(os.lstat(path).st_mode)


def source_name(source):
    path = os.path.normpath(source)
    if os.path.basename(path).upper() in ("BDMV", "VIDEO_TS"):
        path = os.path.dirname(path)
    name = os.path.basename(path)
    if name.lower().endswith((".iso", ".img")):
        name = os.path.splitext(name)[0]
    return filename_stem(name) or "disc"


def find_entry(directory, name, accept):
    # Disc folders are matched case-insensitively; copies often use lowercase names.
    for entry in os.listdir(directory):
        path = os.path.join(directory, entry)
        if entry.upper() == name and accept(path):
            return path
    return None


def detect_media(source, probe=None):
    """Return "dvd" or "bluray" from the disc structure; images and devices are listed with 7z."""
    if symlinked_source(source):
        raise RuntimeError(SOURCE_SYMLINK_ERROR)
    source_stat = os.stat(source)
    if stat.S_ISDIR(source_stat.st_mode):
        source_folder = os.path.basename(os.path.normpath(source)).upper()
        kinds = set()
        for folder, marker, kind in DISC_MARKERS:
            folder_path = source if source_folder == folder else find_entry(source, folder, is_plain_directory)
            if folder_path and find_entry(folder_path, marker, is_plain_file):
                kinds.add(kind)
    elif stat.S_ISREG(source_stat.st_mode) or stat.S_ISBLK(source_stat.st_mode):
        tool = shutil.which("7zz") or shutil.which("7z")
        if not tool:
            raise RuntimeError("Automatic image/device detection requires 7zz or 7z. Choose DVD or Blu-ray manually.")
        limiter = shutil.which("prlimit")
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
        kinds = {kind for folder, marker, kind in DISC_MARKERS if f"{folder}/{marker}" in paths}
    else:
        raise RuntimeError("Source must be a disc image, optical device, or disc folder.")
    if len(kinds) != 1:
        raise RuntimeError(
            "Source has no unique BDMV/index.bdmv or VIDEO_TS/VIDEO_TS.IFO structure. "
            "Choose DVD or Blu-ray manually."
        )
    return kinds.pop()


class DvdToMkvWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="DVD to MKV")
        self.set_default_size(860, 720)
        self.rows = []
        self.stream_rows = []
        self.playlist_streams = {}
        self.stream_playlist = None
        self.running = False
        self.suggested_name = ""
        self.suggested_output = ""
        self.detection_generation = 0  # Invalidates detection on source changes or cancellation.
        self.detection_timeout = None
        self.detecting = False
        self.detection_probe = None
        self.jobs = []
        self.operation = None
        self.source_generation = 0  # Invalidates inspection/copy results on source changes.
        self.chooser_generation = 0
        self.mode_outputs = {False: "", True: ""}
        self.output_mode = False
        self.closing = False
        self.close_dialog = None
        self.close_timer = None
        self.applying_detection = False
        self.log_lock = threading.Lock()
        self.pending_log = deque()
        self.pending_log_chars = 0
        self.dropped_log_chars = 0
        self.log_column = 0

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(18)
        root.set_margin_end(18)
        self.set_child(root)

        mode_box = Gtk.Box(spacing=8)
        root.append(mode_box)
        mode_box.append(Gtk.Label(label="Media", xalign=0))
        self.dvd_button = Gtk.ToggleButton(label="DVD", active=True)
        self.bluray_button = Gtk.ToggleButton(label="Blu-ray", group=self.dvd_button)
        self.dvd_button.connect("toggled", self.on_mode_changed)
        self.bluray_button.connect("toggled", self.on_mode_changed)
        self.dvd_button.connect("clicked", self.on_manual_mode)
        self.bluray_button.connect("clicked", self.on_manual_mode)
        mode_box.append(self.dvd_button)
        mode_box.append(self.bluray_button)

        self.source_entry = self.add_source_row(root)
        self.output_entry = self.add_path_row(root, "Output", "Choose output MKV or directory", self.choose_output)
        self.name_row = Gtk.Box(spacing=8)
        self.name_entry = Gtk.Entry(hexpand=True, placeholder_text="Suggested name without .mkv")
        self.add_field_label(self.name_row, "_Filename", self.name_entry)
        self.name_row.append(self.name_entry)
        root.append(self.name_row)

        self.minlength_row = Gtk.Box(spacing=8)
        self.minimum_spin = Gtk.SpinButton.new_with_range(0, 600, 1)
        self.add_field_label(self.minlength_row, "_Minimum minutes", self.minimum_spin)
        self.minimum_spin.set_value(2)
        self.minlength_row.append(self.minimum_spin)
        root.append(self.minlength_row)

        buttons = Gtk.Box(spacing=8)
        self.inspect_button = Gtk.Button.new_with_mnemonic("_Inspect")
        self.inspect_button.connect("clicked", self.inspect)
        self.copy_button = Gtk.Button.new_with_mnemonic("_Copy Selected")
        self.copy_button.connect("clicked", self.copy_selected)
        buttons.append(self.inspect_button)
        self.inspect_streams_button = Gtk.Button(label="Inspect Streams")
        self.inspect_streams_button.connect("clicked", self.inspect_streams)
        buttons.append(self.inspect_streams_button)
        buttons.append(self.copy_button)
        self.cancel_button = Gtk.Button.new_with_mnemonic("C_ancel")
        self.cancel_button.set_sensitive(False)
        self.cancel_button.connect("clicked", self.cancel_operation)
        buttons.append(self.cancel_button)
        root.append(buttons)
        self.status_label = Gtk.Label(label="Ready", xalign=0)
        self.status_label.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
            ["Operation status", "Current media detection, inspection, or copy status"],
        )
        self.status_label.connect("notify::label", self.on_status_changed)
        root.append(self.status_label)

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
        self.log_adjustment = log_scroll.get_vadjustment()
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
        self.log_timer = GLib.timeout_add(100, self.flush_log)
        self.on_mode_changed()

    def on_status_changed(self, label, _param):
        if hasattr(label, "announce"):
            label.announce(label.get_text(), Gtk.AccessibleAnnouncementPriority.MEDIUM)

    def invalidate_choosers(self, *_args):
        self.chooser_generation += 1

    def on_output_changed(self, *_args):
        self.invalidate_choosers()
        self.mode_outputs[self.output_mode] = self.output_entry.get_text()

    def add_field_label(self, row, text, widget):
        label = Gtk.Label(label=text, use_underline=True, xalign=0)
        label.set_mnemonic_widget(widget)
        row.append(label)

    def add_path_row(self, parent, label, placeholder, callback):
        row = Gtk.Box(spacing=8)
        entry = Gtk.Entry(hexpand=True, placeholder_text=placeholder)
        self.add_field_label(row, f"_{label}", entry)
        row.append(entry)
        button = Gtk.Button(label="Browse")
        button.connect("clicked", callback)
        row.append(button)
        parent.append(row)
        return entry

    def add_source_row(self, parent):
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

    def on_mode_changed(self, button=None):
        if button is not None and not button.get_active():
            return
        self.cancel_detection()
        is_bluray = self.bluray_button.get_active()
        if is_bluray != self.output_mode:
            self.mode_outputs[self.output_mode] = self.output_entry.get_text()
            self.output_mode = is_bluray
            self.output_entry.set_text(self.mode_outputs[is_bluray])
        self.invalidate_choosers()
        self.minlength_row.set_visible(is_bluray)
        self.inspect_streams_button.set_visible(is_bluray)
        self.streams_box.set_visible(is_bluray)
        self.name_row.set_visible(is_bluray)
        self.output_entry.set_placeholder_text("Choose output directory" if is_bluray else "Choose output MKV")
        self.clear_rows()
        self.suggest_name()

    def on_manual_mode(self, *_args):
        if not self.applying_detection:
            self.cancel_detection()
            self.status_label.set_text("Media mode selected manually")

    def on_source_changed(self, *_args):
        self.source_generation += 1
        self.invalidate_choosers()
        self.cancel_detection()
        self.clear_rows()
        self.suggest_name()
        source = self.source_entry.get_text()
        if source:
            self.detecting = True
            self.status_label.set_text("Detecting media type...")
            self.detection_timeout = GLib.timeout_add(
                400, self.start_detection, source, self.detection_generation
            )
        else:
            self.status_label.set_text("Choose a source")

    def cancel_detection(self):
        self.detection_generation += 1
        if self.detection_timeout is not None:
            GLib.source_remove(self.detection_timeout)
            self.detection_timeout = None
        self.detecting = False
        if self.detection_probe is not None:
            self.detection_probe.cancel()
            self.detection_probe = None

    def start_detection(self, source, generation):
        if self.closing or generation != self.detection_generation or source != self.source_entry.get_text():
            return False
        self.detection_timeout = None
        probe = ManagedProcess()
        self.detection_probe = probe

        def worker():
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
            except Exception as error:
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

    def start_worker(self, worker):
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

    def finish_detection(self, source, generation, media, error):
        if self.closing or generation != self.detection_generation or source != self.source_entry.get_text():
            return
        self.detecting = False
        self.detection_probe = None
        if error:
            self.log(f"Media detection failed: {error}")
            self.status_label.set_text("Detection failed - select DVD or Blu-ray manually")
            return
        button = self.bluray_button if media == "bluray" else self.dvd_button
        self.applying_detection = True
        try:
            button.set_active(True)
        finally:
            self.applying_detection = False
        self.status_label.set_text(f"Detected {'Blu-ray' if media == 'bluray' else 'DVD'}")
        self.log(f"Detected {'Blu-ray' if media == 'bluray' else 'DVD'} source.")

    def suggest_name(self, name=None):
        source = self.source_entry.get_text()
        suggestion = filename_stem(name) if name else source_name(source) if source else ""
        if not suggestion and source:
            suggestion = source_name(source)
        if self.name_entry.get_text() in ("", self.suggested_name):
            self.name_entry.set_text(suggestion)
        self.suggested_name = suggestion
        if not self.bluray_button.get_active():
            output = f"{suggestion}.mkv" if suggestion else ""
            if self.output_entry.get_text() in ("", self.suggested_output):
                self.output_entry.set_text(output)
            self.suggested_output = output

    def choose_source_file(self, *_args):
        self.choose_path("Choose ISO or image file", "open", self.source_entry)

    def choose_source_folder(self, *_args):
        self.choose_path("Choose source folder", "select_folder", self.source_entry)

    def choose_output(self, *_args):
        action = "select_folder" if self.bluray_button.get_active() else "save"
        self.choose_path("Choose output", action, self.output_entry)

    def choose_path(self, title, action, entry):
        if self.running or self.closing:
            return
        actions = {
            "open": Gtk.FileChooserAction.OPEN,
            "save": Gtk.FileChooserAction.SAVE,
            "select_folder": Gtk.FileChooserAction.SELECT_FOLDER,
        }
        dialog = Gtk.FileChooserNative.new(
            title, self, actions[action], "_Select", "_Cancel"
        )
        path = entry.get_text()
        if path:
            location = Gio.File.new_for_path(os.path.abspath(path))
            try:
                if action == "save":
                    dialog.set_current_name(os.path.basename(path))
                    initialized = dialog.set_current_folder(location.get_parent())
                elif action == "select_folder":
                    initialized = dialog.set_current_folder(location)
                else:
                    initialized = dialog.set_file(location)
                if not initialized:
                    self.log(f"File chooser could not initialize location: {path}")
            except GLib.Error as error:
                self.log(f"File chooser could not initialize location {path}: {error.message}")
        dialog.connect("response", self.on_native_file_chosen, entry, self.chooser_generation)
        dialog.show()

    def apply_chosen_file(self, selected, entry, generation):
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

    def on_native_file_chosen(self, dialog, response, entry, generation):
        try:
            if response == Gtk.ResponseType.ACCEPT:
                self.apply_chosen_file(dialog.get_file(), entry, generation)
            elif response not in (Gtk.ResponseType.CANCEL, Gtk.ResponseType.DELETE_EVENT):
                self.log(f"File chooser failed (response {response}).")
        except GLib.Error as error:
            self.log(f"File chooser failed: {error.message}")
        finally:
            dialog.destroy()

    def clear_rows(self):
        while child := self.list_box.get_first_child():
            self.list_box.remove(child)
        self.rows = []
        self.clear_stream_rows()
        self.playlist_streams = {}

    def clear_stream_rows(self):
        while child := self.stream_list_box.get_first_child():
            self.stream_list_box.remove(child)
        self.stream_rows = []
        self.stream_playlist = None
        self.streams_label.set_text("Select one playlist and click Inspect Streams")

    def append_log(self, text):
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, text)
        excess = self.log_buffer.get_char_count() - MAX_LOG_CHARS
        if excess > 0:
            self.log_buffer.delete(self.log_buffer.get_start_iter(), self.log_buffer.get_iter_at_offset(excess))
        self.scroll_log_to_end()

    def on_log_scrolled(self, adjustment):
        bottom = adjustment.get_upper() - adjustment.get_page_size()
        self.follow_log = adjustment.get_value() >= bottom - LOG_FOLLOW_THRESHOLD

    def scroll_log_to_end(self, *_args):
        if self.follow_log:
            self.log_adjustment.set_value(
                self.log_adjustment.get_upper() - self.log_adjustment.get_page_size()
            )

    def log(self, text):
        self.queue_log(f"{text}\n")

    def break_long_lines(self, text):
        # Called with log_lock held; log_column carries the open line across chunks.
        pieces = []
        for index, part in enumerate(text.split("\n")):
            if index:
                pieces.append("\n")
                self.log_column = 0
            while self.log_column + len(part) > MAX_LOG_LINE_CHARS:
                room = MAX_LOG_LINE_CHARS - self.log_column
                pieces += [part[:room], "\n"]
                part = part[room:]
                self.log_column = 0
            pieces.append(part)
            self.log_column += len(part)
        return "".join(pieces)

    def queue_log(self, text):
        text = clean_text(text)
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

    def flush_log(self):
        with self.log_lock:
            text = "".join(self.pending_log)
            dropped = self.dropped_log_chars
            self.pending_log.clear()
            self.pending_log_chars = 0
            self.dropped_log_chars = 0
        if dropped:
            text = f"[Activity omitted {dropped} older characters to keep the interface responsive.]\n" + text
            # Start the notice on its own line so it cannot extend an unfinished one.
            end = self.log_buffer.get_end_iter()
            if end.backward_char() and end.get_char() != "\n":
                text = "\n" + text
        if text:
            self.append_log(text)
        self.jobs = [job for job in self.jobs if job.is_alive()]
        return True

    def output_snapshot(self, command):
        return (
            command[-1], self.bluray_button.get_active(), self.name_entry.get_text().strip(),
            [pid for checkbox, pid in self.rows if checkbox.get_active()],
        )

    def check_output_snapshot(self, snapshot, operation):
        try:
            validate_output_paths(*snapshot, operation)
        except (OSError, ValueError) as error:
            self.log(f"Cannot validate output filename: {error}")
            return False
        return True

    def command(self, inspect=False):
        source = self.source_entry.get_text()
        if not source:
            self.log("Choose a source first.")
            return None
        # Keep a relative path starting with a dash from being read as an option.
        if source.startswith("-"):
            source = "./" + source
        if self.bluray_button.get_active():
            command = [SCRIPT_PATH, "--bluray", "--device", source]
            if inspect:
                return command + ["--minlength", str(self.minimum_spin.get_value_as_int()), "--list-playlists"]
            selected = [identifier for checkbox, identifier in self.rows if checkbox.get_active()]
            if not selected:
                self.log("Select at least one playlist.")
                return None
            output = self.output_entry.get_text() or "."
            for identifier in selected:
                command += ["--playlist", identifier]
                # No saved entry copies every stream; an empty list means nothing was chosen.
                if identifier in self.playlist_streams:
                    streams = self.playlist_streams[identifier]
                    if not streams:
                        self.log(f"Select at least one stream for playlist {identifier}.")
                        return None
                    for stream in streams:
                        command += ["--stream-id", f"{identifier}:{stream}"]
            return command + ["--", output]
        if inspect:
            return [SCRIPT_PATH, "--list-streams", "--", source]
        selected = [identifier for checkbox, identifier in self.rows if checkbox.get_active()]
        if not selected:
            self.log("Select at least one stream.")
            return None
        output = self.output_entry.get_text()
        if not output:
            self.log("Choose an output MKV first.")
            return None
        command = [SCRIPT_PATH]
        for identifier in selected:
            command += ["--stream", identifier]
        return command + ["--", source, output]

    def inspect(self, *_args):
        if self.running or self.detecting:
            self.log("Wait for the current operation before inspecting.")
            return
        command = self.command(inspect=True)
        if command:
            self.clear_rows()
            self.run(command, self.load_rows)

    def copy_selected(self, *_args):
        command = self.command()
        if command:
            self.run(command, output_snapshot=self.output_snapshot(command))

    def inspect_streams(self, *_args):
        if self.running or self.detecting:
            self.log("Wait for the current operation before inspecting streams.")
            return
        source = self.source_entry.get_text()
        selected = [identifier for checkbox, identifier in self.rows if checkbox.get_active()]
        if not source:
            self.log("Choose a source first.")
            return
        if source.startswith("-"):
            source = "./" + source
        if len(selected) != 1:
            self.log("Select exactly one playlist to inspect its streams.")
            return
        playlist = selected[0]
        previous = self.playlist_streams.get(playlist)
        if previous is None:
            # Without a saved selection, a failed inspection must not fall back to copying all streams.
            self.playlist_streams[playlist] = []
        command = [SCRIPT_PATH, "--bluray", "--device", source, "--playlist", playlist, "--list-streams"]
        self.run(command, lambda output: self.load_stream_rows(playlist, output, previous, refresh=True))

    def run(self, command, on_success=None, output_snapshot=None):
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
        generation = self.source_generation
        source = self.source_entry.get_text()
        is_bluray = self.bluray_button.get_active()
        self.set_busy(True)
        self.status_label.set_text(
            "Validating output..." if output_snapshot else "Inspecting..." if on_success else "Copying..."
        )

        def worker():
            process_command = command
            output = ""
            success_callback = None
            status = "Failed"
            try:
                # Check again: the source may have been replaced after detection.
                if source and symlinked_source(source):
                    raise RuntimeError(SOURCE_SYMLINK_ERROR)
                if output_snapshot:
                    if not self.check_output_snapshot(output_snapshot, operation):
                        return
                    output_path, bluray_output, name, _playlists = output_snapshot
                    if bluray_output and name and os.path.isdir(output_path):
                        # Insert before the trailing "--" and output path.
                        process_command = command[:-2] + ["--output-name", name] + command[-2:]
                    GLib.idle_add(self.show_copying_status, operation)
                self.log("$ " + " ".join(process_command))
                # Only inspections need stdout; ManagedProcess caps what it collects at 8 MiB.
                result = operation.run(process_command, collect=on_success is not None, log=self.queue_log)
                output = result.stdout
                if result.returncode == 0:
                    success_callback = on_success
                    status = "Completed"
                self.log(f"Exit status: {result.returncode}")
            except OperationCancelled:
                status = "Cancelled"
                self.log("Operation cancelled; subprocesses stopped.")
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                self.log(f"Operation failed: {error}")
            except Exception:
                self.log("Unexpected worker error:\n" + traceback.format_exc().rstrip())
            finally:
                GLib.idle_add(
                    self.finish_run, success_callback, output, generation, source, is_bluray, status
                )

        if not self.start_worker(worker):
            self.finish_run(status="Failed")

    def show_copying_status(self, operation):
        if self.operation is operation and not self.closing and not operation.cancelled.is_set():
            self.status_label.set_text("Copying...")

    def set_busy(self, busy):
        for widget in (
            self.inspect_button, self.copy_button, self.inspect_streams_button,
            self.source_entry.get_parent(), self.output_entry.get_parent(),
            self.name_row, self.minlength_row, self.list_box, self.stream_list_box,
            self.dvd_button, self.bluray_button,
        ):
            widget.set_sensitive(not busy)
        self.cancel_button.set_sensitive(busy)

    def finish_run(self, on_success=None, output="", generation=None, source=None, is_bluray=None, status="Completed"):
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

    def cancel_operation(self, *_args):
        if self.operation is not None:
            self.operation.cancel()
            self.cancel_button.set_sensitive(False)
            self.status_label.set_text("Cancelling...")

    def on_close_request(self, *_args):
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

    def on_close_response(self, dialog, response):
        dialog.destroy()
        self.close_dialog = None
        if response == Gtk.ResponseType.YES:
            self.begin_close()

    def begin_close(self):
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

    def finish_close(self):
        if self.log_timer is None:
            return False
        # Polled every 100 ms until all workers and their subprocesses have stopped.
        if self.running or any(job.is_alive() for job in self.jobs):
            return True
        self.flush_log()
        GLib.source_remove(self.log_timer)
        self.log_timer = None
        self.close_timer = None
        self.destroy()
        return False

    def load_rows(self, output):
        self.clear_rows()
        truncated = False
        if self.bluray_button.get_active():
            for line in output.splitlines():
                fields = line.split("\t", 2)
                if len(fields) == 2 and fields[0] == "# disc-name":
                    self.suggest_name(fields[1])
                elif len(fields) == 3 and PLAYLIST_ID.fullmatch(fields[0]):
                    if len(self.rows) >= MAX_INSPECTION_ROWS:
                        truncated = True
                        break
                    playlist, duration, chapters = fields
                    self.add_row(playlist, clean_label(f"Playlist {playlist}    {duration}    {chapters} chapters"))
            if truncated:
                self.log(f"Inspection listed too many playlists; showing the first {MAX_INSPECTION_ROWS}.")
            if not self.rows:
                self.log("No Blu-ray playlists found in the inspection output. See Activity for details.")
                self.status_label.set_text("No playlists found")
            elif len(self.rows) == 1:
                self.rows[0][0].set_active(True)
                self.inspect_streams()
            else:
                self.log("Select one playlist and click Inspect Streams to list its video, audio, and subtitles.")
        else:
            for line in output.splitlines():
                fields = line.split(maxsplit=1)
                if (
                    len(fields) == 2 and DVD_STREAM_INDEX.fullmatch(fields[0])
                    and fields[1].split()[0] in ("video", "audio", "subtitle", "data", "attachment")
                ):
                    if len(self.rows) >= MAX_INSPECTION_ROWS:
                        truncated = True
                        break
                    stream_index, details = fields
                    self.add_row(stream_index, clean_label(f"Stream {stream_index}  {details}"))
            if truncated:
                self.log(f"Inspection listed too many streams; showing the first {MAX_INSPECTION_ROWS}.")
            if not self.rows:
                self.log("No DVD streams found in the inspection output. See Activity for details.")
                self.status_label.set_text("No streams found")

    def load_stream_rows(self, playlist, output, selected=None, refresh=False):
        streams = []
        seen = set()
        invalid = duplicates = 0
        truncated = False
        for line in output.splitlines():
            if not line.strip() or line == "PID\tTYPE\tCODEC\tLANG\tFORMAT":
                continue
            fields = line.split("\t")
            if (
                len(fields) != 5 or not STREAM_PID.fullmatch(fields[0])
                or int(fields[0], 16) > MAX_STREAM_PID
            ):
                invalid += 1
                continue
            # Normalise so 0x1011 and 0x01011 count as the same stream.
            identifier = f"0x{int(fields[0], 16):04x}"
            if identifier in seen:
                duplicates += 1
                continue
            if len(streams) >= MAX_STREAM_ROWS:
                truncated = True
                break
            seen.add(identifier)
            streams.append((identifier, "  ".join(value for value in fields[1:] if value)))
        if truncated:
            self.log(f"Playlist {playlist} listed too many streams; showing the first {MAX_STREAM_ROWS}.")
        if invalid or duplicates:
            self.log(
                f"Stream inspection ignored {invalid} malformed and {duplicates} duplicate records "
                f"for playlist {playlist}."
            )
        if not streams:
            self.log(f"No streams found for playlist {playlist}. See Activity for details.")
            self.status_label.set_text("Stream inspection failed")
            return
        # Without an earlier choice, a refresh selects every stream; otherwise reuse the saved one.
        if selected is None:
            all_streams = [identifier for identifier, _ in streams]
            selected = all_streams if refresh else self.playlist_streams.get(playlist, all_streams)
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

    def on_stream_toggled(self, *_args):
        if self.stream_playlist is not None:
            self.playlist_streams[self.stream_playlist] = [
                identifier for checkbox, identifier in self.stream_rows if checkbox.get_active()
            ]

    def add_row(self, identifier, label):
        checkbox = Gtk.CheckButton(label=label)
        checkbox.set_margin_top(4)
        checkbox.set_margin_bottom(4)
        self.list_box.append(checkbox)
        self.rows.append((checkbox, identifier))


class DvdToMkvApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.local.dvd_to_mkv")

    def do_activate(self):
        window = self.props.active_window
        if not window:
            window = DvdToMkvWindow(self)
        window.present()

    def request_shutdown(self):
        window = self.props.active_window
        if window:
            if not window.closing:
                window.begin_close()
        else:
            self.quit()
        return True


if __name__ == "__main__":
    application = DvdToMkvApplication()
    # Ctrl+C and SIGTERM go through the normal close path so subprocesses are stopped.
    signal_sources = [
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, application.request_shutdown)
        for sig in (signal.SIGINT, signal.SIGTERM)
    ]
    try:
        application.run()
    finally:
        for source in signal_sources:
            GLib.source_remove(source)
