"""Bank texts from an Android phone: the backup files of SMS Backup & Restore (SyncTech), read on this Mac.

Android has nothing like the iPhone's Text Message Forwarding, so the texts come the long way round: the app on
the phone backs them up on a schedule to a cloud drive the person already uses (Google Drive, Dropbox, OneDrive),
that drive's Mac app puts the file on this Mac, and the nightly sync reads it here.

- Where: the folder in `sms_backup_folder` (or a single file), and the statements folder for a backup dropped in by
  hand. Only the files directly in it: `*.xml`, and `*.xml.zip` without a password (Python can't open the app's
  AES-encrypted zips). Other XML (a calls backup, anything else) is passed over.
- When: a file is read again when it changes (full, archive and incremental backups all work); texts already read
  are skipped by the sync's `seen` list.
- Which texts: received SMS from service senders only, the same rule as the Messages reader
  (`sms.is_service_sender`): never texts from people, never sent texts, never MMS, RCS chats or calls. They're
  handed to the sync like any other text (source 'sms'); nothing else from the file is kept.
- Safe reading: the file is streamed (an archive with photos can be hundreds of MB), and a file that declares its
  own XML entities is refused. Emoji that some phones write as two UTF-16 halves (`&#55357;&#56832;`), which XML
  parsers reject, are put back together first.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

from . import paths, sms

STATE = "sms_backup_files"   # db state: {file path: [size, mtime_ns, 1 if texts else 0]} of the files read
HEAD = 1 << 16               # the root element must start within this many bytes
CHUNK = 1 << 20
MOST_TAG = 256 << 20         # one tag (an MMS photo) bigger than this isn't a backup Margin reads
MOST_FILE = 8 << 30          # nor a file (or a zip's XML) bigger than this


class Unreadable(Exception):
    """A backup Margin can't read. The message is fixed text (no contents), safe for the sync's problems."""


class NotABackup(Exception):
    """Some other file: passed over quietly."""


def folder(settings: dict) -> Path | None:
    """The folder (or file) the person named in sms_backup_folder, if any."""
    value = settings.get("sms_backup_folder")
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(os.path.expanduser(value.strip()))


def find(settings: dict) -> tuple[list[Path], list[str]]:
    """(backup files, notes): the candidate files in the statements folder and sms_backup_folder, oldest first.
    PermissionError when macOS doesn't let this process look into the folder (Full Disk Access)."""
    places, notes = [paths.STATEMENTS_DIR], []
    custom = folder(settings)
    if custom is not None:
        if custom.exists():
            places.append(custom)
        else:
            notes.append("the folder in sms_backup_folder isn't on this Mac (is the drive's app running?)")
    found: dict[str, Path] = {}
    for place in places:
        if place.is_file():
            candidates = [place]
        elif place.is_dir():
            candidates = list(place.iterdir())
        else:
            continue
        for f in candidates:
            name = f.name.lower()
            if not name.startswith(".") and name.endswith((".xml", ".xml.zip")) and f.is_file():
                found.setdefault(os.path.realpath(f), f)
    return sorted(found.values(), key=lambda f: (f.stat().st_mtime, f.name)), notes


def signature(f: Path) -> list[int]:
    st = f.stat()
    return [st.st_size, st.st_mtime_ns]


def changed(conn: sqlite3.Connection, files: list[Path]) -> list[tuple[Path, list[int]]]:
    """The files not read yet in their current form."""
    done = _done(conn)
    out = []
    for f in files:
        sig = signature(f)
        if (done.get(str(f)) or [])[:2] != sig:
            out.append((f, sig))
    return out


def mark_read(conn: sqlite3.Connection, f: Path, sig: list[int], files: list[Path], texts: bool = True) -> None:
    """Remember f as read (texts: it was a texts backup, not some other XML); files that are gone are forgotten."""
    keep = {str(x) for x in files}
    done = {k: v for k, v in _done(conn).items() if k in keep}
    done[str(f)] = [*sig, int(texts)]
    conn.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?, ?)", (STATE, json.dumps(done)))


def newest(conn: sqlite3.Connection, files: list[Path]) -> tuple[int, float | None]:
    """(how many texts backups, the newest one's modification time) among the files already read."""
    done = _done(conn)
    times = [f.stat().st_mtime for f in files if (done.get(str(f)) or [0, 0, 0])[2:] == [1]]
    return len(times), max(times) if times else None


def _done(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM state WHERE key=?", (STATE,)).fetchone()
    try:
        return json.loads(row[0]) if row else {}
    except ValueError:
        return {}


# --- reading a file -------------------------------------------------------------------------------------------
_REF = re.compile(rb"&#(?:[xX]([0-9A-Fa-f]{1,8})|([0-9]{1,10}));")


def _num(m: re.Match) -> int:
    return int(m.group(1), 16) if m.group(1) is not None else int(m.group(2))


def _allowed(n: int) -> bool:
    """A character XML 1.0 allows."""
    return n in (0x9, 0xA, 0xD) or 0x20 <= n <= 0xD7FF or 0xE000 <= n <= 0xFFFD or 0x10000 <= n <= 0x10FFFF


def fix_refs(data: bytes) -> bytes:
    """Character references an XML parser refuses: a UTF-16 pair (&#55357;&#56832;) becomes the one character it
    stands for, anything else XML doesn't allow (a lone half, a control character) becomes U+FFFD."""
    if b"&#" not in data:
        return data
    refs = list(_REF.finditer(data))
    out, last, i = [], 0, 0
    while i < len(refs):
        m, n = refs[i], _num(refs[i])
        if _allowed(n):
            i += 1
            continue
        nxt = refs[i + 1] if i + 1 < len(refs) else None
        if 0xD800 <= n <= 0xDBFF and nxt is not None and nxt.start() == m.end() and 0xDC00 <= _num(nxt) <= 0xDFFF:
            out += [data[last:m.start()], b"&#%d;" % (0x10000 + ((n - 0xD800) << 10) + (_num(nxt) - 0xDC00))]
            last, i = nxt.end(), i + 2
            continue
        out += [data[last:m.start()], b"&#65533;"]
        last, i = m.end(), i + 1
    out.append(data[last:])
    return b"".join(out)


def _chunks(head: bytes, f):
    """The file in pieces cut just after a '>', so no character reference (or pair of them) is ever split."""
    tail, total = head, len(head)
    while True:
        block = f.read(CHUNK)
        total += len(block)
        if total > MOST_FILE:
            raise Unreadable("is bigger than Margin reads (8 GB)")
        data = tail + block
        if not block:
            if data:
                yield fix_refs(data)
            return
        cut = data.rfind(b">") + 1
        if cut:
            yield fix_refs(data[:cut])
            tail = data[cut:]
        else:
            tail = data
        if len(tail) > MOST_TAG:
            raise Unreadable("has an entry too big to read")


def _check_head(head: bytes) -> None:
    i = head.find(b"<smses")
    if i < 0:
        raise NotABackup
    if b"<!DOCTYPE" in head[:i] or b"<!ENTITY" in head[:i]:
        raise Unreadable("declares its own XML entities, so it isn't a backup Margin reads")


@contextlib.contextmanager
def _open(path: Path):
    """The XML inside: the file itself, or the first .xml in a .zip (refused when it has a password)."""
    if not path.name.lower().endswith(".zip"):
        with open(path, "rb") as f:
            yield f
        return
    try:
        with zipfile.ZipFile(path) as z:
            infos = [i for i in z.infolist() if i.filename.lower().endswith(".xml") and not i.is_dir()]
            if not infos:
                raise NotABackup
            if infos[0].flag_bits & 0x1:
                raise Unreadable("has a password, which Margin can't open: turn off the backup password in the app "
                                 "(the file stays in your own drive)")
            if infos[0].file_size > MOST_FILE:
                raise Unreadable("is bigger than Margin reads (8 GB)")
            with z.open(infos[0]) as f:
                yield f
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as e:
        raise Unreadable("isn't a zip Margin can open") from e


def message(attrs: dict, short_codes: bool | set = True) -> dict | None:
    """One <sms> element's attributes as a text for the sync, or None: received texts from service senders only."""
    if attrs.get("type") != "1":                      # 1 received; 2 sent, 3 draft, 4 outbox, 5 failed, 6 queued
        return None
    sender = (attrs.get("address") or "").strip()
    if not sms.is_service_sender(sender, short_codes):
        return None
    body, date = attrs.get("body") or "", (attrs.get("date") or "").strip()
    if not body.strip() or body == "null" or not date.isdigit() or int(date) <= 0:
        return None
    digest = hashlib.sha256("\x1f".join((sender, date, body)).encode("utf-8", "replace")).hexdigest()[:24]
    return {"id": f"android-{digest}", "sender": sender, "ts_epoch": int(date) / 1000, "text": body}


def read(path: Path, short_codes: bool | set = True):
    """Yield the bank texts in one backup file (see message()). NotABackup for other files, Unreadable for a backup
    Margin can't read; OSError (PermissionError) as it comes."""
    with _open(path) as f:
        head = f.read(HEAD)
        _check_head(head)
        parser = ET.XMLPullParser(events=("start", "end"))
        depth, root = 0, None
        try:
            for chunk in _chunks(head, f):
                parser.feed(chunk)
                for event, el in parser.read_events():
                    if event == "start":
                        depth += 1
                        if depth == 1:
                            if el.tag != "smses":
                                raise NotABackup
                            root = el
                        continue
                    depth -= 1
                    if depth == 1:                 # one <sms> or <mms> done: read it, then let it go
                        if el.tag == "sms":
                            m = message(el.attrib, short_codes)
                            if m:
                                yield m
                        root.clear()
            parser.close()
        except ET.ParseError as e:
            where = re.search(r"line \d+", str(e))
            raise Unreadable(f"stops being valid XML ({where.group(0) if where else 'part way'})") from e
