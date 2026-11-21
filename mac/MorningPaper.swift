// The Morning Paper as a small Mac app: one window showing the local paper, no browser needed.
// On launch it starts the server if it isn't running, asks for today's paper (only made if missing),
// and loads it. Clicking the Dock icon again brings the window back. Links to original articles open
// in the default browser. Built by mac/build-app.sh, which writes the project and uv paths into Info.plist.
import Cocoa
import WebKit

let base = URL(string: "http://127.0.0.1:5055/")!

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var lastDay = ""

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        web.navigationDelegate = self
        web.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "The Morning Paper"
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("MorningPaperWindow")
        window.isReleasedWhenClosed = false
        web.loadHTMLString("<body style='background:#f0eee9;font:16px Georgia;color:#55534e;display:flex;align-items:center;justify-content:center;height:90vh'>Getting today's paper…</body>", baseURL: nil)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        DispatchQueue.global().async { self.startServerIfNeeded(); self.openToday() }
    }

    // Dock icon clicked while running: bring the window back; on a new day, fetch the new paper.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        if today() != lastDay { DispatchQueue.global().async { self.startServerIfNeeded(); self.openToday() } }
        return true
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        if !lastDay.isEmpty && today() != lastDay { DispatchQueue.global().async { self.openToday() } }
    }

    func today() -> String {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; return f.string(from: Date())
    }

    func serverUp() -> Bool {
        var req = URLRequest(url: base); req.timeoutInterval = 2
        let sem = DispatchSemaphore(value: 0); var ok = false
        URLSession.shared.dataTask(with: req) { _, resp, _ in ok = (resp as? HTTPURLResponse) != nil; sem.signal() }.resume()
        sem.wait(); return ok
    }

    func startServerIfNeeded() {
        if serverUp() { return }
        let info = Bundle.main.infoDictionary ?? [:]
        let project = info["MPProject"] as? String ?? ""
        let uv = info["MPUV"] as? String ?? "uv"
        let uvDir = (uv as NSString).deletingLastPathComponent
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = ["-c", "export PATH=\"\(uvDir):/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin\"; " +
                       "cd \"\(project)\" && nohup \"\(uv)\" run morning-paper >> data-server.log 2>&1 &"]
        try? p.run(); p.waitUntilExit()
        for _ in 0..<40 { if serverUp() { return }; Thread.sleep(forTimeInterval: 1) }
    }

    func openToday() {
        var req = URLRequest(url: base.appendingPathComponent("generate"))
        req.httpMethod = "POST"; req.timeoutInterval = 10
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { _, _, _ in sem.signal() }.resume()
        _ = sem.wait(timeout: .now() + 10)
        DispatchQueue.main.async {
            self.lastDay = self.today()
            self.web.load(URLRequest(url: base))
        }
    }

    // Anything outside the paper (and every target=_blank link) opens in the default browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, let host = url.host, host != "127.0.0.1", host != "localhost",
           url.scheme == "http" || url.scheme == "https" {
            NSWorkspace.shared.open(url); decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url {
            if url.host == "127.0.0.1" || url.host == "localhost" { webView.load(action.request) } else { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { retryLater() }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { retryLater() }
    func retryLater() { DispatchQueue.global().asyncAfter(deadline: .now() + 3) { self.startServerIfNeeded(); DispatchQueue.main.async { self.web.load(URLRequest(url: base)) } } }

    @objc func reloadPage() { web.reload() }
    @objc func goHome() { web.load(URLRequest(url: base)) }
    @objc func goBack() { web.goBack() }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Hide Morning Paper", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Morning Paper", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Today's Paper", action: #selector(goHome), keyEquivalent: "t").target = self
        view.addItem(withTitle: "Reload", action: #selector(reloadPage), keyEquivalent: "r").target = self
        view.addItem(withTitle: "Back", action: #selector(goBack), keyEquivalent: "[").target = self
        viewItem.submenu = view
        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        winItem.submenu = win
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
