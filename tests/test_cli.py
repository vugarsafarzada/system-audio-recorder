from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from system_audio_recorder.cli import _prompt_for_recording_name, show_recordings
from system_audio_recorder.library import RecordingEntry


class CliLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("system_audio_recorder.cli.add_recording_name")
    @patch("builtins.input", return_value="Demo track")
    def test_name_is_requested_after_recording(self, input_mock, add_name) -> None:
        del input_mock
        original = self.directory / "timestamp.wav"
        named = self.directory / "timestamp_Demo-track.wav"
        add_name.return_value = named

        result = _prompt_for_recording_name(original)

        self.assertEqual(result, named)
        add_name.assert_called_once_with(original, "Demo track")

    @patch("system_audio_recorder.cli.play_recording")
    @patch("system_audio_recorder.cli.list_recordings")
    @patch("builtins.input", side_effect=["1", ""])
    def test_first_list_item_can_be_played(self, input_mock, listing, play) -> None:
        del input_mock
        newest = self.directory / "newest.wav"
        older = self.directory / "older.wav"
        listing.return_value = [
            RecordingEntry(newest, 200, 1_000, 2.0),
            RecordingEntry(older, 100, 1_000, 1.0),
        ]

        show_recordings(self.directory)

        play.assert_called_once_with(newest)


if __name__ == "__main__":
    unittest.main()
