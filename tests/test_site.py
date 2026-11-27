"""The project website (docs/): its demo pages hold nothing but the demo's made-up data, and no page loads anything
from anywhere else. Checked on a fresh `margin demo --export`, and on the pages in docs/demo/ when they're there
(they're never committed; the website workflow makes them, then runs these tests before publishing)."""
import tests  # noqa: F401  (temporary data folder first)

import calendar
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from margin import demo, icon, paths, privacy, site

DOCS = paths.PROJECT_DIR / "docs"
# Margin's own code: the demo's invented people, payees and senders, and Margin's wording.
CODE = "\n".join(f.read_text(encoding="utf-8") for f in sorted((paths.PROJECT_DIR / "margin").glob("*.py"))).lower()
CODE_WORDS = set(re.findall(r"[a-z]+", CODE))
CODE_WORDS |= {w.lower() for names in (calendar.month_name, calendar.month_abbr, calendar.day_name, calendar.day_abbr)
               for name in names for w in re.findall(r"[A-Za-z]+", name)}
ENDING = re.compile(r"(?:••|\*\*|\b[Xx]{2,}|ending (?:in|with) |a/c )\s?(\d{3,})")
EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
LONG_NUMBER = re.compile(r"\d{9,}")


def _json_after(html: str, marker: str):
    i = html.index(marker) + len(marker)
    return json.JSONDecoder().raw_decode(html, i)[0]


def parts(html: str) -> dict:
    """What the page shows: its data, the stand-in server's answers, and how it runs."""
    return {"data": _json_after(html, "\nlet D = "), "state": _json_after(html, "const STATE="),
            "live": _json_after(html, "const LIVE = ")}


def strings(x):
    """Every string value in a JSON value (keys are Margin's code)."""
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from strings(v)


class DemoPages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="margin-site-test-"))
        if site.export(cls.tmp, quiet=True):
            raise AssertionError("margin demo --export failed")
        cls.pages = {f"fresh {p.name}": p.read_text(encoding="utf-8") for p in sorted(cls.tmp.glob("*.html"))}
        cls.pages.update({f"docs/demo/{p.name}": p.read_text(encoding="utf-8")
                          for p in sorted((DOCS / "demo").glob("*.html"))})   # made here or by the workflow

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_both_people_are_exported(self):
        self.assertEqual(sorted(p.name for p in self.tmp.glob("*.html")), sorted(site.PAGES.values()))

    def test_every_word_shown_comes_from_margins_code(self):
        """Names, payees, senders and messages are the demo's inventions: a word from anywhere else (a real
        person's or shop's name) would mean the page wasn't made from the demo."""
        for name, html in self.pages.items():
            p = parts(html)
            for text in strings([p["data"], p["state"]]):
                odd = {w for w in re.findall(r"[A-Za-z]+", text) if len(w) > 1 and w.lower() not in CODE_WORDS}
                with self.subTest(page=name, text=text[:60]):
                    self.assertFalse(odd)
                    self.assertTrue(all(privacy._obviously_fake(n) for n in LONG_NUMBER.findall(text)))

    def test_no_personal_patterns(self):
        """The leak guard's general patterns (PAN, phone numbers, personal emails, home folders, keys) find nothing;
        the owner's own details are checked by the git hooks on their Mac."""
        for name, html in self.pages.items():
            with self.subTest(page=name):
                self.assertEqual(privacy.scan_text(name, html, None), [])

    def test_only_made_up_account_endings_and_addresses(self):
        for name, html in self.pages.items():
            with self.subTest(page=name):
                self.assertLessEqual(set(ENDING.findall(html)), privacy.FAKE_ENDINGS)
                for domain in EMAIL.findall(html):
                    self.assertTrue(domain.endswith(".example") or domain.lower() in CODE, domain)

    def test_it_is_the_demo_and_says_so(self):
        for name, html in self.pages.items():
            p = parts(html)
            with self.subTest(page=name):
                self.assertEqual(p["live"], {"key": demo.KEY, "lock": 0})
                self.assertIn("<title>Margin demo: ", html)
                self.assertIn('id="demo-note"', html)
                self.assertIn('<meta name="robots" content="noindex">', html)
                self.assertEqual(p["state"]["ai"]["name"], "AI")              # the stand-in names no AI company
                self.assertIn(p["data"]["money"]["code"], ("USD", "INR"))

    def test_small_enough_for_the_leak_guard_to_read(self):
        for name, html in self.pages.items():
            with self.subTest(page=name):
                self.assertLess(len(html.encode()), privacy.TEXT_LIMIT)


class Export(unittest.TestCase):
    def test_runs_only_on_a_demo_folder(self):
        """The tests' data folder isn't a demo folder: the export refuses it, as it would refuse yours."""
        out = Path(tempfile.mkdtemp(prefix="margin-site-test-")) / "x.html"
        with self.assertRaises(SystemExit):
            site.page("us", str(out))
        self.assertFalse(out.exists())

    def test_a_changed_page_is_noticed(self):
        with self.assertRaises(RuntimeError):
            site._swap("<html>", "<title>Margin</title>", "x")


class Website(unittest.TestCase):
    PAGES = sorted(DOCS.rglob("*.html"))

    def test_loads_nothing_from_anywhere_else(self):
        """No trackers, fonts, scripts or images from other sites: links only."""
        self.assertTrue(self.PAGES)
        for page in self.PAGES:
            html = page.read_text(encoding="utf-8")
            with self.subTest(page=str(page.relative_to(DOCS))):
                self.assertNotRegex(html, r"<script[^>]*\ssrc=")
                self.assertNotRegex(html, r"""<link[^>]*rel=["']?(?:stylesheet|preload|prefetch|preconnect|dns-prefetch)""")
                for href in re.findall(r"""<link[^>]*\shref=["']?([^"'\s>]+)""", html):
                    self.assertTrue(href.startswith("data:"), href)
                self.assertNotRegex(html, r"""\ssrc=["']?(?:https?:)?//""")
                self.assertNotRegex(html, r"""url\(\s*["']?(?:https?:)?//""")
                self.assertNotRegex(html, r"@import|sendBeacon|XMLHttpRequest|WebSocket|EventSource")
                self.assertNotRegex(html, r"""fetch\(\s*["'`](?:https?:)?//""")

    def test_landing_page(self):
        html = (DOCS / "index.html").read_text(encoding="utf-8")
        self.assertIn(icon.PATH, html)                                   # Margin's own mark
        self.assertIn('href="demo/"', html)
        self.assertIn('href="demo/india.html"', html)
        # The GitHub account is read from the site's address (<owner>.github.io), never written in the file.
        self.assertNotRegex(html, r"github\.com/(?!&lt;owner&gt;)[\w-]+/margin")
        self.assertIn('<meta name="repo" content="">', html)              # filled only when published

    def test_demo_pages_are_never_committed(self):
        """Their invented numbers can equal the owner's real ones by chance: the leak guard would rightly refuse
        them, so they're made where they're published (.github/workflows/website.yml)."""
        r = subprocess.run(["git", "-C", str(paths.PROJECT_DIR), "check-ignore", "-q", "docs/demo/index.html"])
        if r.returncode == 128:
            self.skipTest("not a git checkout")
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
