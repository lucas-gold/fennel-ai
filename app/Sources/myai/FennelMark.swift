import SwiftUI

/// The Fennel logomark: a bulb, a stalk, two sweeping fronds and a crown.
///
/// Drawn as a `Shape` rather than shipped as an asset so it stays crisp at any
/// size, inherits the current colour, and can be animated. Geometry is the
/// 120×120 design grid from the source SVG, scaled to whatever rect it's given.
struct FennelMark: Shape {
    /// Stroke weight on the 120-unit grid; the caller strokes with `lineWidth`.
    static let designWidth: CGFloat = 6.5
    static let grid: CGFloat = 120

    func path(in rect: CGRect) -> Path {
        let s = min(rect.width, rect.height) / Self.grid
        let dx = rect.minX + (rect.width - Self.grid * s) / 2
        let dy = rect.minY + (rect.height - Self.grid * s) / 2
        func p(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
            CGPoint(x: dx + x * s, y: dy + y * s)
        }

        var path = Path()

        // Bulb — two mirrored curves meeting at the base.
        path.move(to: p(53, 62))
        path.addCurve(to: p(38, 85), control1: p(45, 66), control2: p(38, 74))
        path.addCurve(to: p(60, 106), control1: p(38, 97), control2: p(47, 106))
        path.addCurve(to: p(82, 85), control1: p(73, 106), control2: p(82, 97))
        path.addCurve(to: p(67, 62), control1: p(82, 74), control2: p(75, 66))

        // Stalk.
        path.move(to: p(60, 62))
        path.addLine(to: p(60, 28))

        // Fronds sweeping out of the bulb.
        path.move(to: p(53, 62))
        path.addCurve(to: p(34, 32), control1: p(49, 52), control2: p(42, 41))
        path.move(to: p(67, 62))
        path.addCurve(to: p(86, 32), control1: p(71, 52), control2: p(78, 41))

        // Crown.
        for end in [(50.0, 20.0), (60.0, 14.0), (70.0, 20.0)] {
            path.move(to: p(60, 28))
            path.addLine(to: p(end.0, end.1))
        }
        return path
    }
}

/// The mark at a given size, stroked with the weight it was drawn for.
struct FennelLogo: View {
    var size: CGFloat = 22
    var lineWidth: CGFloat? = nil

    var body: some View {
        FennelMark()
            .stroke(style: StrokeStyle(
                lineWidth: lineWidth ?? FennelMark.designWidth * size / FennelMark.grid,
                lineCap: .round, lineJoin: .round))
            .frame(width: size, height: size)
    }
}
