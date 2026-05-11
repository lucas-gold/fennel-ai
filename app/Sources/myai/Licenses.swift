import SwiftUI

/// The licence panel.
///
/// Not decoration: GPL-3.0 requires the licence to accompany the binary, and
/// Apache-2.0 §4 requires its text and any NOTICE to travel with a
/// distribution. Shipping the files in `Contents/Resources` and showing them
/// here is how Fennel satisfies both. Content is read from the bundle rather
/// than pasted into Swift so the app and the repo can never disagree.
struct LicensesView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var selection = Doc.thirdParty

    enum Doc: String, CaseIterable, Identifiable {
        case thirdParty = "Components"
        case gpl = "Fennel (GPL-3.0)"
        case apache = "Apache-2.0"
        case permissive = "MIT / BSD"

        var id: String { rawValue }

        /// (filename, extension) as copied into the bundle by build-app.sh.
        var file: (String, String) {
            switch self {
            case .thirdParty: return ("THIRD-PARTY", "md")
            case .gpl:        return ("LICENSE", "txt")
            case .apache:     return ("APACHE-2.0", "txt")
            case .permissive: return ("PERMISSIVE", "txt")
            }
        }

        var text: String {
            let (name, ext) = file
            guard let url = Bundle.main.url(forResource: name, withExtension: ext),
                  let s = try? String(contentsOf: url, encoding: .utf8)
            else { return "\(name).\(ext) is missing from the app bundle." }
            return s
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Licences").font(Theme.title(15, .bold))
                Spacer()
                Button("Done") { dismiss() }
            }
            .padding(.horizontal, 16).padding(.top, 14).padding(.bottom, 10)

            Picker("", selection: $selection) {
                ForEach(Doc.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(.horizontal, 16)

            Divider().padding(.top, 10)

            ScrollView {
                Text(selection.text)
                    .font(.system(size: 11).monospaced())
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(16)
            }
            .scrollIndicators(.visible)
        }
        .frame(width: 620, height: 480)
    }
}
