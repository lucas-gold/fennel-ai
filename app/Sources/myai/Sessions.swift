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
            HStack(spacing: 8) {
                Text(chat.title(of: chat.currentSessionID))
                    .font(.subheadline.weight(.medium))
                    .lineLimit(1)
                Spacer(minLength: 8)

                Menu {
                    if chat.sessions.isEmpty {
                        Text("No saved chats")
                    }
                    ForEach(chat.sessions) { s in
                        Button {
                            chat.openSession(s.id)
                        } label: {
                            Text(s.id == chat.currentSessionID
                                 ? "✓ \(s.title)" : s.title)
                            Text(s.subtitle)
                        }
                    }
                    if !chat.sessions.isEmpty {
                        Divider()
                        Menu("Delete") {
                            ForEach(chat.sessions) { s in
                                Button(s.title, role: .destructive) {
                                    chat.deleteSession(s.id)
                                }
                            }
                        }
                    }
                } label: {
                    Label("History", systemImage: "clock.arrow.circlepath")
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .onTapGesture { chat.refreshSessions() }

                Button(action: chat.newSession) {
                    Image(systemName: "square.and.pencil")
                }
                .buttonStyle(.plain)
                .help("New chat")

                Button { chat.closeTab(chat.currentSessionID) } label: {
                    Image(systemName: "xmark.circle")
                }
                .buttonStyle(.plain)
                .help("Close this chat (it stays in History)")
            }
            .font(.callout)
            .padding(.horizontal, 12)
            .padding(.vertical, 7)

            if chat.openTabs.count > 1 { tabStrip }
            Divider()
        }
    }

    private var tabStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(chat.openTabs, id: \.self) { id in
                    let active = id == chat.currentSessionID
                    HStack(spacing: 5) {
                        Text(chat.title(of: id))
                            .lineLimit(1).frame(maxWidth: 130, alignment: .leading)
                        Button { chat.closeTab(id) } label: {
                            Image(systemName: "xmark").font(.system(size: 8, weight: .bold))
                        }
                        .buttonStyle(.plain).foregroundStyle(.secondary)
                    }
                    .font(.caption)
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .background(active ? Color.accentColor.opacity(0.18) : Color.gray.opacity(0.10))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                    .contentShape(Rectangle())
                    .onTapGesture { if !active { chat.openSession(id) } }
                }
            }
            .padding(.horizontal, 12).padding(.bottom, 6)
        }
    }
}
