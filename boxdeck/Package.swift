// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "BoxDeck",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "BoxDeck", path: "Sources/BoxDeck")
    ]
)
