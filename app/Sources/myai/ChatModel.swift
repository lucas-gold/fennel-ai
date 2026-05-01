import Foundation
import SwiftUI

enum Role { case user, assistant }

struct ChatMessage: Identifiable {
    let id = UUID()
    let role: Role
    var text: String
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
    @Published var cards: [HomeCard] = []    // home surface, raised by tool calls
    @Published var sessions: [ChatSession] = []
    @Published var currentSessionID = 0
    /// Chats showing in the tab strip. One is the norm — the strip only appears
    /// once a second is opened, so the default shape stays "one ongoing chat".
    @Published var openTabs: [Int] = []
    /// Opt-in networking. Off by default: the app is offline unless asked.
    @Published var dailyUpdates = false
    @Published var location = ""
    @Published var webSearch = false

    private let client = WebSocketClient()
    private let audio = AudioEngine()
    private var activeTurn: Int?
    private var sawTurnEnd = true             // false while a turn is underway

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
        audio.prepare()
        client.connect()
    }

    func toggleListening() {
        listening.toggle()
        if listening { audio.startListening() } else { audio.stopListening() }
    }

    func sendUserText(_ text: String) {
        messages.append(ChatMessage(role: .user, text: text))
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
                messages.append(ChatMessage(role: .user, text: t))
            }
        case "token":
            let turn = msg["turn"] as? Int ?? -1
            appendToken(msg["text"] as? String ?? "", turn: turn)
        case "turn_end":
            sawTurnEnd = true
            activeTurn = nil
        case "tool":
            handleTool(msg)
        case "settings":
            dailyUpdates = msg["daily_updates"] as? Bool ?? false
            location = msg["location"] as? String ?? ""
            webSearch = msg["web_search"] as? Bool ?? false
        case "sessions":
            if let items = msg["items"] as? [[String: Any]] {
                sessions = items.compactMap(ChatSession.init(json:))
            }
            if let cur = msg["current"] as? Int { adoptCurrent(cur) }
        case "session_opened":
            if let id = msg["id"] as? Int { adoptCurrent(id) }
            let rows = msg["messages"] as? [[String: Any]] ?? []
            messages = rows.compactMap { row in
                guard let text = row["text"] as? String, !text.isEmpty,
                      let role = row["role"] as? String else { return nil }
                return ChatMessage(role: role == "user" ? .user : .assistant, text: text)
            }
            activeTurn = nil
            sawTurnEnd = true
        default:
            break
        }
    }

    private func adoptCurrent(_ id: Int) {
        currentSessionID = id
        if !openTabs.contains(id) { openTabs.append(id) }
    }

    // MARK: - chat sessions

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

    /// Push the networking preferences. The backend echoes back what it stored,
    /// so the toggle always reflects reality rather than intent.
    func saveSettings() {
        client.send(Wire.encode("settings", ["daily_updates": dailyUpdates,
                                             "location": location,
                                             "web_search": webSearch]))
    }

    func title(of id: Int) -> String {
        sessions.first { $0.id == id }?.title ?? "New chat"
    }

    /// ✕ on a reminder/event deletes the real Reminders/Calendar entry too —
    /// the card *is* the reminder. Because that's destructive on a one-click
    /// gesture, it leaves an Undo behind instead of vanishing.
    func dismiss(_ card: HomeCard) {
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
    private func handleTool(_ msg: [String: Any]) {
        let id = msg["id"] as? String ?? UUID().uuidString
        guard let name = msg["name"] as? String,
              let args = msg["args"] as? [String: Any],
              let card = HomeCard(id: id, name: name, args: args)
        else { return }

        withAnimation(.easeOut(duration: 0.18)) { cards.insert(card, at: 0) }

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
        if activeTurn != turn || messages.last?.role != .assistant {
            activeTurn = turn
            messages.append(ChatMessage(role: .assistant, text: chunk))
        } else {
            messages[messages.count - 1].text += chunk
        }
    }
}
