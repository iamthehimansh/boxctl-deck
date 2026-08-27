import SwiftUI
import AppKit

@main
struct BoxDeckApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var box = BoxModel()

    var body: some Scene {
        WindowGroup("BoxDeck", id: "main") {
            ContentView()
                .environmentObject(box)
                .frame(minWidth: 900, minHeight: 560)
                .task {
                    delegate.box = box
                    await box.appDidLaunch()
                }
        }
        .windowStyle(.titleBar)
        .commands { CommandGroup(replacing: .newItem) {} }

        // The menu-bar item is an AppKit NSStatusItem (see StatusItem.swift):
        // MenuBarExtra can only show simple content, so its chart had to be an
        // NSImage — which renders at 1x and looks fuzzy on Retina.
    }
}

/// Owns the teardown: the tunnel keeper lives and dies with this app.
final class AppDelegate: NSObject, NSApplicationDelegate {
    var box: BoxModel? {
        didSet {
            guard let b = box else { return }
            Task { @MainActor in
                if statusItem == nil { statusItem = StatusItemController() }
                statusItem?.install(box: b)
            }
        }
    }
    @MainActor private var statusItem: StatusItemController?
    /// Closing the window leaves BoxDeck running in the menu bar (that is where
    /// the live chart and the controls live). Quit explicitly from the panel.
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }
    func applicationWillTerminate(_ n: Notification) {
        MainActor.assumeIsolated { box?.appWillQuit() }
    }
}

// MARK: - root

struct ContentView: View {
    @EnvironmentObject var box: BoxModel

    var body: some View {
        VStack(spacing: 0) {
            StatusBar()
            Divider()
            HSplitView {
                VStack(spacing: 0) {
                    StatsCard()
                    Divider()
                    ServicesPanel()
                    Divider()
                    ActivityLog()
                }
                .frame(minWidth: 300, idealWidth: 340, maxWidth: 420)
                RemoteWorkspace()
                    .frame(minWidth: 480)
            }
        }
        .overlay(alignment: .top) {
            if let b = box.banner {
                Text(b)
                    .font(.callout)
                    .padding(.horizontal, 14).padding(.vertical, 8)
                    .background(.regularMaterial, in: Capsule())
                    .overlay(Capsule().stroke(.orange.opacity(0.5)))
                    .padding(.top, 8)
                    .onTapGesture { box.banner = nil }
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .animation(.easeInOut(duration: 0.2), value: box.banner)
    }
}

struct RemoteWorkspace: View {
    var body: some View {
        TabView {
            FileBrowser()
                .tabItem { Label("Files", systemImage: "folder") }
            AppDrawer()
                .tabItem { Label("Apps", systemImage: "square.grid.2x2") }
        }
        .padding(.top, 4)
    }
}

struct AppDrawer: View {
    @EnvironmentObject var box: BoxModel
    @State private var search = ""
    @State private var command = ""

    private var filtered: [RemoteApp] {
        guard !search.isEmpty else { return box.apps }
        return box.apps.filter { $0.name.localizedCaseInsensitiveContains(search) ||
            $0.detail.localizedCaseInsensitiveContains(search) }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Search \(box.apps.count) applications", text: $search)
                    .textFieldStyle(.roundedBorder)
                if box.appsLoading { ProgressView().controlSize(.small) }
                Button { Task { await box.loadApps() } } label: {
                    Image(systemName: "arrow.clockwise")
                }.help("Reload installed applications")
            }.padding(12)

            if !box.guiReady {
                Label("Xpra must be installed on this Mac and the box before applications can open.",
                      systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(.orange)
                    .padding(.horizontal, 12).padding(.bottom, 8)
            }
            Divider()
            List(filtered) { app in
                HStack(spacing: 10) {
                    Image(systemName: "app.dashed")
                        .font(.title2).foregroundStyle(.blue).frame(width: 28)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(app.name).fontWeight(.medium)
                        if !app.detail.isEmpty {
                            Text(app.detail).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                        }
                    }
                    Spacer()
                    Button("Launch") { Task { await box.launch(app) } }
                        .buttonStyle(.borderedProminent).controlSize(.small)
                        .disabled(!box.guiReady)
                }.padding(.vertical, 4)
            }.listStyle(.inset)

            Divider()
            HStack(spacing: 8) {
                Image(systemName: "terminal")
                TextField("Run GUI command on box, e.g. gedit", text: $command)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { runCommand() }
                Button("Run") { runCommand() }
                    .buttonStyle(.borderedProminent)
                    .disabled(command.trimmingCharacters(in: .whitespaces).isEmpty || !box.guiReady)
            }.padding(12)
        }
    }

    private func runCommand() {
        let value = command
        command = ""
        Task { await box.launchGUICommand(value) }
    }
}

// MARK: - status

struct StatusBar: View {
    @EnvironmentObject var box: BoxModel
    @State private var showingTOTP = false

    var body: some View {
        HStack(spacing: 16) {
            Label {
                Text(box.status.sshOK ? "box connected" : "box offline").fontWeight(.medium)
            } icon: {
                Circle().fill(box.status.sshOK ? .green : .red).frame(width: 9, height: 9)
            }

            chip("route", box.status.route)

            if box.status.passkey {
                chip("auth", box.status.passkeyExpired ? "Touch ID expired"
                     : box.status.passkeyDays.map { String(format: "Touch ID %.0fd", $0) }
                     ?? "Touch ID", tint: box.status.passkeyExpired ? .orange : .blue)
            }
            if let h = box.status.keyHours {
                chip("key", h > 0 ? String(format: "%.1fh", h) : "expired",
                     tint: h > 1 ? .secondary : .orange)
            }
            let up = box.status.tunnels.values.filter { $0 }.count
            chip("tunnels", "\(up)/\(max(box.status.tunnels.count, 2))",
                 tint: up >= 2 ? .secondary : .orange)
            if let u = box.status.gpuUsedMB, let t = box.status.gpuTotalMB {
                chip("gpu", "\(u / 1024)/\(t / 1024) GB")
            }

            Spacer()

            Menu {
                Button { Task { await box.reconnect() } } label: {
                    Label("Renew with Touch ID", systemImage: "touchid")
                }
                Button { showingTOTP = true } label: {
                    Label("Password + TOTP…", systemImage: "number.square")
                }
            } label: {
                Label(box.authBusy ? "Connecting…" : "Authenticate", systemImage: "person.badge.key")
            }
            .disabled(box.authBusy)
            .help("Renew with Touch ID or recover with password + TOTP")

            Button { Task { await box.refreshAll() } } label: {
                Image(systemName: "arrow.clockwise")
            }
            .help("Refresh status and services")
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .sheet(isPresented: $showingTOTP) { TOTPLoginSheet() }
    }

    @ViewBuilder
    func chip(_ k: String, _ v: String, tint: Color = .secondary) -> some View {
        HStack(spacing: 5) {
            Text(k).font(.caption2).foregroundStyle(.tertiary)
            Text(v).font(.caption).foregroundStyle(tint).fontWeight(.medium)
        }
        .padding(.horizontal, 8).padding(.vertical, 4)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
    }
}

struct TOTPLoginSheet: View {
    @EnvironmentObject var box: BoxModel
    @Environment(\.dismiss) private var dismiss
    @State private var password = ""
    @State private var code = ""
    @State private var remote = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Authenticate to the box", systemImage: "lock.shield")
                .font(.headline)
            Text("Use this if the 30-day Touch ID authorization expired. Credentials are sent directly to boxctl and are never saved.")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Form {
                SecureField("SSH password", text: $password)
                TextField("6-digit TOTP", text: $code)
                    .textContentType(.oneTimeCode)
                Toggle("Force public domain (outside home)", isOn: $remote)
            }
            .formStyle(.grouped)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button {
                    Task {
                        if await box.loginWithTOTP(password: password, code: code,
                                                   remote: remote) { dismiss() }
                    }
                } label: {
                    if box.authBusy { ProgressView().controlSize(.small) }
                    else { Text("Connect") }
                }
                .buttonStyle(.borderedProminent)
                .disabled(password.isEmpty || code.count != 6 || box.authBusy)
            }
        }
        .padding(20)
        .frame(width: 440)
    }
}

// MARK: - stats

struct StatsCard: View {
    @EnvironmentObject var box: BoxModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Resources").font(.headline)
                Spacer()
                HStack(spacing: 8) {
                    if let g = box.stats.gpuTempC { Text("GPU \(g)°C") }
                    if let c = box.stats.cpuTempC { Text("CPU \(c)°C") }
                    if let n = box.stats.nvmeTempC { Text("NVMe \(n)°C") }
                }
                .font(.caption).foregroundStyle(.secondary)
            }
            LiveChart(series: [
                .init(id: "gpu", name: "GPU", color: .green, values: box.gpuHistory,
                      current: box.stats.gpuUtil.map { "\($0)%" } ?? "—"),
                .init(id: "vram", name: "VRAM", color: .cyan, values: box.vramHistory,
                      current: BoxStats.pair(box.stats.vramUsedMB, box.stats.vramTotalMB)),
                .init(id: "cpu", name: "CPU", color: .orange, values: box.cpuHistory,
                      current: box.stats.cpuPct.map { "\($0)%" } ?? "—"),
                .init(id: "ram", name: "RAM", color: .purple, values: box.ramHistory,
                      current: BoxStats.pair(box.stats.ramUsedMB, box.stats.ramTotalMB)),
            ], height: 120)

        }
        .padding(14)
    }
}

// MARK: - services

struct ServicesPanel: View {
    @EnvironmentObject var box: BoxModel
    @State private var adding = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("AI services").font(.headline)
                Spacer()
                Button { adding = true } label: { Image(systemName: "plus") }
                    .help("Add a service (writes ~/services.json on the box)")
                Button { Task { await box.loadServices() } } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Reload services.json from the box")
            }
            .buttonStyle(.borderless)
            Text("From ~/services.json on the box. GPU services stop each other (one 16 GB card).")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            ForEach(box.services) { s in
                HStack(alignment: .top, spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(s.label).fontWeight(.medium)
                            if s.enabled {
                                Text("auto-start").font(.caption2)
                                    .padding(.horizontal, 5).padding(.vertical, 1)
                                    .background(.quaternary, in: Capsule())
                            }
                            if s.gpu {
                                Text("GPU").font(.caption2).foregroundStyle(.orange)
                                    .padding(.horizontal, 5).padding(.vertical, 1)
                                    .background(.orange.opacity(0.15), in: Capsule())
                            }
                        }
                        Text(s.detail).font(.caption).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 8)
                    if s.busy {
                        ProgressView().controlSize(.small)
                    } else {
                        Toggle("", isOn: Binding(
                            get: { s.active },
                            set: { _ in Task { await box.toggle(s) } }))
                        .labelsHidden()
                        .toggleStyle(.switch)
                    }
                }
                .padding(10)
                .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 8))
                .contextMenu {
                    Button("Remove \(s.label)", role: .destructive) {
                        Task { await box.removeService(s) }
                    }
                    Button("Restart") { Task { await box.restart(s) } }
                }
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .sheet(isPresented: $adding) { AddServiceSheet() }
    }
}

/// Add a systemd unit to ~/services.json on the box.
struct AddServiceSheet: View {
    @EnvironmentObject var box: BoxModel
    @Environment(\.dismiss) private var dismiss
    @State private var unit = ""
    @State private var label = ""
    @State private var detail = ""
    @State private var gpu = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Add a service").font(.headline)
            Text("Any `systemctl --user` unit on the box.")
                .font(.caption).foregroundStyle(.secondary)
            Form {
                TextField("unit name", text: $unit, prompt: Text("e.g. omni-voice"))
                TextField("label", text: $label, prompt: Text("shown in the app"))
                TextField("what it does", text: $detail, prompt: Text("short description"))
                Toggle("Uses the GPU (stops other GPU services)", isOn: $gpu)
            }
            .formStyle(.grouped)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Add") {
                    Task { await box.addService(unit: unit, label: label, detail: detail, gpu: gpu) }
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .disabled(unit.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(18)
        .frame(width: 420)
    }
}

// MARK: - activity

struct ActivityLog: View {
    @EnvironmentObject var box: BoxModel

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Activity").font(.caption).foregroundStyle(.secondary)
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(box.log.enumerated().reversed()), id: \.offset) { _, line in
                        Text(line).font(.system(.caption2, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .frame(maxHeight: 170)
    }
}

// MARK: - file browser

struct FileBrowser: View {
    @EnvironmentObject var box: BoxModel
    @State private var pathField = ""

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Button { Task { await box.up() } } label: { Image(systemName: "arrow.up") }
                    .help("Parent folder")
                TextField("path on box", text: $pathField)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { Task { await box.listDir(pathField) } }
                if box.loading { ProgressView().controlSize(.small) }
            }
            .padding(.horizontal, 12).padding(.vertical, 8)
            .onAppear { pathField = box.cwd }
            .onChange(of: box.cwd) { pathField = $0 }

            Divider()

            List(box.entries, selection: Binding(
                get: { box.selected.map { Set([$0.id]) } ?? [] },
                set: { ids in box.selected = box.entries.first { ids.contains($0.id) } })
            ) { f in
                HStack(spacing: 8) {
                    Image(systemName: f.isDir ? "folder.fill" : "doc")
                        .foregroundStyle(f.isDir ? .blue : .secondary)
                    Text(f.name).lineLimit(1)
                    Spacer()
                    Text(f.sizeText).font(.caption).foregroundStyle(.tertiary)
                }
                .contentShape(Rectangle())
                .onTapGesture(count: 2) { Task { await box.open(f) } }
                .tag(f.id)
            }
            .listStyle(.inset)

            Divider()
            SelectionBar()
        }
    }
}

struct SelectionBar: View {
    @EnvironmentObject var box: BoxModel

    var body: some View {
        HStack(spacing: 10) {
            if let s = box.selected {
                Image(systemName: s.isDir ? "folder" : "doc")
                Text(s.path).font(.system(.caption, design: .monospaced)).lineLimit(1).truncationMode(.head)
            } else {
                Text("select a file or folder — double-click to open a folder")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            let target = box.selected?.path ?? box.cwd
            Button("Copy path") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(target, forType: .string)
                box.banner = "copied \(target)"
            }
            Button("Terminal") { Task { await box.openTerminal(at: box.selected?.isDir == true ? target : box.cwd) } }
            Button("Open in VS Code") { Task { await box.openInVSCode(target) } }
                .buttonStyle(.borderedProminent)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }
}
