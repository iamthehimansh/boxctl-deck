import Foundation

/// Live resource usage. `box*` come from one ssh round-trip; `mac*` are read
/// locally so the menu bar still shows something useful when the box is offline.
struct BoxStats: Equatable {
    var gpuUtil: Int?          // %
    var vramUsedMB: Int?
    var vramTotalMB: Int?
    var gpuTempC: Int?
    var ramUsedMB: Int?
    var ramTotalMB: Int?
    var cpuPct: Int?
    var load1: Double?
    var ncpu: Int?
    var disks: [DiskUsage] = []
    var cpuTempC: Int?
    var nvmeTempC: Int?
    var stale = true           // true until the first successful poll

    var vramPct: Int? {
        guard let u = vramUsedMB, let t = vramTotalMB, t > 0 else { return nil }
        return Int(Double(u) / Double(t) * 100)
    }
    var ramPct: Int? {
        guard let u = ramUsedMB, let t = ramTotalMB, t > 0 else { return nil }
        return Int(Double(u) / Double(t) * 100)
    }
    /// "5.3/15G" — compact enough for the narrow menu-bar legend.
    static func pair(_ used: Int?, _ total: Int?) -> String {
        guard let u = used, let t = total else { return "—" }
        return String(format: "%.1f/%.0fG", Double(u) / 1024, Double(t) / 1024)
    }

    static func gb(_ mb: Int?) -> String {
        guard let mb else { return "—" }
        return String(format: "%.1fG", Double(mb) / 1024)
    }
}

struct DiskUsage: Identifiable, Equatable {
    let mount: String, used: String, total: String, pct: String
    var id: String { mount }
    /// 0…1 for the bar; "89%" -> 0.89
    var fraction: Double { Double(pct.replacingOccurrences(of: "%", with: "")).map { $0 / 100 } ?? 0 }
}

struct MacStats: Equatable {
    var cpuPct: Int?
    var ramUsedGB: Double?
    var ramTotalGB: Double?
}

enum StatsReader {
    /// One ssh call for everything — cheap enough to poll every few seconds and
    /// it rides the existing ControlMaster connection (no new auth, no prompt).
    static let remoteCommand = """
    nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu \
    --format=csv,noheader,nounits | head -1 | sed 's/^/GPU /'; \
    free -m | awk '/^Mem:/{print "MEM "$3" "$2}'; \
    awk '{print "LOAD "$1}' /proc/loadavg; nproc | sed 's/^/NCPU /'; \
    top -bn1 | awk '/^%Cpu/{printf "CPU %.0f\\n", 100-$8}'; \
    df -h -x tmpfs -x devtmpfs -x squashfs -x overlay --output=target,used,size,pcent 2>/dev/null     | tail -n +2 | grep -vE 'efivars|/boot|/snap' | awk '{print "DISK "$1" "$2" "$3" "$4}';     sensors -u 2>/dev/null | awk '/^coretemp/{c=1} c&&/temp1_input/{printf "CPUTEMP %.0f\\n", $2; exit}';     for f in /sys/class/nvme/*/hwmon*/temp1_input; do [ -f "$f" ] &&     awk '{printf "NVMETEMP %.0f\\n", $1/1000}' "$f" && break; done
    """

    static func parse(_ text: String) -> BoxStats {
        var s = BoxStats()
        for raw in text.split(separator: "\n") {
            let parts = raw.replacingOccurrences(of: ",", with: " ")
                .split(separator: " ").map(String.init).filter { !$0.isEmpty }
            guard let tag = parts.first else { continue }
            switch tag {
            case "GPU" where parts.count >= 5:
                s.gpuUtil = Int(parts[1]); s.vramUsedMB = Int(parts[2])
                s.vramTotalMB = Int(parts[3]); s.gpuTempC = Int(parts[4])
            case "MEM" where parts.count >= 3:
                s.ramUsedMB = Int(parts[1]); s.ramTotalMB = Int(parts[2])
            case "LOAD" where parts.count >= 2: s.load1 = Double(parts[1])
            case "NCPU" where parts.count >= 2: s.ncpu = Int(parts[1])
            case "CPU"  where parts.count >= 2: s.cpuPct = Int(parts[1])
            case "DISK" where parts.count >= 5:
                s.disks.append(.init(mount: parts[1], used: parts[2],
                                     total: parts[3], pct: parts[4]))
            case "CPUTEMP" where parts.count >= 2: s.cpuTempC = Int(Double(parts[1]) ?? 0)
            case "NVMETEMP" where parts.count >= 2: s.nvmeTempC = Int(Double(parts[1]) ?? 0)
            default: break
            }
        }
        s.stale = (s.gpuUtil == nil && s.ramUsedMB == nil)
        return s
    }

    static func box() async -> BoxStats {
        // The script contains its own single quotes (awk/sed programs). Wrapping it
        // in another layer of single quotes mangles them — that silently broke the
        // CPU/NVMe temperature readings. Ship it base64-encoded instead, so the
        // remote shell sees the script byte-for-byte.
        let b64 = Data(remoteCommand.utf8).base64EncodedString()
        let r = await Shell.run(["ssh", "-o", "BatchMode=yes",
                                 "-o", "IdentityAgent=none", "box",
                                 Shell.q("echo \(b64) | base64 -d | bash")], timeout: 25)
        return parse(r.out)
    }

    /// Local Mac usage — host_statistics64 would be nicer, but shelling out keeps
    /// this dependency-free and it is only polled every few seconds.
    static func mac() async -> MacStats {
        var m = MacStats()
        let cpu = await Shell.run(["ps", "-A", "-o", "%cpu", "|", "awk",
                                   Shell.q("{s+=$1} END {printf \"%.0f\", s/`sysctl -n hw.ncpu`}")],
                                  timeout: 12)
        m.cpuPct = Int(cpu.out.trimmingCharacters(in: .whitespacesAndNewlines))
        let mem = await Shell.run(["/usr/bin/vm_stat"], timeout: 12)
        let total = await Shell.run(["sysctl", "-n", "hw.memsize"], timeout: 8)
        if let bytes = Double(total.out.trimmingCharacters(in: .whitespacesAndNewlines)) {
            m.ramTotalGB = bytes / 1_073_741_824
            // "Pages active/wired/compressed" ≈ in-use footprint
            var pages = 0.0
            for line in mem.out.split(separator: "\n") {
                for key in ["Pages active", "Pages wired down", "Pages occupied by compressor"]
                where line.hasPrefix(key) {
                    let digits = line.filter { $0.isNumber }
                    pages += Double(digits) ?? 0
                }
            }
            m.ramUsedGB = pages * 16384 / 1_073_741_824      // 16K page size on Apple silicon
        }
        return m
    }
}
