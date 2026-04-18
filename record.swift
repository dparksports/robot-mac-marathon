import AVFoundation
import Foundation

class VideoAudioRecorder: NSObject {
    private let captureSession = AVCaptureSession()
    private let movieOutput = AVCaptureMovieFileOutput()
    private var videoDevice: AVCaptureDevice?
    private var audioDevice: AVCaptureDevice?
    private var outputURL: URL
    
    override init() {
        // Set output file path to Desktop with timestamp
        let dateFormatter = DateFormatter()
        dateFormatter.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let timestamp = dateFormatter.string(from: Date())
        let desktopPath = NSSearchPathForDirectoriesInDomains(.desktopDirectory, .userDomainMask, true)[0]
        self.outputURL = URL(fileURLWithPath: "\(desktopPath)/recording_\(timestamp).mov")
        
        super.init()
        setupCaptureSession()
    }
    
    private func setupCaptureSession() {
        // Configure session
        captureSession.sessionPreset = .high
        
        // Setup video input
        if let videoDevice = AVCaptureDevice.default(for: .video) {
            self.videoDevice = videoDevice
            do {
                let videoInput = try AVCaptureDeviceInput(device: videoDevice)
                if captureSession.canAddInput(videoInput) {
                    captureSession.addInput(videoInput)
                }
            } catch {
                print("Error setting up video input: \(error)")
                return
            }
        }
        
        // Setup audio input
        if let audioDevice = AVCaptureDevice.default(for: .audio) {
            self.audioDevice = audioDevice
            do {
                let audioInput = try AVCaptureDeviceInput(device: audioDevice)
                if captureSession.canAddInput(audioInput) {
                    captureSession.addInput(audioInput)
                }
            } catch {
                print("Error setting up audio input: \(error)")
                return
            }
        }
        
        // Setup movie output
        if captureSession.canAddOutput(movieOutput) {
            captureSession.addOutput(movieOutput)
        }
    }
    
    func startRecording() {
        // Start the capture session
        captureSession.startRunning()
        
        // Begin recording to the output file
        movieOutput.startRecording(to: outputURL, recordingDelegate: self)
        print("Recording started. Press 'q' to stop and save.")
        
        // Monitor for 'q' key press
        monitorKeyPress()
    }
    
    private func monitorKeyPress() {
        DispatchQueue.global(qos: .userInteractive).async {
            while true {
                let input = readLine()
                if input?.lowercased() == "q" {
                    self.movieOutput.stopRecording()
                    self.captureSession.stopRunning()
                    break
                }
            }
        }
    }
}

extension VideoAudioRecorder: AVCaptureFileOutputRecordingDelegate {
    func fileOutput(_ output: AVCaptureFileOutput, didStartRecordingTo fileURL: URL, from connections: [AVCaptureConnection]) {
        print("Recording to: \(fileURL.path)")
    }
    
    func fileOutput(_ output: AVCaptureFileOutput, didFinishRecordingTo outputFileURL: URL, from connections: [AVCaptureConnection], error: Error?) {
        if let error = error {
            print("Error recording: \(error)")
        } else {
            print("Recording saved to: \(outputFileURL.path)")
        }
        exit(0)
    }
}

func main() {
    // Request camera and microphone permissions
    AVCaptureDevice.requestAccess(for: .video) { videoGranted in
        guard videoGranted else {
            print("Camera access denied.")
            exit(1)
        }
        
        AVCaptureDevice.requestAccess(for: .audio) { audioGranted in
            guard audioGranted else {
                print("Microphone access denied.")
                exit(1)
            }
            
            // Create and start the recorder
            let recorder = VideoAudioRecorder()
            recorder.startRecording()
        }
    }
    
    // Keep the main thread alive
    dispatchMain()
}

main()