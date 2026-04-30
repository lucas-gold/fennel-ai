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
                SettingsMenu()
            }

            VoiceOrb(state: chat.state, listening: chat.listening, level: chat.level)
                .onTapGesture { chat.toggleListening() }
            Text(chat.listening ? "Listening — tap to stop"
                                : (chat.state == .idle ? "Tap to talk" : chat.state.label))
                .font(.callout).foregroundStyle(.secondary)

            Divider()

            if chat.cards.isEmpty {
                Spacer()
                Text("Ask for a reminder, an event, or a list — it shows up here.")
                    .font(.footnote).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                Spacer()
            } else {
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(chat.cards) { card in
                            HomeCardView(card: card, onDismiss: {
                                withAnimation(.easeOut(duration: 0.15)) { chat.dismiss(card) }
                            }, onUndo: { chat.undoDelete(card) })
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// Everything network-related lives behind this one control, off by default.
///
/// The wording is deliberate: this app's whole claim is that nothing leaves the
/// machine, so the exception has to be legible. The daily briefing fetches a
/// fixed list of feeds and reveals nothing about the user — which is why it is
/// its own switch, and why the city is typed rather than read from CoreLocation.
private struct SettingsMenu: View {
    @EnvironmentObject var chat: ChatModel

    var body: some View {
        Menu {
            Toggle("Daily updates", isOn: Binding(
                get: { chat.dailyUpdates },
                set: { chat.dailyUpdates = $0; chat.saveSettings() }))
            Text(chat.dailyUpdates
                 ? "Fetches weather and headlines once a day."
                 : "Off — the app makes no network requests.")
            Divider()
            Text("Weather city")
            TextField("e.g. Toronto", text: $chat.location)
                .onSubmit { chat.saveSettings() }
        } label: {
            Image(systemName: chat.dailyUpdates ? "wifi" : "wifi.slash")
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .help("Network settings")
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
            SessionBar()
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
