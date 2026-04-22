import SwiftUI

struct RootView: View {
    var body: some View {
        HSplitView {
            HomePanel().frame(minWidth: 240, idealWidth: 280)
            ChatPanel().frame(minWidth: 420)
        }
    }
}

/// Reactive home surface: the voice orb + (Stage 3) reminder/calendar/pinned
/// cards driven by backend tool calls.
private struct HomePanel: View {
    @EnvironmentObject var chat: ChatModel

    var body: some View {
        VStack(spacing: 18) {
            HStack(spacing: 8) {
                Text("Home").font(.title2).bold()
                Spacer()
                Circle().fill(chat.connected ? Color.green : Color.red)
                    .frame(width: 8, height: 8)
                Text(chat.connected ? "Backend" : "Offline")
                    .font(.caption).foregroundStyle(.secondary)
            }

            Spacer()
            VoiceOrb(state: chat.state, listening: chat.listening, level: chat.level)
                .onTapGesture { chat.toggleListening() }
            Text(chat.listening ? "Listening — tap to stop"
                                : (chat.state == .idle ? "Tap to talk" : chat.state.label))
                .font(.callout).foregroundStyle(.secondary)
            Spacer()

            Text("Reminders, calendar, and pinned panels will appear here (Stage 3).")
                .font(.footnote).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// The on-screen voice interface: a halo that swells with mic level and recolors
/// by assistant state (idle/listening/thinking/speaking).
private struct VoiceOrb: View {
    let state: AssistantState
    let listening: Bool
    let level: Float

    private var color: Color {
        switch state {
        case .speaking: return .green
        case .thinking: return .orange
        default: return listening ? .accentColor : .secondary
        }
    }

    var body: some View {
        ZStack {
            Circle()
                .fill(color.opacity(0.22))
                .frame(width: 120, height: 120)
                .scaleEffect(1 + CGFloat(level) * 0.6)
                .animation(.easeOut(duration: 0.10), value: level)
            Circle().fill(color).frame(width: 62, height: 62)
            Image(systemName: listening ? "mic.fill" : "mic.slash.fill")
                .font(.system(size: 22, weight: .semibold))
                .foregroundStyle(.white)
        }
        .frame(width: 140, height: 140)
        .contentShape(Circle())
    }
}

private struct ChatPanel: View {
    @EnvironmentObject var chat: ChatModel
    @State private var draft = ""
    private let bottomID = "bottom"

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        ForEach(chat.messages) { MessageRow(message: $0) }
                        Color.clear.frame(height: 1).id(bottomID)
                    }
                    .padding()
                }
                .onChange(of: chat.messages.count) { _, _ in
                    withAnimation(.easeOut(duration: 0.15)) { proxy.scrollTo(bottomID, anchor: .bottom) }
                }
                .onChange(of: chat.messages.last?.text) { _, _ in
                    proxy.scrollTo(bottomID, anchor: .bottom)
                }
            }
            Divider()
            HStack {
                TextField("Message…", text: $draft)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(send)
                Button("Send", action: send).disabled(draft.isEmpty)
            }
            .padding(.horizontal).padding(.top, 8)
            Toggle("Speak typed replies", isOn: $chat.speakTypedReplies)
                .toggleStyle(.checkbox)
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.horizontal).padding(.bottom, 8)
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
