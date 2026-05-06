import SwiftUI

@main
struct FennelApp: App {
    @StateObject private var chat = ChatModel()

    var body: some Scene {
        WindowGroup("Fennel") {
            RootView()
                .environmentObject(chat)
                // 320 sidebar + 460 chat minimum, so the split never fights itself.
                .frame(minWidth: 820, minHeight: 580)
                .onAppear { chat.connect() }
        }
    }
}
