import SwiftUI

/// The Fennel logomark: a squat ribbed bulb under a feathery spray of fronds.
///
/// Two things drive the shape, both learned by rendering it small rather than
/// by reasoning about it. The bulb is deliberately **wider than tall** — a round
/// bulb on a straight stem reads as a Venus symbol, not a vegetable. And the
/// fronds must be **several**: a simplified version with one stalk and a single
/// crossing frond tested *worse* at 32 px, because it collapsed straight back
/// into a symbol. Multiplicity is what makes it a plant.
///
/// Drawn as `Shape`s rather than shipped as an asset so it stays crisp at any
/// size and inherits the current colour. `scripts/make-icon.swift` renders the
/// `.icns` from the same geometry, so the icon cannot drift from the mark.
struct FennelMark: Shape {
    enum Part { case bulb, ribs, sprig }

    /// Stroke weights on the 120-unit design grid, per part.
    static func weight(_ part: Part) -> CGFloat {
        switch part {
        case .bulb:  return 6.5
        case .ribs:  return 4.0
        case .sprig: return 4.2
        }
    }
    static let grid: CGFloat = 120

    var part: Part = .bulb

    func path(in rect: CGRect) -> Path {
        let s = min(rect.width, rect.height) / Self.grid
        let dx = rect.minX + (rect.width - Self.grid * s) / 2
        let dy = rect.minY + (rect.height - Self.grid * s) / 2
        func p(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
            CGPoint(x: dx + x * s, y: dy + y * s)
        }
        var path = Path()

        switch part {
        case .bulb:
            path.move(to: p(30, 86))
            path.addCurve(to: p(60, 106), control1: p(30, 98), control2: p(43, 106))
            path.addCurve(to: p(90, 86), control1: p(77, 106), control2: p(90, 98))
            path.addCurve(to: p(60, 68), control1: p(90, 74), control2: p(78, 68))
            path.addCurve(to: p(30, 86), control1: p(42, 68), control2: p(30, 74))
            path.closeSubpath()

        case .ribs:
            path.move(to: p(47, 70))
            path.addCurve(to: p(48, 104), control1: p(44, 80), control2: p(44, 95))
            path.move(to: p(60, 68))
            path.addLine(to: p(60, 106))
            path.move(to: p(73, 70))
            path.addCurve(to: p(72, 104), control1: p(76, 80), control2: p(76, 95))

        case .sprig:
            // Short stalk, then the fan. The fronds carry the identity.
            path.move(to: p(58, 68))
            path.addCurve(to: p(61, 48), control1: p(57, 60), control2: p(58, 54))
            let fan: [(CGPoint, CGPoint, CGPoint)] = [
                (p(52, 44), p(42, 44), p(34, 48)),
                (p(55, 38), p(48, 32), p(39, 28)),
                (p(60, 37), p(62, 27), p(66, 19)),
                (p(68, 39), p(77, 34), p(86, 32)),
                (p(70, 46), p(80, 48), p(87, 53)),
            ]
            for (c1, c2, end) in fan {
                path.move(to: p(61, 48))
                path.addCurve(to: end, control1: c1, control2: c2)
            }
        }
        return path
    }
}

/// The full mark at a given size, each part stroked at its own weight.
struct FennelLogo: View {
    var size: CGFloat = 22

    var body: some View {
        ZStack {
            stroked(.bulb)
            stroked(.ribs)
            stroked(.sprig)
        }
        .frame(width: size, height: size)
    }

    private func stroked(_ part: FennelMark.Part) -> some View {
        FennelMark(part: part)
            .stroke(style: StrokeStyle(
                lineWidth: FennelMark.weight(part) * size / FennelMark.grid,
                lineCap: .round, lineJoin: .round))
    }
}
