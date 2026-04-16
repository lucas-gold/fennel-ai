import SwiftUI

@main
struct MyAIApp: App {
    @StateObject private var chat = ChatModel()

    var body: some Scene {
        WindowGroup("my_ai") {
            RootView()
                .environmentObject(chat)
                .frame(minWidth: 760, minHeight: 540)
                .onAppear { chat.connect() }
        }
    }
}
