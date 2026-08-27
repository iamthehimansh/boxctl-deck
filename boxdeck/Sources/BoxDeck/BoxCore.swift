import Foundation

// MARK: - shell

/// Runs a command off the main thread and returns (stdout, stderr, exit code).
/// Everything the app knows about the box goes through here — `boxctl` for
/// control-plane work and `ssh box` for reads. No SSH library, no daemon: the
/// same commands you would type, so behaviour matches the terminal exactly.
enum Shell {
    @discardableResult
    static func run(_ args: [String], timeout: TimeInterval = 30,
                    input: String? = nil) async -> (out: String, err: String, code: Int32) {
        await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: "/bin/zsh")
                // login shell so ~/.local/bin (boxctl) and brew are on PATH
                p.arguments = ["-lc", args.joined(separator: " ")]
                // Apps launched from Finder/login items inherit a minimal PATH.
                // Supply the CLI and Homebrew locations explicitly so BoxDeck
                // behaves identically whether opened from Terminal or at login.
                var environment = ProcessInfo.processInfo.environment
                let home = FileManager.default.homeDirectoryForCurrentUser.path
                let helpers = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers").path
                let required = [helpers, "\(home)/.local/bin", "/opt/homebrew/bin",
                                "/opt/homebrew/sbin", "/usr/local/bin",
                                "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
                let inherited = environment["PATH"]?.split(separator: ":").map(String.init) ?? []
                var seen = Set<String>()
                environment["PATH"] = (required + inherited).filter { seen.insert($0).inserted }
                    .joined(separator: ":")
                p.environment = environment
                let o = Pipe(), e = Pipe()
                p.standardOutput = o; p.standardError = e
                let inputPipe = input == nil ? nil : Pipe()
                if let inputPipe { p.standardInput = inputPipe }
                var outData = Data(), errData = Data()
                do { try p.run() } catch {
                    cont.resume(returning: ("", "launch failed: \(error)", 127)); return
                }
                if let inputPipe, let input {
                    inputPipe.fileHandleForWriting.write(Data(input.utf8))
                    try? inputPipe.fileHandleForWriting.close()
                }
                let deadline = DispatchTime.now() + timeout
                DispatchQueue.global().asyncAfter(deadline: deadline) {
                    if p.isRunning { p.terminate() }
                }
                outData = o.fileHandleForReading.readDataToEndOfFile()
                errData = e.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                cont.resume(returning: (String(decoding: outData, as: UTF8.self),
                                        String(decoding: errData, as: UTF8.self),
                                        p.terminationStatus))
            }
        }
    }

    /// Shell-quote a path so spaces/quotes in remote filenames can't break the command.
    static func q(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }
}

// MARK: - models

struct RemoteFile: Identifiable, Hashable {
    let name: String
    let path: String
    let isDir: Bool
    let size: Int64
    var id: String { path }

    var sizeText: String {
        if isDir { return "—" }
        let u = ["B", "K", "M", "G", "T"]; var v = Double(size); var i = 0
        while v >= 1024, i < u.count - 1 { v /= 1024; i += 1 }
        return i == 0 ? "\(size) B" : String(format: "%.1f%@", v, u[i])
    }
}

struct RemoteApp: Identifiable, Hashable, Codable {
    let id: String
    let name: String
    let detail: String
    let icon: String
}

struct BoxService: Identifiable, Hashable, Codable {
    let id: String            // systemd unit name (the `unit` key in services.json)
    var label: String         // shown in the UI
    var detail: String        // what it does
    var gpu: Bool = false     // holds the GPU -> mutually exclusive with other gpu:true units
    var active: Bool = false  // runtime only (not persisted)
    var enabled: Bool = false
    var busy: Bool = false

    enum CodingKeys: String, CodingKey { case id = "unit", label, detail, gpu }
}

struct BoxStatus {
    var route = "…"           // LAN or remote
    var sshOK = false
    var keyHours: Double?     // session-key hours remaining
    var passkey = false
    var passkeyDays: Double?
    var passkeyExpired = false
    var tunnels: [Int: Bool] = [:]
    var gpuUsedMB: Int?
    var gpuTotalMB: Int?
    var serveOK = false
}

// MARK: - box controller

/// Start in the remote home directory; the real path is discovered on connect so
/// no username is baked into the source.
let NSHomeDirectoryOnBox = "~"

@MainActor
final class BoxModel: ObservableObject {
    @Published var status = BoxStatus()
    /// Loaded from ~/services.json ON THE BOX — add an entry there (or via the +
    /// button / the `box-service` skill) and it appears here automatically.
    @Published var services: [BoxService] = []
    @Published var cwd = NSHomeDirectoryOnBox      // resolved on first listing
    @Published var entries: [RemoteFile] = []
    @Published var selected: RemoteFile?
    @Published var apps: [RemoteApp] = []
    @Published var appsLoading = false
    @Published var guiReady = false
    @Published var loading = false
    @Published var banner: String?
    @Published var log: [String] = []
    @Published var launchAtLogin = false      // refreshed after launch
    @Published var authBusy = false
    @Published var stats = BoxStats()
    @Published var mac = MacStats()
    /// Rolling history for the menu-bar sparkline (newest last).
    @Published var gpuHistory: [Double] = []
    @Published var vramHistory: [Double] = []
    @Published var cpuHistory: [Double] = []
    @Published var ramHistory: [Double] = []
    private let historyLen = 40
    private var pollTask: Task<Void, Never>?


    private var tunnelStarted = false

    func setLaunchAtLogin(_ on: Bool) {
        if let err = LoginItem.set(on) {
            banner = err
        } else {
            launchAtLogin = on
            note(on ? "will launch at login" : "won't launch at login")
        }
        launchAtLogin = LoginItem.isEnabled          // trust the system, not our guess
    }

    func note(_ s: String) {
        log.append("\(Self.ts()) \(s)")
        if log.count > 200 { log.removeFirst(log.count - 200) }
    }

    static func ts() -> String {
        let f = DateFormatter(); f.dateFormat = "HH:mm:ss"; return f.string(from: Date())
    }

    // ---- lifecycle -------------------------------------------------------

    /// Start the tunnel keeper when the app opens. boxctl owns the keeper, so we
    /// never spawn a second one (two keepers = a Touch ID popup storm).
    func appDidLaunch() async {
        await refreshStatus()
        await loadServices()
        if !tunnelStarted {
            tunnelStarted = true
            note("starting tunnels (boxctl)")
            _ = await Shell.run(["boxctl", "tunnel", "start"], timeout: 60)
            await refreshStatus()
        }
        await listDir(cwd)
        await loadApps()
        launchAtLogin = LoginItem.isEnabled
        startPolling()
    }

    /// Poll resource usage. Rides the existing ControlMaster connection, so it is
    /// one cheap round-trip and never triggers a Touch ID prompt.
    func startPolling(every seconds: UInt64 = 5) {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            var tick = 0
            while !Task.isCancelled {
                let b = await StatsReader.box()
                let m = await StatsReader.mac()
                await MainActor.run {
                    guard let self else { return }
                    self.stats = b
                    self.mac = m
                    // seed a full window on the first sample so the menu-bar chart
                    // is a chart immediately instead of a blank strip
                    if let g = b.gpuUtil {
                        if self.gpuHistory.isEmpty {
                            self.gpuHistory = Array(repeating: Double(g) / 100, count: self.historyLen)
                        } else { self.push(&self.gpuHistory, Double(g) / 100) }
                    }
                    if let v = b.vramPct {
                        if self.vramHistory.isEmpty {
                            self.vramHistory = Array(repeating: Double(v) / 100, count: self.historyLen)
                        } else { self.push(&self.vramHistory, Double(v) / 100) }
                    }
                    if let c = b.cpuPct {
                        if self.cpuHistory.isEmpty {
                            self.cpuHistory = Array(repeating: Double(c) / 100, count: self.historyLen)
                        } else { self.push(&self.cpuHistory, Double(c) / 100) }
                    }
                    if let r = b.ramPct {
                        if self.ramHistory.isEmpty {
                            self.ramHistory = Array(repeating: Double(r) / 100, count: self.historyLen)
                        } else { self.push(&self.ramHistory, Double(r) / 100) }
                    }
                }
                tick += 1
                if tick.isMultiple(of: 3) { await self?.refreshStatus() }
                if tick.isMultiple(of: 6) { await self?.refreshServices() }
                try? await Task.sleep(nanoseconds: seconds * 1_000_000_000)
            }
        }
    }

    private func push(_ arr: inout [Double], _ v: Double) {
        arr.append(max(0, min(1, v)))
        if arr.count > historyLen { arr.removeFirst(arr.count - historyLen) }
    }

    func stopPolling() { pollTask?.cancel(); pollTask = nil }

    /// Tear the keeper down with the app — "attached to this app only".
    func appWillQuit() {
        let p = Process()
        let bundled = Bundle.main.bundleURL
            .appendingPathComponent("Contents/Helpers/boxctl").path
        let installed = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/bin/boxctl").path
        let boxctl = FileManager.default.isExecutableFile(atPath: bundled) ? bundled : installed
        p.executableURL = URL(fileURLWithPath: boxctl)
        p.arguments = ["tunnel", "stop"]
        try? p.run()
        p.waitUntilExit()
    }

    // ---- status ----------------------------------------------------------

    // ---- services.json (lives on the BOX, single source of truth) ----------

    static let servicesPath = "~/services.json"
    static let defaultServices: [BoxService] = [
        .init(id: "omni-voice", label: "Omni voice",
              detail: "Qwen2.5-Omni audio→audio serve on :8011 (Intern's voice)", gpu: true),
        .init(id: "vllm-brain", label: "gpt-oss brain",
              detail: "vLLM gpt-oss-20b — tools/agent tasks", gpu: true),
    ]

    /// Read ~/services.json from the box. Seeds it with the known units the first
    /// time so the file always exists and is editable by hand or by the skill.
    func loadServices() async {
        let r = await Shell.run(["ssh", "-o", "BatchMode=yes",
                                 "-o", "IdentityAgent=none", "box",
                                 Shell.q("cat \(Self.servicesPath) 2>/dev/null")], timeout: 30)
        guard r.code == 0 else {
            let reason = r.err.split(separator: "\n").last.map(String.init) ?? "SSH failed"
            note("cannot load services — \(reason)")
            banner = "Services unavailable: \(reason.prefix(120))"
            return
        }
        let data = Data(r.out.utf8)
        if let wrapper = try? JSONDecoder().decode([String: [BoxService]].self, from: data),
           let list = wrapper["services"], !list.isEmpty {
            merge(list)
        } else if let list = try? JSONDecoder().decode([BoxService].self, from: data), !list.isEmpty {
            merge(list)                       // tolerate a bare array
        } else {
            note("no services.json on box — seeding defaults")
            services = Self.defaultServices
            await saveServices()
        }
        await refreshServices()
    }

    private func merge(_ list: [BoxService]) {
        // keep runtime flags for units we already know about
        let old = Dictionary(uniqueKeysWithValues: services.map { ($0.id, $0) })
        services = list.map { var n = $0
            if let o = old[$0.id] { n.active = o.active; n.enabled = o.enabled }
            return n
        }
    }

    /// Write the list back to the box (base64 to survive any quoting).
    func saveServices() async {
        let enc = JSONEncoder(); enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let json = try? enc.encode(["services": services]),
              let b64 = String(data: json.base64EncodedData(), encoding: .utf8) else { return }
        let cmd = "printf %s \(b64) | base64 -d > \(Self.servicesPath)"
        let r = await Shell.run(["ssh", "-o", "BatchMode=yes",
                                 "-o", "IdentityAgent=none", "box", Shell.q(cmd)], timeout: 30)
        if r.code != 0 { banner = "save services.json: \(r.err.prefix(120))" }
        else { note("services.json saved (\(services.count) services)") }
    }

    func restart(_ svc: BoxService) async {
        note("restart \(svc.label)")
        _ = await Shell.run(["ssh", "-o", "BatchMode=yes",
                             "-o", "IdentityAgent=none", "box",
                             Shell.q("systemctl --user restart \(svc.id)")], timeout: 120)
        await refreshServices()
    }

    func addService(unit: String, label: String, detail: String, gpu: Bool) async {
        let u = unit.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !u.isEmpty, !services.contains(where: { $0.id == u }) else {
            banner = u.isEmpty ? "unit name required" : "\(u) already in the list"; return
        }
        services.append(.init(id: u, label: label.isEmpty ? u : label, detail: detail, gpu: gpu))
        note("added service \(u)")
        await saveServices()
        await refreshServices()
    }

    func removeService(_ svc: BoxService) async {
        services.removeAll { $0.id == svc.id }
        note("removed service \(svc.id)")
        await saveServices()
    }

    func refreshAll() async {
        await refreshStatus()
        await refreshServices()
    }

    func refreshStatus() async {
        let r = await Shell.run(["boxctl", "status", "--quick"], timeout: 20)
        guard r.code == 0 else {
            let reason = (r.err.isEmpty ? r.out : r.err).split(separator: "\n").last
                .map(String.init) ?? "boxctl failed"
            note("status unavailable — \(reason)")
            banner = "Status unavailable: \(reason.prefix(120))"
            return
        }
        var s = BoxStatus()
        for line in r.out.split(separator: "\n").map(String.init) {
            let plain = line.replacingOccurrences(of: "\u{1B}[[0-9;]*m", with: "",
                                                  options: .regularExpression)
            if plain.contains("route") {
                s.route = plain.contains("LAN") ? "LAN (direct)" : "remote (cloudflared)"
            }
            if plain.contains("auth") && plain.contains("Touch ID") { s.passkey = true }
            if plain.contains("Touch ID EXPIRED") || plain.contains("needs 30-day") {
                s.passkeyExpired = true
            }
            if plain.contains("auth"), let d = plain.firstMatch(#"([0-9.]+)d left"#) {
                s.passkeyDays = Double(d)
            }
            if plain.contains("session key"), let h = plain.firstMatch(#"([0-9.]+)h left"#) {
                s.keyHours = Double(h)
            }
            if plain.contains("ssh box") { s.sshOK = plain.contains("reachable") }
            if plain.contains("tunnel :8011") { s.tunnels[8011] = plain.contains("open") }
            if plain.contains("tunnel :11435") { s.tunnels[11435] = plain.contains("open") }
            if plain.contains("omni serve") { s.serveOK = plain.contains("ok,") }
            if plain.contains("gpu"), let used = plain.firstMatch(#"([0-9]+) MiB"#) {
                s.gpuUsedMB = Int(used)
                let all = plain.matches(#"([0-9]+) MiB"#)
                if all.count > 1 { s.gpuTotalMB = Int(all[1]) }
            }
        }
        status = s
    }

    func refreshServices() async {
        for i in services.indices {
            let unit = services[i].id
            let r = await Shell.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "IdentityAgent=none", "box",
                 Shell.q("systemctl --user is-active \(unit); systemctl --user is-enabled \(unit)")],
                timeout: 40)
            let lines = r.out.split(separator: "\n").map(String.init)
            // Long-running Type=oneshot units remain "activating" until their
            // command finishes, even though their workload and GPU usage are live.
            // Present both active and activating as on in the service switch.
            let state = lines.first?.trimmingCharacters(in: .whitespaces)
            services[i].active = state == "active" || state == "activating"
            services[i].enabled = lines.count > 1 &&
                lines[1].trimmingCharacters(in: .whitespaces) == "enabled"
        }
    }

    func toggle(_ svc: BoxService) async {
        guard let i = services.firstIndex(where: { $0.id == svc.id }) else { return }
        let turningOn = !services[i].active
        // GPU is exclusive — stop the other one first instead of failing cryptically.
        if turningOn, services[i].gpu {
            for j in services.indices where services[j].id != svc.id
                && services[j].gpu && services[j].active {
                note("stopping \(services[j].label) — the 16GB GPU fits only one")
                _ = await Shell.run(["ssh", "-o", "BatchMode=yes",
                                     "-o", "IdentityAgent=none", "box",
                                     Shell.q("systemctl --user stop \(services[j].id)")], timeout: 60)
                services[j].active = false
            }
        }
        services[i].busy = true
        let verb = turningOn ? "start" : "stop"
        note("\(verb) \(svc.label)…")
        let r = await Shell.run(["ssh", "-o", "BatchMode=yes",
                                 "-o", "IdentityAgent=none", "box",
                                 Shell.q("systemctl --user \(verb) \(svc.id)")], timeout: 120)
        if r.code != 0 { banner = "\(svc.label): \(r.err.prefix(120))" }
        // starting the omni serve takes ~60s to load the model
        if turningOn { try? await Task.sleep(nanoseconds: 3_000_000_000) }
        services[i].busy = false
        await refreshServices()
        await refreshStatus()
    }

    // ---- file tree -------------------------------------------------------

    func listDir(_ path: String) async {
        loading = true
        defer { loading = false }
        // -A: include dotfiles, skip . and ..  |  emit: type<TAB>size<TAB>name
        let target = path == "~" ? "$HOME" : Shell.q(path)
        let cmd = "cd \(target) 2>/dev/null && pwd 1>&2 && ls -Ap --file-type 2>/dev/null | head -500 | " +
                  "while IFS= read -r n; do s=$(stat -c%s \"${n%/}\" 2>/dev/null || echo 0); " +
                  "printf '%s\\t%s\\t%s\\n' \"$([ -d \"${n%/}\" ] && echo d || echo f)\" \"$s\" \"${n%/}\"; done"
        let r = await Shell.run(["ssh", "-o", "BatchMode=yes",
                                 "-o", "IdentityAgent=none", "box", Shell.q(cmd)], timeout: 45)
        guard r.code == 0 || !r.out.isEmpty else {
            banner = "cannot read \(path): \(r.err.prefix(100))"
            return
        }
        var out: [RemoteFile] = []
        for line in r.out.split(separator: "\n") {
            let f = line.split(separator: "\t", omittingEmptySubsequences: false).map(String.init)
            guard f.count >= 3 else { continue }
            let name = f[2]
            let full = path.hasSuffix("/") ? path + name : path + "/" + name
            out.append(.init(name: name, path: full, isDir: f[0] == "d",
                             size: Int64(f[1]) ?? 0))
        }
        entries = out.sorted { a, b in
            a.isDir == b.isDir ? a.name.lowercased() < b.name.lowercased() : a.isDir && !b.isDir
        }
        // the shell prints the resolved directory on stderr, so "~" becomes real
        let resolved = r.err.split(separator: "\n").first.map(String.init)?
            .trimmingCharacters(in: .whitespaces) ?? ""
        cwd = resolved.hasPrefix("/") ? resolved : path
    }

    func open(_ f: RemoteFile) async {
        if f.isDir { await listDir(f.path) } else { selected = f }
    }

    func up() async {
        let parent = (cwd as NSString).deletingLastPathComponent
        await listDir(parent.isEmpty ? "/" : parent)
    }

    func openInVSCode(_ path: String) async {
        note("VS Code → \(path)")
        let r = await Shell.run(["boxctl", "code", Shell.q(path)], timeout: 45)
        if r.code != 0 { banner = "VS Code: \(r.err.prefix(120))" }
    }

    // ---- seamless GUI applications -------------------------------------

    func loadApps() async {
        appsLoading = true
        defer { appsLoading = false }
        async let check = Shell.run(["boxctl", "gui", "check"], timeout: 35)
        async let listing = Shell.run(["boxctl", "gui", "apps"], timeout: 60)
        let (ready, result) = await (check, listing)
        guiReady = ready.code == 0
        guard result.code == 0, let data = result.out.data(using: .utf8),
              let decoded = try? JSONDecoder().decode([RemoteApp].self, from: data) else {
            note("cannot load GUI applications")
            if result.code != 0 { banner = "Apps: \((result.err.isEmpty ? result.out : result.err).prefix(120))" }
            return
        }
        apps = decoded
        note("loaded \(decoded.count) GUI applications")
    }

    func launch(_ app: RemoteApp, microphone: Bool = false) async {
        note("GUI → \(app.name)")
        var args = ["boxctl", "gui", "launch", "--desktop", Shell.q(app.id)]
        if microphone { args.append("--microphone") }
        let r = await Shell.run(args, timeout: 45)
        let clean = (r.out + r.err).replacingOccurrences(
            of: "\u{1B}[[0-9;]*m", with: "", options: .regularExpression)
        banner = clean.split(separator: "\n").last.map(String.init)
        if r.code == 3 { guiReady = false }
    }

    func launchGUICommand(_ command: String, microphone: Bool = false) async {
        let value = command.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return }
        note("GUI command → \(value)")
        let encoded = Data(value.utf8).base64EncodedString()
        // Decode locally inside zsh so arbitrary command punctuation stays one
        // argument to boxctl rather than being interpreted on the Mac.
        let mic = microphone ? " --microphone" : ""
        let shell = "boxctl gui launch\(mic) \"$(printf %s \(Shell.q(encoded)) | base64 -D)\""
        let r = await Shell.run([shell], timeout: 45)
        let clean = (r.out + r.err).replacingOccurrences(
            of: "\u{1B}[[0-9;]*m", with: "", options: .regularExpression)
        banner = clean.split(separator: "\n").last.map(String.init)
        if r.code == 3 { guiReady = false }
    }

    /// Open Terminal already ssh'd into the box at `path`.
    /// Uses a temp `.command` file instead of `osascript -e "…do script…"`: that
    /// route nests three levels of quoting (zsh -lc → osascript → Terminal) and
    /// broke on the single quotes in the remote `cd`.
    func openTerminal(at path: String) async {
        let f = FileManager.default.temporaryDirectory
            .appendingPathComponent("boxdeck-\(UUID().uuidString.prefix(8)).command")
        let script = """
        #!/bin/zsh
        exec ssh -t box "cd \(path.replacingOccurrences(of: "\"", with: "\\\"")) && exec \\$SHELL -l"
        """
        do {
            try script.write(to: f, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: f.path)
        } catch {
            banner = "terminal: \(error.localizedDescription)"
            return
        }
        note("terminal → \(path)")
        let r = await Shell.run(["open", "-a", "Terminal", Shell.q(f.path)], timeout: 20)
        if r.code != 0 { banner = "terminal: \(r.err.prefix(120))" }
    }

    /// Turn the tunnel keeper on/off from the menu bar.
    func setTunnels(on: Bool) async {
        note(on ? "starting tunnels" : "stopping tunnels")
        _ = await Shell.run(["boxctl", "tunnel", on ? "start" : "stop"], timeout: 60)
        await refreshStatus()
    }

    func reconnect() async {
        guard !authBusy else { return }
        authBusy = true
        defer { authBusy = false }
        note("boxctl connect (Touch ID if the key expired)")
        let r = await Shell.run(["boxctl", "connect", "--touch-id"], timeout: 180)
        banner = r.out.replacingOccurrences(of: "\u{1B}[[0-9;]*m", with: "",
                                            options: .regularExpression)
            .split(separator: "\n").last.map(String.init)
        await refreshAll()
    }

    func loginWithTOTP(password: String, code: String, remote: Bool) async -> Bool {
        guard !authBusy else { return false }
        authBusy = true
        defer { authBusy = false }
        note("boxctl connect (password + TOTP)")
        guard let data = try? JSONSerialization.data(
            withJSONObject: ["password": password, "totp": code]),
              let payload = String(data: data, encoding: .utf8) else {
            banner = "Could not prepare credentials"
            return false
        }
        var args = ["boxctl", "connect", "--totp", "--stdin-json"]
        if remote { args.append("--remote") }
        let r = await Shell.run(args, timeout: 180, input: payload)
        let clean = (r.out + r.err).replacingOccurrences(
            of: "\u{1B}[[0-9;]*m", with: "", options: .regularExpression)
        banner = clean.split(separator: "\n").last.map(String.init)
        await refreshAll()
        return r.code == 0
    }
}

// MARK: - tiny regex helpers

extension String {
    func firstMatch(_ pattern: String) -> String? { matches(pattern).first }

    func matches(_ pattern: String) -> [String] {
        guard let re = try? NSRegularExpression(pattern: pattern) else { return [] }
        let ns = self as NSString
        return re.matches(in: self, range: NSRange(location: 0, length: ns.length)).compactMap {
            $0.numberOfRanges > 1 ? ns.substring(with: $0.range(at: 1)) : nil
        }
    }
}
