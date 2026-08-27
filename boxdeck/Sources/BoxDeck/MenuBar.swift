import SwiftUI
import AppKit

/// Compact live readout for the macOS menu bar: GPU / VRAM / CPU / RAM of the box.
/// Kept short — the menu bar is precious space.
/// NOTE: a MenuBarExtra label only renders simple content — `Canvas`,
/// `GeometryReader` and most custom drawing come out BLANK in the menu bar.
/// So the graph is rasterised into an NSImage and shown as an `Image`, which is
/// how menu-bar meters are normally done.
struct MenuBarLabel: View {
    @EnvironmentObject var box: BoxModel

    var body: some View {
        Image(nsImage: MenuBarGraph.image(gpu: box.gpuHistory,
                                          vram: box.vramHistory,
                                          vramNow: box.stats.vramPct.map { Double($0) / 100 },
                                          gpuNow: box.stats.gpuUtil,
                                          online: box.status.sshOK))
            .renderingMode(.original)          // keep our colours (not a template)
    }
}

enum MenuBarGraph {
    /// A real live chart: VRAM as a filled area (it is rarely 0, so the graph
    /// always looks alive) with GPU utilisation as a line on top, inside a visible
    /// track — an empty/idle GPU must still read as "a graph", not a blank gap.
    static func image(gpu: [Double], vram: [Double], vramNow: Double?,
                      gpuNow: Int?, online: Bool) -> NSImage {
        let chartW: CGFloat = 54, h: CGFloat = 18
        let iconW: CGFloat = 13, textW: CGFloat = 24
        let w = iconW + chartW + textW + 6
        let img = NSImage(size: NSSize(width: w, height: h))
        img.lockFocus()
        defer { img.unlockFocus() }

        let cfg = NSImage.SymbolConfiguration(pointSize: 11, weight: .regular)
        if let icon = NSImage(systemSymbolName: online ? "cube.fill" : "cube",
                              accessibilityDescription: "box")?
            .withSymbolConfiguration(cfg) {
            icon.draw(in: NSRect(x: 0, y: 3, width: 12, height: 12),
                      from: .zero, operation: .sourceOver, fraction: online ? 1.0 : 0.4)
        }

        guard online else {
            ("off" as NSString).draw(at: NSPoint(x: iconW + 2, y: 3), withAttributes: [
                .font: NSFont.systemFont(ofSize: 9, weight: .medium),
                .foregroundColor: NSColor.secondaryLabelColor])
            return img
        }

        // ---- white graph-paper sheet (matches the big chart, and stays readable
        // on ANY wallpaper — the menu bar takes its colour from the desktop).
        let x0 = iconW + 2, y0: CGFloat = 2, ch = h - 4
        let frame = NSRect(x: x0, y: y0, width: chartW, height: ch)
        let card = NSBezierPath(roundedRect: frame, xRadius: 3, yRadius: 3)
        NSColor.white.setFill()
        card.fill()
        card.addClip()                                  // keep the grid inside the card

        let minor = NSColor(calibratedRed: 0.62, green: 0.72, blue: 0.86, alpha: 0.55)
        let major = NSColor(calibratedRed: 0.42, green: 0.56, blue: 0.78, alpha: 0.75)
        minor.setStroke()
        for i in 0...10 {                               // fine squares
            let gx = x0 + chartW * CGFloat(i) / 10
            let p = NSBezierPath()
            p.move(to: NSPoint(x: gx, y: y0)); p.line(to: NSPoint(x: gx, y: y0 + ch))
            p.lineWidth = 0.4; p.stroke()
        }
        for i in 0...4 {
            let gy = y0 + ch * CGFloat(i) / 4
            let p = NSBezierPath()
            p.move(to: NSPoint(x: x0, y: gy)); p.line(to: NSPoint(x: x0 + chartW, y: gy))
            p.lineWidth = (i == 2) ? 0.8 : 0.4
            (i == 2 ? major : minor).setStroke()
            p.stroke()
        }

        func xs(_ i: Int, _ count: Int) -> CGFloat {
            count <= 1 ? x0 + chartW : x0 + chartW * CGFloat(i) / CGFloat(count - 1)
        }

        // ---- VRAM filled area (blue)
        if vram.count > 1 {
            let area = NSBezierPath()
            area.move(to: NSPoint(x: x0, y: y0))
            for (i, v) in vram.enumerated() {
                area.line(to: NSPoint(x: xs(i, vram.count),
                                      y: y0 + CGFloat(min(max(v, 0), 1)) * ch))
            }
            area.line(to: NSPoint(x: xs(vram.count - 1, vram.count), y: y0))
            area.close()
            NSGradient(colors: [NSColor.systemBlue.withAlphaComponent(0.55),
                                NSColor.systemBlue.withAlphaComponent(0.08)])?
                .draw(in: area, angle: 90)
            NSColor.systemBlue.setStroke()
            let top = NSBezierPath()
            for (i, v) in vram.enumerated() {
                let p = NSPoint(x: xs(i, vram.count),
                                y: y0 + CGFloat(min(max(v, 0), 1)) * ch)
                i == 0 ? top.move(to: p) : top.line(to: p)
            }
            top.lineWidth = 1.0
            top.stroke()
        }

        // ---- GPU line (green→red), drawn thick so 0% still shows as a baseline
        if gpu.count > 1 {
            let pts = gpu.enumerated().map { i, v in
                NSPoint(x: xs(i, gpu.count),
                        y: y0 + max(0.8, CGFloat(min(max(v, 0), 1)) * ch))
            }
            let line = smooth(pts)
            line.lineWidth = 2.4
            line.lineJoinStyle = .round
            tint(gpu.last ?? 0).withAlphaComponent(0.25).setStroke()
            line.stroke()                       // soft glow
            line.lineWidth = 1.3
            tint(gpu.last ?? 0).setStroke()
            line.stroke()
            // live dot on the newest sample
            if let last = gpu.last {
                let p = NSPoint(x: xs(gpu.count - 1, gpu.count),
                                y: y0 + max(0.8, CGFloat(min(max(last, 0), 1)) * ch))
                let d: CGFloat = 2.6
                tint(last).setFill()
                NSBezierPath(ovalIn: NSRect(x: p.x - d/2, y: p.y - d/2,
                                            width: d, height: d)).fill()
            }
        }

        // border on top of the sheet
        NSColor.black.withAlphaComponent(0.25).setStroke()
        let edge = NSBezierPath(roundedRect: frame, xRadius: 3, yRadius: 3)
        edge.lineWidth = 0.7
        edge.stroke()

        // ---- numeric readout so it is precise, not just pretty
        let pct = "\(gpuNow ?? 0)%" as NSString
        pct.draw(at: NSPoint(x: x0 + chartW + 3, y: 3), withAttributes: [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 9, weight: .medium),
            .foregroundColor: NSColor.white,
            .shadow: { let sh = NSShadow(); sh.shadowColor = .black.withAlphaComponent(0.75)
                       sh.shadowBlurRadius = 2.0; return sh }()])
        return img
    }

    /// Catmull-Rom → Bézier so the mini chart curves like the big one.
    static func smooth(_ p: [NSPoint]) -> NSBezierPath {
        let path = NSBezierPath()
        guard p.count > 1 else { return path }
        path.move(to: p[0])
        for i in 0..<(p.count - 1) {
            let p0 = i == 0 ? p[0] : p[i - 1]
            let p1 = p[i], p2 = p[i + 1]
            let p3 = (i + 2 < p.count) ? p[i + 2] : p2
            path.curve(to: p2,
                       controlPoint1: NSPoint(x: p1.x + (p2.x - p0.x) / 6,
                                              y: p1.y + (p2.y - p0.y) / 6),
                       controlPoint2: NSPoint(x: p2.x - (p3.x - p1.x) / 6,
                                              y: p2.y - (p3.y - p1.y) / 6))
        }
        return path
    }

    static func tint(_ v: Double) -> NSColor {
        v > 0.9 ? .systemRed : (v > 0.6 ? .systemOrange : .systemGreen)
    }
}

/// Everything you can do, without opening the window.
struct MenuBarPanel: View {
    @EnvironmentObject var box: BoxModel
    @Environment(\.openWindow) private var openWindow
    @State private var showingTOTP = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // ---- header
            HStack {
                Circle().fill(box.status.sshOK ? .green : .red).frame(width: 8, height: 8)
                Text(box.status.sshOK ? "box connected" : "box offline").fontWeight(.semibold)
                Text(box.status.route).font(.caption).foregroundStyle(.secondary)
                Spacer()
                if let h = box.status.keyHours {
                    Text(h > 0 ? String(format: "key %.1fh", h) : "key expired")
                        .font(.caption2)
                        .foregroundStyle(h > 1 ? Color.secondary : Color.orange)
                }
                if let d = box.status.passkeyDays {
                    Text(String(format: "Touch ID %.0fd", d)).font(.caption2)
                        .foregroundStyle(d > 3 ? Color.secondary : Color.orange)
                } else if box.status.passkeyExpired {
                    Text("Touch ID expired").font(.caption2).foregroundStyle(.orange)
                }
            }

            // ---- live chart (grid + smooth curves + latest-point dots)
            LiveChart(series: [
                .init(id: "gpu", name: "GPU", color: .green, values: box.gpuHistory,
                      current: box.stats.gpuUtil.map { "\($0)%" } ?? "—"),
                .init(id: "vram", name: "VRAM", color: .cyan, values: box.vramHistory,
                      current: BoxStats.pair(box.stats.vramUsedMB, box.stats.vramTotalMB)),
                .init(id: "cpu", name: "CPU", color: .orange, values: box.cpuHistory,
                      current: box.stats.cpuPct.map { "\($0)%" } ?? "—"),
                .init(id: "ram", name: "RAM", color: .purple, values: box.ramHistory,
                      current: BoxStats.pair(box.stats.ramUsedMB, box.stats.ramTotalMB)),
            ], height: 96)


            // temps + every mounted disk with a fill bar
            HStack(spacing: 10) {
                Text("TEMP").font(.caption2).foregroundStyle(.secondary)
                if let g = box.stats.gpuTempC { tempPill("GPU", g) }
                if let c = box.stats.cpuTempC { tempPill("CPU", c) }
                if let n = box.stats.nvmeTempC { tempPill("NVMe", n) }
                Spacer()
            }

            VStack(spacing: 4) {
                ForEach(box.stats.disks) { d in
                    HStack(spacing: 6) {
                        Text(d.mount).font(.caption2).monospaced()
                            .lineLimit(1).frame(width: 92, alignment: .leading)
                        GeometryReader { g in
                            ZStack(alignment: .leading) {
                                Capsule().fill(.quaternary).frame(height: 4)
                                Capsule()
                                    .fill(d.fraction > 0.9 ? Color.red
                                          : (d.fraction > 0.75 ? Color.orange : Color.green))
                                    .frame(width: g.size.width * d.fraction, height: 4)
                            }
                            .frame(maxHeight: .infinity, alignment: .center)
                        }
                        .frame(height: 10)
                        Text("\(d.used)/\(d.total)").font(.caption2).monospacedDigit()
                            .foregroundStyle(.secondary).frame(width: 74, alignment: .trailing)
                        Text(d.pct).font(.caption2).monospacedDigit()
                            .frame(width: 34, alignment: .trailing)
                    }
                }
            }

            Divider()

            // ---- this Mac
            HStack(spacing: 10) {
                Text("this Mac").font(.caption2).foregroundStyle(.secondary)
                if let c = box.mac.cpuPct { Text("CPU \(c)%").font(.caption2) }
                if let u = box.mac.ramUsedGB, let t = box.mac.ramTotalGB {
                    Text(String(format: "RAM %.1f/%.0fG", u, t)).font(.caption2)
                }
                Spacer()
            }

            Divider()

            // ---- services
            Text("AI services").font(.caption).foregroundStyle(.secondary)
            ForEach(box.services) { s in
                HStack {
                    Text(s.label).font(.callout)
                    Spacer()
                    if s.busy {
                        ProgressView().controlSize(.small)
                    } else {
                        Toggle("", isOn: Binding(get: { s.active },
                                                 set: { _ in Task { await box.toggle(s) } }))
                            .labelsHidden().toggleStyle(.switch).controlSize(.mini)
                    }
                }
            }

            Divider()

            // ---- actions
            HStack(spacing: 8) {
                Button {
                    let up = box.status.tunnels.values.contains(true)
                    Task { await box.setTunnels(on: !up) }
                } label: {
                    Label(box.status.tunnels.values.contains(true) ? "Tunnels on" : "Tunnels off",
                          systemImage: "cable.connector")
                }
                Menu {
                    Button { Task { await box.reconnect() } } label: {
                        Label("Renew with Touch ID", systemImage: "touchid")
                    }
                    Button { showingTOTP = true } label: {
                        Label("Password / TOTP…", systemImage: "number.square")
                    }
                } label: {
                    Label("Authenticate", systemImage: "person.badge.key")
                }
                .disabled(box.authBusy)
            }
            .controlSize(.small)

            Toggle(isOn: Binding(
                get: { box.launchAtLogin },
                set: { box.setLaunchAtLogin($0) })) {
                Label("Launch at login", systemImage: "power")
            }
            .toggleStyle(.switch)
            .controlSize(.mini)
            .font(.caption)

            HStack(spacing: 8) {
                Button { openWindow(id: "main"); NSApp.activate(ignoringOtherApps: true) } label: {
                    Label("Open BoxDeck", systemImage: "macwindow")
                }
                Button { Task { await box.openTerminal(at: box.cwd) } } label: {
                    Label("Terminal", systemImage: "terminal")
                }
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }
            }
            .controlSize(.small)
        }
        .padding(14)
        .frame(width: 340)
        .sheet(isPresented: $showingTOTP) { TOTPLoginSheet() }
    }

    @ViewBuilder
    func tempPill(_ name: String, _ c: Int) -> some View {
        HStack(spacing: 3) {
            Text(name).font(.caption2).foregroundStyle(.secondary)
            Text("\(c)°").font(.caption2).monospacedDigit()
                .foregroundStyle(c >= 80 ? Color.red : (c >= 65 ? Color.orange : Color.primary))
        }
        .padding(.horizontal, 5).padding(.vertical, 1)
        .background(.quaternary.opacity(0.5), in: Capsule())
    }

    @ViewBuilder
    func meter(_ name: String, _ frac: Double?, _ value: String, extra: String? = nil) -> some View {
        VStack(spacing: 2) {
            HStack {
                Text(name).font(.caption2).foregroundStyle(.secondary)
                    .frame(width: 42, alignment: .leading)
                Text(value).font(.caption2).monospacedDigit()
                if let e = extra { Text(e).font(.caption2).foregroundStyle(.tertiary) }
                Spacer()
            }
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(.quaternary).frame(height: 4)
                    Capsule()
                        .fill(tint(frac))
                        .frame(width: max(0, min(1, frac ?? 0)) * g.size.width, height: 4)
                }
            }
            .frame(height: 4)
        }
    }

    func tint(_ f: Double?) -> Color {
        guard let f else { return .gray }
        return f > 0.9 ? .red : (f > 0.7 ? .orange : .green)
    }
}
