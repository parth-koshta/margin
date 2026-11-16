"""The Margin app for the Mac: the page in a window of its own instead of a browser tab (see macapp.swift for why).

It's built on this Mac from macapp.swift with swiftc from the Command Line Tools, like the PDF reader and the Touch
ID prompt: no Xcode project and no code from anyone else. A bundle is just folders: Contents/MacOS/Margin (the
program), Contents/Resources/Margin.icns (the icon from icon.py) and Contents/Info.plist, which says where the page
is and where its key is. The bundle is signed ad hoc, like everything compiled on this Mac.

`margin app install` puts it in ~/Applications; `margin demo --app` keeps a separate copy for the demo next to the
demo's data, which can only show the demo.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import urllib.parse
from pathlib import Path

from . import paths

SOURCE = Path(__file__).with_name("macapp.swift")
SWIFTC = "/usr/bin/swiftc"
EXECUTABLE = "Margin"            # macOS shows the app's name from the bundle: "Margin"
BUNDLE_ID = f"{paths.LAUNCHD_LABEL}.app"
DEMO_BUNDLE_ID = f"{paths.LAUNCHD_LABEL}.demo"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/"
              "lsregister")


def can_build() -> bool:
    """Are the Command Line Tools here? (Without them /usr/bin/swiftc only offers to install them.)"""
    if not Path(SWIFTC).exists():
        return False
    return subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True).returncode == 0


def this_mac(url: str) -> bool:
    """Only plain http on this Mac's own names: the app sends its key nowhere else (macapp.swift checks too)."""
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    return u.scheme == "http" and (host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost"))


def info_plist(url: str, key_file: Path, bundle_id: str, fix: str = "margin app install") -> dict:
    return {
        "CFBundleName": "Margin",
        "CFBundleExecutable": EXECUTABLE,
        "CFBundleIdentifier": bundle_id,
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": "Margin",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "CFBundleInfoDictionaryVersion": "6.0",
        "NSPrincipalClass": "NSApplication",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.finance",
        # The page is plain http on this Mac (*.localhost never leaves it); macOS allows that only when asked.
        "NSAppTransportSecurity": {
            "NSAllowsLocalNetworking": True,
            "NSExceptionDomains": {"localhost": {"NSIncludesSubdomains": True,
                                                 "NSExceptionAllowsInsecureHTTPLoads": True}},
        },
        "MarginURL": url,                      # where the page is (only the port can be changed, with --port)
        "MarginKeyFile": str(key_file),        # the key itself is read from here when the app opens, never stored
        "MarginFix": fix,                      # what the app tells you to run when the page doesn't answer
    }


def build(app: Path, url: str, key_file: Path, bundle_id: str = BUNDLE_ID, fix: str = "margin app install") -> Path:
    """Compile and put together the app at `app` (a .app path), replacing what's there. Raises RuntimeError."""
    if not this_mac(url):
        raise RuntimeError(f"the app only opens pages on this Mac, not {url}")
    app = Path(app)
    new = app.with_name(f".{app.name}.new")
    shutil.rmtree(new, ignore_errors=True)
    macos, res = new / "Contents" / "MacOS", new / "Contents" / "Resources"
    macos.mkdir(parents=True)
    res.mkdir()
    try:
        try:
            r = subprocess.run([SWIFTC, "-O", str(SOURCE), "-o", str(macos / EXECUTABLE)],
                               capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError(f"couldn't run swiftc: {e}") from e
        if r.returncode != 0:
            raise RuntimeError(f"swiftc couldn't build the app: {r.stderr.strip()[:400]}")
        from . import icon
        icon.write_icns(str(res / "Margin.icns"))
        with open(new / "Contents" / "Info.plist", "wb") as f:
            plistlib.dump(info_plist(url, key_file, bundle_id, fix), f)
        (new / "Contents" / "PkgInfo").write_text("APPL????")
        r = subprocess.run(["codesign", "--force", "--sign", "-", str(new)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"couldn't sign the app: {r.stderr.strip()[:300]}")
        if app.is_symlink() or app.is_file():
            app.unlink()
        elif app.exists():
            shutil.rmtree(app)
        new.rename(app)
    finally:
        shutil.rmtree(new, ignore_errors=True)
    os.utime(app)
    subprocess.run([LSREGISTER, "-f", str(app)], capture_output=True)    # Finder, Spotlight and the Dock look again
    return app


def stale(app: Path) -> bool:
    """Missing, or older than macapp.swift (then it's built again)."""
    exe = Path(app) / "Contents" / "MacOS" / EXECUTABLE
    return not exe.exists() or exe.stat().st_mtime < SOURCE.stat().st_mtime


def executable(app: Path) -> Path:
    return Path(app) / "Contents" / "MacOS" / EXECUTABLE


def forget(bundle_id: str = BUNDLE_ID) -> None:
    """Remove what the app saved: just its window's size and place."""
    subprocess.run(["defaults", "delete", bundle_id], capture_output=True)
