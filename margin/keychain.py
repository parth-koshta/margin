"""Secrets live in the macOS login Keychain, never in files."""
from __future__ import annotations

import subprocess

SERVICE = "margin"


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_secret(account: str, value: str) -> None:
    # Fed through `security -i` on stdin so the secret never appears in the process list.
    cmd = f"add-generic-password -U -a {_quote(account)} -s {_quote(SERVICE)} -w {_quote(value)}\n"
    r = subprocess.run(["/usr/bin/security", "-i"], input=cmd, text=True, capture_output=True)
    if r.returncode != 0 or "error" in (r.stderr or "").lower():
        raise RuntimeError(f"Keychain write failed for {account}: {r.stderr.strip()}")


def get_secret(account: str) -> str | None:
    r = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", account, "-s", SERVICE, "-w"],
        text=True, capture_output=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def delete_secret(account: str) -> None:
    subprocess.run(["/usr/bin/security", "delete-generic-password", "-a", account, "-s", SERVICE],
                   capture_output=True)
