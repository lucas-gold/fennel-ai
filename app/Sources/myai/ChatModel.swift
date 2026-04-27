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

    func dismiss(_ card: HomeCard) {
        cards.removeAll { $0.id == card.id }
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
                switch card.kind {
                case .reminder:
                    try await EventKitBridge.addReminder(
                        title: card.title,
                        due: EventKitBridge.parseDate(args["due"] as? String),
                        notes: args["notes"] as? String)
                case .event:
                    guard let start = EventKitBridge.parseDate(args["start"] as? String) else {
                        throw EventKitBridge.DeniedError(what: "Calendar")
                    }
                    let end = EventKitBridge.parseDate(args["end"] as? String)
                        ?? start.addingTimeInterval(3600)
                    try await EventKitBridge.addEvent(
                        title: card.title, start: start, end: end,
                        location: args["location"] as? String)
                case .panel, .fact:
                    break               // the card itself is the whole effect
                }
                setStatus(id, .done)
                reply(id, ok: true, error: nil)
            } catch {
                let why = error.localizedDescription
                setStatus(id, .failed(why))
                reply(id, ok: false, error: why)
            }
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
