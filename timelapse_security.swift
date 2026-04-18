import AVFoundation
import CoreGraphics
import Foundation
import IOKit.ps
import VideoToolbox

// ─── Async-signal-safe shutdown flag ───
private var shouldStop: Int32 = 0
private var globalRecorder: SecurityRecorder?

// ─── Configuration ───
struct SecurityConfig {
    var soundThreshold: Float = -30.0          // dB — trigger level
    var recordingDuration: TimeInterval = 60.0  // seconds to record per trigger
    var continuousAudioChunkSeconds: TimeInterval = 3600 // 1 hour audio chunks
    var minBatteryPercent: Int = 5              // auto-shutdown below this
    var batteryLogInterval: TimeInterval = 600  // log battery every 10 min
    var videoPreset: AVCaptureSession.Preset = .medium
    var videoBitRate: Int = 500_000             // 500 Kbps HEVC
    var audioBitRate: Int = 32_000              // 32 Kbps AAC
    var minFreeSpace: Int64 = 5_000_000_000    // 5 GB
}

// ─── Screen Brightness ───
func setScreenBrightness(_ level: Float) {
    #if arch(arm64)
    let service = IOServiceGetMatchingService(kIOMainPortDefault,
        IOServiceMatching("IODisplayConnect"))
    if service != 0 {
        IODisplaySetFloatParameter(service, 0, kIODisplayBrightnessKey as CFString, level)
        IOObjectRelease(service)
    }
    #endif
}

// ─── Battery Monitoring ───
func getBatteryLevel() -> (percent: Int, isCharging: Bool)? {
    guard let snapshot = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
          let sources = IOPSCopyPowerSourcesList(snapshot)?.takeRetainedValue() as? [Any],
          let source = sources.first,
          let desc = IOPSGetPowerSourceDescription(snapshot, source as CFTypeRef)?.takeUnretainedValue() as? [String: Any],
          let capacity = desc[kIOPSCurrentCapacityKey] as? Int,
          let isCharging = desc[kIOPSIsChargingKey] as? Bool else {
        return nil
    }
    return (capacity, isCharging)
}

// ─── Main Recorder ───
class SecurityRecorder: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate,
                        AVCaptureVideoDataOutputSampleBufferDelegate {

    private let config: SecurityConfig
    private let outputDir: String

    // Audio monitoring session (always on — low power)
    private var audioSession: AVCaptureSession?
    private var monitorAudioOutput: AVCaptureAudioDataOutput?
    private let audioMonitorQueue = DispatchQueue(label: "security.audio.monitor")

    // Continuous Audio Writer (saves hourly .m4a chunks)
    private var continuousAudioWriter: AVAssetWriter?
    private var continuousAudioInput: AVAssetWriterInput?
    private var currentAudioStartTime: Date?
    private var currentAudioURL: URL?
    private var audioWriterStarted = false

    // Video recording session (on-demand — camera fully off between triggers)
    private var videoSession: AVCaptureSession?
    private var assetWriter: AVAssetWriter?
    private var videoWriterInput: AVAssetWriterInput?
    private var videoAdaptor: AVAssetWriterInputPixelBufferAdaptor?
    private var audioWriterInput: AVAssetWriterInput?
    private let videoRecordQueue = DispatchQueue(label: "security.video.record")
    private let audioRecordQueue = DispatchQueue(label: "security.audio.record")

    // State
    private enum RecorderState {
        case listening       // mic only, camera fully off
        case recording       // camera on, writing video
        case shuttingDown
    }
    private var state: RecorderState = .listening
    private let stateLock = NSLock()

    private var lastBatteryLogTime: Date = .distantPast
    private var recordingStartTime: Date?
    private var videoFrameCount: Int64 = 0
    private var totalTriggerCount: Int = 0
    private var totalChunksSaved: Int = 0
    private var writerStarted = false
    private var currentVideoURL: URL?
    private var periodicTimer: DispatchSourceTimer?

    init(config: SecurityConfig = SecurityConfig()) {
        self.config = config

        let cwd = FileManager.default.currentDirectoryPath
        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let ts = fmt.string(from: Date())
        self.outputDir = "\(cwd)/security_\(ts)"

        super.init()

        try? FileManager.default.createDirectory(atPath: outputDir,
                                                  withIntermediateDirectories: true)
    }

    // MARK: - Audio Monitoring & Background Writing (always on)

    private func setupAudioMonitoring() {
        let session = AVCaptureSession()

        guard let mic = AVCaptureDevice.default(for: .audio) else {
            print("❌ No microphone found. Cannot run security mode.")
            exit(1)
        }

        do {
            let micInput = try AVCaptureDeviceInput(device: mic)
            if session.canAddInput(micInput) { session.addInput(micInput) }
        } catch {
            print("❌ Mic input error: \(error)")
            exit(1)
        }

        let audioOutput = AVCaptureAudioDataOutput()
        audioOutput.setSampleBufferDelegate(self, queue: audioMonitorQueue)
        if session.canAddOutput(audioOutput) { session.addOutput(audioOutput) }
        self.monitorAudioOutput = audioOutput

        self.audioSession = session
    }

    private func cycleContinuousAudioWriter(with pts: CMTime) {
        // Finish old chunk if exists
        let oldWriter = continuousAudioWriter
        let oldInput = continuousAudioInput
        let oldStarted = audioWriterStarted
        let oldURL = currentAudioURL

        continuousAudioWriter = nil
        continuousAudioInput = nil
        audioWriterStarted = false

        if let writer = oldWriter, oldStarted {
            oldInput?.markAsFinished()
            writer.finishWriting { [weak self] in
                self?.totalChunksSaved += 1
                if let url = oldURL {
                    // Check size
                    if let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
                       let size = attrs[.size] as? Int64 {
                        let mb = Double(size) / 1_000_000
                        print("🎧 Saved background audio chunk → \(url.lastPathComponent) (\(String(format: "%.1f", mb)) MB)")
                    }
                }
            }
        }

        // Start new chunk
        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let ts = fmt.string(from: Date())
        let audioURL = URL(fileURLWithPath: "\(outputDir)/audio_\(ts).m4a")
        currentAudioURL = audioURL
        currentAudioStartTime = Date()

        do {
            let writer = try AVAssetWriter(outputURL: audioURL, fileType: .m4a)
            let audioSettings: [String: Any] = [
                AVFormatIDKey: kAudioFormatMPEG4AAC,
                AVSampleRateKey: 44100,
                AVNumberOfChannelsKey: 1,
                AVEncoderBitRateKey: config.audioBitRate
            ]
            
            let input = AVAssetWriterInput(mediaType: .audio, outputSettings: audioSettings)
            input.expectsMediaDataInRealTime = true
            
            if writer.canAdd(input) {
                writer.add(input)
            } else {
                print("⚠️ Cannot add audio input to continuous writer")
                return
            }

            if !writer.startWriting() {
                print("⚠️ Failed to start background audio writer")
                return
            }
            
            writer.startSession(atSourceTime: pts)
            audioWriterStarted = true
            continuousAudioWriter = writer
            continuousAudioInput = input
            
            print("🎧 Started new continuous audio chunk → \(audioURL.lastPathComponent)")
            
        } catch {
            print("⚠️ Cannot create continuous audio writer: \(error)")
        }
    }

    private func stopContinuousAudioWriter() {
        if let writer = continuousAudioWriter, audioWriterStarted {
            continuousAudioInput?.markAsFinished()
            let sema = DispatchSemaphore(value: 0)
             writer.finishWriting { 
                self.totalChunksSaved += 1
                sema.signal() 
            }
            _ = sema.wait(timeout: .now() + 10)
        }
        continuousAudioWriter = nil
        continuousAudioInput = nil
    }

    // MARK: - Video Recording (on-demand)

    private func startVideoRecording() {
        stateLock.lock()
        guard state == .listening else { stateLock.unlock(); return }
        state = .recording
        stateLock.unlock()

        totalTriggerCount += 1
        recordingStartTime = Date()
        videoFrameCount = 0
        writerStarted = false

        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let ts = fmt.string(from: Date())
        let videoURL = URL(fileURLWithPath: "\(outputDir)/trigger_\(ts).mov")
        currentVideoURL = videoURL

        let session = AVCaptureSession()
        session.sessionPreset = config.videoPreset

        // Camera
        if let camera = AVCaptureDevice.default(for: .video) {
            do {
                let camInput = try AVCaptureDeviceInput(device: camera)
                if session.canAddInput(camInput) { session.addInput(camInput) }
            } catch {
                print("⚠️  Camera error: \(error)")
                resetToListening()
                return
            }
        }

        // Microphone (for audio track in the video file)
        if let mic = AVCaptureDevice.default(for: .audio) {
            do {
                let micInput = try AVCaptureDeviceInput(device: mic)
                if session.canAddInput(micInput) { session.addInput(micInput) }
            } catch {
                print("⚠️  Mic for video error: \(error)")
            }
        }

        let videoOutput = AVCaptureVideoDataOutput()
        videoOutput.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
        ]
        videoOutput.setSampleBufferDelegate(self, queue: videoRecordQueue)
        if session.canAddOutput(videoOutput) { session.addOutput(videoOutput) }

        let audioOutput = AVCaptureAudioDataOutput()
        audioOutput.setSampleBufferDelegate(self, queue: audioRecordQueue)
        if session.canAddOutput(audioOutput) { session.addOutput(audioOutput) }

        self.videoSession = session

        do {
            assetWriter = try AVAssetWriter(outputURL: videoURL, fileType: .mov)
        } catch {
            print("⚠️  Cannot create writer: \(error)")
            resetToListening()
            return
        }

        session.startRunning()
        print("🔴 RECORDING triggered (#\(totalTriggerCount)) → \(videoURL.lastPathComponent)")
    }

    private func stopVideoRecording() {
        stateLock.lock()
        guard state == .recording else { stateLock.unlock(); return }
        state = .listening
        stateLock.unlock()

        videoSession?.stopRunning()
        videoSession = nil

        guard let writer = assetWriter, writerStarted else {
            print("🔵 Recording stopped (no frames captured)")
            assetWriter = nil
            return
        }

        guard writer.status == .writing else {
            print("⚠️  Writer status: \(writer.status.rawValue)")
            assetWriter = nil
            return
        }

        videoWriterInput?.markAsFinished()
        audioWriterInput?.markAsFinished()

        let sema = DispatchSemaphore(value: 0)
        writer.finishWriting { sema.signal() }
        let result = sema.wait(timeout: .now() + 30)
        if result == .timedOut {
            print("⚠️  Writer finishWriting timed out")
        }

        if let url = currentVideoURL, FileManager.default.fileExists(atPath: url.path) {
            let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int64) ?? 0
            let mb = Double(size) / 1_000_000
            let duration = Date().timeIntervalSince(recordingStartTime ?? Date())
            print("🔵 Saved \(videoFrameCount) frames (\(String(format: "%.0f", duration))s, \(String(format: "%.1f", mb)) MB) → \(url.lastPathComponent)")
        }

        assetWriter = nil
        videoWriterInput = nil
        videoAdaptor = nil
        audioWriterInput = nil
    }

    private func resetToListening() {
        stateLock.lock()
        state = .listening
        stateLock.unlock()
    }

    // MARK: - Start & Run

    func start() {
        setScreenBrightness(0.0)
        print("🔅 Screen brightness set to 0")

        setupAudioMonitoring()
        audioSession?.startRunning()

        print("🛡️  Security mode active — camera OFF, mic listening AND recording continuously")
        print("   📁 Output folder: \(outputDir)")
        print("   🎧 Continuous audio: Saving every \(Int(config.continuousAudioChunkSeconds / 60)) minutes (.m4a)")
        print("   🎤 Sound threshold: \(config.soundThreshold) dB → triggers camera")
        print("   🎬 Recording burst: \(Int(config.recordingDuration))s per trigger (.mov)")
        print("   🔋 Auto-shutdown: \(config.minBatteryPercent)%")
        print("   Press Ctrl+C to stop.\n")

        logBattery()

        // Periodic timer for shutdown checks and battery logging
        // Uses DispatchSourceTimer so it works with dispatchMain()
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + 1.0, repeating: 1.0)
        timer.setEventHandler { [weak self] in
            self?.periodicCheck()
        }
        timer.resume()
        periodicTimer = timer
    }

    private func periodicCheck() {
        // Signal-requested shutdown
        if OSAtomicAdd32(0, &shouldStop) != 0 {
            gracefulExit()
        }

        let now = Date()

        // Battery logging
        if now.timeIntervalSince(lastBatteryLogTime) >= config.batteryLogInterval {
            logBattery()
            lastBatteryLogTime = now
        }

        // Battery auto-shutdown
        if let battery = getBatteryLevel() {
            if battery.percent <= config.minBatteryPercent && !battery.isCharging {
                print("\n🔋 Battery critically low (\(battery.percent)%). Shutting down.")
                shutdown()
                exit(0)
            }
        }

        // Disk space check
        if let attrs = try? FileManager.default.attributesOfFileSystem(forPath: NSHomeDirectory()),
           let free = attrs[.systemFreeSize] as? Int64, free < config.minFreeSpace {
            print("\n💾 Low disk space. Shutting down.")
            shutdown()
            exit(0)
        }

        // Fixed-duration recording — stop after configured duration
        stateLock.lock()
        let currentState = state
        stateLock.unlock()

        if currentState == .recording {
            if now.timeIntervalSince(recordingStartTime ?? now) >= config.recordingDuration {
                print("⏰ Recording duration (\(Int(config.recordingDuration))s) complete")
                stopVideoRecording()
            }
        }
    }

    private func logBattery() {
        if let battery = getBatteryLevel() {
            let status = battery.isCharging ? "⚡ charging" : "🔋 on battery"
            print("🔋 Battery: \(battery.percent)% (\(status)) | Triggers: \(totalTriggerCount)")
        }
    }

    func shutdown() {
        print("\n🛑 Shutting down...")
        periodicTimer?.cancel()
        periodicTimer = nil
        stopVideoRecording()
        stopContinuousAudioWriter()
        audioSession?.stopRunning()
        setScreenBrightness(0.5)
        print("📊 Session summary: \(totalChunksSaved) continuous audio chunks, \(totalTriggerCount) camera triggers")
        print("📁 Files saved to: \(outputDir)")
    }

    func gracefulExit() {
        shutdown()
        exit(0)
    }

    // MARK: - AVCaptureAudioDataOutputSampleBufferDelegate / Video

    func captureOutput(_ output: AVCaptureOutput,
                       didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        guard let desc = CMSampleBufferGetFormatDescription(sampleBuffer) else { return }
        let mediaType = CMFormatDescriptionGetMediaType(desc)

        if mediaType == kCMMediaType_Video {
            handleVideoFrame(sampleBuffer)
            return
        }

        guard mediaType == kCMMediaType_Audio else { return }

        // Route audio based on which session it came from.
        // This prevents the race condition where two queues
        // (audioMonitorQueue + audioRecordQueue) try to append
        // to the same AVAssetWriterInput concurrently.

        if output === monitorAudioOutput {
            // ── Audio from the always-on monitor session ──
            // → continuous .m4a writer + trigger detection
            let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
            let now = Date()

            // 1. Manage Continuous Hourly Audio Chunking
            if continuousAudioWriter == nil {
                cycleContinuousAudioWriter(with: pts)
            } else if let startTime = currentAudioStartTime,
                      now.timeIntervalSince(startTime) >= config.continuousAudioChunkSeconds {
                cycleContinuousAudioWriter(with: pts)
            }

            // Append to the active background writer
            if audioWriterStarted, let input = continuousAudioInput, input.isReadyForMoreMediaData {
                input.append(sampleBuffer)
            }

            // 2. Trigger Logic — only from the monitor mic
            stateLock.lock()
            let currentState = state
            stateLock.unlock()

            let level = measureAudioLevel(sampleBuffer)

            if level > config.soundThreshold && currentState == .listening {
                DispatchQueue.main.async { [weak self] in
                    self?.startVideoRecording()
                }
            }

        } else {
            // ── Audio from the video recording session ──
            // → burst .mov file's audio track only
            stateLock.lock()
            let currentState = state
            stateLock.unlock()

            if currentState == .recording && writerStarted {
                if let input = audioWriterInput, input.isReadyForMoreMediaData,
                   let writer = assetWriter, writer.status == .writing {
                    input.append(sampleBuffer)
                }
            }
        }
    }

    private func measureAudioLevel(_ sampleBuffer: CMSampleBuffer) -> Float {
        guard let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else {
            return -160.0
        }

        var lengthAtOffset: Int = 0
        var totalLength: Int = 0
        var data: UnsafeMutablePointer<Int8>?

        let status = CMBlockBufferGetDataPointer(blockBuffer, atOffset: 0,
                                                  lengthAtOffsetOut: &lengthAtOffset,
                                                  totalLengthOut: &totalLength,
                                                  dataPointerOut: &data)
        guard status == kCMBlockBufferNoErr, let samples = data else {
            return -160.0
        }

        let sampleCount = totalLength / 2
        guard sampleCount > 0 else { return -160.0 }

        let int16Ptr = UnsafeRawPointer(samples).bindMemory(to: Int16.self, capacity: sampleCount)
        var sumSquares: Float = 0.0
        for i in 0..<sampleCount {
            let sample = Float(int16Ptr[i]) / Float(Int16.max)
            sumSquares += sample * sample
        }
        let rms = sqrt(sumSquares / Float(sampleCount))
        let db = rms > 0 ? 20.0 * log10(rms) : -160.0
        return db
    }

    private func handleVideoFrame(_ sampleBuffer: CMSampleBuffer) {
        stateLock.lock()
        guard state == .recording else { stateLock.unlock(); return }
        stateLock.unlock()

        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)

        // Lazy-init writer on first video frame
        if !writerStarted {
            let w = CVPixelBufferGetWidth(pixelBuffer)
            let h = CVPixelBufferGetHeight(pixelBuffer)
            setupWriter(width: w, height: h)
            assetWriter!.startWriting()
            assetWriter!.startSession(atSourceTime: pts)
            writerStarted = true

            if assetWriter!.status == .failed {
                print("⚠️  Writer failed: \(assetWriter!.error?.localizedDescription ?? "unknown")")
                resetToListening()
                return
            }
        }

        guard let writer = assetWriter, writer.status == .writing else { return }
        guard let adaptor = videoAdaptor, videoWriterInput?.isReadyForMoreMediaData == true else { return }

        if adaptor.append(pixelBuffer, withPresentationTime: pts) {
            videoFrameCount += 1
        }
    }

    private func setupWriter(width: Int, height: Int) {
        let videoSettings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.hevc,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: config.videoBitRate
            ]
        ]
        videoWriterInput = AVAssetWriterInput(mediaType: .video, outputSettings: videoSettings)
        videoWriterInput!.expectsMediaDataInRealTime = true
        assetWriter!.add(videoWriterInput!)

        videoAdaptor = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: videoWriterInput!,
            sourcePixelBufferAttributes: nil
        )

        let audioSettings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 44100,
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: config.audioBitRate
        ]
        audioWriterInput = AVAssetWriterInput(mediaType: .audio, outputSettings: audioSettings)
        audioWriterInput!.expectsMediaDataInRealTime = true
        assetWriter!.add(audioWriterInput!)
    }
}

// ─── Main Entry Point ───

var config = SecurityConfig()

let args = CommandLine.arguments
for i in 0..<args.count {
    switch args[i] {
    case "--threshold":
        if i + 1 < args.count, let val = Float(args[i + 1]) {
            config.soundThreshold = val
        }
    case "--duration":
        if i + 1 < args.count, let val = TimeInterval(args[i + 1]) {
            config.recordingDuration = val
        }
    case "--audio-chunk":
        if i + 1 < args.count, let val = TimeInterval(args[i + 1]) {
            config.continuousAudioChunkSeconds = val * 60 // convert minutes to seconds
        }
    case "--min-battery":
        if i + 1 < args.count, let val = Int(args[i + 1]) {
            config.minBatteryPercent = val
        }
    case "--help":
        print("""
        Usage: timelapse_security [options]

        Options:
          --threshold <dB>           Sound level to trigger recording (default: -30)
          --duration <seconds>       How long to record per trigger (default: 60)
          --audio-chunk <minutes>    Continuous audio chunk size in minutes (default: 60)
          --min-battery <percent>    Auto-shutdown battery level (default: 5)
          --help                     Show this help

        Power model:
          Mic captures continuous audio saved to 1-hour chunks (~0.3W total).
          Camera is FULLY OFF until sound is detected above the threshold. 
          On trigger, captures camera burst for the configured duration.
          Estimated battery life: ~5.5-6.5 days on a MacBook Air.
        """)
        exit(0)
    default:
        break
    }
}

let recorder = SecurityRecorder(config: config)

// Use DispatchSource signal sources so shutdown runs on the main queue
// (signal() + polling via Timer didn't work because dispatchMain() doesn't pump RunLoop)
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)

let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigintSource.setEventHandler {
    globalRecorder?.gracefulExit()
}
sigintSource.resume()

let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigtermSource.setEventHandler {
    globalRecorder?.gracefulExit()
}
sigtermSource.resume()

AVCaptureDevice.requestAccess(for: .video) { videoGranted in
    guard videoGranted else { print("❌ Camera access denied."); exit(1) }
    AVCaptureDevice.requestAccess(for: .audio) { audioGranted in
        guard audioGranted else { print("❌ Mic access denied."); exit(1) }
        DispatchQueue.main.async {
            globalRecorder = recorder
            recorder.start()
        }
    }
}

dispatchMain()
