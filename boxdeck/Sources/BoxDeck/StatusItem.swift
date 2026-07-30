import AppKit
import SwiftUI
import Combine

/// The menu-bar item, done with AppKit instead of `MenuBarExtra`.
///
/// Why: a `MenuBarExtra` label can only show simple content, so the chart had to be
/// rasterised into an `NSImage` — and `NSImage` + `lockFocus` draws at 1× and is
/// then scaled up on a Retina display, which is what made it look fuzzy. A custom
/// `NSView` draws vectors at native resolution, so lines stay crisp.
@MainActor
final class StatusItemController: NSObject {
    private var item: NSStatusItem?
    private var popover: NSPopover?
    private var bag = Set<AnyCancellable>()
    private weak var box: BoxModel?

    func install(box: BoxModel) {
        self.box = box
        let thickness = NSStatusBar.system.thickness          // ~22pt — the OS caps this
        let view = ChartStatusView(frame: NSRect(x: 0, y: 0, width: 84, height: thickness))
        view.onClick = { [weak self] in self?.toggle(relativeTo: view) }

        let it = NSStatusBar.system.statusItem(withLength: 84)
        it.button?.addSubview(view)
        view.autoresizingMask = [.width, .height]
        view.frame = it.button?.bounds ?? view.frame
        item = it

        let pop = NSPopover()
        pop.behavior = .transient
        pop.contentSize = NSSize(width: 360, height: 560)
        pop.contentViewController = NSHostingController(
            rootView: MenuBarPanel().environmentObject(box).frame(width: 360))
        popover = pop

        // redraw whenever a new sample lands
        box.objectWillChange
            .receive(on: RunLoop.main)
            .sink { [weak view, weak box] _ in
                guard let view, let box else { return }
                view.gpu = box.gpuHistory
                view.vram = box.vramHistory
                view.cpu = box.cpuHistory
                view.gpuNow = box.stats.gpuUtil
                view.online = box.status.sshOK
                view.needsDisplay = true
            }
            .store(in: &bag)
    }

    private func toggle(relativeTo view: NSView) {
        guard let pop = popover else { return }
        if pop.isShown {
            pop.performClose(nil)
        } else {
            pop.show(relativeTo: view.bounds, of: view, preferredEdge: .minY)
            pop.contentViewController?.view.window?.makeKey()
        }
    }
}

/// Crisp, vector-drawn live chart sized to the full menu-bar height.
final class ChartStatusView: NSView {
    var gpu: [Double] = []
    var vram: [Double] = []
    var cpu: [Double] = []
    var gpuNow: Int?
    var online = false
    var onClick: (() -> Void)?

    /// Loaded once — Resources/menubar.png (+@2x), rendered as a template.
    static let catGlyph: NSImage? = {
        guard let url = Bundle.main.url(forResource: "menubar", withExtension: "png"),
              let img = NSImage(contentsOf: url) else { return nil }
        img.isTemplate = true
        img.size = NSSize(width: 13, height: 13)
        return img
    }()

    override func mouseDown(with event: NSEvent) { onClick?() }

    override func draw(_ dirty: NSRect) {
        let b = bounds
        let inset: CGFloat = 1.5
        let chartX = inset + 13                       // room for the cube glyph
        let readoutW: CGFloat = 23
        let chart = NSRect(x: chartX, y: inset,
                           width: b.width - chartX - readoutW - inset,
                           height: b.height - inset * 2)

        // ---- icon
        // the app's cat mark, as a TEMPLATE image so it tints itself for light/dark
        // menu bars (a full-colour icon looks muddy at this size)
        if let icon = Self.catGlyph {
            let r = NSRect(x: inset, y: (b.height - 13) / 2, width: 13, height: 13)
            icon.draw(in: r, from: .zero, operation: .sourceOver,
                      fraction: online ? 1.0 : 0.4)
        }
        guard online else {
            ("off" as NSString).draw(at: NSPoint(x: chartX, y: (b.height - 12) / 2),
                                     withAttributes: [
                .font: NSFont.systemFont(ofSize: 10, weight: .medium),
                .foregroundColor: NSColor.secondaryLabelColor])
            return
        }

        // ---- graph-paper sheet
        let card = NSBezierPath(roundedRect: chart, xRadius: 3, yRadius: 3)
        NSColor.white.withAlphaComponent(0.96).setFill()
        card.fill()
        NSGraphicsContext.saveGraphicsState()
        card.addClip()
        let grid = NSColor(calibratedRed: 0.55, green: 0.66, blue: 0.82, alpha: 0.5)
        grid.setStroke()
        for i in 1..<8 {                              // vertical minor
            let x = chart.minX + chart.width * CGFloat(i) / 8
            let p = NSBezierPath(); p.lineWidth = 0.5
            p.move(to: NSPoint(x: x, y: chart.minY)); p.line(to: NSPoint(x: x, y: chart.maxY)); p.stroke()
        }
        for i in 1..<4 {                              // horizontal minor + 50% major
            let y = chart.minY + chart.height * CGFloat(i) / 4
            let p = NSBezierPath(); p.lineWidth = (i == 2) ? 0.8 : 0.5
            (i == 2 ? grid.withAlphaComponent(0.8) : grid).setStroke()
            p.move(to: NSPoint(x: chart.minX, y: y)); p.line(to: NSPoint(x: chart.maxX, y: y)); p.stroke()
        }

        func pts(_ vals: [Double]) -> [NSPoint] {
            vals.enumerated().map { i, v in
                NSPoint(x: chart.minX + chart.width * CGFloat(i) / CGFloat(max(vals.count - 1, 1)),
                        y: chart.minY + CGFloat(min(max(v, 0), 1)) * chart.height)
            }
        }
        // series: VRAM area (blue), CPU line (orange), GPU line (green)
        if vram.count > 1 {
            let p = pts(vram), line = Self.smooth(p)
            let area = line.copy() as! NSBezierPath
            area.line(to: NSPoint(x: p.last!.x, y: chart.minY))
            area.line(to: NSPoint(x: p.first!.x, y: chart.minY))
            area.close()
            NSGradient(colors: [NSColor.systemBlue.withAlphaComponent(0.45),
                                NSColor.systemBlue.withAlphaComponent(0.06)])?.draw(in: area, angle: 90)
            NSColor.systemBlue.setStroke(); line.lineWidth = 1.2; line.stroke()
        }
        if cpu.count > 1 {
            let line = Self.smooth(pts(cpu))
            NSColor.systemOrange.withAlphaComponent(0.9).setStroke()
            line.lineWidth = 1.1; line.stroke()
        }
        if gpu.count > 1 {
            let p = pts(gpu), line = Self.smooth(p)
            let c: NSColor = (gpu.last ?? 0) > 0.9 ? .systemRed
                : ((gpu.last ?? 0) > 0.6 ? .systemOrange : .systemGreen)
            c.setStroke(); line.lineWidth = 1.6; line.stroke()
            if let last = p.last {                     // live dot
                c.setFill()
                NSBezierPath(ovalIn: NSRect(x: last.x - 2, y: last.y - 2,
                                            width: 4, height: 4)).fill()
            }
        }
        NSGraphicsContext.restoreGraphicsState()
        NSColor.black.withAlphaComponent(0.22).setStroke()
        card.lineWidth = 0.8
        card.stroke()

        // ---- readout
        let s = "\(gpuNow ?? 0)%" as NSString
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 10, weight: .semibold),
            .foregroundColor: NSColor.labelColor]
        s.draw(at: NSPoint(x: chart.maxX + 4,
                           y: (b.height - s.size(withAttributes: attrs).height) / 2),
               withAttributes: attrs)
    }

    static func smooth(_ p: [NSPoint]) -> NSBezierPath {
        let path = NSBezierPath()
        guard p.count > 1 else { return path }
        path.move(to: p[0])
        for i in 0..<(p.count - 1) {
            let p0 = i == 0 ? p[0] : p[i - 1], p1 = p[i], p2 = p[i + 1]
            let p3 = (i + 2 < p.count) ? p[i + 2] : p2
            path.curve(to: p2,
                       controlPoint1: NSPoint(x: p1.x + (p2.x - p0.x) / 6, y: p1.y + (p2.y - p0.y) / 6),
                       controlPoint2: NSPoint(x: p2.x - (p3.x - p1.x) / 6, y: p2.y - (p3.y - p1.y) / 6))
        }
        return path
    }
}
