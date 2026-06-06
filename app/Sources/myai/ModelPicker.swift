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
            .frame(maxHeight: 430)

            Text("Downloaded models live in ~/.cache/huggingface and can be removed here at any time.")
                .font(.system(size: 10)).foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
        }
        .padding(.horizontal, 22)
    }

    private func row(_ m: ModelOption) -> some View {
        Button { chat.chooseModel(m.id) } label: { rowBody(m) }
            .buttonStyle(.plain)
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
        let selected = m.id == chat.setupCurrent
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
            Image(systemName: "chevron.right")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.tertiary)
            if m.installed {
                Button { chat.deleteModel(m.id) } label: {
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
