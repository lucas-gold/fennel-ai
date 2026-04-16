// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "myai",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "myai",
            path: "Sources/myai",
            // Stage 0/1 dev convenience; revisit strict concurrency at Stage 2
            // when the audio engine introduces real cross-actor work.
            swiftSettings: [.swiftLanguageMode(.v5)]
        )
    ]
)
