from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from system_audio_recorder.native import (
    HELPER_SOURCE,
    NativeCaptureError,
    NativeCaptureSession,
    ensure_native_helper,
)


class NativeContractTests(unittest.TestCase):
    def test_swift_helper_builds(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("ScreenCaptureKit is available only on macOS")
        helper = ensure_native_helper()
        self.assertTrue(helper.is_file())
        self.assertTrue(os.access(helper, os.X_OK))

    def test_source_disables_microphone_and_registers_only_audio_output(self) -> None:
        source = HELPER_SOURCE.read_text(encoding="utf-8")
        self.assertIn("configuration.captureMicrophone = false", source)
        self.assertIn("type: .audio", source)
        self.assertNotIn("type: .screen", source)
        self.assertNotIn("type: .microphone", source)

    def test_json_protocol_start_stop_and_wav_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            helper = self._make_fake_helper(root, permission_denied=False)
            output = root / "capture.wav"
            session = NativeCaptureSession(output, 48_000)
            with patch(
                "system_audio_recorder.native.ensure_native_helper",
                return_value=helper,
            ):
                session.start(timeout=5)
                assert session._process is not None
                self.assertNotEqual(os.getpgid(session._process.pid), os.getpgrp())
                result = session.stop(timeout=5)

            self.assertEqual(result.frames, 4_800)
            with wave.open(str(output), "rb") as recording:
                self.assertEqual(recording.getnchannels(), 2)
                self.assertEqual(recording.getframerate(), 48_000)

    def test_json_protocol_permission_denial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            helper = self._make_fake_helper(root, permission_denied=True)
            session = NativeCaptureSession(root / "capture.wav", 48_000)
            with patch(
                "system_audio_recorder.native.ensure_native_helper",
                return_value=helper,
            ):
                with self.assertRaisesRegex(NativeCaptureError, "permission denied"):
                    session.start(timeout=5)

    def _make_fake_helper(self, root: Path, permission_denied: bool) -> Path:
        helper = root / "fake-helper"
        if permission_denied:
            body = "print(json.dumps({'event': 'error', 'message': 'permission denied'}), flush=True)"
        else:
            body = textwrap.dedent(
                """
                print(json.dumps({'event': 'ready'}), flush=True)
                command = json.loads(sys.stdin.readline())
                if command.get('command') == 'stop':
                    output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])
                    with wave.open(str(output), 'wb') as recording:
                        recording.setnchannels(2)
                        recording.setsampwidth(2)
                        recording.setframerate(48000)
                        recording.writeframes(b'\\x00\\x00\\x00\\x00' * 4800)
                    print(json.dumps({
                        'event': 'stopped',
                        'frames': 4800,
                        'duration': 0.1,
                        'warnings': [],
                    }), flush=True)
                """
            )
        script = (
            f"#!{sys.executable}\n"
            "import json\nimport pathlib\nimport sys\nimport wave\n"
            f"{body}\n"
        )
        helper.write_text(script, encoding="utf-8")
        helper.chmod(0o755)
        return helper


if __name__ == "__main__":
    unittest.main()

