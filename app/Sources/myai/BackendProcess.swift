import Foundation

/// Runs the Python backend as a child of the app, so opening Fennel is the
/// whole install: no terminal, no separate server.
///
/// The runtime lives at `Contents/Resources/runtime` — a relocatable CPython
/// with our packages inside it — and the backend source at
/// `Contents/Resources/backend`. In development neither exists, and we fall
/// back to whatever is already listening on the port, so `swift build` plus a
/// hand-started server keeps working.
@MainActor
final class BackendProcess {
    private var process: Process?
    private(set) var bundled = false

    /// Where the backend writes its log, so a user reporting a problem has
    /// something to send and we aren't guessing.
    static let logURL = FileManager.default
        .homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/Fennel/backend.log")

    func start() {
        let res = Bundle.main.resourceURL
        guard let python = res?.appendingPathComponent("runtime/bin/python3.12"),
              let server = res?.appendingPathComponent("backend/server.py"),
              FileManager.default.isExecutableFile(atPath: python.path),
              FileManager.default.fileExists(atPath: server.path)
        else {
            print("[backend] not bundled; expecting a server already running")
            return
        }
        bundled = true

        try? FileManager.default.createDirectory(
            at: Self.logURL.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: Self.logURL.path, contents: nil)
        let handle = try? FileHandle(forWritingTo: Self.logURL)

        let task = Process()
        task.executableURL = python
        task.arguments = [server.path]
        // cwd matters: config.py resolves the VAD model relative to itself, but
        // the backend also writes relative paths for its own scratch files.
        task.currentDirectoryURL = server.deletingLastPathComponent()
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONHOME"] = res!.appendingPathComponent("runtime").path
        task.environment = env
        if let handle {
            task.standardOutput = handle
            task.standardError = handle
        }
        task.terminationHandler = { p in
            print("[backend] exited with status \(p.terminationStatus)")
        }
        do {
            try task.run()
            process = task
            print("[backend] started (pid \(task.processIdentifier))")
        } catch {
            print("[backend] failed to start:", error)
        }
    }

    /// Terminate on quit. Without this the Python child outlives the app and
    /// holds the port, so the next launch silently talks to a stale backend.
    func stop() {
        guard let p = process, p.isRunning else { return }
        p.terminate()
        // SIGTERM first; the models take a moment to release.
        DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
            if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        }
    }
}
