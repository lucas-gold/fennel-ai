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
        default:
            break
        }
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
                let ext = try await performWrite(card)
                if let j = cards.firstIndex(where: { $0.id == card.id }) {
                    cards[j].externalID = ext
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
                let ext = try await performWrite(card)
                if let i = cards.firstIndex(where: { $0.id == id }) {
                    cards[i].externalID = ext
                    cards[i].status = .done
                }
                reply(id, ok: true, error: nil)
            } catch {
                let why = error.localizedDescription
                setStatus(id, .failed(why))
                reply(id, ok: false, error: why)
            }
        }
    }

    /// The real side effect, shared by the first write and by Undo.
    /// Returns the EventKit identifier where there is one.
    private func performWrite(_ card: HomeCard) async throws -> String? {
        switch card.kind {
        case .reminder:
            return try await EventKitBridge.addReminder(
                title: card.title,
                due: EventKitBridge.parseDate(card.args["due"] as? String),
                notes: card.args["notes"] as? String)
        case .event:
            guard let start = EventKitBridge.parseDate(card.args["start"] as? String) else {
                throw EventKitBridge.DeniedError(what: "Calendar")
            }
            let end = EventKitBridge.parseDate(card.args["end"] as? String)
                ?? start.addingTimeInterval(3600)
            return try await EventKitBridge.addEvent(
                title: card.title, start: start, end: end,
                location: card.args["location"] as? String)
        case .panel, .fact, .song:
            return nil                  // the card itself is the whole effect
        }
    }

    private func setStatus(_ id: String, _ status: HomeCard.Status) {
        guard let i = cards.firstIndex(where: { $0.id == id }) else { return }
        cards[i].status = status
    }

    private func reply(_ id: String, ok: Bool, error: String?) {
        var fields: [String: Any] = ["id": id, "ok": ok]
        if let error { fields["error"] = error }
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
