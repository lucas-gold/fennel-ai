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

    var body: some View {
        VStack(spacing: 14) {
            VStack(spacing: 5) {
                Text("Choose a model").font(Theme.title(17, .semibold))
                Text("Only one runs at a time. You can change it next launch.")
                    .font(.system(size: 11.5)).foregroundStyle(.secondary)
            }

            if !chat.setupNote.isEmpty {
                Label(chat.setupNote, systemImage: "exclamationmark.circle.fill")
                    .font(.system(size: 11)).foregroundStyle(.orange)
            }

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

            confirmBar

            Text("Downloaded models live in ~/.cache/huggingface and can be removed here at any time.")
                .font(.system(size: 10)).foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
        }
        .padding(.horizontal, 22)
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

    @ViewBuilder private var confirmBar: some View {
        if let m = chosen {
            VStack(spacing: 6) {
                Button {
                    chat.chooseModel(m.id)
                } label: {
                    Text(m.installed ? "Open \(m.name)"
                                     : "Download \(ModelOption.gb(m.bytes)) and open \(m.name)")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.defaultAction)

                if !m.installed {
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
                Text(m.focus)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .multilineTextAlignment(.leading)
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
            if m.id == chat.setupCurrent {
                Text("LAST USED")
                    .font(.system(size: 8.5, weight: .bold))
                    .padding(.horizontal, 5)
                    .padding(.vertical, 2)
                    .background(Capsule().fill(Theme.accentSolid.opacity(0.25)))
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
            Label(String(format: "~%.1f GB in memory", m.ram), systemImage: "memorychip")
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
            if m.installed {
                if confirmingDelete == m.id {
                    HStack(spacing: 4) {
                        Button("Delete") {
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
                    .help("Delete this download")
                }
            }
        }
    }
}
