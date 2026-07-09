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

    /// Where the backend writes its log, so a problem report has something in
    /// it.
    static let logURL = FileManager.default
        .homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/Fennel/backend.log")

    func start() {
        let res = Bundle.main.resourceURL
        // "runtime/bin/Fennel" is a hard link to the same interpreter, so
        // Activity Monitor names the process after the app. Older bundles have
        // only the original name.
        let named = res?.appendingPathComponent("runtime/bin/Fennel")
        let plain = res?.appendingPathComponent("runtime/bin/python3.12")
        let interpreter = [named, plain].compactMap { $0 }.first {
            FileManager.default.isExecutableFile(atPath: $0.path)
        }
        guard let python = interpreter,
              let server = res?.appendingPathComponent("backend/server.py"),
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
        // So the backend cannot outlive us: if we are force-quit and never reach
        // `stop()`, it notices this pid is gone and exits.
        env["FENNEL_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
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

    /// Terminate on quit, or the Python child holds the port and the next
    /// launch talks to a stale backend.
    func stop() {
        guard let p = process, p.isRunning else { return }
        p.terminate()
        // SIGTERM first; the models take a moment to release.
        DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
            if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        }
    }
}
