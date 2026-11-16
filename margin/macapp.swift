// The Margin app: the Margin page in a window of its own (AppKit + WebKit) instead of a browser tab, and Margin's m
// in the menu bar for a quick glance.
//
// Why a window and not the browser: browser extensions that may read all sites can read a page in a tab, and a tab
// leaves the page in the browser's history, cache and autofill. Here nothing is kept on disk (a private, in-memory
// website data store), there's no address bar, and nothing else can load the page through this window.
//
// The page itself still comes from Margin's own server on this Mac (margin/server.py), which keeps all its checks:
// the secret key, the Host allowlist and the Touch ID lock. This app hands the server its key as the same private
// cookie a browser gets, but read straight from the key file (readable only by you), so the key never appears in an
// address, the history or on a command line.
//
// - Links to anything that isn't the Margin page open in your default browser, never in this window.
// - Margin locks at once when the Mac locks, sleeps or switches user, when you hide this app, and when you quit it
//   (only if the lock is on: "lock_minutes" in settings.json).
// - The menu bar shows only the m, never a figure (anyone near the screen, or a screen recording, sees the menu
//   bar). A click opens a small panel with today's spending, this month, net worth, Sort out and the last sync. It
//   asks the server (/api/glance) only while it's open, keeps nothing once it closes, and while Margin is locked it
//   shows no figures at all, only "Unlock with Touch ID".
// - Closing the window keeps Margin in the menu bar (the page and its figures go with the window); Quit ends it.
// - The window remembers its size and place; nothing else is saved.
//
// Built on this Mac by `margin app install` (margin/macapp.py) with swiftc from the Command Line Tools. Info.plist
// says where the page is (MarginURL, always this Mac) and where its key is (MarginKeyFile). Only the demo's copy
// (MarginDemo) takes --port <n>; the real app ignores it, so nothing that starts it can point it, and its key, at
// another port. The address itself can't be changed from outside, so the real app only ever shows the real page
// and the demo's copy only the demo.
import AppKit
import ServiceManagement
import WebKit

let cookieName = "margin_key"            // server.COOKIE
let info = Bundle.main.infoDictionary ?? [:]
let isDemo = info["MarginDemo"] as? Bool ?? false
/// What to run in Terminal when something's wrong (the demo's copy says how to start the demo).
let fixCommand = info["MarginFix"] as? String ?? "margin app install"

struct Config {
    let base: URL                        // e.g. http://margin.localhost/
    let keyFile: String
}

/// Only plain http on this Mac's own names: the key is never sent anywhere else.
func isThisMac(_ url: URL) -> Bool {
    guard url.scheme == "http", let host = url.host?.lowercased() else { return false }
    return host == "localhost" || host.hasSuffix(".localhost") || host == "127.0.0.1" || host == "::1"
}

func loadConfig() -> Config? {
    guard let text = info["MarginURL"] as? String, var parts = URLComponents(string: text) else { return nil }
    let args = CommandLine.arguments
    if isDemo, let i = args.firstIndex(of: "--port"), i + 1 < args.count, let port = Int(args[i + 1]),
       (1...65535).contains(port) {
        parts.port = port                                // the demo's copy only: the demo can run on any port
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

// MARK: - Talking to Margin's server

final class Client {
    let config: Config?
    var key = ""
    let session = URLSession(configuration: .ephemeral)     // nothing on disk here either

    init(_ config: Config?) { self.config = config }

    /// Read the key (again: it's cheap and the file is the truth). The page service makes it the first time it
    /// starts, so if it isn't there yet, wake the service (its icon needs no key) and read it once more.
    func ensureKey(_ then: @escaping (Bool) -> Void) {
        guard let cfg = config else { return then(false) }
        if let k = readKey(cfg.keyFile) {
            key = k
            return then(true)
        }
        var req = URLRequest(url: cfg.base.appendingPathComponent("favicon.png"), timeoutInterval: 15)
        req.httpShouldHandleCookies = false
        session.dataTask(with: req) { _, _, _ in
            DispatchQueue.main.async {
                if let k = readKey(cfg.keyFile) { self.key = k; then(true) } else { then(false) }
            }
        }.resume()
    }

    /// GET or POST (empty JSON) with the key in the header. Answers on the main queue: (HTTP status or 0, JSON).
    func call(_ path: String, post: Bool = false, timeout: TimeInterval = 5,
              _ done: @escaping (Int, [String: Any]?) -> Void) {
        guard let cfg = config, !key.isEmpty else { return done(0, nil) }
        var req = URLRequest(url: cfg.base.appendingPathComponent(path), timeoutInterval: timeout)
        req.httpShouldHandleCookies = false
        req.setValue(key, forHTTPHeaderField: "X-Margin-Key")
        if post {
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = Data("{}".utf8)
        }
        session.dataTask(with: req) { data, response, _ in
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
            DispatchQueue.main.async { done(code, json) }
        }.resume()
    }
}

// MARK: - The app

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate,
                         NSPopoverDelegate {
    var window: NSWindow!
    var web: WKWebView?
    var client = Client(nil)
    var retried = false                  // one fresh read of the key after a refusal, then say what's wrong
    var titleWatch: NSKeyValueObservation?
    var statusItem: NSStatusItem!
    let popover = NSPopover()
    var panel: Panel!
    var clickWatch: Any?
    var atLogin = false

    func applicationWillFinishLaunching(_ note: Notification) {
        let event = NSAppleEventManager.shared().currentAppleEvent
        atLogin = event?.eventID == AEEventID(kAEOpenApplication)
            && event?.paramDescriptor(forKeyword: AEKeyword(keyAEPropData))?.enumCodeValue == OSType(keyAELaunchedAsLogInItem)
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        client = Client(loadConfig())
        NSApp.mainMenu = makeMenu()
        makeWindow()
        makeStatusItem()
        watchForAway()
        if atLogin {
            NSApp.setActivationPolicy(.accessory)      // started at login: just the menu bar until you open it
        } else {
            openWindow(nil)
        }
    }

    // MARK: Window

    func makeWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Margin"
        window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: 400, height: 480)
        window.isRestorable = false                          // no saved window state on disk
        window.tabbingMode = .disallowed
        window.collectionBehavior.insert(.fullScreenPrimary)
        window.delegate = self
        window.center()
        window.setFrameAutosaveName("Margin")                // remembers size and place (just the frame)
    }

    /// Show the window, with a fresh page if it was closed. fragment: a tab to open on ("sortout", "reviews").
    func openWindow(_ fragment: String?) {
        NSApp.setActivationPolicy(.regular)
        if web == nil {
            let conf = WKWebViewConfiguration()
            conf.websiteDataStore = .nonPersistent()         // cookies, cache and storage live in memory only
            conf.preferences.javaScriptCanOpenWindowsAutomatically = false
            conf.suppressesIncrementalRendering = true
            let view = WKWebView(frame: window.contentLayoutRect, configuration: conf)
            view.navigationDelegate = self
            view.uiDelegate = self
            view.allowsBackForwardNavigationGestures = false
            view.allowsLinkPreview = false
            view.setValue(false, forKey: "drawsBackground")  // the window's own colour until the page paints
            titleWatch = view.observe(\.title, options: [.new]) { [weak self] v, _ in
                DispatchQueue.main.async { self?.window.title = (v.title?.isEmpty ?? true) ? "Margin" : v.title! }
            }
            web = view
            window.contentView = view
            if client.config == nil {
                problem("This copy of Margin doesn't know where its page is.",
                        "Run <code>\(escapeHTML(fixCommand))</code> in Terminal to make it again.")
            } else {
                start(fragment)
            }
        } else if let fragment = fragment, let view = web, isMarginPage(view.url) {
            view.evaluateJavaScript("location.hash='#\(fragment)';location.reload()")   // the page reads it on load
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Closing the window ends the page (and every figure in it); Margin stays in the menu bar.
    func windowWillClose(_ note: Notification) {
        guard let view = web else { return }
        view.stopLoading()
        view.navigationDelegate = nil
        view.uiDelegate = nil
        titleWatch = nil
        web = nil
        window.contentView = nil
        window.title = "Margin"
        NSApp.setActivationPolicy(.accessory)
    }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !window.isVisible { openWindow(nil) }      // opened again from Spotlight, Finder or the Dock
        return true
    }

    func start(_ fragment: String? = nil) {
        client.ensureKey { [weak self] ok in
            guard let self = self else { return }
            if ok { return self.loadPage(fragment) }
            self.problem("Margin's page isn't set up yet.",
                         "Run <code>\(escapeHTML(fixCommand))</code> in Terminal, then open Margin again.")
        }
    }

    func loadPage(_ fragment: String?) {
        guard let view = web, let cfg = client.config, let host = cfg.base.host else { return }
        let props: [HTTPCookiePropertyKey: Any] = [
            .name: cookieName, .value: client.key, .domain: host, .path: "/",
            HTTPCookiePropertyKey("HttpOnly"): "TRUE", .sameSitePolicy: HTTPCookieStringPolicy.sameSiteStrict,
        ]
        guard let cookie = HTTPCookie(properties: props) else {
            return problem("Margin couldn't hand its page the key.",
                           "Run <code>\(escapeHTML(fixCommand))</code> in Terminal.")
        }
        var url = cfg.base
        if let f = fragment, let u = URL(string: "#" + f, relativeTo: cfg.base)?.absoluteURL { url = u }
        view.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) {
            view.load(URLRequest(url: url))
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
        web?.loadHTMLString(html, baseURL: nil)
    }

    func isMarginPage(_ url: URL?) -> Bool {
        guard let url = url, let base = client.config?.base else { return false }
        return url.scheme == base.scheme && url.host?.lowercased() == base.host?.lowercased() && url.port == base.port
    }

    // MARK: Locking when you're away

    func watchForAway() {
        let away: (Notification) -> Void = { [weak self] _ in
            self?.popover.performClose(nil)
            self?.lockNow()
        }
        let ws = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.willSleepNotification, NSWorkspace.screensDidSleepNotification,
                     NSWorkspace.sessionDidResignActiveNotification] {
            ws.addObserver(forName: name, object: nil, queue: .main, using: away)
        }
        DistributedNotificationCenter.default().addObserver(forName: Notification.Name("com.apple.screenIsLocked"),
                                                            object: nil, queue: .main, using: away)
        NotificationCenter.default.addObserver(forName: NSApplication.didHideNotification, object: nil,
                                               queue: .main, using: away)
    }

    /// Ask the server to lock (it's the one that holds the lock), then, if it's on, reload: the server answers with
    /// the lock screen, so no figures stay in the window. With the lock off nothing changes.
    func lockNow(reload: Bool = true, _ done: (() -> Void)? = nil) {
        client.call("api/lock", post: true, timeout: 3) { [weak self] _, _ in
            self?.client.call("api/ping", timeout: 3) { _, reply in
                guard let self = self else { return }
                if reload, reply?["locked"] as? Bool == true, let view = self.web, self.isMarginPage(view.url) {
                    view.reload()
                }
                if self.popover.isShown { self.panel.refresh() }
                done?()
            }
        }
    }

    /// Unlocked from the menu bar panel: a lock screen in the window gives way to the page.
    func unlocked() {
        if let view = web, isMarginPage(view.url) { view.reload() }
    }

    @objc func lockFromMenu(_ sender: Any?) { lockNow() }

    // MARK: Menu bar

    func makeStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        panel = Panel(app: self)
        popover.contentViewController = panel
        popover.behavior = .transient
        popover.animates = true
        popover.delegate = self
        guard let button = statusItem.button else { return }
        let image = NSImage(named: "MenuBarIcon")
            ?? NSImage(systemSymbolName: "chart.bar.fill", accessibilityDescription: nil)
        image?.isTemplate = true                        // drawn in the menu bar's own colour
        image?.size = NSSize(width: 18, height: 18)
        button.image = image
        button.toolTip = isDemo ? "Margin (demo)" : "Margin"
        button.setAccessibilityLabel(isDemo ? "Margin demo" : "Margin")
        button.target = self
        button.action = #selector(togglePanel(_:))
    }

    @objc func togglePanel(_ sender: Any?) {
        if popover.isShown { return popover.performClose(sender) }
        showPanel()
    }

    func showPanel() {
        guard let button = statusItem.button, !popover.isShown else { return }
        panel.render(.loading)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        panel.opened()                                  // after show: answers for a closed panel are dropped
        // Close on a click anywhere else, like a menu.
        clickWatch = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            self?.popover.performClose(nil)
        }
    }

    func popoverDidClose(_ note: Notification) {
        panel.closed()
        if let w = clickWatch { NSEvent.removeMonitor(w) }
        clickWatch = nil
    }

    @objc func toggleLogin(_ sender: Any?) {
        if #available(macOS 13.0, *) {
            do {
                if SMAppService.mainApp.status == .enabled {
                    try SMAppService.mainApp.unregister()
                } else {
                    try SMAppService.mainApp.register()
                }
            } catch {
                NSAlert(error: error).runModal()
            }
        }
    }

    var opensAtLogin: Bool {
        if #available(macOS 13.0, *) { return SMAppService.mainApp.status == .enabled }
        return false
    }

    // MARK: Navigation: only the Margin page in here; everything else goes to your browser

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if isMarginPage(url) || url.scheme == "about" {
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
                           "Run <code>\(escapeHTML(fixCommand))</code> in Terminal to set Margin up again.")
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
                "Its service may be off. Run <code>\(escapeHTML(fixCommand))</code> in Terminal to start it again. "
                + "(\(escapeHTML(e.localizedDescription)))")
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if webView === web { start() }
    }

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

    // MARK: Menus

    @objc func reloadPage(_ sender: Any?) {
        guard let view = web else { return openWindow(nil) }
        if isMarginPage(view.url) { view.reload() } else { retried = false; start() }
    }

    @objc func zoomIn(_ sender: Any?) { web.map { $0.pageZoom = min($0.pageZoom + 0.1, 3) } }
    @objc func zoomOut(_ sender: Any?) { web.map { $0.pageZoom = max($0.pageZoom - 0.1, 0.5) } }
    @objc func actualSize(_ sender: Any?) { web?.pageZoom = 1 }

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

    // MARK: Quitting

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { false }

    /// Lock on the way out, so the page doesn't stay open for another couple of minutes after you're done.
    func applicationShouldTerminate(_ app: NSApplication) -> NSApplication.TerminateReply {
        guard client.config != nil, !client.key.isEmpty else { return .terminateNow }
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

// MARK: - The menu bar panel

/// What the panel shows. Figures only ever come from a fresh /api/glance and are dropped when it closes.
enum PanelState {
    case empty, loading, figures([String: Any]), locked(String?), unlocking, problem(String), outdated
}

final class Panel: NSViewController {
    weak var app: AppDelegate?
    let stack = NSStackView()
    var refreshTimer: Timer?
    var lockTimer: Timer?
    var syncTimer: Timer?
    var hasFigures = false

    init(app: AppDelegate) {
        self.app = app
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    var client: Client? { app?.client }

    override func loadView() {
        let root = NSView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 14, left: 16, bottom: 12, right: 16)
        stack.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            stack.topAnchor.constraint(equalTo: root.topAnchor),
            stack.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            root.widthAnchor.constraint(equalToConstant: 320),
        ])
        view = root
    }

    func opened() {
        refresh()
        refreshTimer = every(30) { [weak self] in self?.refresh() }       // a slow refresh while it's open
        lockTimer = every(3) { [weak self] in self?.checkLock() }         // and a quick look at the lock
    }

    /// While figures are showing, ask the server (cheaply) whether Margin has locked, on its own timer or from the
    /// page's Lock button, so they go within seconds instead of at the next refresh.
    func checkLock() {
        guard hasFigures, isShown else { return }
        client?.call("api/ping", timeout: 3) { [weak self] code, json in
            guard let self = self, self.hasFigures, self.isShown else { return }
            if code == 423 || json?["locked"] as? Bool == true { self.render(.locked(nil)) }
        }
    }

    func closed() {
        refreshTimer?.invalidate()
        lockTimer?.invalidate()
        refreshTimer = nil
        lockTimer = nil
        stopSyncWatch()
        render(.empty)                                   // no figures stay behind in the views
    }

    func every(_ seconds: TimeInterval, _ fn: @escaping () -> Void) -> Timer {
        let t = Timer(timeInterval: seconds, repeats: true) { _ in fn() }
        RunLoop.main.add(t, forMode: .common)
        return t
    }

    func refresh() {
        guard let client = client else { return }
        if client.config == nil { return render(.problem("This copy of Margin doesn't know where its page is.")) }
        client.ensureKey { [weak self] ok in
            guard let self = self, self.isShown else { return }
            guard ok else { return self.render(.problem("Margin's page isn't set up yet.")) }
            client.call("api/glance") { code, json in
                guard self.isShown else { return }
                switch code {
                case 200 where json != nil:
                    self.render(.figures(json!))
                    if ((json!["sync"] as? [String: Any])?["running"] as? Bool) == true { self.watchSync() }
                case 423: self.render(.locked(nil))
                case 404: self.render(.outdated)        // a page service from before the glance
                case 0: self.render(.problem("Margin's page isn't answering."))
                default: self.render(.problem("Margin couldn't read its figures (\(code))."))
                }
            }
        }
    }

    var isShown: Bool { app?.popover.isShown ?? false }

    // MARK: Actions

    @objc func openMargin(_ sender: Any?) { open(nil) }
    @objc func openSortOut(_ sender: Any?) { open("sortout") }
    @objc func openReviews(_ sender: Any?) { open("reviews") }

    func open(_ fragment: String?) {
        app?.popover.performClose(nil)
        app?.openWindow(fragment)
    }

    @objc func lock(_ sender: Any?) {
        app?.lockNow { [weak self] in self?.refresh() }
    }

    @objc func unlock(_ sender: Any?) {
        render(.unlocking)
        client?.call("api/unlock", post: true, timeout: 180) { [weak self] _, json in
            guard let self = self, let app = self.app else { return }
            if json?["ok"] as? Bool == true {
                app.unlocked()
                if self.isShown { self.refresh() } else { app.showPanel() }   // macOS's prompt may have closed it
            } else if self.isShown {
                self.render(.locked(json?["why"] as? String ?? "Not unlocked"))
            }
        }
    }

    @objc func syncNow(_ sender: Any?) {
        client?.call("api/sync", post: true) { [weak self] code, _ in
            guard let self = self, self.isShown else { return }       // closed meanwhile: nothing to watch
            if code == 423 { return self.render(.locked(nil)) }
            self.syncLine?.attributedStringValue = self.statusText("Syncing…", ok: true)
            self.watchSync()
        }
    }

    weak var syncLine: NSTextField?

    func watchSync() {
        guard syncTimer == nil else { return }
        syncTimer = every(2) { [weak self] in
            guard let self = self, self.isShown else { self?.stopSyncWatch(); return }
            self.client?.call("api/progress") { code, json in
                guard self.isShown else { return self.stopSyncWatch() }
                if code == 423 { self.stopSyncWatch(); return self.render(.locked(nil)) }
                let running = json?["running"] as? Bool ?? false
                if running {
                    var text = "Syncing…"
                    if let p = json?["progress"] as? [String: Any], let done = p["done"] as? Double,
                       let total = p["total"] as? Double, total > 0 {
                        text = "Syncing… \(Int(done / total * 100))%"
                    }
                    self.syncLine?.attributedStringValue = self.statusText(text, ok: true)
                } else {
                    self.stopSyncWatch()
                    self.refresh()
                }
            }
        }
    }

    func stopSyncWatch() {
        syncTimer?.invalidate()
        syncTimer = nil
    }

    @objc func showMore(_ sender: NSButton) {
        let menu = NSMenu()
        if !isDemo {
            let login = NSMenuItem(title: "Open at Login", action: #selector(AppDelegate.toggleLogin(_:)),
                                   keyEquivalent: "")
            login.target = app
            login.state = (app?.opensAtLogin ?? false) ? .on : .off
            menu.addItem(login)
            menu.addItem(.separator())
        }
        let quit = NSMenuItem(title: isDemo ? "Quit Margin (stops the demo)" : "Quit Margin",
                              action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        quit.target = NSApp
        menu.addItem(quit)
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: sender.bounds.height + 4), in: sender)
    }

    // MARK: Drawing

    func render(_ state: PanelState) {
        _ = view
        stack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        syncLine = nil
        if case .figures = state { hasFigures = true } else { hasFigures = false }
        switch state {
        case .empty:
            return
        case .loading:
            add(header(nil))
            add(text("…", size: 13, color: .secondaryLabelColor))
        case .figures(let j):
            figures(j)
        case .locked(let why):
            lockedView(why, busy: false)
        case .unlocking:
            lockedView("Waiting for Touch ID…", busy: true)
        case .outdated:
            add(header(nil))
            add(text("The glance needs a newer Margin.", size: 13))
            add(text("Margin's page service is older than this app. Update the Margin folder, then restart the "
                     + "service: margin app install.", size: 11, color: .secondaryLabelColor))
            add(buttons([], primary: button("Open Margin", #selector(openMargin(_:)))))
        case .problem(let message):
            add(header(nil))
            add(text(message, size: 13))
            add(text("Run \(fixCommand) in Terminal, then try again.", size: 11, color: .secondaryLabelColor))
            add(buttons([], primary: button("Open Margin", #selector(openMargin(_:)))))
        }
        // The panel's own height (not the popover's current one), so it grows and shrinks with what it shows.
        let size = NSSize(width: 320, height: ceil(stack.fittingSize.height))
        preferredContentSize = size
        if let popover = app?.popover, popover.isShown { popover.contentSize = size }
    }

    func figures(_ j: [String: Any]) {
        let sync = j["sync"] as? [String: Any] ?? [:]
        let status = header(syncStatus(sync))
        add(status)
        add(separator())

        let today = j["today"] as? [String: Any] ?? [:]
        add(stat("Spent today", today["spent"] as? String ?? "—", size: 24))

        let month = j["month"] as? [String: Any] ?? [:]
        let net = j["net"] as? [String: Any] ?? [:]
        let change = net["change"] as? [String: Any]
        let monthCol = stat(month["label"] as? String ?? "Spent this month", month["spent"] as? String ?? "—", size: 16,
                            note: month["vs"] as? String, trend: month["trend"] as? String, upIsGood: false)
        let netCol = stat("Net worth", net["value"] as? String ?? "—", size: 16,
                          note: change.map { "\($0["amount"] as? String ?? "") \($0["since"] as? String ?? "")" },
                          trend: change?["trend"] as? String, upIsGood: true)
        let cols = NSStackView(views: [monthCol, netCol])
        cols.orientation = .horizontal
        cols.alignment = .top
        cols.distribution = .fillEqually
        cols.spacing = 12
        add(cols, fill: true)
        add(separator())

        let waiting = j["sort_out"] as? Int ?? 0
        if waiting > 0 {
            add(link("\(waiting) waiting in Sort out", symbol: "tray.full", #selector(openSortOut(_:))))
        } else {
            add(link("Nothing waiting in Sort out", symbol: "checkmark.circle", nil))
        }
        let checks = j["checks"] as? Int ?? 0
        if checks > 0 {
            add(link("\(checks) check\(checks == 1 ? "" : "s") to look at", symbol: "exclamationmark.triangle",
                     #selector(openReviews(_:))))
        }
        add(separator())
        var left = [button("Sync now", #selector(syncNow(_:)))]
        if j["lock_on"] as? Bool ?? false { left.append(button("Lock", #selector(lock(_:)))) }
        add(buttons(left, primary: button("Open Margin", #selector(openMargin(_:)))), fill: true)
    }

    func lockedView(_ message: String?, busy: Bool) {
        add(header(nil))
        let lockIcon = NSImageView(image: NSImage(systemSymbolName: "lock.fill", accessibilityDescription: nil)
                                   ?? NSImage())
        lockIcon.symbolConfiguration = .init(pointSize: 22, weight: .regular)
        lockIcon.contentTintColor = .secondaryLabelColor
        let title = text("Margin is locked", size: 15, weight: .semibold)
        let sub = text("Your figures stay hidden until you unlock it.", size: 11, color: .secondaryLabelColor)
        let group = NSStackView(views: [lockIcon, title, sub])
        group.orientation = .vertical
        group.alignment = .centerX
        group.spacing = 4
        if let m = message {
            group.addArrangedSubview(text(m, size: 11, color: busy ? .secondaryLabelColor : .systemRed))
        }
        add(group, fill: true)
        let unlock = button("Unlock with Touch ID", #selector(unlock(_:)))
        unlock.isEnabled = !busy
        unlock.keyEquivalent = "\r"
        add(buttons([button("Open Margin", #selector(openMargin(_:)))], primary: unlock), fill: true)
    }

    // MARK: Pieces

    func add(_ v: NSView, fill: Bool = false) {
        stack.addArrangedSubview(v)
        if fill {
            v.translatesAutoresizingMaskIntoConstraints = false
            v.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -32).isActive = true
        }
    }

    func text(_ s: String, size: CGFloat, weight: NSFont.Weight = .regular, color: NSColor = .labelColor,
              digits: Bool = false) -> NSTextField {
        let l = NSTextField(wrappingLabelWithString: s)
        l.font = digits ? .monospacedDigitSystemFont(ofSize: size, weight: weight) : .systemFont(ofSize: size, weight: weight)
        l.textColor = color
        l.isSelectable = false
        l.preferredMaxLayoutWidth = 288
        return l
    }

    func header(_ status: NSAttributedString?) -> NSView {
        let name = text(isDemo ? "Margin · demo" : "Margin", size: 13, weight: .semibold)
        var views: [NSView] = [name, NSView()]
        if let s = status {
            let l = NSTextField(labelWithAttributedString: s)
            syncLine = l
            views.append(l)
        }
        let row = NSStackView(views: views)
        row.orientation = .horizontal
        row.distribution = .fill
        row.translatesAutoresizingMaskIntoConstraints = false
        row.widthAnchor.constraint(equalToConstant: 288).isActive = true
        return row
    }

    func statusText(_ s: String, ok: Bool) -> NSAttributedString {
        let out = NSMutableAttributedString(string: "● ", attributes: [
            .foregroundColor: ok ? NSColor.systemGreen : NSColor.systemOrange, .font: NSFont.systemFont(ofSize: 9)])
        out.append(NSAttributedString(string: s, attributes: [
            .foregroundColor: NSColor.secondaryLabelColor, .font: NSFont.systemFont(ofSize: 11)]))
        return out
    }

    func syncStatus(_ sync: [String: Any]) -> NSAttributedString {
        if sync["running"] as? Bool == true { return statusText("Syncing…", ok: true) }
        let ok = sync["ok"] as? Bool ?? false
        guard let at = sync["at"] as? String, let when = parseLocal(at) else {
            return statusText("Not synced yet", ok: false)
        }
        let f = DateFormatter()
        if Calendar.current.isDateInToday(when) {
            f.timeStyle = .short
            f.dateStyle = .none
        } else {
            f.setLocalizedDateFormatFromTemplate("d MMM")
        }
        let problems = sync["problems"] as? Int ?? 0
        return statusText("Synced \(f.string(from: when))" + (problems > 0 ? " · \(problems) problem\(problems == 1 ? "" : "s")" : ""),
                          ok: ok)
    }

    func parseLocal(_ iso: String) -> Date? {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return f.date(from: String(iso.prefix(19)))
    }

    func stat(_ caption: String, _ value: String, size: CGFloat, note: String? = nil, trend: String? = nil,
              upIsGood: Bool = true) -> NSView {
        var views: [NSView] = [text(caption, size: 11, color: .secondaryLabelColor),
                               text(value, size: size, weight: .semibold, digits: true)]
        if let n = note, !n.trimmingCharacters(in: .whitespaces).isEmpty {
            var colour = NSColor.secondaryLabelColor
            if trend == "up" { colour = upIsGood ? .systemGreen : .systemRed }
            if trend == "down" { colour = upIsGood ? .systemRed : .systemGreen }
            let t = text(n, size: 11, color: colour)
            t.preferredMaxLayoutWidth = 138
            views.append(t)
        }
        let s = NSStackView(views: views)
        s.orientation = .vertical
        s.alignment = .leading
        s.spacing = 2
        return s
    }

    func link(_ title: String, symbol: String, _ action: Selector?) -> NSView {
        let b = NSButton(title: title, target: action == nil ? nil : self, action: action)
        b.isBordered = false
        b.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        b.imagePosition = .imageLeading
        b.imageHugsTitle = true
        b.font = .systemFont(ofSize: 13)
        b.contentTintColor = action == nil ? .secondaryLabelColor : .controlAccentColor
        b.isEnabled = action != nil
        return b
    }

    func button(_ title: String, _ action: Selector) -> NSButton {
        let b = NSButton(title: title, target: self, action: action)
        b.bezelStyle = .rounded
        b.controlSize = .regular
        return b
    }

    func buttons(_ left: [NSButton], primary: NSButton) -> NSView {
        if primary.keyEquivalent.isEmpty { primary.keyEquivalent = "\r" }
        let more = NSButton(image: NSImage(systemSymbolName: "ellipsis.circle", accessibilityDescription: "More")
                            ?? NSImage(), target: self, action: #selector(showMore(_:)))
        more.isBordered = false
        more.setAccessibilityLabel("More")
        let row = NSStackView(views: [more] + left + [NSView(), primary])
        row.orientation = .horizontal
        row.spacing = 6
        return row
    }

    func separator() -> NSView {
        let b = NSBox()
        b.boxType = .separator
        b.translatesAutoresizingMaskIntoConstraints = false
        b.widthAnchor.constraint(equalToConstant: 288).isActive = true
        return b
    }
}

// `margin app remove` runs the app once with --forget-login before deleting it, so no Login Items entry is left.
if CommandLine.arguments.contains("--forget-login") {
    if #available(macOS 13.0, *) { try? SMAppService.mainApp.unregister() }
    exit(0)
}

// Asked to stop (`margin app remove`, the demo's Ctrl-C): quit the normal way, which locks Margin first.
signal(SIGTERM, SIG_IGN)
let stopSignal = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
stopSignal.setEventHandler {
    // From the run loop, not inside this block: quitting waits for the lock call, whose answer comes on this queue.
    NSApp.perform(#selector(NSApplication.terminate(_:)), with: nil, afterDelay: 0)
}
stopSignal.resume()

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
