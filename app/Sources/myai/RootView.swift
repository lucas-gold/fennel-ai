import SwiftUI

struct RootView: View {
    var body: some View {
        HSplitView {
            HomePanel().frame(minWidth: 240, idealWidth: 280)
            ChatPanel().frame(minWidth: 420)
        }
    }
}

/// Reactive home surface. Stage 3 fills this with reminder/calendar/pinned
/// cards driven by backend tool calls; for now it shows the assistant state
/// (the seed of the voice orb).
private struct HomePanel: View {
    @EnvironmentObject var chat: ChatModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 8) {
                Circle()
                    .fill(chat.state == .idle ? Color.secondary : Color.accentColor)
                    .frame(width: 12, height: 12)
                Text(chat.state.label).font(.headline)
            }
            Text("Home").font(.title2).bold()
            Text("Reminders, calendar, and pinned panels will appear here (Stage 3).")
                .font(.callout).foregroundStyle(.secondary)
            Spacer()
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

private struct ChatPanel: View {
    @EnvironmentObject var chat: ChatModel
    @State private var draft = ""

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(chat.messages) { MessageRow(message: $0) }
                }
                .padding()
            }
            Divider()
            HStack {
                TextField("Message…", text: $draft)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(send)
                Button("Send", action: send).disabled(draft.isEmpty)
            }
            .padding()
        }
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        chat.sendUserText(text)
        draft = ""
    }
}

private struct MessageRow: View {
    let message: ChatMessage

    var body: some View {
        HStack {
            if message.role == .user { Spacer(minLength: 40) }
            Text(message.text)
                .textSelection(.enabled)
                .padding(10)
                .background(
                    message.role == .user
                        ? Color.accentColor.opacity(0.20)
                        : Color.gray.opacity(0.15)
                )
                .clipShape(RoundedRectangle(cornerRadius: 10))
            if message.role == .assistant { Spacer(minLength: 40) }
        }
    }
}
