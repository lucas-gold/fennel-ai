import AVFoundation

/// Native capture + playback through ONE AVAudioEngine with voice-processing I/O.
///
/// `setVoiceProcessingEnabled(true)` is the whole reason capture is native (D6):
/// it gives acoustic echo cancellation, so the mic doesn't hear Kokoro through
/// the speakers and barge-in doesn't fire on the assistant's own voice. Playback
/// must go through the *same* engine's output for the AEC reference to work.
///
/// Mic in  : 512-sample int16 mono @16 kHz frames (`onMicFrame`).
/// Audio out: int16 PCM @24 kHz, scheduled per clause (`play`).
final class AudioEngine {
    var onMicFrame: ((Data) -> Void)?     // one 1024-byte (512-sample) frame
    var onLevel: ((Float) -> Void)?       // 0…1 mic RMS, for the voice orb

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var converter: AVAudioConverter?
    private var monoInFormat: AVAudioFormat?
    private var inRate: Double = 48000

    private let mic16k = AVAudioFormat(
        commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
    private let tts24k = AVAudioFormat(
        commonFormat: .pcmFormatFloat32, sampleRate: 24000, channels: 1, interleaved: false)!

    private let frameBytes = 512 * 2      // 512 int16 samples
    private var pending = Data()
    private var ready = false
    private var isListening = false
    private var currentTurn = -1
    private var sent = 0   // debug: buffers captured while listening

    // MARK: - lifecycle

    /// Request mic permission and start the engine (playback works even if the
    /// mic is denied). Call once at launch.
    func prepare() {
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
            DispatchQueue.main.async { self?.startEngine(mic: granted) }
        }
    }

    private func startEngine(mic: Bool) {
        guard !ready else { return }
        do {
            if mic {
                try engine.inputNode.setVoiceProcessingEnabled(true)
                let inFormat = engine.inputNode.outputFormat(forBus: 0)
                inRate = inFormat.sampleRate
                // Voice processing inflates the mono mic to a 7-channel stream and
                // AVAudioConverter's multichannel downmix yields silence — so we
                // take channel 0 and convert that mono stream to 16 kHz.
                let mono = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                         sampleRate: inRate, channels: 1, interleaved: false)!
                monoInFormat = mono
                converter = AVAudioConverter(from: mono, to: mic16k)
                engine.inputNode.installTap(onBus: 0, bufferSize: 1024, format: inFormat) {
                    [weak self] buffer, _ in self?.handleMic(buffer)
                }
            }
            engine.prepare()
            try engine.start()

            // Attach playback AFTER the engine is running. With voice processing
            // enabled, connecting a player *before* start makes the VP output node
            // fail to initialise (-10875, which surfaced as a CreateRecordingTap
            // NSException / SIGABRT). A post-start connect reconfigures cleanly.
            engine.attach(player)
            engine.connect(player, to: engine.mainMixerNode, format: tts24k)
            player.play()
            ready = true
        } catch {
            print("audio engine start failed:", error)
        }
    }

    // MARK: - capture

    func startListening() { isListening = true }
    func stopListening() { isListening = false; onLevel?(0) }

    private func handleMic(_ input: AVAudioPCMBuffer) {
        guard let chans = input.floatChannelData, let monoFmt = monoInFormat else { return }
        let n = Int(input.frameLength)
        let nc = Int(input.format.channelCount)
        guard n > 0 else { return }

        // Mono buffer from channel 0 (the processed near-end mic).
        guard let mono = AVAudioPCMBuffer(pcmFormat: monoFmt, frameCapacity: AVAudioFrameCount(n))
        else { return }
        mono.frameLength = AVAudioFrameCount(n)
        let md = mono.floatChannelData![0]
        for i in 0..<n { md[i] = chans[0][i] }

        guard let out = convert16k(mono), let ch = out.int16ChannelData else { return }
        let m = Int(out.frameLength)
        guard m > 0 else { return }

        var sum: Float = 0
        for i in 0..<m { let s = Float(ch[0][i]); sum += s * s }
        let rms = (sum / Float(m)).squareRoot() / 32768.0
        let listening = isListening
        DispatchQueue.main.async { self.onLevel?(listening ? min(rms * 4, 1) : 0) }

        guard listening else { return }
        sent += 1
        if sent % 50 == 0 {
            // per-channel raw RMS so we can see which channel carries the mic
            var parts: [String] = []
            for c in 0..<nc {
                var s: Float = 0
                for i in 0..<n { let v = chans[c][i]; s += v * v }
                parts.append(String(format: "%.3f", (s / Float(n)).squareRoot()))
            }
            print("[mic] \(sent) buf out-rms=\(String(format: "%.3f", rms)) raw[\(parts.joined(separator: " "))]")
        }
        ch[0].withMemoryRebound(to: UInt8.self, capacity: m * 2) {
            pending.append($0, count: m * 2)
        }
        while pending.count >= frameBytes {
            onMicFrame?(pending.prefix(frameBytes))
            pending.removeFirst(frameBytes)
        }
    }

    private func convert16k(_ input: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard let converter else { return nil }
        let ratio = 16000.0 / input.format.sampleRate
        let cap = AVAudioFrameCount(Double(input.frameLength) * ratio + 16)
        guard let out = AVAudioPCMBuffer(pcmFormat: mic16k, frameCapacity: cap) else { return nil }
        var fed = false
        var err: NSError?
        let status = converter.convert(to: out, error: &err) { _, inStatus in
            if fed { inStatus.pointee = .noDataNow; return nil }
            fed = true; inStatus.pointee = .haveData; return input
        }
        return status == .error ? nil : out
    }

    // MARK: - playback

    /// Schedule one clause of int16 PCM. A new `turn` flushes anything still
    /// queued from a superseded turn.
    func play(turn: Int, pcm: Data) {
        DispatchQueue.main.async {
            guard self.ready else { return }
            if turn != self.currentTurn {
                self.player.stop(); self.player.reset(); self.currentTurn = turn
            }
            guard let buf = self.buffer(from: pcm) else { return }
            self.player.scheduleBuffer(buf, completionHandler: nil)
            if !self.player.isPlaying { self.player.play() }
        }
    }

    /// Stop playback immediately (barge-in).
    func stopPlayback() {
        DispatchQueue.main.async {
            guard self.ready else { return }
            self.player.stop(); self.player.reset()
        }
    }

    private func buffer(from pcm: Data) -> AVAudioPCMBuffer? {
        let n = pcm.count / 2
        guard n > 0, let buf = AVAudioPCMBuffer(pcmFormat: tts24k, frameCapacity: AVAudioFrameCount(n))
        else { return nil }
        buf.frameLength = AVAudioFrameCount(n)
        let dst = buf.floatChannelData![0]
        pcm.withUnsafeBytes { raw in
            let src = raw.bindMemory(to: Int16.self)
            for i in 0..<n { dst[i] = Float(src[i]) / 32768.0 }
        }
        return buf
    }
}
