// Asks you to unlock Margin with Touch ID (or your Mac password when Touch ID isn't available), using macOS's own
// LocalAuthentication prompt. It briefly becomes the frontmost app while it asks (macOS cancels prompts from
// background programs), then hands the focus back.
// Usage: Margin "reason"   or   Margin --check (is it available? no prompt)
// Exit codes: 0 unlocked, 1 you cancelled, 2 failed (macOS's reason on stderr), 3 no Touch ID or password possible.
import AppKit
import Foundation
import LocalAuthentication

let args = Array(CommandLine.arguments.dropFirst())
let ctx = LAContext()
ctx.localizedCancelTitle = "Cancel"
var err: NSError?
guard ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &err) else {
    FileHandle.standardError.write("\(err?.code ?? 0) \(err?.localizedDescription ?? "unavailable")".data(using: .utf8)!)
    exit(3)
}
if args.first == "--check" {
    let bio = LAContext().canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil)
    print(bio ? "touchid" : "password")
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)          // no Dock icon, no menu bar, but allowed to come to the front
DispatchQueue.main.async {
    app.activate(ignoringOtherApps: true)
    ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: args.first ?? "unlock Margin") { ok, error in
        DispatchQueue.main.async {
            if ok { exit(0) }
            let e = error as NSError?
            FileHandle.standardError.write("\(e?.code ?? 0) \(e?.localizedDescription ?? "failed")".data(using: .utf8)!)
            exit(e?.code == LAError.userCancel.rawValue ? 1 : 2)
        }
    }
}
app.run()
