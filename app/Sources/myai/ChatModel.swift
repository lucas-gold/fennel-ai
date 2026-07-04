import Foundation
import SwiftUI

enum Role { case user, assistant }

struct ChatMessage: Identifiable {
    let id = UUID()
    let role: Role
    var text: String
    /// Position in the transcript. Messages and cards share one counter so the
    /// two can be interleaved in the order things actually happened.
    var seq: Int = 0
}

/// Mirrors the backend `state` control frame; also drives the voice orb.
enum AssistantState: String {
    case idle, listening, thinking, speaking

    var label: String {
        switch self {
        case .idle: return "Idle"
        case .listening: return "Listening"
        case .thinking: return "Thinking"
        case .speaking: return "Speaking"
        }
    }
}

@MainActor
final class ChatModel: ObservableObject {
    @Published var messages: [ChatMessage] = []
    @Published var state: AssistantState = .idle
    @Published var listening = false
    @Published var level: Float = 0          // 0…1 mic RMS for the orb
    @Published var connected = false         // backend reachable?
    @Published var speakTypedReplies = false // speak replies to typed messages too
    @Published var cards: [HomeCard] = []    // tool results, placed in the transcript

    /// A running timer stays pinned beside the orb rather than scrolling away
    /// with the conversation — a countdown you cannot see is not a countdown.
    var pinnedCards: [HomeCard] { cards.filter { $0.kind == .timer } }
    /// Everything else belongs in the timeline, at the point it happened.
    var inlineCards: [HomeCard] { cards.filter { $0.kind != .timer } }
    @Published var sessions: [ChatSession] = []
    @Published var currentSessionID = 0
    /// Chats showing in the tab strip. One is the norm — the strip only appears
    /// once a second is opened, so the default shape stays "one ongoing chat".
    @Published var openTabs: [Int] = []
    /// Opt-in networking. Off by default: the app is offline unless asked.
    @Published var dailyUpdates = false
    @Published var location = ""
    /// "Look things up" — Wikipedia, always free. The web tool additionally
    /// needs a key, which lives in the Keychain and never in the backend's DB.
    @Published var lookups = false
    /// The image model, as shown on its own row at the top of the picker.
    /// Not in the network panel: it is a model, and it belongs with the models.
    @Published var imageModel: ModelOption?
    @Published var imagesEnabled = false
    @Published var webKey = ""
    @Published var hasWebKey = false
    @Published var webPaused = false
    /// What's running locally, reported by the backend so the claim in Settings
    /// always matches the models actually loaded.
    @Published var localModels = ""
    /// First-run setup, driven by the backend's `setup` frames.
    @Published var setupPhase = "checking"
    @Published var setupDetail = ""
    @Published var setupSize = ""
    @Published var setupProgress = 0.0
    @Published var setupEta = 0.0
    @Published var setupDownloading = false
    @Published var setupLoaded = ""       // "1.2 GB of 3.5 GB in memory"
    /// The startup model picker, sent by the backend so the app holds no model
    /// knowledge of its own — add a model to config.MODELS and it appears here.
    @Published var setupModels: [ModelOption] = []
    @Published var setupCurrent = ""      // which one is preselected
    @Published var setupNote = ""         // e.g. why a delete was refused
    /// Which model is actually resident, as opposed to merely last chosen.
    @Published var loadedModelID = ""
    /// What Fennel holds besides the model — Whisper, Kokoro, the embedder and
    /// the Python runtime.
    @Published var overheadBytes = 0
    /// The last answer from checking a pasted model path.
    @Published var probe: [String: Any] = [:]
    @Published var probing = false
    /// The SwiftUI app's own resident size. A separate process from the
    /// backend, so it has to be added in rather than assumed included.
    @Published var appBytes = 0
    /// Subprocesses the backend spawned — image generation, which is where
    /// most of the memory goes while it runs.
    @Published var childBytes = 0
    /// Set while the language model is unloaded so something heavier can run.
    /// The composer and the mic are disabled for the duration — there is
    /// nothing to answer with, and a message typed into the void is worse than
    /// a disabled box that says why.
    @Published var busy = false
    @Published var busyDetail = ""
    /// The model in use, for the chip beside the composer.
    @Published var modelName = ""
    @Published var modelID = ""
    /// Live memory, pushed by the backend every couple of seconds. `modelBytes`
    /// is what MLX actually holds — RSS understates it, because weights are
    /// mapped and only count once touched.
    /// The language model's own share of MLX, not all of MLX. The two are far
    /// apart: Kokoro, Whisper and the embedder live there too, as does MLX's
    /// reusable buffer pool.
    @Published var modelBytes = 0
    @Published var systemUsedBytes = 0
    @Published var systemTotalBytes = 0
    /// Set once the backend has sent this session's history, so the window is
    /// never revealed as an empty chat that fills in a moment later.
    @Published var sessionLoaded = false
    /// Shown as a three-dot bubble, but only once a reply is genuinely slow —
    /// flashing it on every fast turn is worse than not having it.
    @Published var showTyping = false
    /// Hardware echo cancellation. Costs a little of your other audio's volume
    /// while the mic is open, which is why it is a choice rather than a given.
    @Published var echoCancellation = UserDefaults.standard.bool(forKey: "echoCancellation")

    private let client = WebSocketClient()
    private let audio = AudioEngine()
    private var activeTurn: Int?
    private var nextSeq = 0
    /// Next position in the transcript, for a message or a card.
    private func takeSeq() -> Int { nextSeq += 1; return nextSeq }
    private var sawTurnEnd = true             // false while a turn is underway
    private var awaitToken = 0                // invalidates a pending typing timer

    /// Start the one-second fuse for the typing indicator. Anything that ends
    /// the wait — a token, turn_end, a cancel — bumps `awaitToken` so a stale
    /// timer can't light the dots after the reply already arrived.
    private func beginAwaitingReply() {
        awaitToken += 1
        let mine = awaitToken
        showTyping = false
        Task {
            try? await Task.sleep(for: .seconds(1))
            if mine == awaitToken { showTyping = true }
        }
    }

    func connect() {
        client.onControl = { [weak self] msg in
            Task { @MainActor in self?.handle(msg) }
        }
        client.onAudio = { [weak self] turn, _, pcm in
            self?.audio.play(turn: turn, pcm: pcm)
        }
        audio.onMicFrame = { [weak self] data in self?.client.send(data) }
        audio.onLevel = { [weak self] lvl in
            Task { @MainActor in self?.level = lvl }
        }
        client.onStatus = { [weak self] up in
            Task { @MainActor in self?.connected = up }
        }
        webKey = Keychain.get("ollama_api_key")
        audio.echoCancellation = echoCancellation
        audio.prepare()
        client.connect()
    }

    func setEchoCancellation(_ on: Bool) {
        echoCancellation = on
        UserDefaults.standard.set(on, forKey: "echoCancellation")
        audio.echoCancellation = on
        // Takes effect on the next listen: the engine is rebuilt each time the
        // mic opens, and switching mid-capture would drop the current utterance.
    }

    func toggleListening() {
        listening.toggle()
        if listening { audio.startListening() } else { audio.stopListening() }
    }

    func sendUserText(_ text: String) {
        // Typing is an interruption too. Stop locally rather than waiting for
        // the backend's cancel to make the round trip.
        if state == .speaking || state == .thinking { audio.stopPlayback() }
        messages.append(ChatMessage(role: .user, text: text, seq: takeSeq()))
        beginAwaitingReply()
        client.send(Wire.encode("user_text", ["text": text, "speak": speakTypedReplies]))
    }

    // MARK: - incoming control frames

    private func handle(_ msg: [String: Any]) {
        guard let type = msg["type"] as? String else { return }
        switch type {
        case "state":
            if let v = msg["value"] as? String, let s = AssistantState(rawValue: v) {
                switch s {
                case .thinking, .speaking:
                    sawTurnEnd = false
                case .idle:
                    if !sawTurnEnd { audio.stopPlayback() }   // barge-in: turn was cut
                case .listening:
                    break
                }
                state = s
            }
        case "stt":                                    // what the mic was heard to say
            if let t = msg["text"] as? String, !t.isEmpty {
                messages.append(ChatMessage(role: .user, text: t, seq: takeSeq()))
                beginAwaitingReply()
            }
        case "token":
            awaitToken += 1                 // a reply is arriving; drop the dots
            showTyping = false
            let turn = msg["turn"] as? Int ?? -1
            appendToken(msg["text"] as? String ?? "", turn: turn)
        case "cancel":
            // The backend superseded the turn: stop mid-clause rather than
            // finishing the sentence we were already handed.
            audio.stopPlayback()
            activeTurn = nil
            awaitToken += 1
            showTyping = false
        case "turn_end":
            sawTurnEnd = true
            activeTurn = nil
            awaitToken += 1
            showTyping = false
        case "tool":
            handleTool(msg)
        case "setup":
            setupPhase = msg["phase"] as? String ?? "checking"
            setupDetail = msg["detail"] as? String ?? ""
            setupSize = msg["size"] as? String ?? ""
            setupProgress = msg["progress"] as? Double ?? setupProgress
            setupEta = msg["eta"] as? Double ?? setupEta
            setupDownloading = msg["downloading"] as? Bool ?? (setupPhase == "downloading")
            setupLoaded = msg["loaded"] as? String ?? ""
            if let rows = msg["models"] as? [[String: Any]] {
                setupModels = rows.compactMap(ModelOption.init(json:))
            }
            setupCurrent = msg["current"] as? String ?? setupCurrent
            loadedModelID = msg["loaded"] as? String ?? loadedModelID
            overheadBytes = msg["overhead_bytes"] as? Int ?? overheadBytes
            if let l = msg["llm_bytes"] as? Int { modelBytes = l }
            if let u = msg["system_used_bytes"] as? Int { systemUsedBytes = u }
            if let t = msg["system_total_bytes"] as? Int { systemTotalBytes = t }
            setupNote = msg["note"] as? String ?? ""
            if let row = msg["image_model"] as? [String: Any] {
                imageModel = ModelOption(json: row)
                imagesEnabled = row["enabled"] as? Bool ?? imagesEnabled
            }
        case "busy":
            busy = msg["busy"] as? Bool ?? false
            busyDetail = msg["detail"] as? String ?? ""
        case "card_update":
            applyCardUpdate(msg)
        case "model_probe":
            probing = false
            probe = msg
        case "memory":
            modelBytes = msg["llm_bytes"] as? Int ?? modelBytes
            overheadBytes = msg["overhead_bytes"] as? Int ?? overheadBytes
            systemUsedBytes = msg["system_used_bytes"] as? Int ?? 0
            systemTotalBytes = msg["system_total_bytes"] as? Int ?? 0
            appBytes = msg["app_bytes"] as? Int ?? appBytes
            childBytes = msg["child_bytes"] as? Int ?? childBytes
        case "settings":
            dailyUpdates = msg["daily_updates"] as? Bool ?? false
            location = msg["location"] as? String ?? ""
            lookups = msg["lookups"] as? Bool ?? false

            modelName = msg["model_name"] as? String ?? modelName
            modelID = msg["model_id"] as? String ?? modelID
            hasWebKey = msg["has_web_key"] as? Bool ?? false
            webPaused = msg["web_paused"] as? Bool ?? false
            localModels = msg["models"] as? String ?? ""
        case "sessions":
            if let items = msg["items"] as? [[String: Any]] {
                sessions = items.compactMap(ChatSession.init(json:))
            }
            if let cur = msg["current"] as? Int { adoptCurrent(cur) }
        case "split":
            // The model paused to use a tool. End this bubble and show the
            // dots: the next thing it says is a separate remark, arriving
            // after a wait the user should be able to see.
            activeTurn = nil
            showTyping = true
        case "session_opened":
            if let id = msg["id"] as? Int { adoptCurrent(id) }
            let rows = msg["messages"] as? [[String: Any]] ?? []
            messages = rows.compactMap { row in
                guard let text = row["text"] as? String, !text.isEmpty,
                      let role = row["role"] as? String else { return nil }
                return ChatMessage(role: role == "user" ? .user : .assistant,
                                   text: text, seq: takeSeq())
            }
            // Cards come back with the conversation. Rebuilt from what was
            // stored rather than replayed as tool calls: a restored reminder
            // must not create itself in Reminders a second time.
            cards = (msg["cards"] as? [[String: Any]] ?? []).compactMap { row in
                guard let id = row["id"] as? String,
                      let name = row["name"] as? String,
                      let args = row["args"] as? [String: Any],
                      var card = HomeCard(id: id, name: name, args: args)
                else { return nil }
                card.seq = takeSeq()
                if let path = args["path"] as? String { card.imagePath = path }
                switch args["status"] as? String {
                case "failed": card.status = .failed(args["detail"] as? String ?? "Failed")
                case "working": card.status = .failed("Interrupted")
                default: card.status = .done
                }
                return card
            }
            activeTurn = nil
            sawTurnEnd = true
            sessionLoaded = true
        default:
            break
        }
    }

    private func adoptCurrent(_ id: Int) {
        currentSessionID = id
        if !openTabs.contains(id) { openTabs.append(id) }
    }

    // MARK: - chat sessions

    /// The user pressed Download. Until this is sent the backend has made no
    /// network request at all.
    func allowSetupDownload() {
        setupPhase = "downloading"
        client.send(Wire.encode("setup_consent"))
    }

    /// Pick a model. The backend downloads it first if it has to, so this is the
    /// same call whether or not it is already on disk.
    func chooseModel(_ id: String) {
        setupPhase = "checking"
        setupDetail = ""
        client.send(Wire.encode("model_select", ["id": id]))
    }

    /// Remove a downloaded model. The backend answers with a fresh catalogue,
    /// so nothing is guessed here about what is left on disk.
    func deleteModel(_ id: String) {
        client.send(Wire.encode("model_delete", ["id": id]))
    }

    /// Back to the picker without leaving the app. The running model stays
    /// loaded — choosing the same one again should cost nothing.
    func reopenModelPicker() {
        client.send(Wire.encode("model_reopen"))
    }

    /// Abandon a load in progress and go back to the picker. The step already
    /// running on a worker thread has to finish first, so this is not instant.
    /// Free the resident model without choosing another.
    /// Turn picture generation on or off. The backend answers with a fresh
    /// picker, so the row always reflects what was actually stored.
    func setImagesEnabled(_ on: Bool) {
        imagesEnabled = on
        client.send(Wire.encode("image_toggle", ["enabled": on]))
    }

    /// Remove the downloaded image weights.
    func deleteImageModel() { client.send(Wire.encode("image_delete")) }

    /// Look a pasted repo over without downloading it.
    func probeModel(_ id: String) {
        probing = true
        probe = [:]
        client.send(Wire.encode("model_probe", ["id": id]))
    }

    /// Keep it, if the check was clean.
    func addModel(_ id: String) {
        probing = false
        probe = [:]
        client.send(Wire.encode("model_add", ["id": id]))
    }

    func unloadModel() {
        client.send(Wire.encode("model_unload"))
    }

    func cancelModelLoad() {
        client.send(Wire.encode("model_cancel"))
    }

    func newSession()            { client.send(Wire.encode("session_new")) }
    func openSession(_ id: Int)  { client.send(Wire.encode("session_open", ["id": id])) }
    func refreshSessions()       { client.send(Wire.encode("session_list")) }

    /// Delete removes the conversation for good; closing a tab only hides it.
    func deleteSession(_ id: Int) {
        openTabs.removeAll { $0 == id }
        client.send(Wire.encode("session_delete", ["id": id]))
    }

    /// Closing the last chat starts a fresh one rather than leaving an empty
    /// window — there is always exactly one conversation in front of you.
    func closeTab(_ id: Int) {
        openTabs.removeAll { $0 == id }
        if openTabs.isEmpty {
            messages = []
            newSession()
        } else if id == currentSessionID, let next = openTabs.last {
            openSession(next)
        }
    }

    /// Save the key to the Keychain and push it to the backend for this run.
    func saveWebKey(_ key: String) {
        webKey = key.trimmingCharacters(in: .whitespacesAndNewlines)
        Keychain.set(webKey, for: "ollama_api_key")
        saveSettings()
    }

    /// Push the networking preferences. The backend echoes back what it stored,
    /// so the toggle always reflects reality rather than intent.
    func saveSettings() {
        client.send(Wire.encode("settings", ["daily_updates": dailyUpdates,
                                             "location": location,
                                             "lookups": lookups,

                                             "web_key": webKey]))
    }

    func title(of id: Int) -> String {
        sessions.first { $0.id == id }?.title ?? "New chat"
    }

    /// ✕ on a reminder/event deletes the real Reminders/Calendar entry too —
    /// the card *is* the reminder. Because that's destructive on a one-click
    /// gesture, it leaves an Undo behind instead of vanishing.
    func dismiss(_ card: HomeCard) {
        // Stop the render too. Hiding the card while a minute of computation
        // carried on for a picture nobody will see is not a dismissal.
        if card.kind == .image, card.status == .working {
            client.send(Wire.encode("card_cancel", ["id": card.id]))
        }
        // Forget it on the backend as well, or it returns next time the chat is
        // opened — a dismissed card that comes back is not dismissed.
        client.send(Wire.encode("card_forget", ["id": card.id]))
        guard card.kind.writesToEventKit, let ext = card.externalID else {
            cards.removeAll { $0.id == card.id }
            return
        }
        Task {
            do {
                switch card.kind {
                case .reminder: try await EventKitBridge.deleteReminder(id: ext)
                case .event:    try await EventKitBridge.deleteEvent(id: ext)
                default:        break
                }
                setStatus(card.id, .deleted)
                try? await Task.sleep(for: .seconds(6))
                if cards.first(where: { $0.id == card.id })?.status == .deleted {
                    withAnimation(.easeOut(duration: 0.15)) {
                        cards.removeAll { $0.id == card.id }
                    }
                }
            } catch {
                setStatus(card.id, .failed("Couldn't delete: \(error.localizedDescription)"))
            }
        }
    }

    func undoDelete(_ card: HomeCard) {
        guard let i = cards.firstIndex(where: { $0.id == card.id }) else { return }
        cards[i].status = .working
        Task {
            do {
                let r = try await performWrite(card)
                if let j = cards.firstIndex(where: { $0.id == card.id }) {
                    cards[j].externalID = r.id
                    cards[j].status = .done
                }
            } catch {
                setStatus(card.id, .failed(error.localizedDescription))
            }
        }
    }

    // MARK: - tool calls (Stage 3)

    /// Raise the card immediately, then perform the real write. The backend
    /// waits ~2 s for our verdict so what it says out loud matches what
    /// happened — so always reply, success or failure.
    /// Progress and results for a card the *backend* owns — image generation is
    /// the only one, since every other tool is performed here in the app.
    private func applyCardUpdate(_ msg: [String: Any]) {
        guard let id = msg["id"] as? String,
              let i = cards.firstIndex(where: { $0.id == id }) else { return }
        if let d = msg["detail"] as? String { cards[i].detail = d }
        if let p = msg["progress"] as? Double { cards[i].progress = p }
        if let path = msg["path"] as? String { cards[i].imagePath = path }
        switch msg["status"] as? String {
        case "done":   withAnimation(.easeOut(duration: 0.25)) { cards[i].status = .done }
        case "failed": cards[i].status = .failed(msg["detail"] as? String ?? "Couldn't draw that")
        default:       cards[i].status = .working
        }
    }

    private func handleTool(_ msg: [String: Any]) {
        let id = msg["id"] as? String ?? UUID().uuidString
        guard let name = msg["name"] as? String,
              let args = msg["args"] as? [String: Any],
              var card = HomeCard(id: id, name: name, args: args)
        else { return }
        card.seq = takeSeq()
        withAnimation(.easeOut(duration: 0.18)) { cards.append(card) }
        if card.kind == .timer { startTimer(card) }
        // The backend draws this one and reports back via card_update; there is
        // no local write to perform and no tool_result to send.
        if card.kind == .image { return }

        Task {
            do {
                let r = try await performWrite(card)
                if let i = cards.firstIndex(where: { $0.id == id }) {
                    cards[i].externalID = r.id
                    cards[i].items = r.lines.isEmpty ? cards[i].items : r.lines
                    cards[i].status = .done
                }
                reply(id, ok: true, error: nil, data: r.data)
            } catch {
                let why = error.localizedDescription
                setStatus(id, .failed(why))
                reply(id, ok: false, error: why)
            }
        }
    }

    /// The real side effect, shared by the first write and by Undo.
    /// `id` is the EventKit identifier where there is one; `data` is what a
    /// read-style tool sends back for the model to speak from.
    private func performWrite(
        _ card: HomeCard
    ) async throws -> (id: String?, data: [String: Any]?, lines: [String]) {
        switch card.kind {
        // Drawn by the backend, which reports progress over card_update. It is
        // filtered out before this is reached; the case is here so the switch
        // stays exhaustive rather than silently falling through.
        case .image:
            return (nil, nil, [])
        case .reminder:
            let ext = try await EventKitBridge.addReminder(
                title: card.title,
                due: EventKitBridge.parseDate(card.args["due"] as? String),
                notes: card.args["notes"] as? String)
            return (ext, nil, [])
        case .event:
            guard let start = EventKitBridge.parseDate(card.args["start"] as? String) else {
                throw EventKitBridge.DeniedError(what: "Calendar")
            }
            let end = EventKitBridge.parseDate(card.args["end"] as? String)
                ?? start.addingTimeInterval(3600)
            let ext = try await EventKitBridge.addEvent(
                title: card.title, start: start, end: end,
                location: card.args["location"] as? String)
            return (ext, nil, [])
        case .agenda:
            let a = try await EventKitBridge.agenda(range: card.args["range"] as? String ?? "today")
            return (nil, ["items": a.lines, "count": a.count], a.lines)
        case .app:
            try await MacActions.openApp(named: card.args["name"] as? String ?? "")
            return (nil, nil, [])
        case .shortcut:
            try await MacActions.runShortcut(named: card.args["name"] as? String ?? "")
            return (nil, nil, [])
        case .newShortcut:
            // Opening the signed file makes Shortcuts show its Add sheet with
            // every action listed — the user approves before it exists.
            guard let path = card.args["path"] as? String else {
                throw MacActions.Failure(message: "the shortcut file is missing")
            }
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
            return (nil, nil, [])
        case .panel, .fact, .song, .timer, .link, .search:
            return (nil, nil, [])       // the card itself is the whole effect
        }
    }

    /// Fire the timer from the model, not the countdown view: the card lives in
    /// a LazyVStack and may not be rendered when it runs out, and a timer that
    /// only goes off while you happen to be looking at it is not a timer.
    private func startTimer(_ card: HomeCard) {
        guard let ends = card.endsAt else { return }
        Task {
            let delay = ends.timeIntervalSinceNow
            if delay > 0 { try? await Task.sleep(for: .seconds(delay)) }
            guard cards.contains(where: { $0.id == card.id }) else { return }  // dismissed
            NSSound.beep()
            NSApp.requestUserAttention(.criticalRequest)   // bounce the Dock icon
        }
    }

    private func setStatus(_ id: String, _ status: HomeCard.Status) {
        guard let i = cards.firstIndex(where: { $0.id == id }) else { return }
        cards[i].status = status
    }

    private func reply(_ id: String, ok: Bool, error: String?, data: [String: Any]? = nil) {
        var fields: [String: Any] = ["id": id, "ok": ok]
        if let error { fields["error"] = error }
        if let data { fields["data"] = data }
        client.send(Wire.encode("tool_result", fields))
    }

    private func appendToken(_ chunk: String, turn: Int) {
        showTyping = false
        if activeTurn != turn || messages.last?.role != .assistant {
            activeTurn = turn
            messages.append(ChatMessage(role: .assistant, text: chunk, seq: takeSeq()))
        } else {
            messages[messages.count - 1].text += chunk
        }
    }
}
