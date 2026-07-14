import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import transcribe


class TimestampTests(unittest.TestCase):
    def test_offset_timestamps_across_an_hour(self):
        source = "[00:02] Speaker 1: First\n[09:58] Speaker 2: Second"
        self.assertEqual(
            transcribe.offset_timestamps(source, 3600),
            "[1:00:02] Speaker 1: First\n[1:09:58] Speaker 2: Second",
        )

    def test_plain_text_removes_timestamps_only(self):
        source = "[00:02] Speaker 1: Keep the label"
        self.assertEqual(transcribe.strip_timestamps(source), "Speaker 1: Keep the label")

    def test_srt_uses_next_cue_as_end(self):
        source = "[00:00] Speaker 1: One\n[00:08] Speaker 2: Two"
        result = transcribe.transcript_to_srt(source)
        self.assertIn("00:00:00,000 --> 00:00:08,000", result)
        self.assertIn("00:00:08,000 --> 00:00:11,000", result)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        transcribe._requests_used = 0

    def test_single_chunk_is_ok_when_local_capacity_remains(self):
        self.assertEqual(transcribe.estimate_rate_limit_impact(599), (1, "ok"))

    def test_single_chunk_is_blocked_when_local_capacity_is_empty(self):
        transcribe._requests_used = transcribe.FREE_KEY_SESSION_GUARD
        self.assertEqual(transcribe.estimate_rate_limit_impact(60), (1, "blocked"))

    def test_long_audio_warns_when_local_capacity_remains(self):
        self.assertEqual(transcribe.estimate_rate_limit_impact(1500), (3, "warn"))

    def test_long_audio_blocks_when_local_capacity_is_insufficient(self):
        transcribe._requests_used = 19
        self.assertEqual(transcribe.estimate_rate_limit_impact(1200), (2, "blocked"))

    def test_blocked_start_never_enters_provider_worker(self):
        transcribe._requests_used = transcribe.FREE_KEY_SESSION_GUARD

        class FakeApp:
            paid_key = False
            audio_duration = 60

            def __init__(self):
                self.notifications = []
                self.worker_started = False

            def notify(self, message, **kwargs):
                self.notifications.append((message, kwargs))

            def _switch_to(self, _state):
                self.fail("processing screen must not open")

            def _run_transcription(self):
                self.worker_started = True

            def fail(self, message):
                raise AssertionError(message)

        app = FakeApp()
        transcribe.TranscribeApp._start_transcription(app)
        self.assertFalse(app.worker_started)
        self.assertIn("Held before provider work", app.notifications[0][0])


class ChunkWorkflowTests(unittest.TestCase):
    def setUp(self):
        transcribe._requests_used = 0

    def _client(self):
        deleted = []
        return SimpleNamespace(files=SimpleNamespace(delete=lambda name: deleted.append(name)))

    @patch.object(transcribe, "gemini_transcribe")
    @patch.object(transcribe, "upload_and_wait")
    def test_upload_failure_is_returned_with_part_and_phase(self, upload, generate):
        upload.side_effect = lambda _client, path: (
            (_ for _ in ()).throw(RuntimeError("upload unavailable"))
            if path == "part-2.mp3"
            else SimpleNamespace(name=path)
        )
        generate.return_value = "[00:00] Speaker 1: Complete first part"

        text, errors, quota_hit = transcribe.do_transcribe_chunked(
            self._client(), "model", ["part-1.mp3", "part-2.mp3"], sequential=True
        )

        self.assertIn("Complete first part", text)
        self.assertFalse(quota_hit)
        self.assertEqual(errors[0].index, 1)
        self.assertEqual(errors[0].phase, "upload")
        self.assertIn("upload unavailable", errors[0].message)

    @patch.object(transcribe, "gemini_transcribe")
    @patch.object(transcribe, "upload_and_wait")
    def test_transcription_failure_stays_out_of_merged_text(self, upload, generate):
        upload.side_effect = lambda _client, path: SimpleNamespace(name=path)

        def generate_part(_client, _model, uploaded, _ctx, on_retry=None):
            del on_retry
            if uploaded.name == "part-2.mp3":
                raise RuntimeError("provider unavailable")
            return "[00:00] Speaker 1: Kept text"

        generate.side_effect = generate_part
        text, errors, quota_hit = transcribe.do_transcribe_chunked(
            self._client(), "model", ["part-1.mp3", "part-2.mp3"], sequential=True
        )

        self.assertEqual(text, "[00:00] Speaker 1: Kept text")
        self.assertFalse(quota_hit)
        self.assertEqual([(issue.index, issue.phase) for issue in errors], [(1, "transcription")])
        self.assertNotIn("provider unavailable", text)

    @patch.object(transcribe, "gemini_transcribe")
    @patch.object(transcribe, "upload_and_wait")
    def test_quota_exhaustion_marks_current_and_skipped_parts(self, upload, generate):
        upload.side_effect = lambda _client, path: SimpleNamespace(name=path)
        generate.side_effect = transcribe.DailyQuotaExhausted("provider quota exhausted")

        text, errors, quota_hit = transcribe.do_transcribe_chunked(
            self._client(), "model", ["part-1.mp3", "part-2.mp3"], sequential=True
        )

        self.assertEqual(text, "")
        self.assertTrue(quota_hit)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(
            [(issue.index, issue.phase) for issue in errors],
            [(0, "quota"), (1, "quota")],
        )

    @patch.object(transcribe, "upload_and_wait")
    def test_paid_mode_never_exceeds_five_transcription_workers(self, upload):
        upload.side_effect = lambda _client, path: SimpleNamespace(name=path)
        active = 0
        maximum = 0
        lock = threading.Lock()

        def generate(_client, _model, uploaded, _ctx, on_retry=None):
            nonlocal active, maximum
            del on_retry
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return f"[00:00] Speaker 1: {uploaded.name}"

        with patch.object(transcribe, "gemini_transcribe", side_effect=generate):
            text, errors, quota_hit = transcribe.do_transcribe_chunked(
                self._client(),
                "model",
                [f"part-{index}.mp3" for index in range(7)],
                sequential=False,
            )

        self.assertFalse(errors)
        self.assertFalse(quota_hit)
        self.assertGreater(maximum, 1)
        self.assertLessEqual(maximum, transcribe.MAX_CONCURRENT_TRANSCRIBE)
        self.assertEqual(text.count("Speaker 1"), 7)


class AppOrchestrationTests(unittest.TestCase):
    @patch.object(transcribe.genai, "Client")
    @patch.object(transcribe, "do_transcribe_single")
    @patch.object(transcribe, "split_audio")
    def test_short_file_success_reaches_speaker_review(self, split, single, client):
        split.return_value = (None, 24.0)
        single.return_value = "[00:00] Speaker 1: Short file succeeds"
        client.return_value = SimpleNamespace()

        class FakeApp:
            api_key = "test-key"
            audio_path = "/tmp/short.mp3"
            selected_model = "gemini-2.5-flash"
            paid_key = False
            raw_transcript = ""
            run_errors = []
            speaker_list = []

            def __init__(self):
                self.statuses = []
                self.progress = []
                self.shown = None
                self.error = None

            def call_from_thread(self, callback, *args, **kwargs):
                return callback(*args, **kwargs)

            def _set_status(self, message):
                self.statuses.append(message)

            def _set_progress(self, *args):
                self.progress.append(args)

            def notify(self, *_args, **_kwargs):
                pass

            def _show_speakers(self):
                self.shown = "speakers"

            def _show_result(self):
                self.shown = "result"

            def _on_error(self, message):
                self.error = message

        app = FakeApp()
        transcribe.TranscribeApp._execute_transcription(app)

        self.assertIsNone(app.error)
        self.assertEqual(app.shown, "speakers")
        self.assertEqual(app.run_errors, [])
        self.assertIn("Short file succeeds", app.raw_transcript)
        self.assertIn("Transcription complete", app.statuses)


class SpeakerTests(unittest.TestCase):
    def test_speakers_are_sorted_numerically(self):
        text = "Speaker 10: Later\nSpeaker 2: Middle\nSpeaker 1: First"
        self.assertEqual(
            transcribe.find_speakers(text),
            ["Speaker 1", "Speaker 2", "Speaker 10"],
        )


if __name__ == "__main__":
    unittest.main()
