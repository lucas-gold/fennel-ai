import Foundation

/// One row of the startup model picker.
///
/// Everything here comes from the backend's `config.MODELS`, annotated with
/// what is actually on disk — the app deliberately knows nothing about models
/// itself, so adding one is a change to a single Python table.
struct ModelOption: Identifiable, Equatable {
    let id: String            // the Hugging Face repo
    let name: String          // "Everyday", "Agent", …
    let detail: String        // "Qwen3 · 4B"
    let focus: String         // a sentence on what it is for
    let bytes: Int            // download size
    let ram: Double           // approximate GB resident once loaded
    let tools: Bool           // whether its template renders Fennel's tools
    let installed: Bool
    let onDisk: Int           // bytes it currently occupies, 0 if absent
    let hidden: Bool          // kept off the list until asked for

    init?(json: [String: Any]) {
        guard let id = json["id"] as? String, let name = json["name"] as? String
        else { return nil }
        self.id = id
        self.name = name
        detail = json["detail"] as? String ?? ""
        focus = json["focus"] as? String ?? ""
        bytes = json["bytes"] as? Int ?? 0
        ram = json["ram"] as? Double ?? 0
        tools = json["tools"] as? Bool ?? true
        installed = json["installed"] as? Bool ?? false
        onDisk = json["on_disk"] as? Int ?? 0
        hidden = json["hidden"] as? Bool ?? false
    }

    /// "4.3 GB" — decimal GB, to match how the download size is advertised.
    static func gb(_ n: Int) -> String {
        String(format: "%.1f GB", Double(n) / 1_000_000_000)
    }
}
