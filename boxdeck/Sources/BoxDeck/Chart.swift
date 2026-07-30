import SwiftUI

struct Series: Identifiable {
    let id: String
    let name: String
    let color: Color
    let values: [Double]        // 0…1, oldest → newest
    var current: String = ""    // right-hand readout, e.g. "44%"
}

/// Live multi-series chart: faint grid, smooth Catmull-Rom curves through the
/// samples, and a glowing dot on the newest point. Transparent background so it
/// sits on the panel's material.
struct LiveChart: View {
    let series: [Series]
    var height: CGFloat = 110
    var showLegend = true

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Canvas { ctx, size in
                drawGrid(&ctx, size)
                for s in series {
                    guard s.values.count > 1 else { continue }
                    let pts = points(s.values, size)
                    let line = smoothPath(pts)

                    // soft area under the curve
                    var fill = line
                    fill.addLine(to: CGPoint(x: pts.last!.x, y: size.height))
                    fill.addLine(to: CGPoint(x: pts.first!.x, y: size.height))
                    fill.closeSubpath()
                    ctx.fill(fill, with: .linearGradient(
                        Gradient(colors: [s.color.opacity(0.28), s.color.opacity(0.02)]),
                        startPoint: .zero, endPoint: CGPoint(x: 0, y: size.height)))

                    ctx.stroke(line, with: .color(s.color),
                               style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round))

                    // latest point — glow + solid dot
                    if let p = pts.last {
                        ctx.fill(Path(ellipseIn: CGRect(x: p.x - 5, y: p.y - 5, width: 10, height: 10)),
                                 with: .color(s.color.opacity(0.22)))
                        ctx.fill(Path(ellipseIn: CGRect(x: p.x - 2.6, y: p.y - 2.6, width: 5.2, height: 5.2)),
                                 with: .color(s.color))
                    }
                }
            }
            .frame(height: height)
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(.black.opacity(0.12)))

            if showLegend {
                // A single row truncates ("G… 0%", "V… 8…") in the narrow menu-bar
                // panel, so the legend wraps into a grid instead.
                LazyVGrid(columns: [GridItem(.flexible(), alignment: .leading),
                                    GridItem(.flexible(), alignment: .leading)],
                          alignment: .leading, spacing: 4) {
                    ForEach(series) { s in
                        HStack(spacing: 5) {
                            Circle().fill(s.color).frame(width: 7, height: 7)
                            Text(s.name).font(.caption2).foregroundStyle(.secondary)
                            Text(s.current).font(.caption2).monospacedDigit()
                                .fontWeight(.medium).lineLimit(1)
                            Spacer(minLength: 0)
                        }
                        .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    // ---- geometry

    private func points(_ vals: [Double], _ size: CGSize) -> [CGPoint] {
        let n = vals.count
        return vals.enumerated().map { i, v in
            CGPoint(x: size.width * CGFloat(i) / CGFloat(max(n - 1, 1)),
                    y: size.height * (1 - CGFloat(min(max(v, 0), 1))))
        }
    }

    /// Graph-paper look: white sheet with fine minor squares and stronger major
    /// lines every 25% / time division.
    private func drawGrid(_ ctx: inout GraphicsContext, _ size: CGSize) {
        ctx.fill(Path(CGRect(origin: .zero, size: size)), with: .color(.white))

        let minor = Color(red: 0.62, green: 0.72, blue: 0.86).opacity(0.45)
        let major = Color(red: 0.42, green: 0.56, blue: 0.78).opacity(0.65)
        let thin = StrokeStyle(lineWidth: 0.5)
        let thick = StrokeStyle(lineWidth: 0.9)

        let cols = 24, rows = 12                    // minor squares
        for i in 0...cols {
            let x = size.width * CGFloat(i) / CGFloat(cols)
            var p = Path(); p.move(to: CGPoint(x: x, y: 0)); p.addLine(to: CGPoint(x: x, y: size.height))
            ctx.stroke(p, with: .color(minor), style: thin)
        }
        for i in 0...rows {
            let y = size.height * CGFloat(i) / CGFloat(rows)
            var p = Path(); p.move(to: CGPoint(x: 0, y: y)); p.addLine(to: CGPoint(x: size.width, y: y))
            ctx.stroke(p, with: .color(minor), style: thin)
        }
        for i in 0...4 {                            // major: 0/25/50/75/100 %
            let y = size.height * CGFloat(i) / 4
            var p = Path(); p.move(to: CGPoint(x: 0, y: y)); p.addLine(to: CGPoint(x: size.width, y: y))
            ctx.stroke(p, with: .color(major), style: thick)
        }
        for i in 0...6 {                            // major: time divisions
            let x = size.width * CGFloat(i) / 6
            var p = Path(); p.move(to: CGPoint(x: x, y: 0)); p.addLine(to: CGPoint(x: x, y: size.height))
            ctx.stroke(p, with: .color(major), style: thick)
        }
    }

    /// Catmull-Rom → cubic Bézier, so the line flows through every sample
    /// instead of showing hard corners.
    private func smoothPath(_ p: [CGPoint]) -> Path {
        var path = Path()
        guard p.count > 1 else { return path }
        path.move(to: p[0])
        for i in 0..<(p.count - 1) {
            let p0 = i == 0 ? p[0] : p[i - 1]
            let p1 = p[i], p2 = p[i + 1]
            let p3 = (i + 2 < p.count) ? p[i + 2] : p2
            let c1 = CGPoint(x: p1.x + (p2.x - p0.x) / 6, y: p1.y + (p2.y - p0.y) / 6)
            let c2 = CGPoint(x: p2.x - (p3.x - p1.x) / 6, y: p2.y - (p3.y - p1.y) / 6)
            path.addCurve(to: p2, control1: c1, control2: c2)
        }
        return path
    }
}
