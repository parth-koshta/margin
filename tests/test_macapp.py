import tests  # noqa: F401  (a temporary data folder, never the real one)
import argparse
import io
import plistlib
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from margin import __main__ as cli
from margin import macapp


class ThisMacOnly(unittest.TestCase):
    def test_only_plain_http_on_this_macs_own_names(self):
        for url in ("http://margin.localhost/", "http://demo.margin.localhost:8767/", "http://127.0.0.1/",
                    "http://localhost:47215/", "http://[::1]/"):
            self.assertTrue(macapp.this_mac(url), url)
        for url in ("https://margin.localhost/", "http://margin.example.com/", "http://localhost.example.com/",
                    "http://10.0.0.5/", "file:///tmp/x", "http://marginlocalhost/"):
            self.assertFalse(macapp.this_mac(url), url)

    def test_build_refuses_an_address_off_this_mac(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("margin.macapp.subprocess.run") as run:
            with self.assertRaises(RuntimeError):
                macapp.build(Path(tmp) / "Margin.app", "http://margin.example.com/", Path(tmp) / "server.key")
            run.assert_not_called()


class InfoPlist(unittest.TestCase):
    def test_says_where_the_page_and_key_are_but_never_holds_the_key(self):
        key_file = Path(tests.DATA_DIR) / "server.key"
        p = macapp.info_plist("http://margin.localhost/", key_file, macapp.BUNDLE_ID)
        self.assertEqual(p["MarginURL"], "http://margin.localhost/")
        self.assertEqual(p["MarginKeyFile"], str(key_file))
        self.assertEqual((p["CFBundleName"], p["CFBundleExecutable"]), ("Margin", "Margin"))
        self.assertNotIn("demo", p["CFBundleIdentifier"])
        ats = p["NSAppTransportSecurity"]["NSExceptionDomains"]["localhost"]
        self.assertTrue(ats["NSIncludesSubdomains"] and ats["NSExceptionAllowsInsecureHTTPLoads"])
        self.assertNotIn("NSAllowsArbitraryLoads", p["NSAppTransportSecurity"])
        plistlib.dumps(p)                                   # writable as a real Info.plist

    def test_demo_copy_has_its_own_identity(self):
        self.assertNotEqual(macapp.BUNDLE_ID, macapp.DEMO_BUNDLE_ID)
        self.assertFalse(macapp.info_plist("http://margin.localhost/", Path("k"), macapp.BUNDLE_ID)["MarginDemo"])
        self.assertTrue(macapp.info_plist("http://demo.margin.localhost:8765/", Path("k"), macapp.DEMO_BUNDLE_ID,
                                          demo=True)["MarginDemo"])


class MenuBarIcon(unittest.TestCase):
    """The menu bar's m: icon.py's shape, black on clear (a template), centred, and only a picture."""

    def pixels(self, px):
        import struct
        import zlib
        data = macapp.menu_bar_png(px)
        self.assertTrue(data.startswith(b"\x89PNG"))
        w, h, depth, kind = struct.unpack(">IIBB", data[16:26])
        self.assertEqual((w, h, depth, kind), (px, px, 8, 4))           # grey + alpha
        i = data.index(b"IDAT")
        raw = zlib.decompress(data[i + 4:i + 4 + struct.unpack(">I", data[i - 4:i])[0]])
        rows = [raw[r * (1 + 2 * px) + 1:(r + 1) * (1 + 2 * px)] for r in range(px)]
        return [[(row[2 * x], row[2 * x + 1]) for x in range(px)] for row in rows]

    def test_black_glyph_on_clear_and_centred(self):
        px = self.pixels(36)
        self.assertTrue(all(g == 0 for row in px for g, _ in row))       # colour comes from macOS (template)
        self.assertEqual(px[0][0][1], 0)                                 # corners clear: no tile
        lit = [(x, y) for y, row in enumerate(px) for x, (_, a) in enumerate(row) if a]
        xs, ys = [x for x, _ in lit], [y for _, y in lit]
        self.assertLessEqual(abs(min(xs) - (35 - max(xs))), 2)
        self.assertLessEqual(abs(min(ys) - (35 - max(ys))), 2)
        self.assertGreater(len(lit), 200)

    def test_both_sizes_go_in_the_bundle(self):
        self.assertEqual(len(self.pixels(macapp.MENU_PT)), 18)


class Build(unittest.TestCase):
    """build() with swiftc, the icon and codesign stood in: what ends up in the bundle, and the swap."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="margin-app-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.calls = []

    def fake_run(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == macapp.SWIFTC:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"program")
        return mock.Mock(returncode=0, stdout="", stderr="")

    def build(self, app):
        with mock.patch("margin.macapp.subprocess.run", side_effect=self.fake_run), \
                mock.patch("margin.icon.write_icns", side_effect=lambda p: Path(p).write_bytes(b"icns")):
            return macapp.build(app, "http://margin.localhost/", self.tmp / "server.key")

    def test_puts_the_bundle_together_and_replaces_the_old_app(self):
        app = self.tmp / "Margin.app"
        (app / "Contents" / "Resources").mkdir(parents=True)
        (app / "Contents" / "Resources" / "applet.icns").write_bytes(b"old AppleScript app")
        self.build(app)
        self.assertFalse((app / "Contents" / "Resources" / "applet.icns").exists())
        self.assertEqual(macapp.executable(app).read_bytes(), b"program")
        self.assertTrue((app / "Contents" / "Resources" / "Margin.icns").exists())
        for name in ("MenuBarIcon.png", "MenuBarIcon@2x.png"):
            self.assertTrue((app / "Contents" / "Resources" / name).exists())
        with open(app / "Contents" / "Info.plist", "rb") as f:
            self.assertEqual(plistlib.load(f)["MarginURL"], "http://margin.localhost/")
        self.assertFalse((self.tmp / ".Margin.app.new").exists())
        self.assertIn("codesign", [c[0] for c in self.calls])
        self.assertFalse(macapp.stale(app))

    def test_a_failed_compile_leaves_the_old_app_alone(self):
        app = self.tmp / "Margin.app"
        (app / "Contents").mkdir(parents=True)
        (app / "Contents" / "old").write_text("x")
        with mock.patch("margin.macapp.subprocess.run",
                        return_value=mock.Mock(returncode=1, stdout="", stderr="error: nope")):
            with self.assertRaises(RuntimeError):
                macapp.build(app, "http://margin.localhost/", self.tmp / "server.key")
        self.assertTrue((app / "Contents" / "old").exists())
        self.assertFalse((self.tmp / ".Margin.app.new").exists())


class Install(unittest.TestCase):
    """`margin app install`: the window app when swiftc is here, the AppleScript one when it isn't or fails."""

    def run_install(self, can_build, build_error=None):
        a = argparse.Namespace(action="install")
        with mock.patch.object(cli, "_install_web_agent", return_value=True), \
                mock.patch.object(cli, "APP_PATH", str(Path(tests.DATA_DIR) / "Applications" / "Margin.app")), \
                mock.patch("margin.macapp.can_build", return_value=can_build), \
                mock.patch("margin.macapp.build", side_effect=build_error) as build, \
                mock.patch.object(cli, "_make_script_app", return_value=True) as script, \
                mock.patch("margin.server.hostname_ok", return_value=True), \
                mock.patch("margin.server.address", return_value="http://margin.localhost/"), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_app(a), 0)
        return build, script, out.getvalue()

    def test_builds_the_window_app(self):
        build, script, out = self.run_install(True)
        build.assert_called_once()
        self.assertEqual(build.call_args[0][1], "http://margin.localhost/")
        script.assert_not_called()
        self.assertIn("window of its own", out)

    def test_falls_back_without_swiftc(self):
        build, script, out = self.run_install(False)
        build.assert_not_called()
        script.assert_called_once()

    def test_falls_back_when_the_build_fails(self):
        build, script, out = self.run_install(True, RuntimeError("swiftc couldn't build the app"))
        script.assert_called_once()
        self.assertIn("opens your browser instead", out)


class DemoWindow(unittest.TestCase):
    """`margin demo --app`: the demo's own copy of the app, pointed at the demo's port, never at the real page."""

    def setUp(self):
        from margin import demo
        self.demo = demo
        self.tmp = Path(tempfile.mkdtemp(prefix="margin-demo-app-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_builds_its_own_copy_once_and_opens_it_on_the_demo_port(self):
        with mock.patch.object(self.demo, "APP_DIR", self.tmp), \
                mock.patch("margin.macapp.stale", return_value=True), \
                mock.patch("margin.macapp.can_build", return_value=True), \
                mock.patch("margin.macapp.build") as build, \
                mock.patch("margin.demo.subprocess.Popen") as popen, \
                redirect_stdout(io.StringIO()):
            self.demo.open_app(8767)
        app, url, key_file, bundle_id = build.call_args[0]
        self.assertEqual(bundle_id, macapp.DEMO_BUNDLE_ID)
        self.assertTrue(url.startswith(f"http://{self.demo.HOST}:"))
        self.assertEqual(build.call_args[1]["fix"], "margin demo --app")
        self.assertTrue(build.call_args[1]["demo"])
        self.assertEqual(Path(key_file).read_text(), self.demo.KEY)
        self.assertEqual(popen.call_args[0][0][1:], ["--port", "8767"])

    def test_without_swiftc_it_says_so(self):
        with mock.patch.object(self.demo, "APP_DIR", self.tmp), \
                mock.patch("margin.macapp.stale", return_value=True), \
                mock.patch("margin.macapp.can_build", return_value=False), \
                mock.patch("margin.demo.subprocess.Popen") as popen:
            with self.assertRaises(SystemExit):
                self.demo.open_app(8767)
            popen.assert_not_called()


class SwiftSource(unittest.TestCase):
    """What the window promises, checked in macapp.swift itself."""

    def test_private_store_loopback_only_and_no_key_in_the_address(self):
        src = macapp.SOURCE.read_text()
        self.assertIn(".nonPersistent()", src)
        self.assertIn("isThisMac(base)", src)
        self.assertIn('HTTPCookiePropertyKey("HttpOnly")', src)
        self.assertIn("sameSiteStrict", src)
        self.assertNotIn("?k=", src)
        self.assertNotIn("isInspectable = true", src)

    def test_menu_bar_shows_only_the_m_and_the_panel_drops_figures_on_close(self):
        src = macapp.SOURCE.read_text()
        self.assertIn("image?.isTemplate = true", src)
        self.assertNotIn("button.title =", src)                  # never a figure in the menu bar itself
        self.assertIn('client.call("api/glance")', src)
        self.assertIn("render(.empty)", src)
        self.assertIn("case 423: self.render(.locked(nil))", src)


if __name__ == "__main__":
    unittest.main()
