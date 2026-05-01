import SwiftUI

/// One saved conversation, as listed by the backend.
struct ChatSession: Identifiable, Equatable {
    let id: Int
    let title: String
    let updated: Date
    let count: Int

    init?(json: [String: Any]) {
        guard let id = json["id"] as? Int else { return nil }
        self.id = id
        let raw = (json["title"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        title = raw.isEmpty ? "New chat" : raw
        updated = Date(timeIntervalSince1970: json["updated"] as? Double ?? 0)
        count = json["count"] as? Int ?? 0
    }

    var subtitle: String {
        let f = DateFormatter()
        f.doesRelativeDateFormatting = true
        f.dateStyle = .short
        f.timeStyle = .short
        return f.string(from: updated)
    }
}

/// The chat's header: which conversation you're in, and the ways out of it.
///
/// One ongoing chat is the intended shape, so the tab strip stays hidden until
/// a second chat is actually open; past conversations live behind the History
/// menu rather than accumulating as tabs.
struct SessionBar: View {
    @EnvironmentObject var chat: ChatModel

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 4) {
                Text(chat.title(of: chat.currentSessionID))
                    .font(Theme.title(13, .semibold))
                    .lineLimit(1)
                if chat.state == .speaking {
                    Image(systemName: "speaker.wave.2.fill")
                        .font(.system(size: 9))
                        .foregroundStyle(.secondary)
                        .transition(.opacity)
                }
                Spacer(minLength: 8)

                Menu {
                    if chat.sessions.isEmpty { Text("No saved chats") }
                    ForEach(chat.sessions) { s in
                        Button {
                            chat.openSession(s.id)
                        } label: {
                            Text(s.id == chat.currentSessionID ? "\u{2713} \(s.title)" : s.title)
                            Text(s.subtitle)
                        }
                    }
                    if !chat.sessions.isEmpty {
                        Divider()
                        Menu("Delete") {
                            ForEach(chat.sessions) { s in
                                Button(s.title, role: .destructive) { chat.deleteSession(s.id) }
                            }
                        }
                    }
                } label: {
                    Image(systemName: "clock.arrow.circlepath")
                        .font(.system(size: 12, weight: .medium))
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .frame(width: 22)
                .help("Past chats")
                .onTapGesture { chat.refreshSessions() }

                IconButton(symbol: "square.and.pencil", help: "New chat") {
                    chat.newSession()
                }
                IconButton(symbol: "xmark", help: "Close this chat (kept in History)") {
                    chat.closeTab(chat.currentSessionID)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 9)

            if chat.openTabs.count > 1 { tabStrip }
            Divider()
        }
        .background(.ultraThinMaterial)
    }

    private var tabStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(chat.openTabs, id: \.self) { id in
                    let active = id == chat.currentSessionID
                    HStack(spacing: 5) {
                        Text(chat.title(of: id))
                            .lineLimit(1).frame(maxWidth: 120, alignment: .leading)
                        Button { chat.closeTab(id) } label: {
                            Image(systemName: "xmark").font(.system(size: 7, weight: .bold))
                        }
                        .buttonStyle(.plain).foregroundStyle(.secondary)
                    }
                    .font(.system(size: 11, weight: active ? .semibold : .regular))
                    .padding(.horizontal, 9).padding(.vertical, 4)
                    .background(
                        Capsule().fill(active ? Color.accentColor.opacity(0.16)
                                              : Color.primary.opacity(0.05)))
                    .contentShape(Capsule())
                    .onTapGesture { if !active { chat.openSession(id) } }
                }
            }
            .padding(.horizontal, 16).padding(.bottom, 8)
        }
    }
}
