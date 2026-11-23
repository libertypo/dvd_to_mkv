"""GTK-independent coordination for source, drive, and chooser workflows."""

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
from dataclasses import dataclass

from dvdvideo.gui_preferences import PreferencesStore, reusable_source
from dvdvideo.gui_source_workflow import SourceWorkflow, detect_source, scan_drives
from dvdvideo.gui_support import ManagedProcess
from dvdvideo.optical_drives import OpticalDrive


@dataclass(frozen=True)
class SourceUiHooks:
    get_source: Callable[[], str]
    set_source: Callable[[str], None]
    invalidate_choosers: Callable[[], None]
    prepare_source: Callable[[str], None]
    set_status: Callable[[str], None]
    apply_detected_media: Callable[[str], None]
    exclude_device: Callable[[str], None]
    confirm_non_device: Callable[[str], None] = lambda _source: None
    update_title_sensitivity: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class DriveUiHooks:
    render_drives: Callable[[list[OpticalDrive], bool, str | None], None]
    should_clear_device_selections: Callable[[str], bool]
    clear_device_selections: Callable[[], None]
    close_drive_menu: Callable[[], None]


@dataclass(frozen=True)
class SourceRuntimeHooks:
    is_closing: Callable[[], bool]
    is_running: Callable[[], bool]
    schedule_detection: Callable[[str, int], int]
    remove_timeout: Callable[[int], object]
    start_worker: Callable[[Callable[[], None]], bool]
    dispatch_detection: Callable[[str, int, str | None, str | None, bool | None], object]
    dispatch_exclude_device: Callable[[str, int], object]
    dispatch_drive_scan: Callable[[ManagedProcess, list[OpticalDrive], str | None], object]
    dispatch_detection_done: Callable[[ManagedProcess], object]


@dataclass(frozen=True)
class SourceControllerHooks:
    ui: SourceUiHooks
    drives: DriveUiHooks
    runtime: SourceRuntimeHooks
    log: Callable[[str], None]


class SourceController:
    def __init__(self, workflow: SourceWorkflow, hooks: SourceControllerHooks) -> None:
        self.workflow = workflow
        self.hooks = hooks

    def source_changed(self, source: str) -> None:
        self.workflow.source_generation += 1
        self.hooks.ui.invalidate_choosers()
        self.cancel_detection()
        self.hooks.ui.prepare_source(source)
        if source:
            self._schedule_detection(source)
        else:
            self.hooks.ui.set_status("Choose a source")

    def _schedule_detection(self, source: str) -> None:
        self.workflow.detecting = True
        self.hooks.ui.update_title_sensitivity()
        self.hooks.ui.set_status("Detecting media type...")
        self.workflow.schedule_detection(source, self.hooks.runtime.schedule_detection)

    def cancel_detection(self) -> None:
        self.workflow.cancel_detection(self.hooks.runtime.remove_timeout)
        self.hooks.ui.update_title_sensitivity()

    def start_detection(self, source: str, generation: int) -> bool:
        if self.hooks.runtime.is_closing() or generation != self.workflow.detection_generation:
            return False
        probe = self.workflow.begin_detection(
            source, generation, self.hooks.ui.get_source(), closing=self.hooks.runtime.is_closing(),
        )
        if probe is None:
            return False

        def deliver(
            path: str, token: int, media: str | None, error: str | None, device: bool | None,
        ) -> None:
            self.hooks.runtime.dispatch_detection(path, token, media, error, device)

        def exclude_device(path: str) -> None:
            self.hooks.runtime.dispatch_exclude_device(path, generation)

        def worker() -> None:
            try:
                detect_source(
                    source, generation, probe, deliver=deliver,
                    exclude_device=exclude_device, log=self.hooks.log, isolate=True,
                )
            finally:
                self.hooks.runtime.dispatch_detection_done(probe)

        if not self.hooks.runtime.start_worker(worker):
            self.finish_detection(
                source, generation, None,
                "Could not start background worker. See Activity for details.", None,
            )
            self.detection_finished(probe)
        return False

    def detection_finished(self, operation: ManagedProcess) -> None:
        pending = self.workflow.detection_finished(operation)
        if pending is not None:
            self.start_detection(*pending)

    def exclude_device_source(self, source: str, generation: int | None = None) -> None:
        if (
            self.hooks.runtime.is_closing() or source != self.hooks.ui.get_source()
            or generation is not None and generation != self.workflow.detection_generation
        ):
            return
        self.workflow.device_sources.add(PreferencesStore.source_key(source))
        self.hooks.ui.exclude_device(source)
        if reusable_source(source):
            self.hooks.log(f"Block device {source}: saved disc selections will not be restored.")

    def confirm_non_device_source(self, source: str, generation: int | None = None) -> None:
        if (
            self.hooks.runtime.is_closing() or source != self.hooks.ui.get_source()
            or generation is not None and generation != self.workflow.detection_generation
        ):
            return
        self.hooks.ui.confirm_non_device(source)
        self.workflow.device_sources.discard(PreferencesStore.source_key(source))

    def finish_detection(
        self, source: str, generation: int, media: str | None, error: str | None,
        device: bool | None,
    ) -> None:
        if self.hooks.runtime.is_closing() or generation != self.workflow.detection_generation:
            return
        if not self.workflow.finish_detection(
            source, generation, self.hooks.ui.get_source(), closing=self.hooks.runtime.is_closing(),
        ):
            return
        self.hooks.ui.update_title_sensitivity()
        if error:
            self.hooks.log(f"Media detection failed: {error}")
            self.hooks.ui.set_status("Detection failed - select DVD or Blu-ray manually")
            return
        if device is False:
            self.confirm_non_device_source(source, generation)
        self.workflow.applying_detection = True
        try:
            self.hooks.ui.apply_detected_media(media or "dvd")
        finally:
            self.workflow.applying_detection = False
        if media == "bluray":
            self.hooks.ui.set_status("Detected Blu-ray")
        else:
            self.hooks.ui.set_status("Detected DVD")
        self.hooks.log(f"Detected {'Blu-ray' if media == 'bluray' else 'DVD'} source.")

    def scan_drives(self) -> None:
        if (
            self.hooks.runtime.is_closing() or self.hooks.runtime.is_running()
            or self.workflow.drive_scan is not None
        ):
            return
        source = self.hooks.ui.get_source()
        if source and self.hooks.drives.should_clear_device_selections(source):
            self.hooks.drives.clear_device_selections()
        if source and self.workflow.detecting:
            self.cancel_detection()
            self._schedule_detection(source)
        operation = self.workflow.begin_drive_scan()
        self.hooks.drives.render_drives([], True, None)

        def worker() -> None:
            drives: list[OpticalDrive] = []
            error = None
            try:
                drives, error = scan_drives(operation, isolate=True, log=self.hooks.log)
            finally:
                self.hooks.runtime.dispatch_drive_scan(operation, drives, error)

        if not self.hooks.runtime.start_worker(worker):
            self.finish_drive_scan(operation, [], "Could not start drive discovery.")

    def finish_drive_scan(
        self, operation: ManagedProcess, drives: list[OpticalDrive], error: str | None,
    ) -> None:
        discarded = self.workflow.drive_scan is operation and self.workflow.drive_scan_discarded
        if not self.workflow.finish_drive_scan(operation, closing=self.hooks.runtime.is_closing()):
            if discarded and not self.hooks.runtime.is_closing():
                self.hooks.drives.render_drives([], False, None)
            return
        if error:
            self.hooks.log(error)
        for drive in drives:
            if drive.media is None:
                self.hooks.log(f"Optical drive {drive.path}: {drive.status}")
        self.hooks.drives.render_drives(drives, False, error)

    def select_drive(self, drive: OpticalDrive) -> None:
        if self.hooks.runtime.is_closing() or self.hooks.runtime.is_running() or drive.media is None:
            return
        self.hooks.drives.close_drive_menu()
        if self.hooks.ui.get_source() == drive.path:
            self.source_changed(drive.path)
        else:
            self.hooks.ui.set_source(drive.path)
        self.hooks.log(f"Selected optical drive {drive.path}. Refresh drives after changing discs.")

    def apply_chosen_file(
        self,
        has_selection: bool,
        path: str | None,
        generation: int,
        set_path: Callable[[str], None],
    ) -> None:
        if (
            self.hooks.runtime.is_running() or self.hooks.runtime.is_closing()
            or generation != self.workflow.chooser_generation
        ):
            self.hooks.log("Ignored file selection because the source or operation changed.")
            return
        if not has_selection:
            self.hooks.log("File chooser did not return a selection.")
        elif path:
            try:
                path.encode("utf-8")
            except UnicodeEncodeError:
                self.hooks.log(
                    "The selected path is not valid UTF-8 and cannot be displayed. "
                    "Rename the file or folder to use a UTF-8 name, then select it again."
                )
                self.hooks.ui.set_status("File selection failed - see Activity")
                return
            set_path(path)
        else:
            self.hooks.log("The selected location is not a local path.")
