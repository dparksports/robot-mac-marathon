import AVFoundation
import Foundation

// Atomic flag for signal-safe shutdown
private var shouldStop: Int32 = 0

class TimeLapseRecorder: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate,
                         AVCaptureAudioDataOutputSampleBufferDelegate {
    private let captureSession = AVCaptureSession()
    private var assetWriter: AVAssetWriter?
    private var videoWriterInput: AVAssetWriterInput?
    private var audioWriterInput: AVAssetWriterInput?
    private var adaptor: AVAssetWriterInputPixelBufferAdaptor?
    private let outputURL: URL
    private var frameCount: Int64 = 0
    private var lastCaptureTime: Date = .distantPast
    private let captureInterval: TimeInterval
    private var isRecording = false
    private var sessionStartTime: CMTime?
    private let minFreeSpace: Int64 = 5_000_000_000 // 5 GB
    private let videoQueue = DispatchQueue(label: "timelapse.video")
    private let audioQueue = DispatchQueue(label: "timelapse.audio")
    private var hasAudioDevice = false
    private var writerStarted = false

    init(interval: TimeInterval = 1.0) {
        self.captureInterval = interval

        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let ts = fmt.string(from: Date())
        let cwd = FileManager.default.currentDirectoryPath
        self.outputURL = URL(fileURLWithPath: "\(cwd)/timelapse_\(ts).mov")

        super.init()
    }

    // MARK: - Setup

    private func setupCapture() {
        captureSession.sessionPreset = .high

        // Camera
        guard let camera = AVCaptureDevice.default(for: .video) else {
            print("No camera found."); exit(1)
        }
        do {
            let camInput = try AVCaptureDeviceInput(device: camera)
            if captureSession.canAddInput(camInput) { captureSession.addInput(camInput) }
        } catch {
            print("Camera input error: \(error)"); exit(1)
        }

        let videoOutput = AVCaptureVideoDataOutput()
        videoOutput.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
        ]
        videoOutput.setSampleBufferDelegate(self, queue: videoQueue)
        if captureSession.canAddOutput(videoOutput) { captureSession.addOutput(videoOutput) }

        // Microphone
        if let mic = AVCaptureDevice.default(for: .audio) {
            do {
                let micInput = try AVCaptureDeviceInput(device: mic)
                if captureSession.canAddInput(micInput) { captureSession.addInput(micInput) }
                hasAudioDevice = true
            } catch {
                print("Mic input error: \(error) — continuing without audio")
            }

            let audioOutput = AVCaptureAudioDataOutput()
            audioOutput.setSampleBufferDelegate(self, queue: audioQueue)
            if captureSession.canAddOutput(audioOutput) { captureSession.addOutput(audioOutput) }
        } else {
            print("No microphone found — recording video only.")
        }
    }

    private func setupWriter(width: Int, height: Int) {
        do {
            assetWriter = try AVAssetWriter(outputURL: outputURL, fileType: .mov)
        } catch {
            print("Cannot create writer: \(error)"); exit(1)
        }

        // Video input
        let videoSettings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: 2_000_000,
                AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel
            ]
        ]
        videoWriterInput = AVAssetWriterInput(mediaType: .video, outputSettings: videoSettings)
        videoWriterInput!.expectsMediaDataInRealTime = true
        assetWriter!.add(videoWriterInput!)

        let attrs: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: width,
            kCVPixelBufferHeightKey as String: height
        ]
        adaptor = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: videoWriterInput!,
            sourcePixelBufferAttributes: attrs
        )

        // Audio input — add BEFORE startWriting so the writer is properly configured
        if hasAudioDevice {
            let audioSettings: [String: Any] = [
                AVFormatIDKey: kAudioFormatMPEG4AAC,
                AVSampleRateKey: 44100,
                AVNumberOfChannelsKey: 1,
                AVEncoderBitRateKey: 128_000
            ]
            audioWriterInput = AVAssetWriterInput(mediaType: .audio, outputSettings: audioSettings)
            audioWriterInput!.expectsMediaDataInRealTime = true
            assetWriter!.add(audioWriterInput!)
        }
    }

    // MARK: - Recording control

    func start() {
        setupCapture()
        captureSession.startRunning()
        isRecording = true
        print("Time-lapse started (1 frame/\(captureInterval)s + audio) → \(outputURL.path)")
        print("Press Ctrl+C to stop and save.")
    }

    func stop() {
        guard isRecording else { return }
        isRecording = false
        captureSession.stopRunning()

        guard let writer = assetWriter else {
            print("No writer was initialized — nothing to save.")
            return
        }

        // Check writer status before trying to finish
        guard writer.status == .writing else {
            print("Writer is in state \(writer.status.rawValue), cannot finalize.")
            if let err = writer.error {
                print("Writer error: \(err)")
            }
            return
        }

        videoWriterInput?.markAsFinished()
        audioWriterInput?.markAsFinished()

        let sema = DispatchSemaphore(value: 0)
        writer.finishWriting {
            sema.signal()
        }
        // Wait up to 30 seconds for finishWriting to complete
        let result = sema.wait(timeout: .now() + 30)
        if result == .timedOut {
            print("Warning: finishWriting timed out after 30 seconds.")
        }

        if writer.status == .failed {
            print("Writer failed: \(writer.error?.localizedDescription ?? "unknown error")")
        }

        if FileManager.default.fileExists(atPath: outputURL.path) {
            let size = (try? FileManager.default.attributesOfItem(atPath: outputURL.path)[.size] as? Int64) ?? 0
            let mb = Double(size) / 1_000_000
            print("Saved \(frameCount) frames → \(outputURL.path) (\(String(format: "%.1f", mb)) MB)")
        } else {
            print("Warning: output file not found.")
        }
    }

    // MARK: - Disk space check

    private func diskSpaceOK() -> Bool {
        if let attrs = try? FileManager.default.attributesOfFileSystem(forPath: NSHomeDirectory()),
           let free = attrs[.systemFreeSize] as? Int64 {
            if free < minFreeSpace {
                print("\nLow disk space (\(free / 1_000_000) MB). Stopping.")
                return false
            }
        }
        return true
    }

    // MARK: - AVCaptureVideoDataOutputSampleBufferDelegate

    func captureOutput(_ output: AVCaptureOutput,
                       didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        guard isRecording else { return }

        // Determine if this is audio or video
        if let desc = CMSampleBufferGetFormatDescription(sampleBuffer) {
            let mediaType = CMFormatDescriptionGetMediaType(desc)

            if mediaType == kCMMediaType_Audio {
                handleAudio(sampleBuffer)
            } else if mediaType == kCMMediaType_Video {
                handleVideo(sampleBuffer)
            }
        }
    }

    private func handleVideo(_ sampleBuffer: CMSampleBuffer) {
        // Check for signal-requested shutdown (async-signal-safe check)
        if OSAtomicAdd32(0, &shouldStop) != 0 {
            print("\nStopping…")
            self.stop()
            exit(0)
        }

        // Throttle: one frame per captureInterval
        let now = Date()
        if now.timeIntervalSince(lastCaptureTime) < captureInterval { return }
        lastCaptureTime = now

        if !diskSpaceOK() {
            self.stop()
            exit(0)
        }

        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)

        // Lazy-init writer on first video frame
        if !writerStarted {
            let w = CVPixelBufferGetWidth(pixelBuffer)
            let h = CVPixelBufferGetHeight(pixelBuffer)
            setupWriter(width: w, height: h)
            sessionStartTime = pts
            assetWriter!.startWriting()
            assetWriter!.startSession(atSourceTime: pts)
            writerStarted = true

            // Check for immediate writer failure
            if assetWriter!.status == .failed {
                print("Writer failed to start: \(assetWriter!.error?.localizedDescription ?? "unknown")")
                exit(1)
            }
        }

        // Check writer is still healthy
        guard let writer = assetWriter, writer.status == .writing else {
            if let err = assetWriter?.error {
                print("Writer error: \(err)")
            }
            return
        }

        guard let input = videoWriterInput, input.isReadyForMoreMediaData else { return }

        if adaptor!.append(pixelBuffer, withPresentationTime: pts) {
            frameCount += 1
            if frameCount % 60 == 0 {
                let elapsed = Double(frameCount) * captureInterval
                let mins = Int(elapsed) / 60
                let secs = Int(elapsed) % 60
                print("  \(frameCount) frames (\(mins)m \(secs)s elapsed)")
            }
        } else {
            print("Failed to append video frame \(frameCount)")
            if let err = assetWriter?.error {
                print("Writer error: \(err)")
            }
        }
    }

    private func handleAudio(_ sampleBuffer: CMSampleBuffer) {
        // Only write audio if writer is active
        guard writerStarted,
              let writer = assetWriter, writer.status == .writing else { return }

        guard let input = audioWriterInput, input.isReadyForMoreMediaData else { return }
        if !input.append(sampleBuffer) {
            // Don't spam — just silently skip failed audio appends
        }
    }
}

// MARK: - Main

let recorder = TimeLapseRecorder(interval: 1.0)

// Signal handlers: only set an atomic flag — no complex work here.
// The actual shutdown happens on the video queue in handleVideo().
signal(SIGINT) { _ in
    OSAtomicIncrement32(&shouldStop)
}
signal(SIGTERM) { _ in
    OSAtomicIncrement32(&shouldStop)
}

AVCaptureDevice.requestAccess(for: .video) { videoGranted in
    guard videoGranted else { print("Camera access denied."); exit(1) }
    AVCaptureDevice.requestAccess(for: .audio) { audioGranted in
        guard audioGranted else { print("Mic access denied."); exit(1) }
        recorder.start()
    }
}

dispatchMain()
