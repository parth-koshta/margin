"""Text out of PDF statements (CDSL CAS, INDmoney, NPS), password-protected ones included.

macOS's own PDFKit does the reading, through the small Swift program next to this file. It's compiled once into
the data folder (the Command Line Tools include the compiler) and rebuilt only when the source changes. The PDF
password is the PAN, kept in the Keychain and handed over on stdin, never on the command line.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from . import keychain, paths

SOURCE = Path(__file__).with_name("pdftext.swift")
PAN_ACCOUNT = "pan"      # opens CDSL CAS and INDmoney statements
PRAN_ACCOUNT = "pran"    # opens the NPS statement


class WrongPassword(Exception):
    pass


def _binary() -> Path:
    out = paths.DATA_DIR / "bin" / "pdftext"
    if not out.exists() or out.stat().st_mtime < SOURCE.stat().st_mtime:
        out.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["/usr/bin/swiftc", "-O", str(SOURCE), "-o", str(out)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"couldn't build the PDF reader: {r.stderr.strip()[:300]}")
    return out


def secret(account: str) -> str | None:
    return keychain.get_secret(account)


def text(data: bytes, password: str | None = None, layout: bool = False) -> str:
    """The PDF's text, pages separated by form feeds. layout=True keeps each table row on one line (built from
    the characters' positions). Raises WrongPassword if it's locked and the password fails."""
    paths.TMP_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=paths.TMP_DIR)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        r = subprocess.run([str(_binary()), *(["--layout"] if layout else []), tmp], input=(password or "").encode(),
                           capture_output=True, timeout=180)
    finally:
        os.unlink(tmp)
    if r.returncode == 2:
        raise WrongPassword("the PDF's password didn't work")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip() or "couldn't read the PDF")
    return r.stdout.decode("utf-8", errors="replace")
