// Renders the Fennel logomark to an .icns, from the same geometry the app draws.
// One source of truth for the mark: no hand-exported PNGs to drift out of sync.
import AppKit

let sizes = [16, 32, 64, 128, 256, 512, 1024]
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "app/Resources"
let iconset = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("Fennel.iconset")
try? FileManager.default.removeItem(at: iconset)
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)

// Same geometry as FennelMark.swift — squat ribbed bulb, feathery spray.
// Grouped by stroke weight, because the parts are not drawn at the same weight.
func markPaths(_ side: CGFloat) -> [(NSBezierPath, CGFloat)] {
    let s = side / 120
    func p(_ x: CGFloat, _ y: CGFloat) -> NSPoint { NSPoint(x: x * s, y: side - y * s) }

    let bulb = NSBezierPath()
    bulb.move(to: p(30, 86))
    bulb.curve(to: p(60, 106), controlPoint1: p(30, 98), controlPoint2: p(43, 106))
    bulb.curve(to: p(90, 86), controlPoint1: p(77, 106), controlPoint2: p(90, 98))
    bulb.curve(to: p(60, 68), controlPoint1: p(90, 74), controlPoint2: p(78, 68))
    bulb.curve(to: p(30, 86), controlPoint1: p(42, 68), controlPoint2: p(30, 74))
    bulb.close()

    let ribs = NSBezierPath()
    ribs.move(to: p(47, 70))
    ribs.curve(to: p(48, 104), controlPoint1: p(44, 80), controlPoint2: p(44, 95))
    ribs.move(to: p(60, 68)); ribs.line(to: p(60, 106))
    ribs.move(to: p(73, 70))
    ribs.curve(to: p(72, 104), controlPoint1: p(76, 80), controlPoint2: p(76, 95))

    let sprig = NSBezierPath()
    sprig.move(to: p(58, 68))
    sprig.curve(to: p(61, 48), controlPoint1: p(57, 60), controlPoint2: p(58, 54))
    let fan: [(NSPoint, NSPoint, NSPoint)] = [
        (p(52, 44), p(42, 44), p(34, 48)),
        (p(55, 38), p(48, 32), p(39, 28)),
        (p(60, 37), p(62, 27), p(66, 19)),
        (p(68, 39), p(77, 34), p(86, 32)),
        (p(70, 46), p(80, 48), p(87, 53)),
    ]
    for (c1, c2, end) in fan {
        sprig.move(to: p(61, 48))
        sprig.curve(to: end, controlPoint1: c1, controlPoint2: c2)
    }

    for path in [bulb, ribs, sprig] {
        path.lineCapStyle = .round
        path.lineJoinStyle = .round
    }
    return [(bulb, 6.5 * s), (ribs, 4.0 * s), (sprig, 4.2 * s)]
}

func render(_ side: Int) -> Data {
    let px = CGFloat(side)
    let image = NSImage(size: NSSize(width: px, height: px))
    image.lockFocus()
    // Rounded-square ground in the app's accent, mark knocked out in white:
    // macOS icons read better as a filled tile than a floating glyph.
    let inset = px * 0.06
    let tile = NSBezierPath(roundedRect: NSRect(x: inset, y: inset,
                                                width: px - inset*2, height: px - inset*2),
                            xRadius: px * 0.22, yRadius: px * 0.22)
    let gradient = NSGradient(starting: NSColor(srgbRed: 0.42, green: 0.44, blue: 0.98, alpha: 1),
                              ending: NSColor(srgbRed: 0.67, green: 0.40, blue: 0.95, alpha: 1))!
    gradient.draw(in: tile, angle: -45)
    NSColor.white.setStroke()
    let shift = NSAffineTransform()
    shift.translateX(by: px * 0.11, yBy: px * 0.11)
    for (path, width) in markPaths(px * 0.78) {
        path.transform(using: shift as AffineTransform)
        path.lineWidth = width
        path.stroke()
    }
    image.unlockFocus()
    let tiff = image.tiffRepresentation!
    return NSBitmapImageRep(data: tiff)!.representation(using: .png, properties: [:])!
}

for s in sizes {
    try render(s).write(to: iconset.appendingPathComponent("icon_\(s)x\(s).png"))
    if s <= 512 {
        try render(s * 2).write(to: iconset.appendingPathComponent("icon_\(s)x\(s)@2x.png"))
    }
}
let task = Process()
task.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
task.arguments = ["-c", "icns", iconset.path, "-o", "\(out)/Fennel.icns"]
try task.run(); task.waitUntilExit()
print(task.terminationStatus == 0 ? "wrote \(out)/Fennel.icns" : "iconutil failed")
