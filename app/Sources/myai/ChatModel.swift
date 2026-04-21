import Foundation

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
        audio.prepare()
        client.connect()
    }

    func toggleListening() {
        listening.toggle()
        if listening { audio.startListening() } else { audio.stopListening() }
    }

    func sendUserText(_ text: String) {
        messages.append(ChatMessage(role: .user, text: text))
        client.send(Wire.encode("user_text", ["text": text]))
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
        default:
            break
        }
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
