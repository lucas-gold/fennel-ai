import SwiftUI

/// A card raised on the home screen by a backend tool call (D-HOME). The card
/// is the visible half of the tool call; `EventKitBridge` is the real half.
struct HomeCard: Identifiable {
    enum Kind: String {
        case reminder = "set_reminder"
        case event = "add_event"
        case panel = "show_panel"
        case fact = "set_fact"
        case song = "recommend_song"
        case timer = "set_timer"
        case agenda = "agenda"
        case link = "open_link"
        case search = "search_web"
        case app = "open_app"
        case shortcut = "run_shortcut"
        case newShortcut = "create_shortcut"

        var icon: String {
            switch self {
            case .reminder: return "checklist"
            case .event: return "calendar"
            case .panel: return "square.text.square"
            case .fact: return "brain"
            case .song: return "music.note"
            case .timer: return "timer"
            case .agenda: return "list.bullet.rectangle"
            case .link: return "safari"
            case .search: return "magnifyingglass"
            case .app: return "app.badge"
            case .shortcut: return "bolt.fill"
            case .newShortcut: return "wand.and.stars"
            }
        }

        var tint: Color {
            switch self {
            case .reminder: return .orange
            case .event: return .blue
            case .panel: return .purple
            case .fact: return .teal
            case .song: return .pink
            case .timer: return .red
            case .agenda: return .indigo
            case .link: return .cyan
            case .search: return .mint
            case .app: return .gray
            case .shortcut: return .yellow
            case .newShortcut: return .yellow
            }
        }

        /// Kinds backed by a real entry outside the app, which dismissing must
        /// therefore delete rather than just hide.
        var writesToEventKit: Bool { self == .reminder || self == .event }
    }

    /// Whether the real-world write actually landed. Shown on the card so a
    /// permission failure is visible, not just spoken once and lost.
    enum Status: Equatable {
        case working, done, failed(String), deleted
    }

    let id: String
    let kind: Kind
    let title: String
    var subtitle: String?
    var body: String?
    var items: [String] = []
    var status: Status = .working
    /// Where this sits in the transcript, shared with messages.
    var seq: Int = 0
    /// The EventKit identifier once written, so ✕ can remove the real entry.
    var externalID: String?
    /// The normalized tool arguments, kept so Undo can re-create the entry.
    let args: [String: Any]

    /// Build from a `tool` control frame. Returns nil for a tool with no card.
    init?(id: String, name: String, args: [String: Any]) {
        guard let kind = Kind(rawValue: name) else { return nil }
        self.id = id
        self.kind = kind
        self.args = args
        switch kind {
        case .reminder:
            title = args["title"] as? String ?? "Reminder"
            subtitle = EventKitBridge.parseDate(args["due"] as? String).map(Self.when)
            body = args["notes"] as? String
        case .event:
            title = args["title"] as? String ?? "Event"
            let start = EventKitBridge.parseDate(args["start"] as? String)
            subtitle = start.map(Self.when)
            body = args["location"] as? String
        case .panel:
            title = args["title"] as? String ?? "Note"
            body = args["body"] as? String
            items = args["items"] as? [String] ?? []
            status = .done              // nothing to write; it exists by being shown
        case .fact:
            title = args["value"] as? String ?? ""
            subtitle = "Remembered"
            status = .done
        case .song:
            title = args["title"] as? String ?? ""
            subtitle = args["artist"] as? String
            body = args["why"] as? String
            status = .done
        case .timer:
            title = args["label"] as? String ?? "Timer"
            endsAt = EventKitBridge.parseDate(args["ends"] as? String)
            status = .done
        case .agenda:
            let range = args["range"] as? String ?? "today"
            title = ["today": "Today", "tomorrow": "Tomorrow", "week": "This week"][range] ?? "Agenda"
            subtitle = nil
        case .link:
            title = args["label"] as? String ?? "Link"
            subtitle = URL(string: args["url"] as? String ?? "")?.host
            status = .done
        case .app:
            title = args["name"] as? String ?? "App"
            subtitle = "Opening"
        case .shortcut:
            title = args["name"] as? String ?? "Shortcut"
            subtitle = "Running"
        case .newShortcut:
            title = args["name"] as? String ?? "New shortcut"
            subtitle = "Review and add in Shortcuts"
            items = (args["steps"] as? [[String: Any]] ?? []).map { step in
                let type = (step["type"] as? String ?? "").replacingOccurrences(of: "_", with: " ")
                let value = step["value"].map { "\($0)" } ?? ""
                return value.isEmpty ? type : "\(type): \(value)"
            }
        case .search:
            let hits = args["results"] as? [[String: Any]] ?? []
            let names = hits.compactMap { $0["title"] as? String }
            // The query leads, not the top hit. Wikipedia's first result isn't
            // always the one the reply drew on, and showing only that made the
            // card look confidently wrong ("My Bed" for a question about beds).
            title = args["query"] as? String ?? "Search"
            searchSource = args["source"] as? String ?? "Wikipedia"
            // Just the source and a count. Joining every result title made the
            // subheading longer than the card — a web search returns five.
            subtitle = names.count > 1
                ? "\(searchSource) · \(names.count) results"
                : searchSource
            body = hits.first?["extract"] as? String
            // Web result titles run long ("… | CoinMarketCap"), so the other
            // hits are listed short — enough to see what was found without the
            // card turning into a wall of headlines.
            items = names.count > 1
                ? names.dropFirst().prefix(3).map {
                    $0.count > 58 ? String($0.prefix(56)) + "…" : $0
                  }
                : []
            searchLink = (hits.first?["link"] as? String).flatMap(URL.init(string:))
            status = .done
        }
    }

    /// Article link for a `.search` card, shown as a "Read more" button.
    var searchLink: URL?
    /// Which source answered — the link label said "Wikipedia" even for a web
    /// search, which was simply untrue about where the button went.
    var searchSource = "Wikipedia"

    /// When a `.timer` card fires. The countdown is drawn from this rather than
    /// ticked in the model, so it stays correct if the app is busy.
    var endsAt: Date?

    /// Search URL for the streaming services. Both are universal links, so they
    /// open the native app when it's installed and the web player otherwise.
    func musicURL(_ host: String) -> URL? {
        let q = "\(title) \(subtitle ?? "")"
            .addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
        return URL(string: host == "spotify"
                   ? "https://open.spotify.com/search/\(q)"
                   : "https://music.apple.com/search?term=\(q)")
    }

    private static func when(_ date: Date) -> String {
        let f = DateFormatter()
        f.doesRelativeDateFormatting = true      // "Today", "Tomorrow"
        f.dateStyle = .medium
        f.timeStyle = .short
        return f.string(from: date)
    }
}

struct HomeCardView: View {
    let card: HomeCard
    let onDismiss: () -> Void
    var onUndo: () -> Void = {}

    var body: some View {
        if card.status == .deleted { deletedStrip } else { full }
    }

    /// A dismissed reminder/event is really gone from Reminders/Calendar, so it
    /// leaves an undo behind rather than vanishing on one stray click.
    private var deletedStrip: some View {
        HStack(spacing: 8) {
            Image(systemName: "trash").font(.caption2).foregroundStyle(.secondary)
            Text("Deleted “\(card.title)”")
                .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            Spacer(minLength: 0)
            Button("Undo", action: onUndo).buttonStyle(.plain)
                .font(.caption.weight(.semibold)).foregroundStyle(Color.accentColor)
        }
        .padding(.horizontal, 11).padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardSurface()
    }

    private var full: some View {
        HStack(alignment: .top, spacing: 12) {
            // The colour lives in the glyph, not behind it. A filled disc made
            // every card shout for attention in a column of quiet bubbles.
            Image(systemName: card.kind.icon)
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(card.kind.tint)
                .frame(width: 20)
                .padding(.top, 1)

            VStack(alignment: .leading, spacing: 3) {
                Text(card.title)
                    .font(.system(size: 13, weight: .semibold))
                    .fixedSize(horizontal: false, vertical: true)
                if let subtitle = card.subtitle {
                    Text(subtitle).font(.system(size: 11.5)).foregroundStyle(.secondary)
                }
                if let body = card.body, !body.isEmpty {
                    Text(body)
                        .font(.system(size: 11.5)).foregroundStyle(.secondary)
                        .lineSpacing(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !card.items.isEmpty {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(card.items, id: \.self) { item in
                            HStack(alignment: .top, spacing: 5) {
                                Circle().fill(card.kind.tint.opacity(0.55))
                                    .frame(width: 3, height: 3).padding(.top, 5)
                                Text(item).font(.system(size: 11))
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    .padding(.top, 3)
                }
                if card.kind == .song { musicButtons }
                if card.kind == .timer, let ends = card.endsAt { CountdownLabel(ends: ends) }
                if card.kind == .search, let url = card.searchLink {
                    Link(card.searchSource == "Wikipedia"
                         ? "Read on Wikipedia"
                         : "Open \(url.host ?? "source")", destination: url)
                        .font(.caption2.weight(.medium)).padding(.top, 4)
                }
                if card.kind == .link, let url = URL(string: card.args["url"] as? String ?? "") {
                    Link("Open", destination: url)
                        .font(.caption2.weight(.medium)).padding(.top, 4)
                }
                if card.kind == .agenda && card.items.isEmpty && card.status == .done {
                    Text("Nothing scheduled").font(.caption).foregroundStyle(.secondary)
                }
                statusLine
            }

            Spacer(minLength: 0)

            Button(action: onDismiss) {
                Image(systemName: "xmark")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(.secondary)
                    .frame(width: 18, height: 18)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(card.kind.writesToEventKit ? "Delete" : "Dismiss")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardSurface()
        .transition(.move(edge: .top).combined(with: .opacity))
    }

    /// Live countdown, redrawn once a second off the wall clock rather than a
    /// ticking counter, so it stays right even if the app was busy or asleep.
    private struct CountdownLabel: View {
        let ends: Date

        // Display only — ChatModel.startTimer owns the chime, because this view
        // lives in a LazyVStack and may not exist when the timer runs out.
        var body: some View {
            TimelineView(.periodic(from: .now, by: 1)) { ctx in
                let left = Int(ends.timeIntervalSince(ctx.date).rounded(.up))
                Text(left > 0
                     ? String(format: "%d:%02d", left / 60, left % 60)
                     : "Time's up")
                    .font(.system(size: 19, weight: .medium, design: .rounded).monospacedDigit())
                    .foregroundStyle(left > 0 ? Color.primary : Color.red)
            }
            .padding(.top, 2)
        }
    }

    /// Handing off to Music/Spotify is the one thing in the app that leaves the
    /// machine — hence a button the user presses, never an automatic open.
    @ViewBuilder private var musicButtons: some View {
        HStack(spacing: 6) {
            if let url = card.musicURL("apple") {
                Link("Apple Music", destination: url)
            }
            if let url = card.musicURL("spotify") {
                Link("Spotify", destination: url)
            }
        }
        .font(.caption2.weight(.medium))
        .buttonStyle(.plain)
        .padding(.top, 4)
    }

    @ViewBuilder private var statusLine: some View {
        switch card.status {
        case .working:
            Text("Adding…").font(.caption2).foregroundStyle(.secondary)
        case .done, .deleted:
            EmptyView()
        case .failed(let why):
            Label(why, systemImage: "exclamationmark.triangle.fill")
                .font(.caption2)
                .foregroundStyle(.orange)
                .padding(.top, 2)
        }
    }
}
