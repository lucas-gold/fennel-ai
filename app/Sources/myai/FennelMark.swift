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
    enum Part { case bulb, ribs, stalk, fronds }

    /// Stroke weights on the 120-unit design grid, per part.
    static func weight(_ part: Part) -> CGFloat {
        switch part {
        case .bulb:   return 6.5
        case .ribs:   return 5.5   // two heavy ribs read better than three thin
        case .stalk:  return 6.5
        case .fronds: return 4.0   // kept finer so they still read as feathery
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
            path.move(to: p(50, 69))
            path.addCurve(to: p(50, 105), control1: p(46, 80), control2: p(46, 95))
            path.move(to: p(70, 69))
            path.addCurve(to: p(70, 105), control1: p(74, 80), control2: p(74, 95))

        case .stalk:
            path.move(to: p(58, 68))
            path.addCurve(to: p(61, 47), control1: p(57, 60), control2: p(58, 54))

        case .fronds:
            let fan: [(CGPoint, CGPoint, CGPoint)] = [
                (p(52, 43), p(42, 43), p(34, 47)),
                (p(55, 37), p(48, 31), p(39, 27)),
                (p(60, 36), p(62, 26), p(66, 18)),
                (p(68, 38), p(77, 33), p(86, 31)),
                (p(70, 45), p(80, 47), p(87, 52)),
            ]
            for (c1, c2, end) in fan {
                path.move(to: p(61, 47))
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
            stroked(.stalk)
            stroked(.fronds)
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
