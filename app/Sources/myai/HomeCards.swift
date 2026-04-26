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

        var icon: String {
            switch self {
            case .reminder: return "checklist"
            case .event: return "calendar"
            case .panel: return "square.text.square"
            case .fact: return "brain"
            case .song: return "music.note"
            }
        }

        var tint: Color {
            switch self {
            case .reminder: return .orange
            case .event: return .blue
            case .panel: return .purple
            case .fact: return .teal
            case .song: return .pink
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
        }
    }

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
        .padding(.horizontal, 10).padding(.vertical, 7)
        .background(Color.gray.opacity(0.10))
        .clipShape(RoundedRectangle(cornerRadius: 9))
    }

    private var full: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: card.kind.icon)
                .foregroundStyle(card.kind.tint)
                .font(.system(size: 14, weight: .semibold))
                .frame(width: 18)

            VStack(alignment: .leading, spacing: 4) {
                Text(card.title).font(.callout).fontWeight(.medium)
                if let subtitle = card.subtitle {
                    Text(subtitle).font(.caption).foregroundStyle(.secondary)
                }
                if let body = card.body, !body.isEmpty {
                    Text(body).font(.caption).foregroundStyle(.secondary)
                }
                if !card.items.isEmpty {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(card.items, id: \.self) { item in
                            Text("• \(item)").font(.caption)
                        }
                    }
                    .padding(.top, 2)
                }
                if card.kind == .song { musicButtons }
                statusLine
            }

            Spacer(minLength: 0)

            Button(action: onDismiss) {
                Image(systemName: "xmark")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .help("Dismiss")
        }
        .padding(10)
        .background(card.kind.tint.opacity(0.10))
        .overlay(
            RoundedRectangle(cornerRadius: 9)
                .stroke(card.kind.tint.opacity(0.28), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 9))
        .transition(.move(edge: .top).combined(with: .opacity))
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
