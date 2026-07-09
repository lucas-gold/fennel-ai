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
    @State private var showHistory = false
    /// Which row is asking "delete?". A saved conversation cannot be got back.
    @State private var confirmingDelete: Int?

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

                // A popover rather than a Menu: an NSMenu row cannot hold its own
                // buttons, so opening and deleting would need two separate lists.
                IconButton(symbol: "clock.arrow.circlepath", help: "Past chats") {
                    chat.refreshSessions()
                    confirmingDelete = nil
                    showHistory.toggle()
                }
                .popover(isPresented: $showHistory, arrowEdge: .bottom) {
                    historyList
                }

                IconButton(symbol: "square.and.pencil", help: "New chat") {
                    chat.newSession()
                }
                IconButton(symbol: "xmark", help: "Close this chat (kept in History)") {
                    chat.closeTab(chat.currentSessionID)
                }
                // Closing keeps the conversation; this is the one that does not.
                IconButton(symbol: "trash", help: "Delete this chat for good") {
                    chat.refreshSessions()
                    confirmingDelete = chat.currentSessionID
                    showHistory = true
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 9)

            if chat.openTabs.count > 1 { tabStrip }
            Divider()
        }
        .background(.ultraThinMaterial)
    }

    private var historyList: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Past chats")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 12).padding(.top, 10).padding(.bottom, 6)
            if chat.sessions.isEmpty {
                Text("No saved chats yet.")
                    .font(.system(size: 11)).foregroundStyle(.tertiary)
                    .padding(.horizontal, 12).padding(.bottom, 12)
            } else {
                ScrollView {
                    VStack(spacing: 2) {
                        ForEach(chat.sessions) { s in historyRow(s) }
                    }
                    .padding(.horizontal, 8).padding(.bottom, 8)
                }
                .frame(maxHeight: 320)
            }
        }
        .frame(width: 360)
    }

    private func historyRow(_ s: ChatSession) -> some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(s.title)
                    .font(.system(size: 12, weight: s.id == chat.currentSessionID
                                  ? .semibold : .regular))
                    .lineLimit(1)
                Text("\(s.subtitle) · \(s.count) messages")
                    .font(.system(size: 10)).foregroundStyle(.tertiary)
            }
            Spacer(minLength: 8)
            if confirmingDelete == s.id {
                Button("Delete") {
                    chat.deleteSession(s.id)
                    confirmingDelete = nil
                }
                .font(.system(size: 11, weight: .semibold)).foregroundStyle(Color.red)
                Button("Cancel") { confirmingDelete = nil }
                    .font(.system(size: 11)).foregroundStyle(Color.secondary)
            } else {
                Button(s.id == chat.currentSessionID ? "Open" : "Load") {
                    chat.openSession(s.id)
                    showHistory = false
                }
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(Theme.accentSolid)
                Button { confirmingDelete = s.id } label: {
                    Image(systemName: "trash").font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .frame(width: 20, height: 18)
                        .contentShape(Rectangle())
                }
            }
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 9).padding(.vertical, 7)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous)
            .fill(s.id == chat.currentSessionID ? Theme.bubble : Color.clear))
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
