import SwiftUI

/// The startup model picker.
///
/// Which model is loaded is the biggest single difference between two sessions
/// of Fennel — how fast it answers, how much RAM it holds, whether it will
/// write you fiction, whether its tools work at all. That is worth a deliberate
/// choice at launch rather than a setting buried behind a gear.
///
/// The rows come entirely from the backend (`config.MODELS` plus what is on
/// disk), so adding a model never touches this file.
struct ModelPicker: View {
    @EnvironmentObject var chat: ChatModel

    /// Selecting and committing are deliberately two presses. The first press
    /// only highlights: committing can mean a multi-gigabyte download and
    /// always means a minute of loading, which is far too much to hang on a
    /// stray click in a list.
    @State private var pending: String?
    /// Which row is asking "delete?". Deleting means re-downloading gigabytes
    /// to undo, so it asks first — and asks in place, because a sheet over a
    /// list this small hides the thing being talked about.
    @State private var confirmingDelete: String?
    /// A Hugging Face path the user is considering.
    @State private var customPath = ""

    var body: some View {
        VStack(spacing: 14) {
            VStack(spacing: 4) {
                Text("Choose a Fennel model")
                    .font(Theme.title(17, .semibold))
                Text(ramLine)
                    .font(.system(size: 11.5).monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            if !chat.setupNote.isEmpty {
                Label(chat.setupNote, systemImage: "exclamationmark.circle.fill")
                    .font(.system(size: 11)).foregroundStyle(.orange)
            }

            // Pinned above the list, not scrolled with it: it is a switch that
            // applies whatever you pick below, so it should not slide away.
            if let img = chat.imageModel { imageRow(img) }

            ScrollView {
                VStack(spacing: 8) {
                    ForEach(chat.setupModels) { m in
                        row(m)
                    }
                }
                .padding(.horizontal, 2)
            }
            .scrollIndicators(.automatic)
            .frame(maxHeight: 390)

            customField

            confirmBar

            // No hint that anything is hidden — that is the point of hiding it.
            // The path is a dot-directory, so it is easier to open than to
            // navigate to; saying where they live and not offering to show you
            // was half an answer.
            Button {
                let dir = FileManager.default.homeDirectoryForCurrentUser
                    .appendingPathComponent(".cache/huggingface/hub")
                NSWorkspace.shared.open(dir)
            } label: {
                Text("Downloaded models live in ~/.cache/huggingface and can be removed here at any time.")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
                    .underline()
                    .multilineTextAlignment(.center)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Open the folder in Finder")
        }
        .padding(.horizontal, 22)
        .padding(.bottom, 10)
    }

    /// Measured, not advertised: the free figure is what decides whether the
    /// next model down the list will actually fit.
    private var ramLine: String {
        guard chat.systemTotalBytes > 0 else { return "Reading memory…" }
        let free = max(0, chat.systemTotalBytes - chat.systemUsedBytes)
        return "Total RAM: \(ModelOption.gb(chat.systemTotalBytes))    "
             + "Available RAM: \(ModelOption.gb(free))"
    }

    /// The picture model, above the language models and not one of them: it is
    /// a switch rather than a choice, since it never runs on its own — you still
    /// have to pick something to talk to.
    private func imageRow(_ m: ModelOption) -> some View {
        HStack(spacing: 10) {
            Image(systemName: "photo.artframe")
                .font(.system(size: 14))
                .foregroundStyle(chat.imagesEnabled ? Color.purple : Color.secondary)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(m.name).font(.system(size: 12.5, weight: .semibold))
                    Text(m.detail).font(.system(size: 10.5)).foregroundStyle(.secondary)
                }
                Text(m.focus)
                    .font(.system(size: 10.5)).foregroundStyle(.secondary)
                    .lineLimit(1)
                Text(imageState(m))
                    .font(.system(size: 9.5))
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 6)
            if m.installed {
                Button { chat.deleteImageModel() } label: {
                    Image(systemName: "trash").font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .frame(width: 20, height: 18)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Delete the image model")
            }
            Toggle("", isOn: Binding(get: { chat.imagesEnabled },
                                     set: { chat.setImagesEnabled($0) }))
                .toggleStyle(.switch)
                .controlSize(.mini)
                .labelsHidden()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .fill(Theme.bubble)
                .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous)
                    .strokeBorder(chat.imagesEnabled
                                  ? Color.purple.opacity(0.45)
                                  : Color.primary.opacity(0.07), lineWidth: 1)))
    }

    /// The same sentence whether or not it is downloaded — what it costs does
    /// not change with that, and the download is stated once, on the button.
    /// A range because it is one: full size wants a lot more than the smaller
    /// picture it falls back to when memory is short.
    private func imageState(_ m: ModelOption) -> String {
        let size = ModelOption.gb(m.bytes)
        guard m.peakBytes > 0 else { return size }
        let lo = String(format: "%.0f", Double(m.peakLowBytes) / 1_000_000_000)
        let hi = String(format: "%.0f", Double(m.peakBytes) / 1_000_000_000)
        return "\(size)  ·  \(lo)–\(hi) GB in memory during image generation only"
    }

    /// Paste any MLX model from Hugging Face. Checked before it is kept —
    /// config and chat template are a few kilobytes, and everything that goes
    /// wrong with an unfamiliar model is visible in them.
    @ViewBuilder private var customField: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                TextField("Add any MLX model — owner/model-name", text: $customPath)
                    .textFieldStyle(.plain)
                    .font(.system(size: 11))
                    .padding(.horizontal, 9).padding(.vertical, 5)
                    .background(RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(Theme.bubble))
                    .onSubmit { check() }
                Button(chat.probing ? "Checking…" : "Check") { check() }
                    .font(.system(size: 11))
                    .disabled(customPath.trimmingCharacters(in: .whitespaces).isEmpty
                              || chat.probing)
            }
            probeVerdict
        }
        .padding(.top, 2)
    }

    private func check() {
        let path = customPath.trimmingCharacters(in: .whitespaces)
        guard !path.isEmpty else { return }
        chat.probeModel(path)
    }

    @ViewBuilder private var probeVerdict: some View {
        let p = chat.probe
        if !p.isEmpty {
            let problems = p["problems"] as? [String] ?? []
            let warnings = p["warnings"] as? [String] ?? []
            let ok = p["ok"] as? Bool ?? false
            VStack(alignment: .leading, spacing: 3) {
                ForEach(problems, id: \.self) { t in
                    Label(t, systemImage: "xmark.circle.fill")
                        .foregroundStyle(Color.red)
                }
                ForEach(warnings, id: \.self) { t in
                    Label(t, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(Color.orange)
                }
                if ok { addRow(p) }
            }
            .font(.system(size: 10))
            .buttonStyle(.plain)
            .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func addRow(_ p: [String: Any]) -> some View {
        let detail = p["detail"] as? String ?? ""
        let size = ModelOption.gb(p["bytes"] as? Int ?? 0)
        return HStack(spacing: 8) {
            Label("\(detail) · \(size)", systemImage: "checkmark.circle.fill")
                .foregroundStyle(Color.green)
            Button("Add it") {
                chat.addModel(p["id"] as? String ?? "")
                customPath = ""
            }
            .font(.system(size: 10, weight: .semibold))
        }
    }

    private func row(_ m: ModelOption) -> some View {
        Button { pending = m.id; confirmingDelete = nil } label: { rowBody(m) }
            .buttonStyle(.plain)
    }

    /// The model the confirm button would open: whatever has been clicked, or
    /// last launch's choice so the button is useful the moment the list appears.
    private var chosen: ModelOption? {
        chat.setupModels.first { $0.id == (pending ?? chat.setupCurrent) }
    }

    /// Everything that still has to come down the wire before this can run:
    /// the language model, and the picture model if it is switched on. Stated
    /// as one figure on the button rather than as a warning on each row.
    private var pendingDownload: Int {
        var total = 0
        if let m = chosen, !m.installed { total += m.bytes }
        if chat.imagesEnabled, let img = chat.imageModel, !img.installed {
            total += img.bytes
        }
        return total
    }

    private func confirmLabel(_ m: ModelOption) -> String {
        pendingDownload > 0
            ? "Download \(ModelOption.gb(pendingDownload)) and open \(m.name)"
            : "Open \(m.name)"
    }

    @ViewBuilder private var confirmBar: some View {
        if let m = chosen {
            VStack(spacing: 6) {
                Button {
                    chat.chooseModel(m.id)
                } label: {
                    Text(confirmLabel(m)).frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.defaultAction)

                if pendingDownload > 0 {
                    Text("Downloads once, then it works offline.")
                        .font(.system(size: 10)).foregroundStyle(.tertiary)
                }
            }
        }
    }

    // Split out of `row` deliberately: as one expression the whole card was too
    // much for the type checker to infer in reasonable time.
    private func rowBody(_ m: ModelOption) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                titleLine(m)
                // A custom row has no description — nobody wrote one — so it
                // simply omits the line rather than leaving a gap.
                if !m.focus.isEmpty {
                    Text(m.focus)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .lineSpacing(2)
                        .fixedSize(horizontal: false, vertical: true)
                        .multilineTextAlignment(.leading)
                }
                metaLine(m)
            }
            Spacer(minLength: 0)
            trailing(m)
        }
        .padding(.horizontal, 13)
        .padding(.vertical, 11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(background(m))
        .contentShape(Rectangle())
    }

    private func background(_ m: ModelOption) -> some View {
        let selected = m.id == (pending ?? chat.setupCurrent)
        return RoundedRectangle(cornerRadius: 12, style: .continuous)
            .fill(Theme.bubble)
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(selected ? Theme.accentSolid.opacity(0.55)
                                           : Color.primary.opacity(0.07),
                                  lineWidth: 1))
    }

    @ViewBuilder private func titleLine(_ m: ModelOption) -> some View {
        HStack(spacing: 7) {
            Text(m.name).font(.system(size: 13.5, weight: .semibold))
            Text(m.detail).font(.system(size: 11)).foregroundStyle(.secondary)
            // "In use" means the weights are still resident — reopening the
            // picker does not unload them, so returning to the same model is
            // free. The x makes that untrue when the RAM is wanted back.
            if m.id == chat.loadedModelID {
                HStack(spacing: 3) {
                    Text("IN USE").font(.system(size: 8.5, weight: .bold))
                    Button { chat.unloadModel() } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 7, weight: .bold))
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Unload from memory")
                }
                .padding(.horizontal, 5).padding(.vertical, 2)
                .background(Capsule().fill(Theme.accentSolid.opacity(0.3)))
            } else if m.id == chat.setupCurrent {
                Text("LAST USED")
                    .font(.system(size: 8.5, weight: .bold))
                    .padding(.horizontal, 5)
                    .padding(.vertical, 2)
                    .background(Capsule().fill(Color.secondary.opacity(0.22)))
            }
        }
    }

    @ViewBuilder private func metaLine(_ m: ModelOption) -> some View {
        let disk: String = m.installed
            ? "Installed · " + ModelOption.gb(m.onDisk)
            : "Downloads " + ModelOption.gb(m.bytes)
        HStack(spacing: 10) {
            Label(disk, systemImage: m.installed ? "internaldrive" : "arrow.down.circle")
                .foregroundStyle(m.installed ? Color.green : Color.secondary)
            // Everything, not just the weights: the number people want is
            // "what will Fennel be holding if I pick this", and splitting the
            // model from its fixed companions only made that a sum to do.
            Label("~\(ModelOption.gb(m.bytes + chat.overheadBytes + chat.appBytes)) in memory",
                  systemImage: "memorychip")
                .foregroundStyle(Color.secondary)
            // Stated plainly rather than hidden: on a model whose template
            // ignores `tools=`, reminders, timers, the agenda and web search
            // simply do not exist.
            if !m.tools {
                Label("No tools", systemImage: "wrench.and.screwdriver")
                    .foregroundStyle(Color.orange)
            }
        }
        .font(.system(size: 10))
    }

    @ViewBuilder private func trailing(_ m: ModelOption) -> some View {
        VStack(spacing: 6) {
            // A tick, not a chevron: pressing a row no longer navigates
            // anywhere, it just marks which one the button below will open.
            Image(systemName: m.id == (pending ?? chat.setupCurrent)
                  ? "checkmark.circle.fill" : "circle")
                .font(.system(size: 13))
                .foregroundStyle(m.id == (pending ?? chat.setupCurrent)
                                 ? Theme.accentSolid : Color.secondary.opacity(0.4))
            // A model added by hand can always be taken off the list; a listed
            // one only once there is something on disk to remove. Without this
            // a custom row that failed to download, or had not been downloaded
            // yet, was stuck there for good.
            if m.installed || m.custom {
                if confirmingDelete == m.id {
                    HStack(spacing: 4) {
                        Button(m.installed ? "Delete" : "Remove") {
                            chat.deleteModel(m.id)
                            confirmingDelete = nil
                        }
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Color.red)
                        Button("Cancel") { confirmingDelete = nil }
                            .font(.system(size: 10))
                            .foregroundStyle(Color.secondary)
                    }
                    .buttonStyle(.plain)
                } else {
                    Button { confirmingDelete = m.id } label: {
                        Image(systemName: "trash")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                            .frame(width: 20, height: 20)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help(m.installed ? "Delete this download" : "Remove from the list")
                }
            }
        }
    }
}
