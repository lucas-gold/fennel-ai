import SwiftUI

struct RootView: View {
    @EnvironmentObject var launch: LaunchState

    var body: some View {
        ZStack {
            main
            if launch.starting { FirstRunOverlay() }
        }
    }

    /// Side by side while there's room; stacked once the window is narrow
    /// enough that a 300pt column would squeeze the conversation.
    private var main: some View {
        GeometryReader { geo in
            let wide = geo.size.width >= 760
            Group {
                if wide {
                    HStack(spacing: 0) {
                        HomePanel(stacked: false)
                            .frame(width: 300)
                            .background(.ultraThinMaterial)
                        Divider()
                        ChatPanel()
                    }
                } else {
                    VStack(spacing: 0) {
                        HomePanel(stacked: true)
                            .background(.ultraThinMaterial)
                        Divider()
                        ChatPanel()
                    }
                }
            }
            .animation(.easeInOut(duration: 0.2), value: wide)
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
        // The picker needs room for a list; every other phase is a short
        // paragraph and looks lost at that width.
        .frame(maxWidth: chat.setupPhase == "choose_model" ? 640 : 420)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(.regularMaterial)
        .transition(.opacity)
        .onAppear { breathe = true }
    }

    @ViewBuilder private var content: some View {
        switch chat.setupPhase {
        case "choose_model":
            ModelPicker()

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
            VStack(spacing: 12) {
                Text("Downloading \(chat.modelName.isEmpty ? "models" : chat.modelName)")
                    .font(Theme.title(16, .semibold))
                VStack(spacing: 6) {
                    ProgressView(value: chat.setupProgress)
                        .progressViewStyle(.linear)
                        .frame(width: 340)
                    HStack {
                        Text(chat.setupDetail.isEmpty ? "Starting…" : chat.setupDetail)
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                            .monospacedDigit()
                            .lineLimit(1)
                        Spacer(minLength: 12)
                        // The percentage, plainly. A bar alone gives no sense of
                        // whether a multi-gigabyte download is worth waiting out.
                        Text("\(Int(chat.setupProgress * 100))%")
                            .font(.system(size: 11, weight: .semibold).monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                    .frame(width: 340)
                }
                Text("Downloads once, then it works offline. You can leave this running.")
                    .font(.system(size: 10)).foregroundStyle(.tertiary)
                    .multilineTextAlignment(.center)
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
    var stacked = false

    var body: some View {
        VStack(spacing: 0) {
            header
            orbSection
            // Only live things live here now. Everything a turn *produced* sits
            // in the transcript, at the point it happened.
            if !chat.pinnedCards.isEmpty { pinned }
            if !stacked { Spacer(minLength: 0) }
        }
        .frame(maxWidth: .infinity)
    }

    private var pinned: some View {
        VStack(spacing: 8) {
            ForEach(chat.pinnedCards) { card in
                HomeCardView(card: card, onDismiss: {
                    withAnimation(.easeOut(duration: 0.18)) { chat.dismiss(card) }
                }, onUndo: { chat.undoDelete(card) })
            }
        }
        .padding(.horizontal, Theme.gutter)
        .padding(.bottom, 14)
    }

    private var header: some View {
        HStack(spacing: 6) {
            FennelLogo(size: 15).foregroundStyle(.tertiary)
            Text("Fennel")
                .font(Theme.title(13, .semibold))
                .foregroundStyle(.tertiary)
            // Only when something is wrong. A permanent green dot is chrome
            // reporting that nothing is happening.
            if !chat.connected {
                Circle().fill(Color.orange).frame(width: 6, height: 6)
                    .help("Backend not running")
            }
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
        .padding(.vertical, stacked ? 16 : 28)
    }

    private var statusLine: String {
        switch orbState {
        case .speaking: return "Speaking"
        case .thinking: return "Thinking"
        default: return chat.listening ? "Listening — tap to stop" : "Tap to talk"
        }
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
                .fill(RadialGradient(colors: [colors[0].opacity(0.30), .clear],
                                     center: .center, startRadius: 10, endRadius: 95))
                .frame(width: 190, height: 190)
                .scaleEffect(swell)
                .animation(.easeOut(duration: 0.12), value: level)

            // Concentric rings drifting outward. Staggered so they read as one
            // slow pulse rather than three things moving; each fades as it goes,
            // which is what stops it looking like a loading spinner.
            ForEach(0..<3, id: \.self) { i in
                let delay = Double(i) * 1.5
                Circle()
                    .strokeBorder(colors[0].opacity(0.26), lineWidth: 1)
                    .frame(width: 132, height: 132)
                    .scaleEffect(pulse ? 1.42 : 0.94)
                    .opacity(pulse ? 0 : 0.9)
                    .animation(.easeOut(duration: 4.5).repeatForever(autoreverses: false)
                                .delay(delay), value: pulse)
            }

            // No rim and no symbol inside. A hard white edge made it read as a
            // button, and the mic glyph put a piece of UI at the centre of the
            // one thing that should just look like presence. State is carried by
            // colour and motion; the word underneath says the rest.
            Circle()
                .fill(LinearGradient(colors: colors,
                                     startPoint: .topLeading, endPoint: .bottomTrailing))
                .frame(width: 104, height: 104)
                .shadow(color: colors[0].opacity(0.45), radius: 34, y: 0)
                .scaleEffect(1 + CGFloat(min(level, 1)) * 0.06)
                .animation(.easeOut(duration: 0.12), value: level)
                .overlay {
                    // Muted reads as absence, not as a symbol: the orb simply
                    // dims when the mic is shut.
                    if !listening && state == .idle {
                        Circle().fill(.black.opacity(0.34)).frame(width: 104, height: 104)
                    }
                }
        }
        .frame(width: 190, height: 190)
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

    private var online: Bool { chat.dailyUpdates || chat.lookups }

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
                    // Attribution belongs where the switches are, not only in a
                    // licences sheet: these are the parties data reaches.
                    Text("When on, Fennel uses Wikipedia (CC BY-SA), Open-Meteo (CC BY 4.0), BBC/NPR/Ars Technica feeds, and Ollama for web search.")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                        .fixedSize(horizontal: false, vertical: true)
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

                toggleRow("Echo cancellation", isOn: Binding(
                    get: { chat.echoCancellation },
                    set: { chat.setEchoCancellation($0) }),
                    note: "Stops Fennel hearing itself through your speakers, but macOS quietens your other audio while the mic is open — there is no way to have one without the other. Off by default; Fennel still filters its own voice in software.")

                Divider()

                toggleRow("Look things up", isOn: Binding(
                    get: { chat.lookups },
                    set: { chat.lookups = $0; chat.saveSettings() }),
                    note: "Lets Fennel look things up when it doesn't know something. Free, and it sends your search terms — and nothing else — to Wikipedia.")

                if chat.lookups { webSearchKey }
            }
            .padding(16)
            .frame(width: 320)
        }
        .sheet(isPresented: $showLicenses) { LicensesView() }
    }

    /// Optional upgrade: the user's own Ollama key turns on live web search.
    /// Deliberately presented as an extra rather than a requirement — Wikipedia
    /// stays the default so the app is fully useful with no account anywhere.
    @ViewBuilder private var webSearchKey: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                SecureField("Ollama API key (optional)", text: $chat.webKey)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 11))
                    .onSubmit { chat.saveWebKey(chat.webKey) }
                Button("Save") { chat.saveWebKey(chat.webKey) }
                    .controlSize(.small)
            }
            if chat.webPaused {
                Label("Out of allowance — paused for a few hours. Wikipedia still works.",
                      systemImage: "clock.badge.exclamationmark")
                    .font(.system(size: 10)).foregroundStyle(.orange)
            } else if chat.hasWebKey {
                Label("Live web search on — powered by Ollama.",
                      systemImage: "checkmark.circle.fill")
                    .font(.system(size: 10)).foregroundStyle(.green)
            } else {
                Text("Allows Fennel to perform live web searches with your own Ollama key — stored in your Keychain, never in its database. Free key from ollama.com/settings/keys.")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.leading, 2)
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
                LazyVStack(alignment: .leading, spacing: 16) {
                    if chat.messages.isEmpty && chat.inlineCards.isEmpty { welcome }
                    // Messages and cards share one counter, so the transcript is
                    // simply everything that happened, in order.
                    ForEach(entries, id: \.id) { entry in
                        switch entry.kind {
                        case .message(let m): MessageRow(message: m)
                        case .card(let c):    TranscriptCard(card: c)
                        }
                    }
                    if chat.showTyping { TypingIndicator() }
                    Color.clear.frame(height: 1).id(bottomID)
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 22)
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

    /// One ordered list of everything in the conversation.
    private struct Entry: Identifiable {
        enum Kind { case message(ChatMessage), card(HomeCard) }
        let id: String
        let seq: Int
        let kind: Kind
    }

    private var entries: [Entry] {
        let m = chat.messages.map { Entry(id: $0.id.uuidString, seq: $0.seq, kind: .message($0)) }
        let c = chat.inlineCards.map { Entry(id: $0.id, seq: $0.seq, kind: .card($0)) }
        return (m + c).sorted { $0.seq < $1.seq }
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
                // A rounded rectangle, not a capsule: a capsule's end caps are
                // half its height, so as the field grew past one line the curve
                // ate into the text. The cap of 5 lines also cut off a message
                // still being typed — 14 is roughly a third of the window and
                // scrolls past that.
                TextField("Message", text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.system(size: 13))
                    .lineLimit(1...14)
                    .focused($focused)
                    .onSubmit(send)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 9)
                    .background(RoundedRectangle(cornerRadius: 17, style: .continuous)
                        .fill(.background.secondary))
                    .overlay(RoundedRectangle(cornerRadius: 17, style: .continuous)
                        .strokeBorder(
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

            HStack(spacing: 12) {
                // Which model is answering, where you can see it while typing.
                // Clicking it reopens the picker; the running model stays in
                // memory, so coming back to the same one costs nothing.
                Button { chat.reopenModelPicker() } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "arrow.triangle.2.circlepath")
                            .font(.system(size: 9, weight: .semibold))
                        Text(chat.modelName.isEmpty ? "Model" : chat.modelName)
                            .font(.system(size: 10, weight: .medium))
                    }
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .background(Capsule().fill(Theme.bubble))
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .help("Switch model")

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

/// A tool result inside the conversation.
///
/// Searches are the exception and get a single line rather than a card: the
/// answer is already in the reply above, so a card would repeat it. What is
/// genuinely useful is where it came from and a way to open it.
private struct TranscriptCard: View {
    @EnvironmentObject var chat: ChatModel
    let card: HomeCard

    var body: some View {
        if card.kind == .search {
            sourceLine
        } else {
            HomeCardView(card: card, onDismiss: {
                withAnimation(.easeOut(duration: 0.18)) { chat.dismiss(card) }
            }, onUndo: { chat.undoDelete(card) })
            .frame(maxWidth: 420, alignment: .leading)
        }
    }

    private var sourceLine: some View {
        HStack(spacing: 5) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 9, weight: .semibold))
            Text(card.searchSource)
                .font(.system(size: 11, weight: .medium))
            if let first = card.subtitle?.split(separator: "·").last
                .map({ $0.trimmingCharacters(in: .whitespaces) }), !first.isEmpty {
                Text("· \(card.title)").font(.system(size: 11)).lineLimit(1)
            }
            if let url = card.searchLink {
                Link(destination: url) {
                    Image(systemName: "arrow.up.right")
                        .font(.system(size: 9, weight: .semibold))
                }
                .buttonStyle(.plain)
            }
        }
        .foregroundStyle(.tertiary)
        .padding(.top, -6)
    }
}

/// Assistant replies are plain text on the page; only the user gets a bubble.
/// Two bubble styles facing each other is heavier than this needs to be, and the
/// asymmetry makes it obvious at a glance who said what.
private struct MessageRow: View {
    let message: ChatMessage

    /// Render the model's markdown rather than printing its asterisks.
    ///
    /// `inlineOnlyPreservingWhitespace` is the important part: the default
    /// parser collapses newlines, which turns a list into one run-on line.
    /// Partial markdown arrives constantly while streaming — `**bol` — and that
    /// simply renders literally until the closing pair lands, so no special
    /// casing is needed for it. Speech is unaffected: `speakable()` strips the
    /// same syntax before Kokoro ever sees it.
    private var rendered: AttributedString {
        let options = AttributedString.MarkdownParsingOptions(
            allowsExtendedAttributes: false,
            interpretedSyntax: .inlineOnlyPreservingWhitespace,
            failurePolicy: .returnPartiallyParsedIfPossible)
        return (try? AttributedString(markdown: message.text, options: options))
            ?? AttributedString(message.text)
    }

    var body: some View {
        HStack {
            if message.role == .user { Spacer(minLength: 70) }
            Text(rendered)
                .font(.system(size: 13.5))
                .lineSpacing(3)
                .textSelection(.enabled)
                .padding(.horizontal, 15)
                .padding(.vertical, 11)
                .background {
                    RoundedRectangle(cornerRadius: 20, style: .continuous)
                        .fill(message.role == .user
                              ? AnyShapeStyle(Theme.accent)
                              : AnyShapeStyle(Theme.bubble))
                }
                .foregroundStyle(message.role == .user ? AnyShapeStyle(.white)
                                                       : AnyShapeStyle(.primary))
            if message.role == .assistant { Spacer(minLength: 70) }
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
