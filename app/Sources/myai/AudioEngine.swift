import AVFoundation

/// Capture and playback through AVAudioEngine with voice-processing I/O.
///
/// `setVoiceProcessingEnabled(true)` is why capture is native: it gives
/// acoustic echo cancellation, so the mic doesn't hear Kokoro through the
/// speakers. Playback has to go through the same engine's output for the
/// echo reference to work.
///
/// The engine runs in one of three modes and switching builds a brand-new
/// one, which is what keeps the microphone closed until the user taps.
///
/// Mic in  : 512-sample int16 mono @16 kHz frames (`onMicFrame`).
/// Audio out: int16 PCM @24 kHz, scheduled per clause (`play`).
final class AudioEngine {
    var onMicFrame: ((Data) -> Void)?     // one 1024-byte (512-sample) frame
    var onLevel: ((Float) -> Void)?       // 0…1 mic RMS, for the voice orb

    /// `.voice` opens the mic (and lights the system indicator); `.playback`
    /// must never touch `inputNode`, or macOS opens an input stream anyway.
    private enum Mode { case off, playback, voice }
    private var mode: Mode = .off
    private var wantMic = false
    private var micGranted = false
    /// Hardware echo cancellation, off by default: it can't be had without
    /// ducking, since AVAudioVoiceProcessingOtherAudioDuckingLevel offers
    /// Default/Min/Mid/Max and no "off". Fennel rejects its own voice in
    /// software too — a stricter barge-in gate and a transcript check — so
    /// this is the belt rather than the braces.
    var echoCancellation = UserDefaults.standard.bool(forKey: "echoCancellation")
    private var queued = 0                // clauses still scheduled on the player

    private var engine = AVAudioEngine()
    private var player = AVAudioPlayerNode()
    private var converter: AVAudioConverter?
    private var monoInFormat: AVAudioFormat?

    private let mic16k = AVAudioFormat(
        commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
    private let tts24k = AVAudioFormat(
        commonFormat: .pcmFormatFloat32, sampleRate: 24000, channels: 1, interleaved: false)!

    private let frameBytes = 512 * 2      // 512 int16 samples
    private var pending = Data()
    private var currentTurn = -1
    private var sent = 0   // debug: buffers captured while listening

    // MARK: - lifecycle

    /// Note what we are already allowed to do. Does not prompt and does not
    /// start the engine: nothing touches the mic until the user taps, so the
    /// permission prompt lands when the reason for it is obvious.
    func prepare() {
        micGranted = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
    }

    /// Rebuild the graph for `want`. A fresh AVAudioEngine each time, because
    /// disabling voice processing on a reused input node leaves the input
    /// stream open — and an engine that never touches `inputNode` is provably
    /// not recording.
    private func setMode(_ want: Mode) {
        guard want != mode else { return }
        teardown()
        guard want != .off else { return }

        let mic = (want == .voice) && micGranted
        do {
            engine = AVAudioEngine()
            player = AVAudioPlayerNode()
            if mic {
                if echoCancellation {
                    try engine.inputNode.setVoiceProcessingEnabled(true)
                    if #available(macOS 14.0, *) {
                        // Least available, but not none — see `echoCancellation`.
                        engine.inputNode.voiceProcessingOtherAudioDuckingConfiguration =
                            AVAudioVoiceProcessingOtherAudioDuckingConfiguration(
                                enableAdvancedDucking: false, duckingLevel: .min)
                    }
                }
                let inFormat = engine.inputNode.outputFormat(forBus: 0)
                // Voice processing inflates the mono mic to a 7-channel stream, and
                // AVAudioConverter's multichannel downmix yields silence. Take channel
                // 0 and convert that to 16 kHz.
                let mono = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                         sampleRate: inFormat.sampleRate,
                                         channels: 1, interleaved: false)!
                monoInFormat = mono
                converter = AVAudioConverter(from: mono, to: mic16k)
                engine.inputNode.installTap(onBus: 0, bufferSize: 1024, format: inFormat) {
                    [weak self] buffer, _ in self?.handleMic(buffer)
                }
                engine.prepare()
                try engine.start()
                // Attach playback after the engine is running. With voice processing on,
                // connecting a player first makes the VP output node fail to initialise
                // (-10875, surfacing as a CreateRecordingTap NSException).
                engine.attach(player)
                engine.connect(player, to: engine.mainMixerNode, format: tts24k)
            } else {
                // The opposite order here, and not a style choice: `prepare()` on a graph
                // with nothing attached raises an ObjC exception Swift cannot catch.
                // Connecting the player first gives the graph its output node.
                engine.attach(player)
                engine.connect(player, to: engine.mainMixerNode, format: tts24k)
                engine.prepare()
                try engine.start()
            }
            player.play()
            mode = want
            // Report the echo-cancellation state explicitly: if voice processing
            // fails to engage, nothing else in the logs would say so.
            let aec = mic ? (engine.inputNode.isVoiceProcessingEnabled ? "AEC on" : "AEC OFF") : "no mic"
            print("[audio] mode=\(want) mic=\(mic ? "OPEN" : "closed") \(aec)")
        } catch {
            print("audio engine start failed:", error)
            mode = .off
        }
    }

    private func teardown() {
        guard mode != .off else { return }
        player.stop()
        if mode == .voice { engine.inputNode.removeTap(onBus: 0) }
        engine.stop()
        queued = 0
        pending.removeAll()
        mode = .off
    }

    // MARK: - capture

    func startListening() {
        wantMic = true
        guard micGranted else {
            // First tap: prompt, then start only if still wanted — the user may
            // have toggled off again while the dialog was up.
            AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.micGranted = granted
                    if granted, self.wantMic { self.setMode(.voice) }
                }
            }
            return
        }
        DispatchQueue.main.async { self.setMode(.voice) }
    }

    /// Close the mic — but not mid-sentence: if the assistant is still speaking,
    /// the downgrade waits for the queue to drain (see `play`).
    func stopListening() {
        wantMic = false
        onLevel?(0)
        DispatchQueue.main.async { self.closeMicIfIdle() }
    }

    /// Go all the way to `.off`, not `.playback`: idle should hold no audio
    /// device at all. `play` spins a playback engine back up on demand.
    private func closeMicIfIdle() {
        guard mode == .voice, !wantMic, queued == 0 else { return }
        setMode(.off)
    }

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
        DispatchQueue.main.async { self.onLevel?(min(rms * 4, 1)) }

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
            if self.mode == .off { self.setMode(.playback) }   // typed reply, mic closed
            guard self.mode != .off else { return }
            if turn != self.currentTurn {
                self.player.stop(); self.player.reset()
                self.queued = 0
                self.currentTurn = turn
            }
            guard let buf = self.buffer(from: pcm) else { return }
            self.queued += 1
            self.player.scheduleBuffer(buf) { [weak self] in
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.queued = max(0, self.queued - 1)
                    self.closeMicIfIdle()
                }
            }
            if !self.player.isPlaying { self.player.play() }
        }
    }

    /// Stop playback immediately (barge-in).
    func stopPlayback() {
        DispatchQueue.main.async {
            guard self.mode != .off else { return }
            self.player.stop(); self.player.reset()
            self.queued = 0
            self.closeMicIfIdle()
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
