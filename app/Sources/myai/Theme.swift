import SwiftUI

/// One place for the visual language, so spacing and radii stay consistent
/// across views.
///
/// The look is deliberately quiet: a calm surface, one accent gradient used
/// only by the orb and the user's own words, and generous whitespace. The
/// screen should reward a glance without competing with the conversation.
enum Theme {
    static let radius: CGFloat = 14
    static let cardRadius: CGFloat = 18
    static let gutter: CGFloat = 20
    /// The one surface everything that isn't the user speaks from, so a tool
    /// card reads as part of the conversation rather than a widget in it.
    static let bubble = Color.white.opacity(0.055)
    static let bubbleRadius: CGFloat = 18

    /// The single accent gradient. Reserved for the orb and the user's bubbles;
    /// everything else stays neutral so these read as "you" and "it".
    static let accent = LinearGradient(
        colors: [Color(red: 0.42, green: 0.44, blue: 0.98),
                 Color(red: 0.67, green: 0.40, blue: 0.95)],
        startPoint: .topLeading, endPoint: .bottomTrailing)

    /// The gradient's first stop as a plain colour, for the places that need a
    /// `Color` rather than a `ShapeStyle` — a hairline border, a small badge.
    /// A gradient in a 1pt stroke is invisible anyway.
    static let accentSolid = Color(red: 0.42, green: 0.44, blue: 0.98)

    static func stateColors(_ state: AssistantState, listening: Bool) -> [Color] {
        switch state {
        case .speaking:  return [Color(red: 0.20, green: 0.78, blue: 0.60),
                                 Color(red: 0.30, green: 0.68, blue: 0.90)]
        case .thinking:  return [Color(red: 0.98, green: 0.66, blue: 0.28),
                                 Color(red: 0.95, green: 0.45, blue: 0.45)]
        default:
            return listening
                ? [Color(red: 0.42, green: 0.44, blue: 0.98),
                   Color(red: 0.67, green: 0.40, blue: 0.95)]
                : [Color.secondary.opacity(0.45), Color.secondary.opacity(0.30)]
        }
    }

    /// Headings use the rounded face — softer than the default, still unmistakably
    /// native, and it suits something you talk to.
    static func title(_ size: CGFloat, _ weight: Font.Weight = .semibold) -> Font {
        .system(size: size, weight: weight, design: .rounded)
    }
}

/// A surface that reads as a raised panel without a hard border.
struct CardSurface: ViewModifier {
    var tint: Color = .clear          // kept for the icon; the surface is neutral
    var radius: CGFloat = Theme.cardRadius

    func body(content: Content) -> some View {
        content
            .background(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .fill(Theme.bubble))
            .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
    }
}

extension View {
    func cardSurface(tint: Color = .clear, radius: CGFloat = Theme.cardRadius) -> some View {
        modifier(CardSurface(tint: tint, radius: radius))
    }
}

/// Small circular icon button used across the chrome.
struct IconButton: View {
    let symbol: String
    var help: String = ""
    var active = false
    let action: () -> Void

    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(active ? Color.accentColor : Color.secondary)
                .frame(width: 26, height: 26)
                .background(
                    Circle().fill(Color.primary.opacity(hovering ? 0.08 : 0)))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(help)
    }
}
