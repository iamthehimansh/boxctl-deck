import ServiceManagement
import SwiftUI

/// "Launch at login", backed by SMAppService (macOS 13+). Apple deprecated the old
/// login-item APIs; this registers the app itself with launchd.
@MainActor
enum LoginItem {
    static var isEnabled: Bool {
        if #available(macOS 13.0, *) { return SMAppService.mainApp.status == .enabled }
        return false
    }

    /// Returns nil on success, otherwise a message worth showing the user —
    /// registration genuinely fails for an unsigned app run from a random folder,
    /// so we surface that instead of silently doing nothing.
    static func set(_ on: Bool) -> String? {
        guard #available(macOS 13.0, *) else { return "needs macOS 13+" }
        do {
            if on { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
            return nil
        } catch {
            return "login item: \(error.localizedDescription) — move BoxDeck.app to /Applications and retry"
        }
    }
}
