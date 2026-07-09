import SwiftUI

@main
struct FennelApp: App {
    @StateObject private var chat = ChatModel()
    @StateObject private var launch = LaunchState()

    var body: some Scene {
        WindowGroup("Fennel") {
            RootView()
                .environmentObject(chat)
                .environmentObject(launch)
                // 320 sidebar + 460 chat minimum, so the split never fights itself.
                .frame(minWidth: 820, minHeight: 580)
                .onAppear { launch.begin(chat: chat) }
        }
    }
}

/// Owns the backend child process and the first-run state the UI shows while
/// models download. Separate from ChatModel so the conversation layer stays
/// unaware of how the backend got there.
@MainActor
final class LaunchState: ObservableObject {
    @Published var starting = true

    private let backend = BackendProcess()
    private var started = false

    func begin(chat: ChatModel) {
        guard !started else { return }
        started = true
        backend.start()
        chat.connect()
        // Follows the backend for the life of the app rather than latching once:
        // the picker can be reopened from the chat, which puts the backend back
        // into setup.
        Task {
            while true {
                let up = chat.connected && chat.setupPhase == "ready"
                         && chat.sessionLoaded
                if up == starting {
                    withAnimation(.easeOut(duration: 0.3)) { starting = !up }
                }
                try? await Task.sleep(for: .milliseconds(200))
            }
        }
        NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification, object: nil, queue: .main
        ) { [backend] _ in
            MainActor.assumeIsolated { backend.stop() }
        }
    }
}
