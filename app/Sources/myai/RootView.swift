import SwiftUI

struct RootView: View {
    @EnvironmentObject var launch: LaunchState

    var body: some View {
        ZStack {
            main
            if launch.starting { FirstRunOverlay() }
        }
    }

    private var main: some View {
        HStack(spacing: 0) {
            HomePanel()
                .frame(width: 320)
                .background(.ultraThinMaterial)
            Divider()
            ChatPanel()
                .frame(minWidth: 460)
        }
        .background(Color(nsColor: .textBackgroundColor))
    }
}

/// Shown until the backend reports its models are loaded.
///
/// The consent step is the point of this screen. Fennel's claim is that it runs
/// on your machine, so the one moment it needs the network is the moment that
/// most deserves asking — and the backend makes no request at all until the
/// button here is pressed.
private struct FirstRunOverlay: View {
    @EnvironmentObject var chat: ChatModel
    @State private var breathe = false

    var body: some View {
        VStack(spacing: 18) {
            FennelLogo(size: 54)
                .foregroundStyle(Theme.accent)
                .opacity(breathe ? 1 : 0.5)
                .animation(.easeInOut(duration: 1.6).repeatForever(autoreverses: true),
                           value: breathe)
            content
        }
        .frame(maxWidth: 420)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(.regularMaterial)
        .transition(.opacity)
        .onAppear { breathe = true }
    }

    @ViewBuilder private var content: some View {
        switch chat.setupPhase {
        case "needs_consent":
            VStack(spacing: 12) {
                Text("One-time setup").font(Theme.title(16, .semibold))
                Text("Fennel needs to download the models it runs on — about \(chat.setupSize). This is the only time it uses the internet unless you turn on daily updates or Wikipedia lookups.")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .lineSpacing(3)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Download \(chat.setupSize)") { chat.allowSetupDownload() }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                Text(chat.localModels.isEmpty ? "" : chat.localModels)
                    .font(.system(size: 10).monospaced())
                    .foregroundStyle(.tertiary)
                    .multilineTextAlignment(.center)
            }
            .padding(.horizontal, 28)

        case "loading", "checking":
            VStack(spacing: 6) {
                Text(chat.connected ? "Getting Fennel ready" : "Starting Fennel")
                    .font(Theme.title(15, .semibold))
                Text(chat.setupDetail.isEmpty ? "Loading the models it runs on."
                                              : chat.setupDetail)
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                if !chat.setupLoaded.isEmpty {
                    Text(chat.setupLoaded)
                        .font(.system(size: 10).monospacedDigit())
                        .foregroundStyle(.tertiary)
                }
                if chat.setupEta > 1 { Countdown(seconds: chat.setupEta) }
            }

        case "downloading":
            VStack(spacing: 10) {
                Text("Downloading").font(Theme.title(15, .semibold))
                ProgressView(value: chat.setupProgress)
                    .progressViewStyle(.linear)
                    .frame(width: 300)
                Text(chat.setupDetail.isEmpty ? "Starting…" : chat.setupDetail)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                Text("You can leave this running — it only happens once.")
                    .font(.system(size: 10)).foregroundStyle(.tertiary)
            }

        case "failed":
            VStack(spacing: 8) {
                Label("Setup failed", systemImage: "exclamationmark.triangle.fill")
                    .font(Theme.title(15, .semibold)).foregroundStyle(.orange)
                Text(chat.setupDetail)
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 28)

        default:
            VStack(spacing: 5) {
                Text(chat.connected ? "Getting Fennel ready" : "Starting Fennel")
                    .font(Theme.title(15, .semibold))
                Text(chat.setupDetail.isEmpty
                     ? "Loading the models it runs on."
                     : chat.setupDetail)
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
    }
}

/// Counts down from the backend's estimate, which is the duration the *last*
/// successful start actually took rather than a guess. Stops at "any moment now"
/// instead of going negative or pretending to be precise.
private struct Countdown: View {
    let seconds: Double
    @State private var start = Date()

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { ctx in
            let left = Int((seconds - ctx.date.timeIntervalSince(start)).rounded())
            Text(left > 0 ? "About \(left)s remaining" : "Any moment now…")
                .font(.system(size: 11, weight: .medium).monospacedDigit())
                .foregroundStyle(.secondary)
        }
        .onChange(of: seconds) { _, _ in start = Date() }
    }
}

// MARK: - Home

/// The voice surface: the orb, and whatever the assistant has put on screen.
private struct HomePanel: View {
    @EnvironmentObject var chat: ChatModel

    var body: some View {
        VStack(spacing: 0) {
            header
            orbSection
            if chat.cards.isEmpty { emptyState } else { cardList }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var header: some View {
        HStack(spacing: 6) {
            FennelLogo(size: 17).foregroundStyle(Theme.accent)
            Text("Fennel").font(Theme.title(15, .bold))
            Circle()
                .fill(chat.connected ? Color.green : Color.orange)
                .frame(width: 6, height: 6)
                .help(chat.connected ? "Connected" : "Backend not running")
            Spacer()
            SettingsMenu()
        }
        .padding(.horizontal, Theme.gutter)
        .padding(.top, 14)
    }

    /// The orb is the *voice* surface. During a text-only exchange the mic is
    /// shut, so lighting it amber for "thinking" made a closed microphone look
    /// active — the typing dots in the transcript are the right feedback there.
    /// It still turns green when speech is actually coming out of the speakers.
    private var orbState: AssistantState {
        if chat.listening || chat.state == .speaking { return chat.state }
        return .idle
    }

    private var orbSection: some View {
        VStack(spacing: 14) {
            VoiceOrb(state: orbState, listening: chat.listening, level: chat.level)
                .onTapGesture { chat.toggleListening() }
            Text(statusLine)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.secondary)
                .contentTransition(.opacity)
                .animation(.easeInOut(duration: 0.2), value: statusLine)
        }
        .padding(.vertical, 28)
    }

    private var statusLine: String {
        switch orbState {
        case .speaking: return "Speaking"
        case .thinking: return "Thinking"
        default: return chat.listening ? "Listening — tap to stop" : "Tap to talk"
        }
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Spacer()
            Image(systemName: "sparkles")
                .font(.system(size: 18))
                .foregroundStyle(.tertiary)
            Text("When Fennel does something for you — a reminder,\na timer, a lookup — the card lands here.")
                .font(.system(size: 11))
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
                .lineSpacing(2)
            Spacer()
        }
        .padding(.horizontal, Theme.gutter)
    }

    private var cardList: some View {
        ScrollView {
            LazyVStack(spacing: 8) {
                ForEach(chat.cards) { card in
                    HomeCardView(card: card, onDismiss: {
                        withAnimation(.easeOut(duration: 0.18)) { chat.dismiss(card) }
                    }, onUndo: { chat.undoDelete(card) })
                }
            }
            .padding(.horizontal, Theme.gutter)
            .padding(.bottom, 16)
        }
        .scrollIndicators(.never)
    }
}

/// The orb. Three concentric layers that each read at a glance: a soft halo that
/// breathes with mic level, a glass body, and the state colour. Motion is slow
/// and continuous — a fast or jittery orb makes the whole app feel anxious.
private struct VoiceOrb: View {
    let state: AssistantState
    let listening: Bool
    let level: Float

    @State private var pulse = false

    private var colors: [Color] { Theme.stateColors(state, listening: listening) }
    private var swell: CGFloat { 1 + CGFloat(min(level, 1)) * 0.28 }

    var body: some View {
        ZStack {
            // Halo — tracks the voice, so you can see it hearing you.
            Circle()
                .fill(RadialGradient(colors: [colors[0].opacity(0.34), .clear],
                                     center: .center, startRadius: 8, endRadius: 88))
                .frame(width: 176, height: 176)
                .scaleEffect(swell)
                .animation(.easeOut(duration: 0.12), value: level)

            // Slow breath, so it looks alive while idle.
            Circle()
                .strokeBorder(colors[0].opacity(0.22), lineWidth: 1)
                .frame(width: 128, height: 128)
                .scaleEffect(pulse ? 1.06 : 0.97)
                .animation(.easeInOut(duration: 2.6).repeatForever(autoreverses: true),
                           value: pulse)

            Circle()
                .fill(LinearGradient(colors: colors,
                                     startPoint: .topLeading, endPoint: .bottomTrailing))
                .frame(width: 92, height: 92)
                .shadow(color: colors[0].opacity(0.34), radius: 18, y: 6)
                .overlay(
                    Circle().strokeBorder(.white.opacity(0.20), lineWidth: 1))
                .scaleEffect(1 + CGFloat(min(level, 1)) * 0.05)
                .animation(.easeOut(duration: 0.12), value: level)

            Image(systemName: listening ? "waveform" : "mic.slash.fill")
                .font(.system(size: 24, weight: .medium))
                .foregroundStyle(.white)
                .shadow(radius: 2)
                .contentTransition(.symbolEffect(.replace))
        }
        .frame(width: 176, height: 176)
        .contentShape(Circle())
        .animation(.easeInOut(duration: 0.35), value: state)
        .onAppear { pulse = true }
    }
}

// MARK: - Settings

/// Everything network-related behind one control, off by default.
///
/// The wording is deliberate: this app's claim is that nothing leaves the
/// machine, so the exception has to be legible. The daily briefing reads a fixed
/// source list and reveals nothing about the user; search sends their actual
/// question. Two switches, because those are two different promises.
private struct SettingsMenu: View {
    @EnvironmentObject var chat: ChatModel
    @State private var open = false
    @State private var showLicenses = false

    private var online: Bool { chat.dailyUpdates || chat.webSearch }

    var body: some View {
        IconButton(symbol: online ? "wifi" : "wifi.slash",
                   help: "Network settings", active: online) { open.toggle() }
        .popover(isPresented: $open, arrowEdge: .bottom) {
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Network").font(Theme.title(14, .semibold))
                    Text("Everything else in Fennel runs on this Mac.")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                    if !chat.localModels.isEmpty {
                        Text(chat.localModels)
                            .font(.system(size: 10).monospaced())
                            .foregroundStyle(.tertiary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Button("Licences") { showLicenses = true }
                        .buttonStyle(.link)
                        .font(.system(size: 10))
                }

                Divider()

                toggleRow("Daily updates", isOn: Binding(
                    get: { chat.dailyUpdates },
                    set: { chat.dailyUpdates = $0; chat.saveSettings() }),
                    note: "Once a day, Fennel fetches headlines from BBC World, NPR and Ars Technica, plus your local forecast from Open-Meteo. It sends no information about you.")

                HStack(spacing: 6) {
                    TextField("City for weather", text: $chat.location)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(size: 12))
                        .onSubmit { chat.saveSettings() }
                    Button("Save") { chat.saveSettings() }
                        .controlSize(.small)
                }
                .disabled(!chat.dailyUpdates)

                if chat.dailyUpdates && chat.location.isEmpty {
                    Label("Enter your city to get a daily forecast from Open-Meteo.",
                          systemImage: "exclamationmark.circle.fill")
                        .font(.system(size: 10)).foregroundStyle(.orange)
                }

                Divider()

                toggleRow("Look things up on Wikipedia", isOn: Binding(
                    get: { chat.webSearch },
                    set: { chat.webSearch = $0; chat.saveSettings() }),
                    note: "Lets Fennel query Wikipedia's API when it doesn't know something or may be out of date. Your search terms are sent to Wikipedia; nothing else is.")
            }
            .padding(16)
            .frame(width: 320)
        }
        .sheet(isPresented: $showLicenses) { LicensesView() }
    }

    private func toggleRow(_ title: String, isOn: Binding<Bool>, note: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Toggle(title, isOn: isOn)
                .toggleStyle(.switch)
                .controlSize(.small)
                .font(.system(size: 12, weight: .medium))
            Text(note)
                .font(.system(size: 10)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - Chat

private struct ChatPanel: View {
    @EnvironmentObject var chat: ChatModel
    @State private var draft = ""
    @FocusState private var focused: Bool
    private let bottomID = "bottom"

    var body: some View {
        VStack(spacing: 0) {
            SessionBar()
            transcript
            composer
        }
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    if chat.messages.isEmpty { welcome }
                    ForEach(chat.messages) { MessageRow(message: $0) }
                    if chat.showTyping { TypingIndicator() }
                    Color.clear.frame(height: 1).id(bottomID)
                }
                .padding(.horizontal, 22)
                .padding(.vertical, 18)
            }
            .scrollIndicators(.never)
            .onChange(of: chat.messages.count) { _, _ in
                withAnimation(.easeOut(duration: 0.18)) {
                    proxy.scrollTo(bottomID, anchor: .bottom)
                }
            }
            .onChange(of: chat.messages.last?.text) { _, _ in
                proxy.scrollTo(bottomID, anchor: .bottom)
            }
        }
    }

    private var welcome: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Hello.").font(Theme.title(26, .bold))
            Text("Tap the orb and talk, or just type below.")
                .font(.system(size: 13)).foregroundStyle(.secondary)
        }
        .padding(.top, 40)
        .padding(.bottom, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var composer: some View {
        VStack(spacing: 0) {
            Divider()
            HStack(spacing: 10) {
                TextField("Message", text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.system(size: 13))
                    .lineLimit(1...5)
                    .focused($focused)
                    .onSubmit(send)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 9)
                    .background(Capsule().fill(.background.secondary))
                    .overlay(Capsule().strokeBorder(
                        Color.primary.opacity(focused ? 0.16 : 0.08), lineWidth: 1))

                Button(action: send) {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 13, weight: .bold))
                        .foregroundStyle(.white)
                        .frame(width: 30, height: 30)
                        .background(Circle().fill(
                            draft.isEmpty ? AnyShapeStyle(Color.secondary.opacity(0.35))
                                          : AnyShapeStyle(Theme.accent)))
                }
                .buttonStyle(.plain)
                .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty)
                .animation(.easeOut(duration: 0.15), value: draft.isEmpty)
            }
            .padding(.horizontal, 18)
            .padding(.top, 12)

            HStack {
                Toggle("Speak typed replies", isOn: $chat.speakTypedReplies)
                    .toggleStyle(.checkbox)
                    .controlSize(.small)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                Spacer()
            }
            .padding(.horizontal, 20)
            .padding(.top, 6)
            .padding(.bottom, 10)
        }
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        chat.sendUserText(text)
        draft = ""
    }
}

/// Assistant replies are plain text on the page; only the user gets a bubble.
/// Two bubble styles facing each other is heavier than this needs to be, and the
/// asymmetry makes it obvious at a glance who said what.
private struct MessageRow: View {
    let message: ChatMessage

    var body: some View {
        HStack {
            if message.role == .user { Spacer(minLength: 60) }
            Text(message.text)
                .font(.system(size: 13))
                .lineSpacing(2.5)
                .textSelection(.enabled)
                .padding(.horizontal, message.role == .user ? 13 : 0)
                .padding(.vertical, message.role == .user ? 9 : 0)
                .background {
                    if message.role == .user {
                        RoundedRectangle(cornerRadius: 16, style: .continuous)
                            .fill(Theme.accent)
                    }
                }
                .foregroundStyle(message.role == .user ? AnyShapeStyle(.white)
                                                       : AnyShapeStyle(.primary))
            if message.role == .assistant { Spacer(minLength: 60) }
        }
        .transition(.opacity.combined(with: .move(edge: .bottom)))
    }
}

/// Three dots while it thinks — the chat should never look frozen.
private struct TypingIndicator: View {
    @State private var phase = 0.0

    var body: some View {
        HStack(spacing: 4) {
            ForEach(0..<3, id: \.self) { i in
                Circle()
                    .fill(Color.secondary.opacity(0.5))
                    .frame(width: 5, height: 5)
                    .scaleEffect(phase == Double(i) ? 1.35 : 0.85)
                    .animation(.easeInOut(duration: 0.3), value: phase)
            }
        }
        .onAppear {
            Timer.scheduledTimer(withTimeInterval: 0.28, repeats: true) { t in
                DispatchQueue.main.async { phase = (phase + 1).truncatingRemainder(dividingBy: 3) }
                if phase.isNaN { t.invalidate() }
            }
        }
    }
}
