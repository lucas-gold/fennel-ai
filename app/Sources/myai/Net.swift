import Foundation

/// JSON control-frame codec. Mirrors `backend/protocol.py` — keep in sync.
/// (Named `Wire`, not `Protocol`, to avoid Foundation's `Protocol` class.)
enum Wire {
    static func encode(_ type: String, _ fields: [String: Any] = [:]) -> String {
        var obj = fields
        obj["type"] = type
        guard let data = try? JSONSerialization.data(withJSONObject: obj),
              let s = String(data: data, encoding: .utf8)
        else { return "{\"type\":\"\(type)\"}" }
        return s
    }

    static func decode(_ raw: String) -> [String: Any]? {
        guard let data = raw.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return obj
    }
}

/// Thin URLSession WebSocket client to the local backend. Text frames are
/// control JSON; binary frames (audio) arrive from Stage 2 onward.
final class WebSocketClient {
    private let url = URL(string: "ws://127.0.0.1:8420")!
    private var task: URLSessionWebSocketTask?

    /// Both called off the main thread; hop to the main actor in the handler.
    var onControl: (([String: Any]) -> Void)?
    var onAudio: ((_ turn: Int, _ seq: Int, _ pcm: Data) -> Void)?

    func connect() {
        let task = URLSession(configuration: .default).webSocketTask(with: url)
        self.task = task
        task.resume()
        receive()
    }

    func send(_ text: String) {
        task?.send(.string(text)) { error in
            if let error { print("ws send error:", error) }
        }
    }

    func send(_ data: Data) {          // mic frames
        task?.send(.data(data)) { error in
            if let error { print("ws send(data) error:", error) }
        }
    }

    private func receive() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                print("ws receive error:", error) // Stage 2: reconnect/backoff
            case .success(let message):
                switch message {
                case .string(let text):
                    if let msg = Wire.decode(text) { self.onControl?(msg) }
                case .data(let data):
                    self.handleAudio(data)   // ">II" header + int16 PCM
                @unknown default:
                    break
                }
                self.receive()
            }
        }
    }

    private func handleAudio(_ data: Data) {
        guard data.count > 8 else { return }
        let turn = data.prefix(4).reduce(0) { ($0 << 8) | Int($1) }
        let seq = data.subdata(in: 4..<8).reduce(0) { ($0 << 8) | Int($1) }
        onAudio?(turn, seq, data.subdata(in: 8..<data.count))
    }
}
