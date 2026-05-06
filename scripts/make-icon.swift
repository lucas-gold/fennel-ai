// Renders the Fennel logomark to an .icns, from the same geometry the app draws.
// One source of truth for the mark: no hand-exported PNGs to drift out of sync.
import AppKit

let sizes = [16, 32, 64, 128, 256, 512, 1024]
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "app/Resources"
let iconset = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("Fennel.iconset")
try? FileManager.default.removeItem(at: iconset)
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)

func markPath(_ side: CGFloat) -> NSBezierPath {
    let s = side / 120
    func p(_ x: CGFloat, _ y: CGFloat) -> NSPoint { NSPoint(x: x * s, y: side - y * s) }
    let path = NSBezierPath()
    path.move(to: p(53, 62))
    path.curve(to: p(38, 85), controlPoint1: p(45, 66), controlPoint2: p(38, 74))
    path.curve(to: p(60, 106), controlPoint1: p(38, 97), controlPoint2: p(47, 106))
    path.curve(to: p(82, 85), controlPoint1: p(73, 106), controlPoint2: p(82, 97))
    path.curve(to: p(67, 62), controlPoint1: p(82, 74), controlPoint2: p(75, 66))
    path.move(to: p(60, 62)); path.line(to: p(60, 28))
    path.move(to: p(53, 62))
    path.curve(to: p(34, 32), controlPoint1: p(49, 52), controlPoint2: p(42, 41))
    path.move(to: p(67, 62))
    path.curve(to: p(86, 32), controlPoint1: p(71, 52), controlPoint2: p(78, 41))
    for e in [(50.0, 20.0), (60.0, 14.0), (70.0, 20.0)] {
        path.move(to: p(60, 28)); path.line(to: p(e.0, e.1))
    }
    path.lineWidth = 6.5 * s
    path.lineCapStyle = .round
    path.lineJoinStyle = .round
    return path
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
    let scaled = markPath(px * 0.78)
    let shift = NSAffineTransform()
    shift.translateX(by: px * 0.11, yBy: px * 0.11)
    scaled.transform(using: shift as AffineTransform)
    scaled.stroke()
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
