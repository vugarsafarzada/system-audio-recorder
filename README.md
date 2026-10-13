# Native macOS System Audio Recorder

A Python 3.11+ terminal application that records audio playing through macOS by
using Apple's ScreenCaptureKit. It does not use BlackHole, virtual audio devices,
audio routing changes, FFmpeg, Homebrew packages, or third-party Python modules.

The native helper registers only ScreenCaptureKit's system-audio output. It does
not register a screen or microphone output, and it explicitly sets microphone
capture to false. No video frames, screenshots, or microphone samples are saved.

## Requirements

- macOS 15 or newer
- Python 3.11 or newer
- Xcode or Xcode Command Line Tools for the first local build

The Swift compiler is a build-time requirement only. The compiled helper links
only Apple system frameworks and has no third-party runtime dependencies.

MP3 output is intentionally unavailable. The installed Core Audio encoder
registry and AVAudioConverter were checked on this Mac, and neither provides an
MP3 encoder. WAV is the only enabled output format. AAC was not substituted.

## Run

```bash
cd /Users/vugarsafarzada/Developer/MyProjects/project-4
python3 main.py
```

On the first Start command, the application compiles the reviewed Swift source
to `.build/system-audio-capture`. Later starts reuse that binary unless the Swift
source changes. Nothing is captured during the build or before Start is chosen.

The terminal menu provides Start, Stop, and Exit. Start asks for WAV confirmation
and a 48,000 Hz or 44,100 Hz sample rate. While recording, elapsed time is shown
and `stop` or Ctrl+C finalizes the WAV.

Recordings are written to `recordings/` with timestamped names. They are first
written to a unique temporary path, validated as stereo 16-bit PCM WAV, and then
published atomically without replacing an existing file.

## Privacy permission

The first capture attempt may display Apple's Screen & System Audio Recording
permission prompt. This occurs only after an explicit Start command and uses
`CGRequestScreenCaptureAccess`, the supported macOS permission request.

If access is denied:

1. Open **System Settings > Privacy & Security > Screen & System Audio
   Recording**.
2. Enable access for the native helper or the terminal host shown by macOS.
3. Return to the application and choose Start again.

The application does not bypass, edit, or reset privacy settings. It never
changes the selected audio output device.

## Architecture

- `main.py` starts the Python terminal interface.
- `system_audio_recorder/recorder.py` owns the safe recording lifecycle and WAV
  validation.
- `system_audio_recorder/native.py` builds and supervises one helper process.
- `native/SystemAudioCapture.swift` captures only ScreenCaptureKit audio and
  streams it into an AVFoundation WAV writer.
- Python and Swift exchange bounded line-delimited JSON over private stdin and
  stdout pipes. Closing the controlling pipe makes the helper finalize and exit.

ScreenCaptureKit may internally inspect display availability because its content
filter requires a display, but this application never registers a screen output
and never writes visual content.

## Tests

```bash
python3 -m unittest discover -v
```

The automated suite covers WAV integrity, both sample rates, repeated sessions,
collision-safe publication, empty and corrupted output, interruption recovery,
permission denial, Swift compilation, microphone/video source restrictions, and
the Python-to-helper JSON protocol.

An end-to-end signal test still requires macOS permission and audible system
playback during a recording. A successful automated or synthetic WAV test alone
does not prove that live system audio was captured.

# system-audio-recorder
