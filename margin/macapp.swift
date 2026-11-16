// The Margin app: the Margin page in a window of its own (AppKit + WebKit), instead of a browser tab.
//
// Why a window and not the browser: browser extensions that may read all sites can read a page in a tab, and a tab
// leaves the page in the browser's history, cache and autofill. Here nothing is kept on disk (a private, in-memory
// website data store), there's no address bar, and nothing else can load the page through this window.
//
// The page itself still comes from Margin's own server on this Mac (margin/server.py), which keeps all its checks:
// the secret key, the Host allowlist and the Touch ID lock. This window hands the server its key as the same private
// cookie a browser gets, but read straight from the key file (readable only by you), so the key never appears in an
// address, the history or on a command line.
//
// - Links to anything that isn't the Margin page open in your default browser, never in this window.
// - Margin locks at once when the Mac locks, sleeps or switches user, when you hide this app, and when you quit it
//   (only if the lock is on: "lock_minutes" in settings.json).
// - The window remembers its size and place; nothing else is saved.
//
// Built on this Mac by `margin app install` (margin/macapp.py) with swiftc from the Command Line Tools. Info.plist
// says where the page is (MarginURL, always this Mac) and where its key is (MarginKeyFile). The demo's copy takes
// --port <n>; the address itself can't be changed from outside, so the real app only ever shows the real page and
// the demo's copy only the demo.
import AppKit
import WebKit

let cookieName = "margin_key"            // server.COOKIE

struct Config {
    let base: URL                        // e.g. http://margin.localhost/
    let keyFile: String
}

/// What to run in Terminal when something's wrong (Info.plist MarginFix; the demo's copy says how to start the demo).
let fixCommand = escapeHTML(Bundle.main.infoDictionary?["MarginFix"] as? String ?? "margin app install")

/// Only plain http on this Mac's own names: the key is never sent anywhere else.
func isThisMac(_ url: URL) -> Bool {
    guard url.scheme == "http", let host = url.host?.lowercased() else { return false }
    return host == "localhost" || host.hasSuffix(".localhost") || host == "127.0.0.1" || host == "::1"
}

func loadConfig() -> Config? {
    let info = Bundle.main.infoDictionary ?? [:]
    guard let text = info["MarginURL"] as? String, var parts = URLComponents(string: text) else { return nil }
    let args = CommandLine.arguments
    if let i = args.firstIndex(of: "--port"), i + 1 < args.count, let port = Int(args[i + 1]), (1...65535).contains(port) {
        parts.port = port
    }
    parts.path = "/"
    parts.query = nil
    parts.fragment = nil
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    let keyFile = info["MarginKeyFile"] as? String ?? home + "/Library/Application Support/Margin/server.key"
    guard let base = parts.url, isThisMac(base) else { return nil }
    return Config(base: base, keyFile: keyFile)
}

func readKey(_ path: String) -> String? {
    guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return nil }
    let key = text.trimmingCharacters(in: .whitespacesAndNewlines)
    let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_"))
    return !key.isEmpty && key.unicodeScalars.allSatisfy({ allowed.contains($0) }) ? key : nil
}

func escapeHTML(_ s: String) -> String {
    s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        .replacingOccurrences(of: ">", with: "&gt;")
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var config: Config?
    var key = ""
    var retried = false                  // one fresh read of the key after a refusal, then say what's wrong
    var titleWatch: NSKeyValueObservation?
    let session = URLSession(configuration: .ephemeral)     // nothing on disk here either

    // MARK: - Start

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.mainMenu = makeMenu()
        let conf = WKWebViewConfiguration()
        conf.websiteDataStore = .nonPersistent()             // cookies, cache and storage live in memory only
        conf.preferences.javaScriptCanOpenWindowsAutomatically = false
        conf.suppressesIncrementalRendering = true
        web = WKWebView(frame: .zero, configuration: conf)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = false
        web.allowsLinkPreview = false
        web.setValue(false, forKey: "drawsBackground")       // the window's own colour until the page paints (no white flash)

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Margin"
        window.contentView = web
        window.contentMinSize = NSSize(width: 400, height: 480)
        window.isRestorable = false                          // no saved window state on disk
        window.tabbingMode = .disallowed
        window.collectionBehavior.insert(.fullScreenPrimary)
        window.delegate = self
        window.center()
        window.setFrameAutosaveName("Margin")                // remembers size and place (just the frame)
        titleWatch = web.observe(\.title, options: [.new]) { [weak self] view, _ in
            DispatchQueue.main.async { self?.window.title = (view.title?.isEmpty ?? true) ? "Margin" : view.title! }
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        watchForAway()

        config = loadConfig()
        guard config != nil else {
            return problem("This copy of Margin doesn't know where its page is.",
                           "Run <code>\(fixCommand)</code> in Terminal to make it again.")
        }
        start()
    }

    /// Read the key, put it in the private cookie, load the page. The page service makes the key the first time it
    /// starts, so if it isn't there yet, wake the service (the icon needs no key) and read it again.
    func start() {
        guard let cfg = config else { return }
        if let k = readKey(cfg.keyFile) {
            key = k
            return loadPage()
        }
        wake { [weak self] in
            guard let self = self else { return }
            if let k = readKey(cfg.keyFile) {
                self.key = k
                self.loadPage()
            } else {
                self.problem("Margin's page isn't set up yet.",
                             "Run <code>\(fixCommand)</code> in Terminal, then open Margin again.")
            }
        }
    }

    func wake(_ then: @escaping () -> Void) {
        guard let cfg = config else { return then() }
        var req = URLRequest(url: cfg.base.appendingPathComponent("favicon.png"), timeoutInterval: 15)
        req.httpShouldHandleCookies = false
        session.dataTask(with: req) { _, _, _ in DispatchQueue.main.async(execute: then) }.resume()
    }

    func loadPage() {
        guard let cfg = config, let host = cfg.base.host else { return }
        let props: [HTTPCookiePropertyKey: Any] = [
            .name: cookieName, .value: key, .domain: host, .path: "/",
            HTTPCookiePropertyKey("HttpOnly"): "TRUE", .sameSitePolicy: HTTPCookieStringPolicy.sameSiteStrict,
        ]
        guard let cookie = HTTPCookie(properties: props) else {
            return problem("Margin couldn't hand its page the key.", "Run <code>\(fixCommand)</code> in Terminal.")
        }
        web.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) { [weak self] in
            self?.web.load(URLRequest(url: cfg.base))
        }
    }

    func problem(_ title: String, _ detail: String) {
        let html = """
        <!doctype html><html><head><meta charset="utf-8"><style>
        :root{color-scheme:light dark} html,body{height:100%;margin:0}
        body{display:flex;align-items:center;justify-content:center;font:15px/1.5 -apple-system,system-ui,sans-serif;
          color:CanvasText;background:Canvas;text-align:center;padding:24px}
        main{max-width:440px} h1{font-size:21px;font-weight:600;margin:0 0 10px;letter-spacing:-.01em}
        p{margin:0 0 8px;opacity:.72} code{font:13px ui-monospace,Menlo,monospace}
        </style></head><body><main><h1>\(escapeHTML(title))</h1><p>\(detail)</p>
        <p>Then press ⌘R to try again.</p></main></body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    func isMarginPage(_ url: URL?) -> Bool {
        guard let url = url, let base = config?.base else { return false }
        return url.scheme == base.scheme && url.host?.lowercased() == base.host?.lowercased() && url.port == base.port
    }

    // MARK: - Locking when you're away

    func watchForAway() {
        let ws = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.willSleepNotification, NSWorkspace.screensDidSleepNotification,
                     NSWorkspace.sessionDidResignActiveNotification] {
            ws.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in self?.lockNow() }
        }
        DistributedNotificationCenter.default().addObserver(
            forName: Notification.Name("com.apple.screenIsLocked"), object: nil, queue: .main) { [weak self] _ in
            self?.lockNow()
        }
        NotificationCenter.default.addObserver(forName: NSApplication.didHideNotification, object: nil,
                                               queue: .main) { [weak self] _ in self?.lockNow() }
    }

    /// Ask the server to lock (it's the one that holds the lock), then, if it's on, reload: the server answers with
    /// the lock screen, so no figures stay in this window. With the lock off nothing changes.
    func lockNow(reload: Bool = true, _ done: (() -> Void)? = nil) {
        guard let cfg = config, !key.isEmpty else { done?(); return }
        var req = URLRequest(url: cfg.base.appendingPathComponent("api/lock"), timeoutInterval: 3)
        req.httpMethod = "POST"
        req.httpShouldHandleCookies = false
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(key, forHTTPHeaderField: "X-Margin-Key")
        req.httpBody = Data("{}".utf8)
        session.dataTask(with: req) { [weak self] _, _, _ in
            guard let self = self else { return }
            var ping = URLRequest(url: cfg.base.appendingPathComponent("api/ping"), timeoutInterval: 3)
            ping.httpShouldHandleCookies = false
            ping.setValue(self.key, forHTTPHeaderField: "X-Margin-Key")
            self.session.dataTask(with: ping) { data, _, _ in
                let reply = (data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]) ?? [:]
                DispatchQueue.main.async {
                    if reload, reply["locked"] as? Bool == true, self.isMarginPage(self.web.url) {
                        self.web.reload()
                    }
                    done?()
                }
            }.resume()
        }.resume()
    }

    @objc func lockFromMenu(_ sender: Any?) { lockNow() }

    // MARK: - Navigation: only the Margin page in here; everything else goes to your browser

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if isMarginPage(url) || url.absoluteString == "about:blank" || url.scheme == "about" {
            return decisionHandler(.allow)
        }
        decisionHandler(.cancel)
        if action.navigationType == .linkActivated { openOutside(url) }
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let status = (response.response as? HTTPURLResponse)?.statusCode ?? 200
        if response.isForMainFrame && status == 403 && isMarginPage(response.response.url) {
            decisionHandler(.cancel)
            if !retried {                      // the key may have just been made: read it again, once
                retried = true
                return start()
            }
            return problem("Margin's page didn't accept this app's key.",
                           "Run <code>\(fixCommand)</code> in Terminal to set Margin up again.")
        }
        if response.isForMainFrame && status == 200 { retried = false }
        decisionHandler(.allow)
    }

    /// target=_blank links and window.open: outside links to the browser, Margin's own in this window.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url {
            if isMarginPage(url) { webView.load(URLRequest(url: url)) } else { openOutside(url) }
        }
        return nil
    }

    func openOutside(_ url: URL) {
        guard ["http", "https", "mailto"].contains(url.scheme?.lowercased() ?? "") else { return }
        NSWorkspace.shared.open(url)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
        failed(error)
    }

    func webView(_ webView: WKWebView, didFail nav: WKNavigation!, withError error: Error) {
        failed(error)
    }

    func failed(_ error: Error) {
        let e = error as NSError
        if e.code == NSURLErrorCancelled || (e.domain == "WebKitErrorDomain" && e.code == 102) { return }
        problem("Margin's page isn't answering.",
                "Its service may be off. Run <code>\(fixCommand)</code> in Terminal to start it again. "
                + "(\(escapeHTML(e.localizedDescription)))")
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { start() }

    // Page dialogs, as Mac sheets (WebKit shows nothing without these).
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    // MARK: - Menus

    @objc func reloadPage(_ sender: Any?) {
        if isMarginPage(web.url) { web.reload() } else { retried = false; start() }
    }

    @objc func zoomIn(_ sender: Any?) { web.pageZoom = min(web.pageZoom + 0.1, 3) }
    @objc func zoomOut(_ sender: Any?) { web.pageZoom = max(web.pageZoom - 0.1, 0.5) }
    @objc func actualSize(_ sender: Any?) { web.pageZoom = 1 }

    func makeMenu() -> NSMenu {
        let bar = NSMenu()
        func menu(_ title: String, _ items: [NSMenuItem]) -> NSMenu {
            let m = NSMenu(title: title)
            items.forEach(m.addItem)
            let holder = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            holder.submenu = m
            bar.addItem(holder)
            return m
        }
        func item(_ title: String, _ action: Selector?, _ key: String = "",
                  _ mods: NSEvent.ModifierFlags = .command, target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            i.target = target
            return i
        }
        _ = menu("Margin", [
            item("About Margin", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            item("Lock Margin", #selector(lockFromMenu(_:)), "l", target: self),
            .separator(),
            item("Hide Margin", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item("Show All", #selector(NSApplication.unhideAllApplications(_:))),
            .separator(),
            item("Quit Margin", #selector(NSApplication.terminate(_:)), "q"),
        ])
        _ = menu("File", [item("Close Window", #selector(NSWindow.performClose(_:)), "w")])
        _ = menu("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        _ = menu("View", [
            item("Reload Page", #selector(reloadPage(_:)), "r", target: self),
            .separator(),
            item("Actual Size", #selector(actualSize(_:)), "0", target: self),
            item("Zoom In", #selector(zoomIn(_:)), "=", target: self),
            item("Zoom Out", #selector(zoomOut(_:)), "-", target: self),
            .separator(),
            item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
        ])
        let windows = menu("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Zoom", #selector(NSWindow.performZoom(_:))),
            .separator(),
            item("Bring All to Front", #selector(NSApplication.arrangeInFront(_:))),
        ])
        NSApp.windowsMenu = windows
        return bar
    }

    // MARK: - Quitting

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    /// Lock on the way out, so the page doesn't stay open for another couple of minutes after you're done.
    func applicationShouldTerminate(_ app: NSApplication) -> NSApplication.TerminateReply {
        guard config != nil, !key.isEmpty else { return .terminateNow }
        var answered = false
        let finish = {
            if answered { return }
            answered = true
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        lockNow(reload: false, finish)
        DispatchQueue.main.asyncAfter(deadline: .now() + 2, execute: finish)
        return .terminateLater
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
