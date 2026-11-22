"""The project website's demo pages: Margin's own page on the demo's made-up people, as static files.

`margin demo --export` writes docs/demo/index.html (Jordan in Seattle, dollars) and docs/demo/india.html (Kavya in
Kochi, rupees) for the website in docs/ (published by GitHub Pages). Each one is built fresh in a
temporary folder by `margin demo`'s own code, so it never reads your data folder or the demo's cache, and holds
only the demo's invented data. Every tab works and nothing is saved:
- a stand-in for Margin's server, inside the page (STAND_IN), answers with what the demo's server said when the page
  was made, plays the demo's pretend sync and turns down every change ("read-only");
- the page's clock is set to the moment it was made, so "this month" is the demo's month whenever it's opened;
- it loads nothing from anywhere else.
Export again after changing the page (dashboard.py) or the demo (demo.py), then look at docs/ in a browser.
tests/test_site.py checks the pages hold nothing but the demo's made-up data.
The pages are never committed (.gitignore): their invented numbers can equal someone's real ones by chance, which the
leak guard rightly refuses. The website workflow (.github/workflows/website.yml) makes them on GitHub's Mac, which
has no one's data, tests them and publishes docs/.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import paths

OUT = paths.PROJECT_DIR / "docs" / "demo"
PAGES = {"us": "index.html", "india": "india.html"}
WHO = {"us": ("Jordan Lee in Seattle", "Jordan in Seattle, in dollars"),
       "india": ("Kavya Menon in Kochi", "Kavya in Kochi, in rupees")}
READ_ONLY = "This demo is read-only, so nothing changes here. In Margin on your Mac, it would."


def export(out: str | Path | None = None, quiet: bool = False) -> int:
    """`margin demo --export`: both demo people, each built in a fresh temporary folder by a process of its own (the
    data folder is chosen once, when Margin's code loads)."""
    out = Path(out) if out else OUT
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o755)
    with tempfile.TemporaryDirectory(prefix="margin-site-") as tmp:
        for persona, name in PAGES.items():
            folder = Path(tmp) / persona
            folder.mkdir()
            env = dict(os.environ, MARGIN_DEMO_DATA=tmp, MARGIN_DATA=str(folder), PYTHONPATH=str(paths.PROJECT_DIR))
            r = subprocess.run([sys.executable, "-c", "import sys; from margin import site; site.page(*sys.argv[1:])",
                                persona, str(out / name)], env=env, stdout=subprocess.DEVNULL if quiet else None)
            if r.returncode:
                print(f"Couldn't export the {persona} demo.", file=sys.stderr)
                return 1
            if not quiet:
                print(f"Wrote {out / name} ({WHO[persona][1]}).")
    return 0


def page(persona: str, file: str) -> None:
    """One demo person's page, written to `file`. Runs only on a demo folder (demo._guard)."""
    from . import demo
    demo._guard()
    print(demo.build(persona), flush=True)         # first: the server's code writes default settings when it loads
    from . import ai, dashboard, db, networth, server, sync
    conn = db.connect()
    settings = sync.load_settings()
    ai.run(conn, settings)                          # the AI stand-in's sorting, so Sort out shows what it did
    sync.refresh_dashboard(conn)
    conn.commit()
    html = dashboard.build(conn, networth.snapshot(conn), server._status(conn), live_key=demo.KEY, lock_minutes=0)
    state = server._state(conn)
    conn.close()
    state.pop("data")                               # the page has it already (D); the stand-in hands that back
    at = datetime.now().replace(microsecond=0)
    Path(file).write_text(static(html, state, persona, at), encoding="utf-8")
    os.chmod(file, 0o644)                           # a web page (the demo's folder makes files private)


def _swap(html: str, old: str, new: str) -> str:
    if html.count(old) != 1:
        raise RuntimeError(f"The page changed ({old[:40]!r} isn't there once): update margin/site.py to match.")
    return html.replace(old, new)


def static(html: str, state: dict, persona: str, at: datetime) -> str:
    """The live page (built with the demo's key) made to work as a file: the stand-in, the clock, the demo notice."""
    from . import demo, icon
    who, short = WHO[persona]
    other = "india" if persona == "us" else "us"
    to_other = "./" if PAGES[other] == "index.html" else PAGES[other]
    blob = lambda x: json.dumps(x, ensure_ascii=False).replace("</", "<\\/")   # noqa: E731
    stand_in = (STAND_IN.replace("/*STATE*/null", blob(state)).replace("/*PHASES*/null", blob(demo.PHASES))
                .replace("/*SECONDS*/0", json.dumps(demo.SYNC_SECONDS)).replace("/*AT*/", at.isoformat())
                .replace("/*READ_ONLY*/", json.dumps(READ_ONLY)))
    head = (f'<link rel="icon" href="data:image/svg+xml,{quote(icon.svg().decode())}" type="image/svg+xml">\n'
            f'<meta name="description" content="Margin\'s demo: {who}, made-up money. A read-only copy.">\n'
            '<meta name="robots" content="noindex">')
    note = (f'<div class="banner" id="demo-note"><span class="bdot" style="background:var(--accent)"></span>'
            f'<span class="muted" style="flex:1 1 260px"><b>Demo with made-up money:</b> {who}. Nothing you click here '
            f'is saved.</span>'
            f'<a class="link" style="color:var(--accent)" href="{to_other}">{WHO[other][1]}</a>'
            f'<a class="link" style="color:var(--accent)" href="../">About Margin</a></div>\n')
    html = _swap(html, "<title>Margin</title>", f"<title>Margin demo: {short}</title>")
    html = _swap(html, icon.HEAD_TAGS, head)
    html = _swap(html, "<main>\n", "<main>\n" + note)
    return _swap(html, "<script>\nlet D = ", stand_in + "<script>\nlet D = ")


# The stand-in for Margin's server. It runs before the page's own script; `D` is the page's data (its first `let`).
STAND_IN = r"""<script>
/* Margin's demo website (margin/site.py): a stand-in for Margin's server, answering with what the demo's server said
   when this page was made. Nothing is sent anywhere and nothing is saved. The clock is set to that moment, so the
   demo's "today" is the day it was made. */
(()=>{
  const STATE=/*STATE*/null, PHASES=/*PHASES*/null, SECONDS=/*SECONDS*/0, READ_ONLY=/*READ_ONLY*/;
  const Real=Date, shift=new Real('/*AT*/').getTime()-Real.now();
  window.Date=class extends Real{ constructor(...a){ if(a.length) super(...a); else super(Real.now()+shift); }
    static now(){ return Real.now()+shift; } };
  if(new URLSearchParams(location.search).has('preview')) document.documentElement.classList.add('preview');
  const p2=n=>String(n).padStart(2,'0');
  const stamp=d=>`${d.getFullYear()}-${p2(d.getMonth()+1)}-${p2(d.getDate())}T${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
  let started=null, synced=null;
  function progress(){                                  // the demo's pretend sync (demo.py, PHASES)
    if(started===null) return {running:false,progress:null};
    const t=(Date.now()-started)/1000/SECONDS;
    if(t>=1){ started=null; synced=stamp(new Date()); return {running:false,progress:null}; }
    const i=Math.min(Math.floor(t*PHASES.length),PHASES.length-1), [phase,total]=PHASES[i];
    return {running:true,progress:{phase,total,done:Math.min(total,Math.round(total*(t*PHASES.length-i)))}};
  }
  function answer(path, post){
    if(path==='/api/state') return {...STATE, data: synced ? {...D, status:{...D.status, at:synced}} : D};
    if(path==='/api/progress') return progress();
    if(path==='/api/ping') return {ok:true};
    if(path==='/api/sync'&&post){ if(started===null) started=Date.now(); return {ok:true}; }
    return {error:READ_ONLY};
  }
  const realFetch=window.fetch.bind(window);
  window.fetch=async(url,opts={})=>{
    const path=String(url);
    if(!path.startsWith('/api/')) return realFetch(url,opts);
    return new Response(JSON.stringify(answer(path,(opts.method||'GET').toUpperCase()==='POST')),
                        {status:200,headers:{'Content-Type':'application/json'}});
  };
})();
</script>
<style>.preview #demo-note{display:none}</style>
"""
