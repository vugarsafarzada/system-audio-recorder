import AVFoundation
import CoreGraphics
import CoreMedia
import Foundation
import ScreenCaptureKit

enum HelperError: LocalizedError {
    case invalidArguments(String)
    case permissionDenied
    case noDisplay
    case writer(String)
    case noAudio

    var errorDescription: String? {
        switch self {
        case .invalidArguments(let message):
            return message
        case .permissionDenied:
            return "Screen & System Audio Recording permission was not granted. Open System Settings > Privacy & Security > Screen & System Audio Recording, allow this helper, then start a new recording."
        case .noDisplay:
            return "ScreenCaptureKit found no display to use as the system audio source."
        case .writer(let message):
            return message
        case .noAudio:
            return "No system audio frames were captured."
        }
    }
}

enum ProtocolOutput {
    private static let lock = NSLock()

    static func send(_ values: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(values),
              let data = try? JSONSerialization.data(withJSONObject: values),
              let line = String(data: data, encoding: .utf8) else {
            return
        }
        lock.lock()
        print(line)
        fflush(stdout)
        lock.unlock()
    }
}

final class WaveAssetWriter: @unchecked Sendable {
    private let writer: AVAssetWriter
    private let input: AVAssetWriterInput
    private(set) var frames: Int64 = 0
    private var started = false
    private var failure: Error?
    private let failureHandler: (String) -> Void

    init(
        outputURL: URL,
        sampleRate: Int,
        failureHandler: @escaping (String) -> Void
    ) throws {
        self.failureHandler = failureHandler
        writer = try AVAssetWriter(outputURL: outputURL, fileType: .wav)
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: 2,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]
        input = AVAssetWriterInput(mediaType: .audio, outputSettings: settings)
        input.expectsMediaDataInRealTime = true
        guard writer.canAdd(input) else {
            throw HelperError.writer("AVFoundation rejected the WAV audio input.")
        }
        writer.add(input)
    }

    func append(_ sampleBuffer: CMSampleBuffer) {
        guard failure == nil, CMSampleBufferDataIsReady(sampleBuffer) else {
            return
        }
        if !started {
            guard writer.startWriting() else {
                recordFailure(
                    writer.error ?? HelperError.writer("Could not start WAV writing.")
                )
                return
            }
            writer.startSession(atSourceTime: CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
            started = true
        }
        guard input.isReadyForMoreMediaData else {
            recordFailure(
                HelperError.writer(
                    "The WAV writer could not keep up with real-time system audio."
                )
            )
            return
        }
        guard input.append(sampleBuffer) else {
            recordFailure(
                writer.error ?? HelperError.writer("Could not append system audio.")
            )
            return
        }
        frames += Int64(CMSampleBufferGetNumSamples(sampleBuffer))
    }

    private func recordFailure(_ error: Error) {
        guard failure == nil else {
            return
        }
        failure = error
        failureHandler(error.localizedDescription)
    }

    func finish(completion: @escaping (Result<Int64, Error>) -> Void) {
        if let failure {
            writer.cancelWriting()
            completion(.failure(failure))
            return
        }
        guard started, frames > 0 else {
            writer.cancelWriting()
            completion(.failure(HelperError.noAudio))
            return
        }
        input.markAsFinished()
        writer.finishWriting { [writer, frames] in
            if writer.status == .completed {
                completion(.success(frames))
            } else {
                completion(
                    .failure(writer.error ?? HelperError.writer("Could not finalize the WAV file."))
                )
            }
        }
    }
}

final class CaptureController: NSObject, SCStreamOutput, SCStreamDelegate {
    private let outputURL: URL
    private let sampleRate: Int
    private let audioQueue = DispatchQueue(label: "system-audio.capture")
    private let stateLock = NSLock()
    private var stream: SCStream?
    private var waveWriter: WaveAssetWriter?
    private var interruption: String?
    private var interruptionReported = false

    init(outputURL: URL, sampleRate: Int) {
        self.outputURL = outputURL
        self.sampleRate = sampleRate
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false,
            onScreenWindowsOnly: false
        )
        guard let display = content.displays.first else {
            throw HelperError.noDisplay
        }

        let writer = try WaveAssetWriter(
            outputURL: outputURL,
            sampleRate: sampleRate
        ) { [weak self] message in
            self?.reportInterruption(message)
        }
        let filter = SCContentFilter(
            display: display,
            excludingApplications: [],
            exceptingWindows: []
        )
        let configuration = SCStreamConfiguration()
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = true
        configuration.captureMicrophone = false
        configuration.sampleRate = sampleRate
        configuration.channelCount = 2
        configuration.width = 2
        configuration.height = 2
        configuration.queueDepth = 3
        configuration.showsCursor = false

        let captureStream = SCStream(
            filter: filter,
            configuration: configuration,
            delegate: self
        )
        try captureStream.addStreamOutput(
            self,
            type: .audio,
            sampleHandlerQueue: audioQueue
        )
        waveWriter = writer
        stream = captureStream
        try await captureStream.startCapture()
    }

    func stop() async throws -> (frames: Int64, warnings: [String]) {
        if let stream {
            do {
                try await stream.stopCapture()
            } catch {
                setInterruption("ScreenCaptureKit did not stop cleanly: \(error.localizedDescription)")
            }
        }
        self.stream = nil

        let frames: Int64 = try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Int64, Error>) in
            audioQueue.async { [waveWriter] in
                guard let waveWriter else {
                    continuation.resume(throwing: HelperError.writer("WAV writer is unavailable."))
                    return
                }
                waveWriter.finish { result in
                    continuation.resume(with: result)
                }
            }
        }
        waveWriter = nil
        let warning = currentInterruption()
        return (frames, warning.map { [$0] } ?? [])
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of outputType: SCStreamOutputType
    ) {
        guard outputType == .audio else {
            return
        }
        waveWriter?.append(sampleBuffer)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        let message = "System audio capture stopped unexpectedly: \(error.localizedDescription)"
        reportInterruption(message)
    }

    private func reportInterruption(_ message: String) {
        setInterruption(message)
        stateLock.lock()
        let shouldReport = !interruptionReported
        interruptionReported = true
        stateLock.unlock()
        if shouldReport {
            ProtocolOutput.send(["event": "interrupted", "message": message])
        }
    }

    private func setInterruption(_ message: String) {
        stateLock.lock()
        if interruption == nil {
            interruption = message
        }
        stateLock.unlock()
    }

    private func currentInterruption() -> String? {
        stateLock.lock()
        defer { stateLock.unlock() }
        return interruption
    }
}

struct Arguments {
    let outputURL: URL
    let sampleRate: Int

    static func parse(_ arguments: [String]) throws -> Arguments {
        var outputPath: String?
        var sampleRate: Int?
        var index = 0
        while index < arguments.count {
            switch arguments[index] {
            case "--output" where index + 1 < arguments.count:
                outputPath = arguments[index + 1]
                index += 2
            case "--sample-rate" where index + 1 < arguments.count:
                sampleRate = Int(arguments[index + 1])
                index += 2
            default:
                throw HelperError.invalidArguments("Invalid native helper arguments.")
            }
        }
        guard let outputPath, !outputPath.isEmpty else {
            throw HelperError.invalidArguments("A WAV output path is required.")
        }
        guard let sampleRate, sampleRate == 44_100 || sampleRate == 48_000 else {
            throw HelperError.invalidArguments("Sample rate must be 44100 or 48000 Hz.")
        }
        return Arguments(
            outputURL: URL(fileURLWithPath: outputPath).standardizedFileURL,
            sampleRate: sampleRate
        )
    }
}

func waitForStopCommand() async -> Bool {
    await withCheckedContinuation { continuation in
        DispatchQueue.global(qos: .userInitiated).async {
            guard let line = readLine(),
                  let data = line.data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  object["command"] as? String == "stop" else {
                continuation.resume(returning: false)
                return
            }
            continuation.resume(returning: true)
        }
    }
}

@main
struct SystemAudioCaptureHelper {
    static func main() async {
        guard #available(macOS 15.0, *) else {
            ProtocolOutput.send([
                "event": "error",
                "message": "Native capture requires macOS 15 or newer.",
            ])
            return
        }

        do {
            let arguments = try Arguments.parse(Array(CommandLine.arguments.dropFirst()))
            guard CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess() else {
                throw HelperError.permissionDenied
            }

            let controller = CaptureController(
                outputURL: arguments.outputURL,
                sampleRate: arguments.sampleRate
            )
            try await controller.start()
            ProtocolOutput.send([
                "event": "ready",
                "sample_rate": arguments.sampleRate,
                "channels": 2,
                "microphone": false,
                "video_output": false,
            ])

            if !(await waitForStopCommand()) {
                ProtocolOutput.send([
                    "event": "warning",
                    "message": "The controlling process closed; finalizing the recording.",
                ])
            }

            let result = try await controller.stop()
            ProtocolOutput.send([
                "event": "stopped",
                "frames": result.frames,
                "duration": Double(result.frames) / Double(arguments.sampleRate),
                "warnings": result.warnings,
            ])
        } catch {
            ProtocolOutput.send([
                "event": "error",
                "message": error.localizedDescription,
            ])
        }
    }
}

