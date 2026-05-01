import AppKit
import Foundation

/// Launching apps and running the user's own Shortcuts.
///
/// Shortcuts is the important one. HomeKit has no macOS framework for
/// third-party apps, so lights and scenes can't be driven directly — but the
/// Shortcuts app can drive them, and it can also send messages, control media,
/// and anything else the user has wired up. Delegating to it turns one tool
/// into every automation they already own, and keeps the blast radius inside
/// something they authored and can inspect.
enum MacActions {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    // MARK: - apps

    @MainActor
    static func openApp(named name: String) async throws {
        guard let url = locate(name) else {
            throw Failure(message: "I couldn't find an app called \(name)")
        }
        let config = NSWorkspace.OpenConfiguration()
        config.activates = true
        _ = try await NSWorkspace.shared.openApplication(at: url, configuration: config)
    }

    private static func locate(_ name: String) -> URL? {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let base = trimmed.hasSuffix(".app") ? String(trimmed.dropLast(4)) : trimmed
        // Bundle id first (handles "safari" → com.apple.Safari), then the usual
        // install locations, which covers apps NSWorkspace can't resolve by name.
        if let url = NSWorkspace.shared.urlForApplication(
            withBundleIdentifier: "com.apple.\(base.replacingOccurrences(of: " ", with: ""))") {
            return url
        }
        for dir in ["/Applications", "/System/Applications",
                    "/System/Applications/Utilities",
                    NSHomeDirectory() + "/Applications"] {
            let candidate = URL(fileURLWithPath: dir).appendingPathComponent("\(base).app")
            if FileManager.default.fileExists(atPath: candidate.path) { return candidate }
        }
        return nil
    }

    // MARK: - shortcuts

    /// Run a shortcut by name. On a miss we return the available names so the
    /// assistant can say what *does* exist instead of just failing.
    static func runShortcut(named name: String) async throws {
        let available = shortcutNames()
        let match = available.first { $0.caseInsensitiveCompare(name) == .orderedSame }
            ?? available.first { $0.localizedCaseInsensitiveContains(name) }
        guard let match else {
            let list = available.prefix(8).joined(separator: ", ")
            throw Failure(message: available.isEmpty
                ? "there are no shortcuts set up on this Mac"
                : "there's no shortcut called \(name). Available: \(list)")
        }
        let (status, err) = run("/usr/bin/shortcuts", ["run", match])
        guard status == 0 else {
            throw Failure(message: err.isEmpty
                ? "the shortcut \(match) failed to run" : err)
        }
    }

    static func shortcutNames() -> [String] {
        let (status, out) = run("/usr/bin/shortcuts", ["list"])
        guard status == 0 else { return [] }
        return out.split(separator: "\n").map(String.init).filter { !$0.isEmpty }
    }

    /// → (exit status, combined trimmed output). Returns a non-zero status
    /// rather than throwing if the binary is missing, so callers have one path.
    private static func run(_ path: String, _ args: [String]) -> (Int32, String) {
        guard FileManager.default.isExecutableFile(atPath: path) else {
            return (127, "the shortcuts command isn't available on this Mac")
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: path)
        task.arguments = args
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = pipe
        do { try task.run() } catch { return (127, error.localizedDescription) }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        let text = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return (task.terminationStatus, text)
    }
}
