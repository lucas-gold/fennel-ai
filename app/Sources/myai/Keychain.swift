import Foundation
import Security

/// The API key lives in the Keychain, not in the backend's SQLite.
///
/// Everything else Fennel stores is a preference — a city, a switch — and
/// plaintext is fine for those. A key is a credential: it belongs somewhere the
/// OS protects, and it should never appear in a database a user might copy,
/// sync or hand to someone debugging. The backend receives it over the loopback
/// socket and holds it only for as long as it is running.
enum Keychain {
    private static let service = "garden.fennel.app"

    static func set(_ value: String, for account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
        guard !value.isEmpty, let data = value.data(using: .utf8) else { return }
        var add = query
        add[kSecValueData as String] = data
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(add as CFDictionary, nil)
    }

    static func get(_ account: String) -> String {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var out: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data, let s = String(data: data, encoding: .utf8)
        else { return "" }
        return s
    }
}
