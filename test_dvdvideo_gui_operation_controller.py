import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import ANY, patch

from dvdvideo.gui_commands import (
    build_bluray_streams_command,
    build_disc_details_command,
)
from dvdvideo.gui_listing import Listing, parse_dvd_streams, parse_playlists
from dvdvideo.gui_operation_controller import (
    DialogHooks,
    ListingHooks,
    OperationController,
    OperationControllerHooks,
    OperationInputs,
    OperationRuntimeHooks,
    OperationUiHooks,
)
from dvdvideo.gui_operations import (
    DvdBatchJob,
    OperationHooks,
    OperationRequest,
    OperationResult,
    OutputSpec,
    VerificationSpec,
    WorkerLifecycle,
    execute_dvd_batch,
    prepare_dvd_batch,
    review_dvd_batch,
)
from dvdvideo.gui_preferences import Profile, TrackInfo
from dvdvideo.gui_selection import SelectionCoordinator
from dvdvideo.gui_support import ManagedProcess


class OperationControllerTest(unittest.TestCase):
    def setUp(self):
        self.source = "-disc"
        self.source_generation = 3
        self.bluray = True
        self.detecting = False
        self.output_format = "auto"
        self.visible_ids = ["0"]
        self.output_path = "/tmp/movie.mkv"
        self.output_name = "Movie"
        self.dvd_title = "1"
        self.statuses: list[str] = []
        self.busy_states: list[bool] = []
        self.messages: list[str] = []
        self.notifications: list[tuple[str, str]] = []
        self.workers = []
        self.selection = SelectionCoordinator()
        self.lifecycle = WorkerLifecycle()
        self.controller = OperationController(
            self.lifecycle,
            self.selection,
            OperationControllerHooks(
                get_inputs=self.inputs,
                listing=ListingHooks(
                    clear_rows=lambda: None,
                    load_rows=lambda _value: None,
                    load_stream_rows=lambda *_args: None,
                    update_stream_format=lambda _playlist: None,
                ),
                dialogs=DialogHooks(
                    show_disc_details=lambda *_args: None,
                    present_m2ts_confirmation=lambda _playlist: None,
                    present_verification_dialog=lambda *_args: None,
                    present_copy_summary=lambda *_args: None,
                ),
                ui=OperationUiHooks(
                    log_inspect_action=lambda: None,
                    set_busy=self.busy_states.append,
                    set_status=self.statuses.append,
                    invalidate_choosers=lambda: None,
                    set_output_path=lambda path: setattr(self, "output_path", path),
                    notify_completion=lambda kind, status: self.notifications.append((kind, status)),
                ),
                runtime=OperationRuntimeHooks(
                    cancel_drive_scan=lambda: None,
                    start_worker=self.start_worker,
                    dispatch_ui=lambda action: action(),
                    queue_log=self.messages.append,
                    log=self.messages.append,
                    exclude_device_source=lambda _source: None,
                ),
            ),
        )

    def inputs(self):
        return OperationInputs(
            "/tmp/dvd-to-mkv.sh", self.source, self.bluray, self.detecting,
            self.source_generation, None, self.output_path, self.output_format,
            self.output_name, 2, tuple(self.visible_ids), self.dvd_title,
        )

    def start_worker(self, worker):
        self.workers.append(worker)
        return True

    def test_command_helpers_normalize_hyphenated_sources(self):
        self.assertEqual(
            build_bluray_streams_command("/script", "-disc", "00851"),
            ["/script", "--bluray", "--device", "./-disc", "--playlist", "00851", "--list-streams"],
        )
        self.assertEqual(
            build_disc_details_command("/script", "-disc", is_bluray=False),
            ["/script", "--disc-details", "--", "-disc", "1"],
        )
        self.assertEqual(
            build_disc_details_command("/script", "-disc", is_bluray=True, playlist="851"),
            ["/script", "--bluray", "--device", "./-disc", "--playlist", "851", "--disc-details"],
        )

    def test_command_helpers_reject_empty_sources(self):
        with self.assertRaisesRegex(ValueError, "Choose a source first"):
            build_bluray_streams_command("/script", "", "851")
        with self.assertRaisesRegex(ValueError, "Choose a source first"):
            build_disc_details_command("/script", "", is_bluray=False)

    def test_selected_dvd_title_is_used_for_inspection_copy_details_preview_and_verification(self):
        self.bluray = False
        self.dvd_title = "3"
        self.visible_ids = ["0"]
        self.assertEqual(self.controller.command(inspect=True)[-2:], ["./-disc", "3"])
        command = self.controller.command()
        self.assertEqual(command[-3:], ["./-disc", self.output_path, "3"])
        self.assertEqual(self.controller.output_snapshot().path, self.output_path)
        with patch.object(self.controller, "start") as start:
            self.controller.inspect_disc_details()
            self.assertEqual(start.call_args.args[0][-2:], ["-disc", "3"])
            self.controller.preview_selected()
            self.assertTrue(start.call_args.kwargs["preview"])
        calls = []
        hooks = self.controller.hooks
        self.controller.hooks = replace(hooks, dialogs=replace(
            hooks.dialogs, present_verification_dialog=lambda *args: calls.append(args),
        ))
        self.controller.show_verify_dialog()
        self.assertEqual(calls[0][2], "3")

    def test_discovery_missing_lsdvd_keeps_title_one(self):
        self.bluray = False
        with patch("dvdvideo.gui_operation_controller.shutil.which", return_value=None):
            self.controller.discover_dvd_titles()
        self.assertIn("title 1 remains available", self.messages[-1])
        self.assertEqual(self.dvd_title, "1")
        self.assertEqual(self.workers, [])

    def test_discovery_runs_supervised_with_bounded_output(self):
        self.bluray = False
        with patch("dvdvideo.gui_operation_controller.shutil.which", return_value="/usr/bin/lsdvd"), \
             patch.object(self.controller, "start") as start:
            self.controller.discover_dvd_titles()
        self.assertEqual(start.call_args.args[0][:3], ["/usr/bin/lsdvd", "-Oj", "-acs"])
        self.assertEqual(start.call_args.kwargs["timeout"], 60)
        self.assertTrue(start.call_args.kwargs["quiet_stdout"])

    def test_bluray_inspection_chains_longest_without_selecting_it(self):
        self.visible_ids = []
        loaded = []
        hooks = self.controller.hooks
        self.controller.hooks = replace(hooks, listing=replace(hooks.listing, load_rows=loaded.append))
        with patch.object(self.controller, "start") as start:
            self.controller.inspect()
            callback = start.call_args.kwargs["on_listing"]
            listing = parse_playlists("1\t00:05:00\t1\n2\t01:30:00\t4\n")
            callback(listing)
            self.assertEqual(loaded, [listing])
            self.assertEqual(self.visible_ids, [])
            self.assertEqual(start.call_count, 2)
            self.assertEqual(start.call_args.args[0][-3:], ["--playlist", "2", "--list-streams"])

    def test_bluray_inspection_without_candidate_does_not_guess(self):
        with patch.object(self.controller, "start") as start:
            self.controller.inspect()
            start.call_args.kwargs["on_listing"](parse_playlists("1\tunknown\t1\n2\tunknown\t1\n"))
            self.assertEqual(start.call_count, 1)

    def test_cancelled_or_stale_bluray_listing_does_not_start_track_inspection(self):
        listing = parse_playlists("1\t01:30:00\t4\n")
        for stale in (False, True):
            with self.subTest(stale=stale):
                self.controller.inspect()
                if stale:
                    self.source_generation += 1
                else:
                    self.lifecycle.operation.cancel()
                with (
                    patch("dvdvideo.gui_operation_controller.execute_operation", return_value=OperationResult(
                        "Completed", listing=listing, deliver_success=True,
                    )),
                    patch.object(self.controller, "inspect_playlist") as inspect_playlist,
                ):
                    self.workers.pop()()
                    inspect_playlist.assert_not_called()
                self.assertFalse(self.lifecycle.running)

    def test_summary_rejects_changed_dvd_title(self):
        response = self.prepare_summary()
        self.dvd_title = "2"
        response(True)
        self.assertFalse(self.lifecycle.running)
        self.assertIn("not started", self.messages[-1])

    def test_dvd_batch_prepares_without_copy_and_confirmation_keeps_it_on_cancel(self):
        self.bluray = False
        responses = []
        hooks = self.controller.hooks
        self.controller.hooks = replace(
            hooks,
            get_inputs=lambda: replace(self.inputs(), profile=Profile(audio_languages=("eng",))),
            dialogs=replace(hooks.dialogs, present_copy_summary=lambda _text, response: responses.append(response)),
        )
        jobs = (DvdBatchJob("2", ("0",), "/tmp/title-02.mkv"), DvdBatchJob("5", ("0",), "/tmp/title-05.mkv"))
        prepared = OperationResult("Completed", "summary", deliver_success=True, batch_jobs=jobs)
        self.controller.prepare_dvd_batch(("2", "5"))
        with patch("dvdvideo.gui_operation_controller.prepare_dvd_batch", return_value=prepared):
            self.workers.pop()()
        self.assertEqual(responses, [])
        self.assertEqual(self.controller.dvd_batch.jobs, jobs)
        self.assertFalse(self.lifecycle.running)
        self.controller.copy_selected()
        with patch("dvdvideo.gui_operation_controller.review_dvd_batch", return_value=prepared):
            self.workers.pop()()
        self.assertEqual(len(responses), 1)
        responses.pop()(False)
        self.assertEqual(self.controller.dvd_batch.jobs, jobs)
        self.assertFalse(self.workers)
        self.controller.copy_selected()
        with patch("dvdvideo.gui_operation_controller.review_dvd_batch", return_value=prepared):
            self.workers.pop()()
        responses.pop()(True)
        with patch("dvdvideo.gui_operation_controller.execute_dvd_batch", return_value=OperationResult(
            "Cancelled", batch_completed=("2",),
        )):
            self.workers.pop()()
        self.assertEqual(self.controller.dvd_batch.jobs, (jobs[1],))

    def test_dvd_batch_confirmation_rejects_changed_settings(self):
        self.bluray = False
        responses = []
        hooks = self.controller.hooks
        self.controller.hooks = replace(
            hooks,
            get_inputs=lambda: replace(self.inputs(), profile=Profile(audio_languages=("eng",))),
            dialogs=replace(hooks.dialogs, present_copy_summary=lambda _text, response: responses.append(response)),
        )
        prepared = OperationResult(
            "Completed", "summary", deliver_success=True,
            batch_jobs=(DvdBatchJob("2", ("0",), "/tmp/title.mkv"),),
        )
        self.controller.prepare_dvd_batch(("2",))
        with patch("dvdvideo.gui_operation_controller.prepare_dvd_batch", return_value=prepared):
            self.workers.pop()()
        self.controller.copy_selected()
        with patch("dvdvideo.gui_operation_controller.review_dvd_batch", return_value=prepared):
            self.workers.pop()()
        self.assertEqual(len(responses), 1)
        self.output_path = "/tmp/changed.mkv"
        responses[0](True)
        self.assertFalse(self.lifecycle.running)
        self.assertIn("not started", self.messages[-1])
        self.assertIsNotNone(self.controller.dvd_batch)

    def test_dvd_batch_ignores_single_title_track_selection_changes(self):
        self.bluray = False
        responses = []
        hooks = self.controller.hooks
        self.controller.hooks = replace(
            hooks,
            get_inputs=lambda: replace(self.inputs(), profile=Profile(audio_languages=("eng",))),
            dialogs=replace(hooks.dialogs, present_copy_summary=lambda _text, response: responses.append(response)),
        )
        prepared = OperationResult(
            "Completed", "summary", deliver_success=True,
            batch_jobs=(DvdBatchJob("2", ("0", "1"), "/tmp/title.mkv"),),
        )
        self.controller.prepare_dvd_batch(("2",))
        with patch("dvdvideo.gui_operation_controller.prepare_dvd_batch", return_value=prepared):
            self.workers.pop()()
        self.visible_ids = ["0", "3"]
        self.controller.copy_selected()
        with patch("dvdvideo.gui_operation_controller.review_dvd_batch", return_value=prepared):
            self.workers.pop()()
        self.visible_ids = ["0"]
        responses.pop()(True)
        with patch("dvdvideo.gui_operation_controller.execute_dvd_batch", return_value=OperationResult(
            "Completed", batch_completed=("2",),
        )) as execute:
            self.workers.pop()()
        self.assertEqual(execute.call_args.args[1], prepared.batch_jobs)
        self.assertIsNone(self.controller.dvd_batch)

    def test_dvd_batch_rejects_profile_change_after_summary(self):
        self.bluray = False
        responses = []
        profile = [Profile(audio_languages=("eng",))]
        hooks = self.controller.hooks
        self.controller.hooks = replace(
            hooks,
            get_inputs=lambda: replace(self.inputs(), profile=profile[0]),
            dialogs=replace(hooks.dialogs, present_copy_summary=lambda _text, response: responses.append(response)),
        )
        prepared = OperationResult(
            "Completed", "summary", deliver_success=True,
            batch_jobs=(DvdBatchJob("2", ("0", "1"), "/tmp/title.mkv"),),
        )
        self.controller.prepare_dvd_batch(("2",))
        with patch("dvdvideo.gui_operation_controller.prepare_dvd_batch", return_value=prepared):
            self.workers.pop()()
        self.controller.copy_selected()
        with patch("dvdvideo.gui_operation_controller.review_dvd_batch", return_value=prepared):
            self.workers.pop()()
        profile[0] = Profile(audio_languages=("jpn",))
        responses.pop()(True)
        self.assertFalse(self.workers)
        self.assertIsNotNone(self.controller.dvd_batch)
        self.assertIn("not started", self.messages[-1])

    def test_failed_or_cancelled_replacement_keeps_batch_and_clear_restores_single_copy(self):
        self.bluray = False
        hooks = self.controller.hooks
        self.controller.hooks = replace(
            hooks, get_inputs=lambda: replace(self.inputs(), profile=Profile(audio_languages=("eng",))),
        )
        jobs = (DvdBatchJob("2", ("0",), "/tmp/title.mkv"),)
        self.controller.prepare_dvd_batch(("2",))
        with patch("dvdvideo.gui_operation_controller.prepare_dvd_batch", return_value=OperationResult(
            "Completed", deliver_success=True, batch_jobs=jobs,
        )):
            self.workers.pop()()
        for status in ("Failed", "Cancelled"):
            self.controller.prepare_dvd_batch(("5",))
            with patch("dvdvideo.gui_operation_controller.prepare_dvd_batch", return_value=OperationResult(status)):
                self.workers.pop()()
            self.assertEqual(self.controller.dvd_batch.jobs, jobs)
        self.output_path = "/tmp/changed.mkv"
        with patch.object(self.controller, "start") as start:
            self.controller.copy_selected()
            start.assert_not_called()
        self.assertIn("no longer matches", self.messages[-1])
        self.controller.clear_dvd_batch()
        with patch.object(self.controller, "start") as start:
            self.controller.copy_selected()
            start.assert_called_once()

    def test_reinspection_gate_survives_start_failure_and_cancellation(self):
        hooks = self.controller.hooks
        self.controller.hooks = replace(
            hooks,
            listing=replace(hooks.listing, clear_rows=self.selection.clear),
            runtime=replace(hooks.runtime, start_worker=lambda _worker: False),
        )
        self.selection.requires_reinspection = True
        self.controller.inspect()
        self.assertTrue(self.selection.requires_reinspection)
        self.assertEqual(self.statuses[-1], "Failed")
        self.controller.hooks = replace(
            self.controller.hooks,
            runtime=replace(self.controller.hooks.runtime, start_worker=self.start_worker),
        )
        self.controller.inspect()
        self.lifecycle.operation.cancel()
        self.controller.finish_run(status="Completed")
        self.assertTrue(self.selection.requires_reinspection)
        self.assertEqual(self.statuses[-1], "Cancelled")

    def test_copy_preflight_builds_stream_inspection_command(self):
        with patch.object(self.controller, "start") as start:
            self.controller.copy_selected()
        start.assert_called_once_with(
            ["/tmp/dvd-to-mkv.sh", "--bluray", "--device", "./-disc",
             "--playlist", "0", "--list-streams"],
            on_listing=ANY, listing_parser=ANY,
        )
        self.assertFalse(self.lifecycle.running)
        self.assertEqual(self.workers, [])

    def test_confirmation_only_continues_for_current_source(self):
        with patch.object(self.controller, "copy_selected") as copy:
            self.controller.confirm_m2ts_response("0", self.source_generation, True)
            copy.assert_called_once_with()
            copy.reset_mock()
            self.controller.confirm_m2ts_response("1", self.source_generation - 1, True)
            copy.assert_not_called()
        self.assertEqual(self.selection.m2ts_confirmed, {"0"})

    def prepare_summary(self):
        self.bluray = False
        self.visible_ids = ["0", "1"]
        self.summaries = []
        hooks = self.controller.hooks
        self.controller.hooks = replace(
            hooks,
            dialogs=replace(
                hooks.dialogs,
                present_copy_summary=lambda text, response: self.summaries.append((text, response)),
            ),
        )
        self.controller.copy_selected()
        with patch(
            "dvdvideo.gui_operation_controller.execute_operation",
            return_value=OperationResult("Completed", "Output: /tmp/movie.mkv", deliver_success=True),
        ) as execute:
            self.workers.pop()()
        self.assertTrue(execute.call_args.args[0].summary_only)
        request = execute.call_args.args[0]
        assert request.output_argument_index is not None
        self.assertEqual(request.command[request.output_argument_index], self.output_path)
        self.assertFalse(self.lifecycle.running)
        self.assertIn("Output: /tmp/movie.mkv", self.summaries[0][0])
        self.assertIn("Selected tracks\n  0", self.summaries[0][0])
        self.assertIn("\n  1", self.summaries[0][0])
        return self.summaries[0][1]

    def test_summary_cancel_preserves_selection_and_does_not_copy(self):
        response = self.prepare_summary()
        response(False)
        self.assertEqual(self.visible_ids, ["0", "1"])
        self.assertEqual(self.workers, [])
        self.assertIn("cancelled before extraction", self.messages[-1])

    def test_dvd_copy_summary_warns_when_no_selected_audio(self):
        self.selection.dvd_track_info = [
            TrackInfo("0", "video", ""),
            TrackInfo("1", "subtitle", "eng"),
            TrackInfo("2", "audio", "jpn"),
        ]
        self.prepare_summary()
        warning = "Warning: no audio track was selected for DVD title 1; the output will have no audio."
        self.assertIn(warning, self.summaries[0][0])
        self.assertIn(warning, self.messages)

    def test_dvd_copy_summary_distinguishes_audio_and_unknown_tracks(self):
        for tracks, expected_warning in (
            ([TrackInfo("0", "video", ""), TrackInfo("1", "audio", "eng")], False),
            ([TrackInfo("0", "video", "")], True),
        ):
            with self.subTest(tracks=tracks):
                self.selection.dvd_track_info = tracks
                self.prepare_summary()
                warning = "Warning: audio selection could not be determined; check whether the output includes audio."
                self.assertEqual(warning in self.summaries[0][0], expected_warning)
                self.assertEqual(warning in self.messages, expected_warning)
                self.messages.clear()

    def test_bluray_summary_warns_per_filtered_playlist_not_original_m2ts(self):
        self.visible_ids = ["5", "6", "7"]
        self.output_path = "/tmp"
        self.selection.playlist_mvc = {"5": False, "6": False, "7": True}
        self.selection.playlist_streams = {"5": ["0x1000"], "6": ["0x1000", "0x1100"]}
        self.selection.playlist_track_info = {
            "5": [TrackInfo("0x1000", "video", ""), TrackInfo("0x1100", "audio", "eng")],
            "6": [TrackInfo("0x1000", "video", ""), TrackInfo("0x1100", "audio", "eng")],
        }
        summaries = []
        hooks = self.controller.hooks
        self.controller.hooks = replace(hooks, dialogs=replace(
            hooks.dialogs, present_copy_summary=lambda text, response: summaries.append(text),
        ))
        self.controller.copy_selected()
        with patch(
            "dvdvideo.gui_operation_controller.execute_operation",
            return_value=OperationResult("Completed", "Output summary", deliver_success=True),
        ):
            self.workers.pop()()
        warning = "Warning: no audio track was selected for playlist 5; the output will have no audio."
        self.assertIn(warning, summaries[0])
        self.assertIn(warning, self.messages)
        self.assertEqual(summaries[0].count("Warning:"), 1)

    def test_summary_accept_starts_copy_exactly_once(self):
        response = self.prepare_summary()
        response(True)
        self.assertTrue(self.lifecycle.running)
        self.assertEqual(len(self.workers), 1)
        with patch(
            "dvdvideo.gui_operation_controller.execute_operation",
            return_value=OperationResult("Completed"),
        ) as execute:
            self.workers.pop()()
        self.assertFalse(execute.call_args.args[0].summary_only)
        request = execute.call_args.args[0]
        assert request.output_argument_index is not None
        self.assertEqual(request.command[request.output_argument_index], self.output_path)
        response(True)
        self.assertEqual(self.workers, [])

    def test_summary_rejects_changed_source_output_and_tracks(self):
        for field, value in (
            ("source_generation", 4), ("source", "different.iso"),
            ("output_name", "Different"), ("output_path", "/tmp/other.mkv"),
            ("visible_ids", ["0"]), ("output_format", "m2ts"),
        ):
            with self.subTest(field=field):
                original = getattr(self, field)
                response = self.prepare_summary()
                setattr(self, field, value)
                response(True)
                self.assertEqual(self.workers, [])
                self.assertIn("settings, or operation changed", self.messages[-1])
                setattr(self, field, original)

    def test_cancelled_summary_preparation_does_not_open_confirmation(self):
        self.bluray = False
        received = []
        hooks = self.controller.hooks
        self.controller.hooks = replace(
            hooks,
            dialogs=replace(hooks.dialogs, present_copy_summary=lambda *args: received.append(args)),
        )
        self.controller.copy_selected()
        self.lifecycle.operation.cancel()
        with patch(
            "dvdvideo.gui_operation_controller.execute_operation",
            return_value=OperationResult("Completed", "Summary", deliver_success=True),
        ):
            self.workers.pop()()
        self.assertEqual(received, [])
        self.assertEqual(self.statuses[-1], "Cancelled")
        self.assertEqual(self.workers, [])

    def test_start_dispatches_result_and_restores_idle_state(self):
        listing = Listing(rows=[("0", "Playlist")])
        received = []
        self.controller.start(["inspect"], on_listing=received.append, listing_parser=lambda _text: listing)
        self.assertTrue(self.lifecycle.running)
        self.assertEqual(self.busy_states, [True])
        with patch(
            "dvdvideo.gui_operation_controller.execute_operation",
            return_value=OperationResult("Completed", "text", listing, True),
        ) as execute:
            self.workers.pop()()

        request = execute.call_args.args[0]
        self.assertTrue(request.collect)
        self.assertIs(request.listing_parser("unused"), listing)
        self.assertEqual(received, [listing])
        self.assertEqual(self.busy_states, [True, False])
        self.assertEqual(self.statuses, ["Inspecting...", "Completed"])
        self.assertFalse(self.lifecycle.running)

    def test_finish_ignores_stale_source_results(self):
        received = []
        self.controller.finish_run(
            received.append,
            "old result",
            self.source_generation - 1,
            self.source,
            self.bluray,
        )
        self.assertEqual(received, [])
        self.assertIn(
            "Ignored stale operation results because the source or media mode changed.",
            self.messages,
        )

    def test_completion_notifications_only_for_copy_and_verification_outcomes(self):
        from pathlib import Path

        output = OutputSpec("/tmp/movie.mkv", False, "", (), ())
        verification = VerificationSpec("source", Path("/tmp/movie.mkv"), False, "1", ("0",))
        for kind, start in (
            ("Copy", partial(self.controller.start, ["operation"], output_snapshot=output)),
            ("Verification", partial(self.controller.start, ["operation"], verification=verification)),
            (None, partial(self.controller.start, ["operation"], output_snapshot=output, summary_only=True)),
            (None, partial(self.controller.start, ["operation"], on_success=lambda _text: None)),
            (None, partial(self.controller.start, ["operation"], preview=True)),
        ):
            for status in ("Completed", "Verified", "Failed", "Cancelled"):
                with self.subTest(kind=kind, status=status):
                    self.notifications.clear()
                    start()
                    with patch(
                        "dvdvideo.gui_operation_controller.execute_operation",
                        return_value=OperationResult(status),
                    ):
                        self.workers.pop()()
                    expected = [(kind, status)] if kind and status != "Cancelled" else []
                    self.assertEqual(self.notifications, expected)

    def test_notifications_suppressed_for_stale_results_or_shutdown(self):
        for closing in (False, True):
            with self.subTest(closing=closing):
                self.lifecycle.closing = closing
                self.controller.finish_run(
                    generation=self.source_generation - 1, source=self.source,
                    is_bluray=self.bluray, notification_kind="Copy",
                )
                self.assertEqual(self.notifications, [])

    def test_notification_respects_late_cancellation_result(self):
        for copy in (False, True):
            with self.subTest(copy=copy):
                self.notifications.clear()
                self.lifecycle.begin().cancel()
                self.controller.finish_run(
                    status="Completed" if copy else "Verified",
                    completed_copy=copy, notification_kind="Copy" if copy else "Verification",
                )
                self.assertEqual(self.notifications, [("Copy", "Completed")] if copy else [])

    def test_copy_worker_start_failure_notifies_failure(self):
        hooks = self.controller.hooks
        self.controller.hooks = replace(
            hooks, runtime=replace(hooks.runtime, start_worker=lambda _worker: False),
        )
        self.controller.start(
            ["copy"], output_snapshot=OutputSpec("/tmp/movie.mkv", False, "", (), ()),
        )
        self.assertEqual(self.notifications, [("Copy", "Failed")])


class DvdBatchTest(unittest.TestCase):
    def setUp(self):
        self.messages = []
        self.operation = ManagedProcess()
        self.hooks = OperationHooks(
            log=self.messages.append, queue_log=self.messages.append,
            exclude_device=lambda _path: None, preview_started=lambda: None,
            copy_started=lambda: None, output_changed=lambda _path: None,
        )
        self.request = OperationRequest(
            command=(), script_path="/script", source="/disc.iso",
            is_bluray=False, collect=False,
            output=OutputSpec("/tmp/show.mkv", False, "", (), ()),
        )
        self.profile = Profile(audio_languages=("jpn",), subtitle_languages=("eng",))

    @staticmethod
    def listing(extra_audio):
        rows = [
            "INDEX\tTYPE\tCODEC\tPROFILE\tCH\tLAYOUT\tLANG\tTITLE",
            "0\tvideo\tmpeg2video\t\t\t\t\t",
            "1\taudio\tac3\t\t2\tstereo\tjpn\t",
        ]
        if extra_audio:
            rows.append("2\taudio\tac3\t\t2\tstereo\teng\t")
        rows.append(f"{3 if extra_audio else 2}\tsubtitle\tdvd_subtitle\t\t\t\teng\t")
        return parse_dvd_streams("\n".join(rows))

    def test_preparation_inspects_each_title_and_uses_its_own_stream_ids(self):
        requests = []

        def execute(request, _operation, _hooks):
            requests.append(request)
            if request.output is None:
                listing = self.listing(request.dvd_title == "5")
                return OperationResult("Completed", listing=listing, deliver_success=True)
            return OperationResult("Completed", f"Summary {request.output.path}", deliver_success=True)

        with patch("dvdvideo.gui_operations.execute_operation", side_effect=execute):
            result = prepare_dvd_batch(self.request, ("2", "5"), self.profile, self.operation, self.hooks)
        self.assertEqual(result.status, "Completed")
        self.assertEqual(
            [(job.title, job.streams, job.output) for job in result.batch_jobs],
            [("2", ("0", "1", "2"), "/tmp/show-title-02.mkv"),
             ("5", ("0", "1", "3"), "/tmp/show-title-05.mkv")],
        )
        self.assertEqual([request.dvd_title for request in requests], ["2", "2", "5", "5"])
        self.assertIn("show-title-05.mkv", result.output)

    def test_missing_requested_audio_warns_in_prepared_and_reviewed_summaries(self):
        def execute(request, _operation, _hooks):
            if request.output is None:
                return OperationResult("Completed", listing=self.listing(False), deliver_success=True)
            return OperationResult("Completed", "preflight", deliver_success=True)

        with patch("dvdvideo.gui_operations.execute_operation", side_effect=execute):
            prepared = prepare_dvd_batch(
                self.request, ("2",), Profile(audio_languages=("fra",)), self.operation, self.hooks,
            )
            reviewed = review_dvd_batch(
                self.request, prepared.batch_jobs, (), self.operation, self.hooks,
            )
            matched = prepare_dvd_batch(
                self.request, ("2",), Profile(audio_languages=("jpn",)), self.operation, self.hooks,
            )
            subtitles_only = prepare_dvd_batch(
                self.request, ("2",), Profile(subtitle_languages=("eng",)), self.operation, self.hooks,
            )
            subtitles_review = review_dvd_batch(
                self.request, subtitles_only.batch_jobs, (), self.operation, self.hooks,
            )
        warning = "Warning: no audio track matched the profile for DVD title 2; the output will have no audio."
        no_audio = "Warning: no audio track was selected for DVD title 2; the output will have no audio."
        self.assertEqual(prepared.batch_jobs[0].streams, ("0",))
        self.assertIn(warning, self.messages)
        self.assertIn(warning, prepared.output)
        self.assertIn(warning, reviewed.output)
        self.assertNotIn(warning, matched.output)
        self.assertNotIn(warning, subtitles_only.output)
        self.assertIn(no_audio, self.messages)
        self.assertIn(no_audio, subtitles_only.output)
        self.assertIn(no_audio, subtitles_review.output)
        self.assertNotIn(no_audio, matched.output)

    def test_batch_continues_after_failure_and_reports_skipped_titles(self):
        jobs = (DvdBatchJob("2", ("0",), "/tmp/a.mkv"), DvdBatchJob("5", ("0",), "/tmp/b.mkv"))
        progress = []
        hooks = replace(self.hooks, batch_title_started=lambda *args: progress.append(args))
        with patch("dvdvideo.gui_operations.execute_operation", side_effect=[
            OperationResult("Failed"), OperationResult("Completed"),
        ]) as execute:
            result = execute_dvd_batch(self.request, jobs, ("1",), self.operation, hooks)
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(progress, [("2", 1, 2), ("5", 2, 2)])
        self.assertEqual(result.status, "Failed")
        self.assertEqual(result.batch_failures, ("1", "2"))
        self.assertTrue(any("completed titles: 5" in message for message in self.messages))

    def test_cancel_stops_remaining_jobs(self):
        jobs = (DvdBatchJob("2", ("0",), "/tmp/a.mkv"), DvdBatchJob("5", ("0",), "/tmp/b.mkv"))

        def cancel(*_args):
            self.operation.cancel()
            return OperationResult("Completed")

        with patch("dvdvideo.gui_operations.execute_operation", side_effect=cancel) as execute:
            result = execute_dvd_batch(self.request, jobs, (), self.operation, self.hooks)
        self.assertEqual(result.status, "Cancelled")
        self.assertEqual(execute.call_count, 1)

    def test_late_cancel_records_successful_copy_and_stops_remaining_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "disc.iso"
            source.write_bytes(b"synthetic disc")
            script = root / "copier"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "source, output, title = sys.argv[sys.argv.index('--') + 1:]\n"
                "pathlib.Path(output).write_text('synthetic title ' + title)\n"
            )
            script.chmod(0o755)
            jobs = tuple(
                DvdBatchJob(title, ("0",), str(root / f"title-{title}.mkv"))
                for title in ("2", "5")
            )
            request = replace(self.request, source=str(source), script_path=str(script))
            original_wait = subprocess.Popen.wait

            def cancel_after_exit(process, *args, **kwargs):
                status = original_wait(process, *args, **kwargs)
                if process.args[0] == str(script):
                    self.operation.cancel()
                return status

            for count in (1, 2):
                with self.subTest(count=count):
                    self.operation = ManagedProcess()
                    with patch.object(subprocess.Popen, "wait", cancel_after_exit):
                        result = execute_dvd_batch(request, jobs[:count], (), self.operation, self.hooks)

                    self.assertEqual(result.status, "Completed" if count == 1 else "Cancelled")
                    self.assertEqual(result.batch_completed, ("2",))
                    self.assertEqual(Path(jobs[0].output).read_text(), "synthetic title 2")
                    self.assertFalse(Path(jobs[1].output).exists())
                    retry = review_dvd_batch(request, jobs, (), ManagedProcess(), self.hooks)
                    self.assertEqual(retry.batch_jobs, (jobs[1],))
                    self.assertEqual(Path(jobs[0].output).read_text(), "synthetic title 2")
                    Path(jobs[0].output).unlink()

    def test_cancel_during_published_dvd_cleanup_records_only_that_title(self):
        from gui_batch_test_support import SCRIPT, CopyFixture

        fixture = CopyFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.add_recording_copy_tools()
        output = fixture.root / "title-02.mkv"
        marker = fixture.root / "cleanup-started"
        real_rm = shutil.which("rm")
        self.assertIsNotNone(real_rm)
        fixture.add_tool("rm", f"""#!{sys.executable}
import os, pathlib, sys, time
output = pathlib.Path({str(output)!r})
marker = pathlib.Path({str(marker)!r})
if sys.argv[1:2] == ['-rf'] and output.exists() and not marker.exists():
    marker.touch()
    print('CLEANUP_READY', flush=True)
    time.sleep(60)
os.execv({real_rm!r}, sys.argv)
""")
        jobs = (
            DvdBatchJob("2", ("0",), str(output)),
            DvdBatchJob("3", ("0",), str(fixture.root / "title-03.mkv")),
        )
        request = replace(self.request, source=str(fixture.device), script_path=str(SCRIPT))

        def cancel(text):
            self.messages.append(text)
            if "CLEANUP_READY" in text:
                self.operation.cancel()

        hooks = replace(self.hooks, queue_log=cancel)
        with (
            patch.dict(os.environ, fixture.env, clear=True),
            patch("dvdvideo.gui_support.MAX_DIRECT_ARGUMENT_BYTES", 1),
        ):
            result = execute_dvd_batch(request, jobs, (), self.operation, hooks)
            review = review_dvd_batch(request, (jobs[1],), (), ManagedProcess(), self.hooks)

        self.assertTrue(marker.exists())
        self.assertEqual(result.status, "Cancelled")
        self.assertEqual(result.batch_completed, ("2",))
        self.assertEqual(output.read_bytes(), b"mkv")
        self.assertEqual(review.batch_jobs, (jobs[1],))
        self.assertFalse(Path(jobs[1].output).exists())
        self.assertTrue(any("published" in message.lower() for message in self.messages))

    def test_empty_profile_is_rejected(self):
        result = prepare_dvd_batch(self.request, ("2",), Profile(), self.operation, self.hooks)
        self.assertEqual(result.status, "Failed")
        self.assertIn("profile", self.messages[-1])

    def test_batch_preflight_preserves_existing_output_and_continues_after_copy_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "disc.iso"
            source.write_bytes(b"synthetic disc")
            script = root / "copier"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "args = sys.argv[1:]\n"
                "if '--list-streams-tsv' in args:\n"
                "    print('0\\tvideo\\tmpeg2video\\t\\t\\t\\t\\t')\n"
                "    print('1\\taudio\\tac3\\t\\t2\\tstereo\\tjpn\\t')\n"
                "    sys.exit(0)\n"
                "source, output, title = args[args.index('--') + 1:]\n"
                "if title == '3': sys.exit(1)\n"
                "pathlib.Path(output).write_text('synthetic title ' + title)\n"
            )
            script.chmod(0o755)
            existing = root / "show-title-02.mkv"
            existing.write_text("existing output")
            request = replace(
                self.request, source=str(source), script_path=str(script),
                output=OutputSpec(str(root / "show.mkv"), False, "", (), ()),
            )
            prepared = prepare_dvd_batch(request, ("2", "3", "5"), self.profile, self.operation, self.hooks)
            self.assertEqual(prepared.batch_failures, ("2",))
            self.assertEqual([job.title for job in prepared.batch_jobs], ["3", "5"])
            late_existing = root / "show-title-03.mkv"
            late_existing.write_text("created after preparation")
            reviewed = review_dvd_batch(
                request, prepared.batch_jobs, prepared.batch_failures, self.operation, self.hooks,
            )
            self.assertEqual([job.title for job in reviewed.batch_jobs], ["5"])
            self.assertIn("Titles skipped during preparation: 2", reviewed.output)
            self.assertIn("Titles skipped during copy preflight (kept in batch): 3", reviewed.output)
            self.assertEqual(late_existing.read_text(), "created after preparation")
            late_existing.unlink()
            result = execute_dvd_batch(
                request, prepared.batch_jobs, prepared.batch_failures, self.operation, self.hooks,
            )
            self.assertEqual(result.status, "Failed")
            self.assertEqual(result.batch_failures, ("2", "3"))
            self.assertEqual(existing.read_text(), "existing output")
            self.assertEqual((root / "show-title-05.mkv").read_text(), "synthetic title 5")


if __name__ == "__main__":
    unittest.main()
