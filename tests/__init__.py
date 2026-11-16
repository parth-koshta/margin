"""Tests never touch your real data folder or your real Messages.

margin.paths reads MARGIN_DATA once, when it's first imported, and some modules read settings.json / rules.json from
that folder at import time. So this points it at a fresh, empty temporary folder before anything imports margin.
Every test module starts with `import tests`, which runs this first both for
`python -m unittest discover -s tests -t .` and for `-s tests` (the project folder is on sys.path either way).

Bank texts live in the Messages database, outside the data folder, so that path is pointed at a file that doesn't
exist in the temporary folder. No test can open the real one, and every Mac sees the same thing: GitHub's test Macs
can read their Messages database, most people's Python can't. A test that needs texts fakes them.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

if "margin.paths" in sys.modules:   # too late: margin already chose its data folder
    raise RuntimeError("tests must be imported before margin, so they can't reach the real data folder")

DATA_DIR = tempfile.mkdtemp(prefix="margin-tests-")
os.environ["MARGIN_DATA"] = DATA_DIR
atexit.register(shutil.rmtree, DATA_DIR, ignore_errors=True)

from margin import paths  # noqa: E402  (only now: MARGIN_DATA is set)

paths.MESSAGES_DB = Path(DATA_DIR) / "no-messages" / "chat.db"
