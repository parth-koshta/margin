"""The Margin page, built on request by the local server (server.py). Nothing is written to disk: a saved copy
would sit outside the Touch ID lock."""
from __future__ import annotations

import json
import sqlite3

from . import icon, money, parse, paths
from .categorize import NON_FLOW


def _settings() -> dict:
    try:
        return json.loads(paths.SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def data(conn: sqlite3.Connection, nw: dict, status: dict) -> dict:
    txns = []
    for r in conn.execute("""SELECT id, ts, amount, direction, category, counterparty, vpa, payee_was, account, source,
                                    raw FROM transactions WHERE duplicate_of IS NULL ORDER BY ts DESC"""):
        t = {"id": r["id"], "d": r["ts"][:10], "a": round(r["amount"], 2), "dir": r["direction"], "c": r["category"],
             "p": r["counterparty"] or r["vpa"] or "", "acct": r["account"] or "", "src": r["source"]}
        if r["payee_was"] is not None:           # a payee you named (payees.py): what its alert called it
            t["w"] = r["payee_was"] or r["vpa"] or ""
        orig = money.original(r["raw"])          # paid in another currency: show what it was
        if orig:
            t["fx"] = list(orig)
        txns.append(t)
    history = [dict(r) for r in conn.execute("SELECT date, net FROM networth_history ORDER BY date")]
    ignored = set(_settings().get("ignored_senders", []))
    review = [dict(r) for r in conn.execute(
        "SELECT ts, source, sender, reason, snippet FROM review ORDER BY ts DESC LIMIT 400")
        if parse.sender_key(r["sender"]) not in ignored][:200]
    from .tidy import SPEND
    from . import invest
    try:
        inv = invest.snapshot(conn, _settings())
    except Exception as e:   # noqa: BLE001  (the rest of the page still works)
        inv = {"error": str(e)}
    return {"txns": txns, "history": history, "nw": nw, "status": status, "review": review, "invest": inv,
            "nonflow": sorted(NON_FLOW), "spend_cats": [c for c in SPEND if c != "Other spending"],
            "projects": list(_settings().get("project_categories", [])), "money": money.info(money.home(_settings()))}


def build(conn: sqlite3.Connection, nw: dict, status: dict, live_key: str | None = None,
          lock_minutes: float = 0) -> str:
    """The page. With live_key (served by the Margin app) it also gets the Sort out tab and Sync now, and locks
    itself after lock_minutes away (see lock.py)."""
    blob = json.dumps(data(conn, nw, status), ensure_ascii=False).replace("</", "<\\/")
    live = json.dumps({"key": live_key, "lock": lock_minutes} if live_key else None)
    mark = icon.svg().decode().replace("<svg ", '<svg class="mark" aria-hidden="true" ', 1)
    return (TEMPLATE.replace("<!--ICON-->", icon.HEAD_TAGS).replace("<!--MARK-->", mark)
            .replace("/*DATA*/null", blob).replace("/*LIVE*/null", live))


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Margin</title>
<!--ICON-->
<style>
/* Design tokens. Light by default, dark with the system (or data-theme). One accent; green and red only for
   gains and losses; the seven investment colours (validated categorical order) only where the type matters. */
:root{color-scheme:light;
  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;
  --display:-apple-system,BlinkMacSystemFont,"SF Pro Display",system-ui,sans-serif;
  --bg:#f7f7f5;--surface:#ffffff;--line:#e9e9e5;--line2:#f0f0ec;--chip:#f0f0ed;--hover:#ebebe7;
  --text:#121211;--text2:#4f4f4b;--muted:#6c6c67;
  --accent:#2a78d6;--accent-soft:rgba(42,120,214,.10);
  --pos:#167a3d;--neg:#c2392f;--warn:#a96b00;--warn-soft:rgba(237,161,0,.14);--neg-soft:rgba(194,57,47,.10);
  --in:#cfcfc9;--track:#eeeeea;--shadow:0 1px 2px rgba(18,18,17,.06);--pop:0 12px 32px rgba(0,0,0,.14),0 2px 6px rgba(0,0,0,.06);
  --c1:#2a78d6;--c2:#eb6834;--c3:#1baf7a;--c4:#eda100;--c5:#e87ba4;--c6:#008300;--c7:#6250d6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
  --bg:#0c0c0d;--surface:#151517;--line:#242427;--line2:#1d1d20;--chip:#202023;--hover:#232326;
  --text:#f2f2f0;--text2:#b9b9b4;--muted:#8f8f8a;
  --accent:#4a8fe7;--accent-soft:rgba(74,143,231,.14);
  --pos:#3fb56d;--neg:#f0675d;--warn:#e0a422;--warn-soft:rgba(224,164,34,.14);--neg-soft:rgba(240,103,93,.14);
  --in:#3b3b40;--track:#232326;--shadow:0 1px 2px rgba(0,0,0,.4);--pop:0 12px 32px rgba(0,0,0,.5),0 2px 6px rgba(0,0,0,.3);
  --c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;--c5:#d55181;--c6:#008300;--c7:#9085e9}}
:root[data-theme="dark"]{color-scheme:dark;
  --bg:#0c0c0d;--surface:#151517;--line:#242427;--line2:#1d1d20;--chip:#202023;--hover:#232326;
  --text:#f2f2f0;--text2:#b9b9b4;--muted:#8f8f8a;
  --accent:#4a8fe7;--accent-soft:rgba(74,143,231,.14);
  --pos:#3fb56d;--neg:#f0675d;--warn:#e0a422;--warn-soft:rgba(224,164,34,.14);--neg-soft:rgba(240,103,93,.14);
  --in:#3b3b40;--track:#232326;--shadow:0 1px 2px rgba(0,0,0,.4);--pop:0 12px 32px rgba(0,0,0,.5),0 2px 6px rgba(0,0,0,.3);
  --c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;--c5:#d55181;--c6:#008300;--c7:#9085e9}
:root{--out:var(--accent);--grid:var(--line2);--seq:var(--accent);--good:var(--pos);--bad:var(--neg);--tot:var(--accent)}

*{box-sizing:border-box}
[hidden]{display:none!important}
html{scroll-padding-top:84px}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 var(--sans);-webkit-font-smoothing:antialiased}
button,input,select{font:inherit;color:inherit}
svg{display:block;overflow:visible}
svg[role="img"]{width:100%}
svg.i{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;flex:none}
svg text{fill:var(--muted);font:11.5px var(--sans)}
.num,.amt{font-variant-numeric:tabular-nums}
.pos,.up{color:var(--pos)}.neg,.down{color:var(--neg)}.muted,.sub2{color:var(--muted)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--chip);padding:1px 5px;border-radius:5px}

/* Top bar */
.top{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:24px;height:64px;padding:0 max(32px,calc((100% - 1056px)/2));
  background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:saturate(1.6) blur(16px);-webkit-backdrop-filter:saturate(1.6) blur(16px);
  border-bottom:1px solid var(--line2)}
.brand{display:flex;align-items:center;gap:10px;border:0;background:none;padding:0;cursor:pointer;font:650 17px/1 var(--display);letter-spacing:-.02em;color:var(--text)}
.brand .mark{width:28px;height:28px}
.nav{display:flex;gap:2px}
.nav button{display:inline-flex;align-items:center;gap:7px;border:0;background:none;padding:7px 13px;border-radius:999px;color:var(--text2);font-weight:500;cursor:pointer;white-space:nowrap}
.nav button:hover{color:var(--text);background:var(--hover)}
.nav button[aria-selected="true"]{color:var(--text);background:var(--chip)}
.badge{font:600 11px/18px var(--sans);min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:var(--text);color:var(--bg);text-align:center}
.actions{margin-left:auto;display:flex;align-items:center;gap:4px}
.private{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);padding:5px 10px;border:1px solid var(--line);border-radius:999px;margin-right:8px;white-space:nowrap}
.private svg.i{width:13px;height:13px}
.syncstate{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--muted);margin-right:6px;white-space:nowrap}
.syncstate i{width:7px;height:7px;border-radius:50%;background:var(--pos);box-shadow:0 0 0 3px color-mix(in srgb,var(--pos) 18%,transparent)}
.syncstate i.bad{background:var(--warn);box-shadow:0 0 0 3px var(--warn-soft)}
.update{display:flex;align-items:center;font:inherit;font-size:12px;font-weight:500;color:var(--accent);background:var(--accent-soft);padding:5px 10px;border:0;border-radius:999px;margin-right:6px;white-space:nowrap;cursor:pointer}
.syncbar{display:inline-flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);margin-right:6px;white-space:nowrap}
.syncbar .track{width:96px;height:4px;border-radius:2px;background:var(--track);overflow:hidden}
.syncbar .fill{display:block;height:100%;background:var(--accent);width:0;transition:width .3s}
.iconbtn{width:36px;height:36px;border-radius:10px;border:0;background:none;color:var(--text2);display:grid;place-items:center;cursor:pointer;flex:none}
.iconbtn:hover{background:var(--hover);color:var(--text)}
.iconbtn:disabled{opacity:.35;cursor:default;background:none}
#syncbtn:disabled{opacity:1}#syncbtn:disabled svg{animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

main{max-width:1120px;margin:0 auto;padding:40px 32px 96px}
.problems{list-style:none;margin:0 0 20px;padding:12px 16px;border-radius:12px;background:var(--warn-soft);font-size:13px}
.problems li{margin:2px 0}
.banner{display:flex;align-items:center;gap:8px 12px;flex-wrap:wrap;margin:0 0 22px;padding:12px 16px;border-radius:12px;background:var(--surface);border:1px solid var(--line);font-size:13.5px}
.banner .bdot{width:8px;height:8px;border-radius:50%;background:var(--warn);flex:none}
.banner.bad .bdot{background:var(--neg)}
.banner .muted{flex:1}

/* Type */
.eyebrow{font-size:13px;font-weight:500;color:var(--muted);margin-bottom:6px}
.hero{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:18px}
.big{font:600 58px/1.02 var(--display);letter-spacing:-.045em;font-variant-numeric:tabular-nums}
.big .cur{font-weight:400;color:var(--muted);letter-spacing:-.02em}
.delta{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;margin-top:12px;font-weight:450;color:var(--muted)}
.delta .pos,.delta .neg{font-weight:550}
.arrow{font-size:10px;margin-right:3px;vertical-align:1px}
h1{font:650 30px/1.15 var(--display);letter-spacing:-.03em;margin:0}
.pagehead{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:24px;flex-wrap:wrap}
.pagetools{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.sub{color:var(--muted);margin:8px 0 0}
.monthpick{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.title-select{font:650 30px/1.15 var(--display);letter-spacing:-.03em;color:var(--text);border:0;background-color:transparent;padding:0 30px 0 0;margin:0 0 0 -4px;
  -webkit-appearance:none;appearance:none;cursor:pointer;border-radius:8px;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 16 16'%3E%3Cpath d='M4 6l4 4 4-4' fill='none' stroke='%238f8f8a' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 6px center;background-size:16px}
.title-select:hover{background-color:var(--hover)}
.mrange{color:var(--muted);font-size:15px}

/* Controls */
.seg{display:inline-flex;gap:2px;padding:3px;border-radius:10px;background:var(--chip);flex:none}
.seg button{border:0;background:none;color:var(--text2);font-weight:500;font-size:13px;line-height:1;padding:7px 12px;border-radius:7px;cursor:pointer;white-space:nowrap}
.seg button[aria-pressed="true"]{background:var(--surface);color:var(--text);box-shadow:var(--shadow)}
.btn{height:34px;padding:0 14px;border-radius:10px;border:1px solid var(--line);background:var(--surface);color:var(--text);font-weight:500;font-size:13px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.btn:hover{border-color:var(--muted)}
.btn.primary{background:var(--text);border-color:var(--text);color:var(--bg)}
.btn.ghost{border-color:transparent;background:var(--chip)}
.btn:disabled{opacity:.5;cursor:default}
.link,.linkbtn{display:inline-flex;align-items:center;gap:3px;border:0;background:none;padding:0;color:var(--text2);font-size:13px;font-weight:500;cursor:pointer;text-decoration:none}
.link:hover,.linkbtn:hover{color:var(--text)}
.link svg.i{width:14px;height:14px}
.linkbtn{color:var(--accent)}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:6px}
.search{flex:1;display:flex;align-items:center;gap:8px;height:38px;padding:0 12px;border-radius:10px;background:var(--chip);color:var(--muted);min-width:200px}
.search input{flex:1;min-width:0;border:0;background:none;outline:none;height:100%}
.search input::-webkit-search-cancel-button{opacity:.5}
select.pill,.filters select{height:38px;border:1px solid var(--line);background-color:var(--surface);border-radius:10px;padding:0 32px 0 12px;font-size:13px;font-weight:500;color:var(--text2);
  -webkit-appearance:none;appearance:none;cursor:pointer;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath d='M3 4.5 6 7.5 9 4.5' fill='none' stroke='%238f8f8a' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 11px center;background-size:12px}
select.pill:hover,.filters select:hover{border-color:var(--muted)}
.fchips{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;flex-wrap:nowrap}
.fchips::-webkit-scrollbar{display:none}
.fchips button{border:1px solid var(--line);background:none;color:var(--text2);font-weight:500;font-size:13px;line-height:1;padding:8px 13px;border-radius:999px;white-space:nowrap;cursor:pointer}
.fchips button:hover{color:var(--text)}
.fchips button[aria-pressed="true"]{background:var(--text);border-color:var(--text);color:var(--bg)}
.focus{display:inline-flex;align-items:center;gap:4px;height:38px;padding:0 4px 0 14px;border-radius:999px;background:var(--accent-soft);color:var(--accent);font-size:13px;font-weight:550;white-space:nowrap}
.focus button{border:0;background:none;color:inherit;cursor:pointer;font-size:17px;line-height:1;padding:4px 9px;border-radius:999px}
.focus button:hover{background:var(--accent-soft)}

/* Surfaces */
.panel{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:22px 24px;min-width:0}
.ph{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.ph h3{margin:0;font:600 15px/1.3 var(--sans);letter-spacing:-.01em;display:flex;align-items:center;gap:8px}
.ph-meta{font-size:13px;color:var(--muted)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}
.cols.c75{grid-template-columns:7fr 5fr}
.cols:first-child{margin-top:0}
.stack{display:flex;flex-direction:column;gap:16px;min-width:0}
.empty{color:var(--muted);padding:10px 0}
.hint{font-size:12.5px;color:var(--muted);margin:12px 0 0}
.note{margin-top:14px;font-size:13px;color:var(--muted);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.chartnote{font-size:12.5px;color:var(--muted);margin:6px 0 0}
details>summary{cursor:pointer;font-weight:600;font-size:14px;list-style:none;display:flex;align-items:center;gap:8px}
details>summary::-webkit-details-marker{display:none}
details>summary:before{content:"";width:6px;height:6px;border-right:1.6px solid var(--muted);border-bottom:1.6px solid var(--muted);transform:rotate(-45deg);transition:transform .15s;margin-right:2px}
details[open]>summary:before{transform:rotate(45deg)}
details[open]>summary{margin-bottom:12px}

/* Key figures: hairlines, no boxes */
.stats{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin:28px 0}
.stat{padding:18px 20px 20px;min-width:0}
.stat:first-child{padding-left:0}
.stat+.stat{border-left:1px solid var(--line)}
.stat .k{font-size:13px;color:var(--muted)}
.stat .v{font:600 25px/1.2 var(--display);letter-spacing:-.03em;margin-top:5px;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stat .s{font-size:12.5px;color:var(--muted);margin-top:3px}
.stat .s .pos,.stat .s .neg{font-weight:550}

/* Charts */
.chart{position:relative}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12.5px;color:var(--text2)}
.legend i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px;vertical-align:0}
#tip{position:fixed;pointer-events:none;background:var(--text);color:var(--bg);padding:7px 10px;border-radius:9px;font-size:12px;line-height:1.45;opacity:0;transition:opacity .08s;z-index:40;white-space:nowrap;box-shadow:var(--pop)}
#tip .t2{opacity:.65}

/* Ranked bars (categories, income) and people (payees) */
.bars{display:flex;flex-direction:column}
.barrow{display:grid;grid-template-columns:minmax(96px,150px) 1fr minmax(84px,auto);gap:14px;align-items:center;padding:7px 0}
.barrow .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.barrow .track{height:6px;border-radius:3px;background:var(--track);overflow:hidden}
.barrow .fill{display:block;height:100%;border-radius:3px;background:var(--accent)}
.barrow .amt{text-align:right;color:var(--text)}
.barrow.pick{cursor:pointer;margin:0 -8px;padding:7px 8px;border-radius:9px}
.barrow.pick:hover,.barrow.on{background:var(--hover)}
.barrow.on .name{font-weight:600}
.barrow.person{grid-template-columns:auto 1fr auto;padding:8px}
.barrow.person:not(:last-child){border-bottom:1px solid var(--line2)}
.av{width:36px;height:36px;flex:none;border-radius:50%;display:grid;place-items:center;font:600 12.5px/1 var(--sans);background:var(--chip);color:var(--text2);letter-spacing:.02em}
.who{min-width:0}
.who b{display:block;font-weight:550;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.who span{font-size:12.5px;color:var(--muted)}
/* a payee's name in Transactions: click to give it your own (payees.py) */
.nmbtn{display:block;max-width:100%;border:0;background:none;color:inherit;font:inherit;font-weight:550;padding:0;cursor:text;text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nmbtn:hover{text-decoration:underline dotted var(--muted);text-underline-offset:3px}
.nmedit{display:block;height:26px;width:min(260px,100%);border:1px solid var(--accent);border-radius:7px;background:var(--surface);color:var(--text);padding:0 7px;font:inherit;font-weight:550;margin:-2px 0 1px}
.nmedit:focus{outline:none}

/* Allocation */
.stackbar{display:flex;gap:2px;height:10px;margin:2px 0 12px}
.stackbar i{display:block;height:100%;border-radius:3px;min-width:3px}
.lrow{display:grid;grid-template-columns:auto 1fr auto auto;gap:12px;align-items:center;padding:9px 0;border-top:1px solid var(--line2)}
.lrow .dot{width:8px;height:8px;border-radius:50%}
.lrow .pct{color:var(--muted);font-size:13px;min-width:40px;text-align:right}
.lrow .val{min-width:96px;text-align:right;font-variant-numeric:tabular-nums}
.lrow small{display:block;font-size:12px;color:var(--muted)}
.lrow.acct{grid-template-columns:1fr auto}

/* Insight (the AI's take) */
.spark{width:28px;height:28px;border-radius:8px;display:grid;place-items:center;background:var(--accent-soft);color:var(--accent);flex:none}
.spark svg.i{width:15px;height:15px}
.rv-head{font:600 21px/1.3 var(--display);letter-spacing:-.02em;margin:0 0 4px}
.rv-meta{font-size:12.5px;color:var(--muted);margin-bottom:12px}
.bul{margin:0;padding:0;list-style:none}
.bul li{position:relative;padding:5px 0 5px 18px;color:var(--text2)}
.bul li:before{content:"";position:absolute;left:2px;top:13px;width:5px;height:5px;border-radius:50%;background:var(--muted)}
.watch{margin-top:10px;padding:10px 12px;border-radius:10px;background:var(--warn-soft);font-size:13px}
.watch b{display:block;font-weight:600}
.take{display:grid;grid-template-columns:1fr 1fr;gap:4px 24px}
.fine{font-size:12px;color:var(--muted);margin-top:12px}

/* Investments */
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0 16px}
.tile{padding:16px 18px;border-radius:16px;background:var(--surface);border:1px solid var(--line);min-width:0}
.tile .n{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .n i{width:8px;height:8px;border-radius:50%;flex:none}
.tile .v{font:600 21px/1.2 var(--display);letter-spacing:-.025em;margin-top:10px;font-variant-numeric:tabular-nums}
.tile .s{font-size:12.5px;color:var(--muted);margin-top:2px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.tile.alt{background:none;border-style:dashed}
.hold{width:100%;border-collapse:collapse}
.hold th{font-size:12px;font-weight:500;color:var(--muted);text-align:right;padding:0 0 10px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
.hold th:first-child,.hold td:first-child{text-align:left;padding-left:0}
.hold td{text-align:right;padding:13px 0 13px 12px;border-bottom:1px solid var(--line2);vertical-align:middle;font-variant-numeric:tabular-nums}
.hold td+td{white-space:nowrap}
.hold tr:last-child td{border-bottom:0}
.hold td small{display:block;font-size:12.5px;color:var(--muted)}
.hold td small.up{color:var(--pos)}.hold td small.down{color:var(--neg)}
.hold th.al,.hold td.al{text-align:right}
.hname{display:flex;gap:12px;align-items:center;min-width:180px}
.hname i{width:8px;height:8px;border-radius:50%;flex:none}
.hname b{font-weight:550;display:block}
.hname small{white-space:normal}
.wcell{display:inline-flex;align-items:center;gap:8px}
.wbar{width:52px;height:4px;border-radius:2px;background:var(--track);overflow:hidden}
.wbar i{display:block;height:100%;background:var(--text2)}
.alert{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:500;padding:5px 10px;border-radius:999px;background:var(--chip);color:var(--text2);cursor:pointer;border:0;white-space:nowrap}
.alert svg.i{width:13px;height:13px}
.alert.add{background:none;color:var(--muted)}
.alert.add:hover,.alert.set:hover{background:var(--hover);color:var(--text)}
.alert.hit{background:var(--neg-soft);color:var(--neg);font-weight:600}
.stopedit{display:inline-flex;gap:4px;align-items:center}
.stopedit input{width:100px;height:32px;border:1px solid var(--line);border-radius:8px;background:var(--surface);padding:0 8px}
.stopedit .btn{height:32px;padding:0 10px}
.kv{display:flex;justify-content:space-between;gap:16px;padding:10px 0;border-top:1px solid var(--line2);font-size:13px}
.kv:first-child{border-top:0}
.kv>span:first-child{color:var(--text2);white-space:nowrap}
.kv>span:last-child{text-align:right;color:var(--muted)}
.bench{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:11px 0;border-top:1px solid var(--line2)}
.bench:first-child{border-top:0}
.bench .r{display:flex;gap:16px;font-variant-numeric:tabular-nums}
.bench .r span{min-width:64px;text-align:right}
.sources{margin-top:16px}

/* Transactions */
.txsum{font-size:13px;color:var(--muted);margin:10px 0 0}
.day{font-size:13px;font-weight:600;color:var(--text2);padding:20px 0 4px;display:flex;justify-content:space-between;gap:12px}
.day span{font-weight:450;color:var(--muted);font-variant-numeric:tabular-nums}
.tx{display:flex;align-items:center;gap:14px;padding:10px 0;border-bottom:1px solid var(--line2)}
.tx .who{flex:1}
.cat{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:500;line-height:1;padding:6px 10px;border-radius:999px;background:var(--chip);color:var(--text2);border:1px solid transparent;white-space:nowrap}
button.cat{cursor:pointer}
button.cat:hover,button.cat[aria-expanded="true"]{background:var(--hover);color:var(--text)}
.cat svg{width:10px;height:10px;opacity:.6}
.cat.unset{background:var(--warn-soft);color:var(--warn)}
.amt{min-width:96px;text-align:right;font-weight:550;white-space:nowrap}
.amt.in,.in-amt{color:var(--pos)}
.pill{display:inline-block;padding:3px 9px;border-radius:999px;background:var(--chip);color:var(--text2);font-size:12px;white-space:nowrap}
table.plain{width:100%;border-collapse:collapse;font-size:13px}
table.plain th{text-align:left;color:var(--muted);font-weight:500;font-size:12px;padding:0 10px 8px 0;border-bottom:1px solid var(--line)}
table.plain td{padding:10px 10px 10px 0;border-bottom:1px solid var(--line2);vertical-align:middle}
table.plain td.num{text-align:right}

/* Sort out */
.progress{height:8px;border-radius:4px;background:var(--track);overflow:hidden;margin-top:16px;max-width:520px}
.progress .fill{display:block;height:100%;border-radius:4px;background:var(--accent);transition:width .4s}
.section-h{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin:34px 0 4px;flex-wrap:wrap}
.section-h h2{margin:0;font:600 17px/1.3 var(--sans);letter-spacing:-.01em}
.section-h>span{font-size:13px;color:var(--muted)}
.lead{color:var(--muted);font-size:13.5px;margin:4px 0 14px;max-width:760px}
.lead b{color:var(--text2)}
.solist{border-top:1px solid var(--line)}
.sorow{display:grid;grid-template-columns:auto 1fr auto;gap:14px;align-items:center;padding:16px 0;border-bottom:1px solid var(--line2)}
.sorow .meta{font-size:13px;color:var(--muted);margin-top:2px}
.sorow .main b{font-weight:600}
.namebtn{border:0;background:none;color:var(--text);font:inherit;font-weight:600;padding:0;cursor:pointer;text-align:left;display:inline-flex;gap:6px;align-items:baseline}
.namebtn .car{color:var(--muted);font-size:10px;transition:transform .12s;display:inline-block}
.namebtn[aria-expanded="true"] .car{transform:rotate(90deg)}
.sorow .right{display:flex;align-items:center;gap:10px;justify-content:flex-end;flex-wrap:wrap}
.sorow .total{font-weight:550;font-variant-numeric:tabular-nums;min-width:80px;text-align:right}
.matchf{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted);margin-top:8px}
.matchf input{height:28px;border:1px solid var(--line);border-radius:8px;background:var(--surface);padding:0 8px;font-size:12.5px;width:200px;max-width:100%}
.matchf input:focus{outline:none;border-color:var(--accent)}
.pays{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:12.5px;color:var(--text2);margin-top:10px;padding:10px 12px;border-radius:10px;background:var(--chip)}
.catbtn{display:inline-flex;align-items:center;justify-content:space-between;gap:10px;min-width:180px;height:36px;padding:0 12px;border:1px solid var(--line);border-radius:10px;background:var(--surface);color:var(--text2);font-size:13px;font-weight:500;cursor:pointer;text-align:left}
.catbtn:hover,.catbtn[aria-expanded="true"]{border-color:var(--accent);color:var(--text)}
.catbtn svg{width:12px;height:12px;flex:none}
.sorow.pay{grid-template-columns:auto 1fr;align-items:start}
.sorow.pay .av{margin-top:2px}
.top2{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.sugs{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.sug{border:1px solid var(--line);background:var(--surface);color:var(--text);font-size:13px;font-weight:500;line-height:1;padding:8px 12px;border-radius:999px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.sug:hover{border-color:var(--muted)}
.sug.best{border-color:color-mix(in srgb,var(--accent) 55%,transparent);background:var(--accent-soft);color:var(--accent)}
.sug.best:hover{border-color:var(--accent)}
.sug svg.i{width:13px;height:13px}
.sug.more{background:none;color:var(--text2)}
.sug:disabled{opacity:.5;cursor:default}
.adv{display:flex;flex-wrap:wrap;align-items:center;gap:4px 14px;margin-top:8px;font-size:12.5px;color:var(--muted)}
.adv .matchf{margin-top:0}
.example{margin-top:8px;padding:9px 12px;border-radius:10px;background:var(--chip);font-size:13px;color:var(--text2)}
.ai-hint{font-size:12.5px;color:var(--accent);margin-top:4px}
.acts{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{display:inline-flex;align-items:center;gap:4px;padding:3px 4px 3px 11px;border-radius:999px;background:var(--chip);font-size:12.5px}
.chip button{border:0;background:none;color:var(--muted);cursor:pointer;font-size:15px;line-height:1;padding:2px 7px;border-radius:999px}
.chip button:hover{color:var(--text);background:var(--hover)}
.status{color:var(--muted);font-size:13.5px;margin:0}
.warn{color:var(--warn)}
.ai-group{border-top:1px solid var(--line2);padding:12px 0}
.ai-group:first-child{border-top:0}
.ai-group summary{font-weight:500}
.ai-t{width:100%;border-collapse:collapse;margin-top:6px;font-size:13px}
.ai-t td{padding:7px 10px 7px 0;border-bottom:1px solid var(--line2);vertical-align:middle}
.ai-t .ex{color:var(--muted)}
.ai-t td.num{text-align:right}
.ai-t tr.undone td:first-child{text-decoration:line-through;color:var(--muted)}

/* Reviews */
.flag{display:grid;grid-template-columns:auto 1fr auto;gap:14px;align-items:center;padding:16px 0;border-bottom:1px solid var(--line2)}
.flag:last-child{border-bottom:0}
.flag:first-child{padding-top:4px}
.ficon{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;background:var(--warn-soft);color:var(--warn)}
.ficon.high{background:var(--neg-soft);color:var(--neg)}
.ficon.low{background:var(--chip);color:var(--muted)}
.flag .t{font-weight:600}
.flag .w{color:var(--text2);font-size:13.5px}
.flag .m{color:var(--muted);font-size:12.5px;margin-top:2px}
.flag.fine{opacity:.55}
.rv-flags{margin-top:18px;border-top:1px solid var(--line);padding-top:6px}
.rv-flags h4{margin:10px 0 0;font-size:13px;font-weight:600;color:var(--text2)}

/* Popovers */
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:var(--text);color:var(--bg);padding:10px 16px;border-radius:12px;font-size:13px;opacity:0;transition:opacity .15s;z-index:50;pointer-events:none;max-width:calc(100% - 32px);display:flex;gap:14px;align-items:center;box-shadow:var(--pop)}
#toast.on{opacity:1;pointer-events:auto}
#toast .linkbtn{color:var(--bg);font-weight:600;text-decoration:underline}
.menu{position:fixed;z-index:45;width:260px;background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--pop);padding:6px;display:flex;flex-direction:column;max-height:360px}
.menu input{height:34px;margin:0 0 6px;border-radius:9px;border:1px solid var(--line);background:var(--bg);padding:0 10px}
.menu input:focus{outline:none;border-color:var(--accent)}
.menu .list{overflow:auto}
.menu .grp{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;padding:8px 8px 4px}
.menu .it{display:flex;align-items:center;justify-content:space-between;width:100%;border:0;background:none;color:var(--text);font-size:13px;padding:7px 9px;border-radius:8px;cursor:pointer;text-align:left}
.menu .it[aria-selected="true"]{background:var(--hover)}
.menu .it .ck{color:var(--accent);font-weight:700}
.menu .none{color:var(--muted);font-size:13px;padding:8px}

/* Phone tab bar */
.tabbar{display:none;position:fixed;left:0;right:0;bottom:0;z-index:30;justify-content:space-around;padding:8px 6px calc(8px + env(safe-area-inset-bottom));
  background:color-mix(in srgb,var(--bg) 85%,transparent);backdrop-filter:saturate(1.6) blur(16px);-webkit-backdrop-filter:saturate(1.6) blur(16px);border-top:1px solid var(--line2)}
.tabbar button{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;border:0;background:none;font-size:10.5px;font-weight:500;color:var(--muted);cursor:pointer;position:relative;padding:2px 0}
.tabbar button svg.i{width:23px;height:23px}
.tabbar button[aria-selected="true"]{color:var(--accent)}
.tabbar .badge{position:absolute;top:-4px;left:calc(50% + 5px);font-size:10px;line-height:16px;height:16px;min-width:16px;padding:0 4px;border-radius:8px}

@media (max-width:900px){
  .private{display:none}
  .tiles{grid-template-columns:repeat(2,1fr)}
}
@media (max-width:760px){
  .top{height:54px;padding:0 16px;gap:12px}
  .nav,.syncstate{display:none}
  .tabbar{display:flex}
  main{padding:20px 16px calc(96px + env(safe-area-inset-bottom))}
  .hero{flex-direction:column;align-items:flex-start;gap:16px}
  .big{font-size:42px}
  h1,.title-select{font-size:26px}
  .pagehead{align-items:flex-start}
  .stats{grid-template-columns:1fr 1fr;margin:22px 0}
  .stat,.stat:first-child{padding:14px 14px 16px 0}
  .stat:nth-child(even){padding-left:14px}
  .stat:nth-child(3){border-left:0}
  .stat:nth-child(n+3){border-top:1px solid var(--line)}
  .stat .v{font-size:20px}
  .cols,.cols.c75,.take{grid-template-columns:1fr}
  .panel{padding:18px;border-radius:16px}
  .tiles{gap:10px}
  .tile .v{font-size:18px}
  .barrow{grid-template-columns:minmax(80px,110px) 1fr auto;gap:10px}
  .barrow.person{grid-template-columns:auto 1fr auto}
  .hold thead,.hold .wide{display:none}
  .hold tr{display:grid;grid-template-columns:1fr auto;column-gap:12px;padding:12px 0;border-bottom:1px solid var(--line2)}
  .hold tr:last-child{border-bottom:0}
  .hold td{border:0;padding:0}
  .hold td:first-child{grid-row:1 / span 2;align-self:center}
  .hold td:nth-child(2) small{display:none}
  .hold td:nth-child(3){font-size:12.5px}
  .hold td:nth-child(3) small{display:inline;margin-left:5px}
  .hname{min-width:0}
  .tx .cat{display:none}
  .search{min-width:100%}
  .sorow{grid-template-columns:auto 1fr}
  .sorow .right{grid-column:1 / -1;justify-content:flex-start}
  .flag{grid-template-columns:auto 1fr}
  .flag .acts{grid-column:2;justify-content:flex-start}
  .kv{flex-direction:column;gap:2px}.kv>span:last-child{text-align:left}
}
</style></head>
<body>
<header class="top">
  <button class="brand" data-goto="home" aria-label="Margin, home"><!--MARK-->Margin</button>
  <nav class="nav" id="tabs" role="tablist">
    <button role="tab" aria-selected="true" data-view="home">Home</button>
    <button role="tab" aria-selected="false" data-view="overview">Spending</button>
    <button role="tab" aria-selected="false" data-view="invest">Investments</button>
    <button role="tab" aria-selected="false" data-view="sortout" class="live-only" hidden>Sort out<span class="badge" id="so-badge" hidden></span></button>
    <button role="tab" aria-selected="false" data-view="reviews" class="live-only" hidden>Reviews<span class="badge" id="rv-badge" hidden></span></button>
  </nav>
  <div class="actions">
    <span class="private" title="Everything Margin reads, stores and shows stays on this Mac"><svg class="i" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>On this Mac</span>
    <span class="syncbar" id="syncbar" hidden><span id="sync-text"></span><span class="track"><span class="fill" id="sync-fill"></span></span></span>
    <span class="syncstate" id="status"></span>
    <button class="update" id="update" hidden></button>
    <button class="iconbtn" id="syncbtn" hidden title="Sync now" aria-label="Sync now"><svg class="i" viewBox="0 0 24 24"><path d="M20 11.5A8 8 0 0 0 5.6 7M4 4v4h4M4 12.5A8 8 0 0 0 18.4 17M20 20v-4h-4"/></svg></button>
    <button class="iconbtn" id="lockbtn" hidden title="Lock Margin now (Touch ID to open again)" aria-label="Lock Margin now"><svg class="i" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg></button>
  </div>
</header>
<main>
<ul class="problems" id="problems" hidden></ul>

<div id="view-home">
  <div class="banner bad" id="home-stops" hidden></div>
  <section class="hero">
    <div><div class="eyebrow" id="home-eyebrow">Net worth</div><div class="big" id="home-big"></div><div class="delta" id="home-delta"></div></div>
    <span class="seg" id="home-range" role="group" aria-label="Range"><button data-r="182" aria-pressed="false">6M</button><button data-r="365" aria-pressed="true">1Y</button><button data-r="730" aria-pressed="false">2Y</button><button data-r="0" aria-pressed="false">All</button></span>
  </section>
  <div class="chart"><svg id="home-nw" height="240" role="img" aria-label="Net worth over time"></svg></div>
  <p class="chartnote" id="home-nw-note" hidden></p>
  <section class="stats" id="home-kpis"></section>
  <section class="cols">
    <div class="panel"><div class="ph"><h3>Where your money is</h3><button class="link" data-goto="invest">All holdings<svg class="i" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></button></div>
      <div class="stackbar" id="home-alloc"></div><div id="home-kinds"></div></div>
    <div class="panel"><div class="ph"><h3 id="home-cat-title">Spending this month</h3><button class="link" data-goto="overview">Details<svg class="i" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></button></div>
      <div class="bars" id="home-cats"></div><p class="note" id="home-cat-nudge" hidden></p></div>
  </section>
  <section class="cols">
    <div class="panel"><div class="ph"><h3 id="nw-title">Accounts</h3><span class="ph-meta" id="nw-meta"></span></div><div id="nwparts"></div></div>
    <div class="panel"><div class="ph"><h3><span class="spark"><svg class="i" viewBox="0 0 24 24"><path d="M12 3.5l1.9 5 5 1.9-5 1.9-1.9 5-1.9-5-5-1.9 5-1.9z"/><path d="M18.5 15.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/></svg></span>The AI’s take</h3><button class="link" data-goto="invest">Investments<svg class="i" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></button></div><div id="home-take"></div></div>
  </section>
</div>

<div id="view-invest" hidden>
  <ul class="problems" id="inv-setup" hidden></ul>
  <section class="hero">
    <div><div class="eyebrow">Investments</div><div class="big" id="inv-big"></div><div class="delta" id="inv-delta"></div></div>
    <span class="pagetools">
      <span class="seg" id="inv-mode" role="group" aria-label="Lines"><button data-m="total" aria-pressed="true">Total</button><button data-m="kinds" aria-pressed="false">By type</button></span>
      <span class="seg" id="inv-range" role="group" aria-label="Range"><button data-r="182" aria-pressed="false">6M</button><button data-r="365" aria-pressed="true">1Y</button><button data-r="0" aria-pressed="false">All</button></span>
    </span>
  </section>
  <div class="legend" id="inv-legend" style="margin-bottom:10px"></div>
  <div class="chart"><svg id="inv-line" height="240" role="img" aria-label="Investments value over time"></svg></div>
  <p class="chartnote" id="inv-line-note"></p>
  <section class="tiles" id="inv-kpis"></section>
  <div class="panel">
    <div class="ph"><h3>Holdings <span class="ph-meta" id="inv-count"></span></h3><span class="fchips" id="inv-filter" role="group" aria-label="Type"></span></div>
    <table class="hold"><thead><tr><th>Name</th><th>Value</th><th>Return</th><th class="wide">Weight</th><th class="wide al">Price alert</th></tr></thead><tbody id="inv-rows"></tbody></table>
    <p class="hint">Price alerts: each night Margin checks the closing price and sends a Mac notification when a holding closes at or below your level. Nothing is ever sold.</p>
  </div>
  <section class="cols c75">
    <div class="panel"><div class="ph"><h3><span class="spark"><svg class="i" viewBox="0 0 24 24"><path d="M12 3.5l1.9 5 5 1.9-5 1.9-1.9 5-1.9-5-5-1.9 5-1.9z"/></svg></span>The AI’s analysis</h3>
      <span class="pagetools"><span class="seg" id="inv-rkind" role="group" aria-label="Analysis"><button data-k="weekly" aria-pressed="true">Weekly</button><button data-k="monthly" aria-pressed="false">Monthly</button></span><button class="btn live-only" id="inv-run" hidden>Analyse now</button></span></div>
      <div id="inv-review"></div></div>
    <div class="panel"><div class="ph"><h3>Markets</h3><span class="ph-meta">1 month · 1 year</span></div><div id="inv-bench"></div></div>
  </section>
  <details class="panel sources"><summary>How these numbers are worked out</summary><div id="inv-sources"></div></details>
</div>

<div id="view-overview" hidden>
  <section class="pagehead">
    <div><div class="eyebrow">Spending</div>
      <div class="monthpick"><select id="month" class="title-select" aria-label="Month"></select><span class="mrange" id="mrange"></span></div></div>
    <span class="pagetools">
      <button class="iconbtn" id="mprev" aria-label="Previous month" title="Previous month"><svg class="i" viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg></button>
      <button class="iconbtn" id="mnext" aria-label="Next month" title="Next month"><svg class="i" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></button>
      <span class="seg" id="mmode" role="group" aria-label="How months are counted" hidden><button data-m="pay" aria-pressed="true" title="Each month starts on the day your salary arrives">Pay cycle</button><button data-m="calendar" aria-pressed="false">Calendar</button></span>
    </span>
  </section>
  <div class="banner" id="flagbar" hidden></div>
  <section class="stats" id="kpis" style="margin-top:0"></section>
  <div class="panel">
    <div class="ph"><h3>Last 12 months</h3><div class="legend"><span><i style="background:var(--in)"></i>Money in</span><span><i style="background:var(--out)"></i>Spent</span></div></div>
    <svg id="monthly" height="220" role="img" aria-label="Money in and spent, month by month"></svg>
  </div>
  <section class="cols c75">
    <div class="panel"><div class="ph"><h3 id="cat-title">Where it went</h3><span class="ph-meta" id="cat-meta"></span></div><div class="bars" id="cats"></div><p class="note" id="cat-nudge" hidden></p></div>
    <div class="stack">
      <div class="panel"><div class="ph"><h3>Where it came from</h3></div><div class="bars" id="incomes"></div></div>
      <div class="panel"><div class="ph"><h3>Top payees</h3></div><div class="bars" id="payees"></div></div>
    </div>
  </section>
  <div class="panel" id="tx-card" style="margin-top:16px">
    <div class="ph"><h3 id="tx-title">Transactions</h3><span class="ph-meta" id="tx-count"></span></div>
    <div class="filters">
      <span class="focus" id="focus" hidden><span id="focus-label"></span><button id="focus-clear" title="Show all transactions" aria-label="Show all transactions">×</button></span>
      <label class="search"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></svg><input id="q" type="search" placeholder="Search payee, account or category" aria-label="Search transactions"></label>
      <select id="catf" aria-label="Category"><option value="">All categories</option></select>
      <select id="dirf" aria-label="Money in or out"><option value="">In and out</option><option value="in">Money in</option><option value="out">Spent</option></select>
    </div>
    <p class="txsum" id="tx-sum" hidden></p>
    <div id="tx"></div>
    <p class="hint" id="tx-hint">Wrong category? In Terminal: <code>margin rule "swiggy" "Food &amp; dining"</code>. It applies to past and future transactions.</p>
  </div>
  <div class="panel" id="rv-card" style="margin-top:16px">
    <div class="ph"><h3>Needs a look (not counted)</h3></div>
    <p class="status">Messages that look like money moving but came from a sender Margin doesn't treat as a bank, or that it couldn't read. To count a sender: <code>margin trust "sender"</code>.</p>
    <table class="plain"><thead><tr><th>Date</th><th>From</th><th>Why</th><th>Text</th></tr></thead><tbody id="review"></tbody></table>
  </div>
</div>

<div id="view-sortout" hidden>
  <section class="pagehead"><div style="flex:1;min-width:260px">
    <div class="eyebrow">Sort out</div><h1><span id="so-pct">–</span> sorted</h1>
    <div class="progress"><span class="fill" id="so-fill"></span></div>
    <p class="sub" id="so-meter-text"></p></div></section>
  <div class="panel" id="ai-card">
    <div class="ph"><h3><span class="spark"><svg class="i" viewBox="0 0 24 24"><path d="M12 3.5l1.9 5 5 1.9-5 1.9-1.9 5-1.9-5-5-1.9 5-1.9z"/></svg></span>Sorted by AI</h3><button class="btn primary" id="ai-run">Ask AI to sort now</button></div>
    <p class="status" id="ai-status"></p>
    <div id="ai-list" style="margin-top:6px"></div>
  </div>
  <div class="section-h"><h2 id="so-p-title">Payees</h2><span>Pick a category once: past and future payments to that payee follow</span></div>
  <div class="filters" style="margin:10px 0 4px">
    <label class="search"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></svg><input id="so-q" type="search" placeholder="Filter payees" aria-label="Filter payees"></label>
    <select id="so-dir" aria-label="Show"><option value="out">Spending</option><option value="in">Income</option><option value="">Both</option></select>
    <select id="so-sort" aria-label="Order"><option value="money">Most money first</option><option value="n">Most payments first</option></select>
  </div>
  <div class="solist" id="so-payees"></div>
  <p class="hint"><b>Transfer</b> and <b>Investments</b> are your own money moving (a card bill, your other accounts, SIPs), so they don't count as spending or income. <span id="so-nameless"></span></p>
  <div id="so-s-card">
    <div class="section-h"><h2 id="so-s-title">Not counted yet</h2><button class="btn" id="so-hideall" hidden>Hide all</button></div>
    <p class="lead">Margin only counts what your <b>bank or card company</b> tells you. These senders aren't on its list of banks, so nothing from them is counted. Most are shops and apps repeating a payment your bank already reported. <b>Hide</b> keeps a sender uncounted and clears it from here; <b>It's my bank</b> is only for a bank or card Margin doesn't know.</p>
    <div class="solist" id="so-senders"></div>
    <p class="hint" id="so-queued"></p>
  </div>
  <div class="panel" id="so-unread-card" style="margin-top:28px" hidden>
    <details><summary id="so-unread-title">Bank alerts Margin couldn't read</summary>
      <p class="status" style="margin-bottom:10px">From your banks, but no amount could be found, so they aren't counted. If these are real payments, ask your coding agent to teach the reader this format.</p>
      <table class="plain"><thead><tr><th>Date</th><th>From</th><th>Text</th></tr></thead><tbody id="so-unread"></tbody></table>
    </details>
  </div>
  <div class="panel" id="so-choices" style="margin-top:16px" hidden>
    <details><summary>Your choices</summary>
      <div class="cols c75" style="margin-top:4px">
        <div><table class="plain"><thead><tr><th>Rule: match</th><th>Category</th><th></th></tr></thead><tbody id="so-rules"></tbody></table></div>
        <div><p class="status" style="margin-bottom:8px">Counted as a bank</p><div class="chips" id="so-trusted"></div>
          <p class="status" style="margin:16px 0 8px">Hidden (not a bank)</p><div class="chips" id="so-ignored"></div></div>
      </div>
    </details>
  </div>
</div>

<div id="view-reviews" hidden>
  <section class="pagehead"><div><div class="eyebrow">Reviews</div><h1 id="rv-h1">Reviews</h1></div><span class="ph-meta" id="rv-ai"></span></section>
  <div class="panel" id="rv-open-card"><div id="rv-open"></div></div>
  <div class="panel" style="margin-top:16px">
    <div class="ph">
      <span class="seg" id="rv-kind" role="group" aria-label="Review type"><button aria-pressed="true" data-k="daily">Daily</button><button aria-pressed="false" data-k="weekly">Weekly</button><button aria-pressed="false" data-k="monthly">Monthly</button></span>
      <span class="pagetools"><select id="rv-pick" class="pill" aria-label="Period"></select><button class="btn" id="rv-run">Review now</button></span>
    </div>
    <div id="rv-body"></div>
  </div>
</div>
</main>

<nav class="tabbar" id="tabbar" aria-label="Pages">
  <button data-view="home" aria-selected="true"><svg class="i" viewBox="0 0 24 24"><path d="M4 10.5l8-6 8 6V19a1 1 0 0 1-1 1h-4.5v-5.5h-5V20H5a1 1 0 0 1-1-1z"/></svg>Home</button>
  <button data-view="overview" aria-selected="false"><svg class="i" viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="13" rx="2.5"/><path d="M3 10.5h18M7 15h4"/></svg>Spending</button>
  <button data-view="invest" aria-selected="false"><svg class="i" viewBox="0 0 24 24"><path d="M4 17l5-5 4 3 7-8"/><path d="M15 7h5v5"/></svg>Invest</button>
  <button data-view="sortout" aria-selected="false" class="live-only" hidden><svg class="i" viewBox="0 0 24 24"><path d="M4 13l2.5-7h11L20 13v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z"/><path d="M4 13h4.5l1 2h5l1-2H20"/></svg>Sort out<span class="badge" id="so-badge2" hidden></span></button>
  <button data-view="reviews" aria-selected="false" class="live-only" hidden><svg class="i" viewBox="0 0 24 24"><path d="M12 3.5l1.9 5 5 1.9-5 1.9-1.9 5-1.9-5-5-1.9 5-1.9z"/><path d="M18.5 15.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/></svg>Reviews<span class="badge" id="rv-badge2" hidden></span></button>
</nav>
<div id="toast" role="status" aria-live="polite"></div>
<div class="menu" id="catmenu" role="dialog" aria-label="Choose a category" hidden>
  <input id="catq" type="search" placeholder="Type to filter" autocomplete="off" aria-label="Filter categories">
  <div class="list" id="catlist" role="listbox"></div>
</div>
<div id="tip"></div>
<script>
let D = /*DATA*/null;
const LIVE = /*LIVE*/null;
const NONFLOW = new Set(D.nonflow);
// Money in the home currency (D.money, from money.py), written its own way: ₹1,23,456 · ₹1.2L / $123,456 · $1.2M
const CUR = D.money||{code:'INR',symbol:'₹',decimals:2,locale:'en-IN',grouping:'indian'}, LOC = CUR.locale;
const fmt = new Intl.NumberFormat(LOC,{style:'currency',currency:CUR.code,maximumFractionDigits:0});
const fmtc = new Intl.NumberFormat(LOC,{style:'currency',currency:CUR.code});      // with the currency's own decimals
const money = v => CUR.code!=='INR'&&CUR.decimals&&Math.abs(v)<100 ? fmtc.format(v) : fmt.format(Math.round(v));
const compact = v => { const a=Math.abs(v), s=v<0?'-':'', y=CUR.symbol;
  const steps = CUR.grouping==='indian' ? [[1e7,'Cr'],[1e5,'L'],[1e3,'k']] : [[1e9,'B'],[1e6,'M'],[1e3,'K']];
  for(const [n,u] of steps) if(a>=n){ const v=a/n; return s+y+v.toFixed(v>=10||n===1e3?0:1)+u; }
  return s+y+Math.round(a); };
const inCur = (v,cur) => { try{ return new Intl.NumberFormat(LOC,{style:'currency',currency:cur}).format(v); }catch(e){ return cur+' '+v; } };
const orig = t => t.fx ? ` <span class="sub2" title="Paid in ${t.fx[0]}, converted at that day's rate">${inCur(t.fx[1],t.fx[0])}</span>` : '';
const esc = s => String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Banks print payees in capitals ("AMAZON PAY INDIA"): show them in normal case, keeping real acronyms.
const ACRO=new Set(['UPI','NEFT','IMPS','RTGS','ATM','EMI','HDFC','ICICI','SBI','IDFC','HPCL','BPCL','IOCL','LIC','KFC','PVR','BBPS','IRCTC','EPF','PPF','NPS','SIP','ETF','USA','UK','US','NYC','LLC','ACH','IRS','CVS','TJ']);
const disp = s => { s=String(s??''); if(!s||/[a-z]/.test(s)) return s;
  return s.split(/(\s+)/).map(w=>{ const core=w.replace(/[^A-Z0-9&]/g,''); if(!core||/\d/.test(w)||ACRO.has(core)||(core.length<=4&&!/[AEIOUY]/.test(core))) return w;
    return w.charAt(0)+w.slice(1).toLowerCase(); }).join(''); };
const initials = s => { const w=disp(s).replace(/['’.]/g,'').replace(/[^\p{L}\p{N} ]/gu,' ').split(/\s+/).filter(Boolean); return (w.length?w.slice(0,2).map(x=>x[0]).join(''):'•').toUpperCase(); };
// The big numbers: the currency sign quieter than the digits, wherever this locale puts it.
const bigMoney = v => fmt.formatToParts(Math.round(v)).map(p=>p.type==='currency'?`<span class="cur">${esc(p.value)}</span>`:esc(p.value)).join('');
const arrow = v => `<span class="arrow">${v>=0?'▲':'▼'}</span>`;
const $ = id => document.getElementById(id);
const tip = $('tip');
function showTip(html, e){ tip.innerHTML=html; tip.style.opacity=1; moveTip(e); }
function moveTip(e){ const w=tip.offsetWidth; let x=e.clientX+14; if(x+w>innerWidth-8) x=e.clientX-w-14; tip.style.left=x+'px'; tip.style.top=(e.clientY+14)+'px'; }
function hideTip(){ tip.style.opacity=0; }

// Months: calendar months, or pay cycles that start on each salary credit (salary on the 25th pays for
// the next month, so "Nov" = 25 Oct – 24 Nov). The key is always "YYYY-MM" of the month a cycle pays for.
// A salary whose bank text never reached Margin would merge several months into one cycle, so a missing
// payday is estimated: the same day of the month as the salary before it (GUESSED marks those).
let MODE='calendar', STARTS=[], GUESSED=new Set(), VIEW='home';
const pad2 = n => String(n).padStart(2,'0');
const isoDay = x => `${x.getFullYear()}-${pad2(x.getMonth()+1)}-${pad2(x.getDate())}`;   // local, not UTC
const addDays = (d, n) => { const x=new Date(d+'T00:00:00'); x.setDate(x.getDate()+n); return isoDay(x); };
const daysApart = (a, b) => Math.round((new Date(b+'T00:00:00')-new Date(a+'T00:00:00'))/864e5);
function cycles(){
  const days=[...new Set(D.txns.filter(t=>t.c==='Salary'&&t.dir==='in').map(t=>t.d))].sort(), real=[];
  for(const d of days){ if(!real.length||daysApart(real[real.length-1],d)>20) real.push(d); }
  STARTS=[]; GUESSED=new Set();
  real.forEach((d,k)=>{ STARTS.push(d);
    const until=real[k+1]||addDays(today,-7), day=+d.slice(8,10); let [y,m]=d.split('-').map(Number);
    for(;;){ if(++m>12){ m=1; y++; }                               // a week's grace before the latest one counts as missing
      const g=`${y}-${pad2(m)}-${pad2(Math.min(day,new Date(y,m,0).getDate()))}`;
      if(real[k+1] ? daysApart(g,until)<20 : g>until) break;
      STARTS.push(g); GUESSED.add(g); } });
  let saved=null; try{ saved=localStorage.getItem('margin.months'); }catch(e){}
  MODE = real.length>=2 ? (saved||'pay') : 'calendar';
}
function cycleIndex(d){ let lo=0,hi=STARTS.length-1; if(!STARTS.length||d<STARTS[0]) return -1;
  while(lo<hi){ const mid=(lo+hi+1)>>1; if(STARTS[mid]<=d) lo=mid; else hi=mid-1; } return lo; }
function ym(d){ if(MODE!=='pay') return d.slice(0,7); const i=cycleIndex(d); if(i<0) return d.slice(0,7);
  return addDays(STARTS[i],15).slice(0,7); }
function cycleRange(m){ if(MODE!=='pay') return ''; const i=STARTS.findIndex(s=>ym(s)===m); if(i<0) return '';
  const end=STARTS[i+1]?addDays(STARTS[i+1],-1):null;
  return `${dshort(STARTS[i])} – ${end?dshort(end):'today'}`+(GUESSED.has(STARTS[i])?' · no salary text, payday estimated':''); }
const CHEV='<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const dshort = d => `${+d.slice(8,10)} ${MON[+d.slice(5,7)-1]}`;                       // 9 Nov
const dyear  = d => `${dshort(d)} ${d.slice(2,4)}`;                                     // 9 Nov 26
const dtime  = d => `${dshort(d)}${d.length>=16?', '+d.slice(11,16):''}`;              // 9 Nov, 03:12
const monthName = m => `${MON[+m.slice(5,7)-1]} ${m.slice(0,4)}`;
const today = isoDay(new Date());                                       // local date (UTC is a day behind before 05:30)
let nowM = today.slice(0,7);
function lastMonths(n, end){ const out=[]; let [y,m]=end.split('-').map(Number);
  for(let i=0;i<n;i++){ out.unshift(`${y}-${String(m).padStart(2,'0')}`); m--; if(!m){m=12;y--;} } return out; }
const counted = t => !NONFLOW.has(t.c);
const SPENDC = new Set(D.spend_cats||[]);
const paidBack = t => t.dir==='in' && SPENDC.has(t.c);      // e.g. a friend's share of a bill
const spendAmt = t => t.dir==='out' ? t.a : (paidBack(t) ? -t.a : 0);
function totals(m){ let i=0,o=0,inv=0;
  for(const t of D.txns){ if(ym(t.d)!==m) continue;
    if(t.c==='Investments'){ inv += t.dir==='out'?t.a:-t.a; continue; }
    if(!counted(t)) continue; if(paidBack(t)) o-=t.a; else if(t.dir==='in') i+=t.a; else o+=t.a; }
  return {i,o,inv}; }
// A month still running is compared with the same number of days of the month before, not all of it.
function monthStart(m){ if(MODE==='pay'){ const s=STARTS.find(x=>ym(x)===m); if(s) return s; } return m+'-01'; }
function spentSoFar(m, days){ const end=addDays(monthStart(m),days); let o=0;
  for(const t of D.txns){ if(ym(t.d)!==m||t.d>=end||!counted(t)) continue; if(paidBack(t)) o-=t.a; else if(t.dir==='out') o+=t.a; }
  return o; }

// status
function status(){
  const st=D.status, at=new Date(st.at), sameDay=isoDay(at)===isoDay(new Date());
  const when=sameDay?at.toLocaleTimeString(LOC,{hour:'numeric',minute:'2-digit'}):at.toLocaleDateString(LOC,{day:'numeric',month:'short'});
  $('status').innerHTML = `<i class="${st.ok&&!st.problems.length?'':'bad'}"></i>Synced ${esc(when)}`;
  $('status').title = `Updated ${at.toLocaleString(LOC,{dateStyle:'medium',timeStyle:'short'})} · syncs nightly at 00:00 · ${D.txns.length.toLocaleString(LOC)} transactions`;
  const p=$('problems'); p.hidden=!st.problems.length; p.innerHTML=st.problems.map(x=>`<li>${esc(x)}</li>`).join('');
  // a newer release of Margin (found by the nightly check): installed only by the person, with margin update
  const u=st.update, ub=$('update'); ub.hidden=!u;
  if(u){ const how='run margin update in Terminal, or ask Claude Code to update Margin';
    ub.textContent=`Update: ${u.version}`;
    ub.title=`Margin ${u.version} is out (${u.changes} change${u.changes===1?'':'s'}). To install it, ${how}. Your data isn't touched.`;
    ub.onclick=()=>toast(`To install Margin ${u.version}, ${how}.`); }
}
status();

// month picker + category filter (rebuilt when data changes)
let months=[], sel=nowM;
function controls(){
  cycles(); nowM=ym(today); if(!months.length||!sel) sel=nowM;
  $('mmode').hidden=STARTS.length<2; $('mmode').querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.m===MODE));
  months=[...new Set([nowM,...D.txns.map(t=>ym(t.d))])].sort().reverse();
  if(!months.includes(sel)) sel=nowM;
  $('month').innerHTML=months.map(m=>`<option value="${m}">${monthName(m)}</option>`).join(''); $('month').value=sel;
  $('mrange').textContent=cycleRange(sel); stepState();
  const cf=$('catf').value, cats=[...new Set(D.txns.map(t=>t.c))].sort();
  $('catf').innerHTML='<option value="">All categories</option>'+cats.map(c=>`<option>${esc(c)}</option>`).join(''); $('catf').value=cats.includes(cf)?cf:'';
}
// ‹ › step through the months (the list is newest first)
function stepState(){ const i=months.indexOf(sel); $('mprev').disabled=i<0||i>=months.length-1; $('mnext').disabled=i<=0; }
function pickMonth(m){ sel=m; $('month').value=m; $('mrange').textContent=cycleRange(m); stepState(); draw(); }
controls();
$('month').onchange=e=>pickMonth(e.target.value);
$('mprev').onclick=()=>{ const i=months.indexOf(sel); if(i<months.length-1) pickMonth(months[i+1]); };
$('mnext').onclick=()=>{ const i=months.indexOf(sel); if(i>0) pickMonth(months[i-1]); };
$('mmode').querySelectorAll('button').forEach(b=>b.onclick=()=>{ try{ localStorage.setItem('margin.months',b.dataset.m); }catch(e){}
  months=[]; sel=null; controls(); draw(); });
['q','catf','dirf'].forEach(id=>$(id).addEventListener('input',drawTx));

function kpis(){
  const {i,o,inv}=totals(sel), saved=i-o, rate=i>0?Math.round(saved/i*100):null;
  const prev=lastMonths(2,sel)[0], p=totals(prev), pm=MON[+prev.slice(5,7)-1];
  // ▲/▼ against last month, green when it's good news (more in, less spent)
  const delta=(a,b,upGood,by='')=>{ if(!b) return `No data for ${pm}`; const d=Math.round((a-b)/b*100);
    if(!d) return `Same as ${pm}${by}`; return `<span class="${(d>0)===upGood?'pos':'neg'}">${arrow(d)}${Math.abs(d)}%</span> vs ${pm}${by}`; };
  const running=sel===nowM, soFar=running?spentSoFar(prev,daysApart(monthStart(sel),today)+1):p.o;
  const P=D.projects||[], proj=D.txns.filter(t=>ym(t.d)===sel&&P.includes(t.c)).reduce((a,t)=>a+spendAmt(t),0);
  const anySms=D.txns.some(t=>t.src==='sms'), here=D.txns.some(t=>ym(t.d)===sel&&t.src==='sms');
  const tiles=[
    ['Money in',money(i),anySms&&!here?'<span class="warn" title="Salary and other SMS-only alerts are missing">No bank texts this month</span>':delta(i,p.i,true)],
    ['Spent',money(o),(proj>0?`incl. ${compact(proj)} ${esc(P.length===1?P[0].toLowerCase():'big projects')} · `:'')+delta(o,soFar,false,running?' by now':'')],
    ['Saved',money(saved),rate===null?'No income recorded':`${rate}% of income`],
    ['Invested',money(inv),CUR.code==='INR'?'SIPs, brokers, deposits':'Brokers, funds, deposits'],
  ];
  $('kpis').innerHTML=tiles.map(statHtml).join('');
}
const statHtml=([l,v,s])=>`<div class="stat"><div class="k">${esc(l)}</div><div class="v" title="${esc(v)}">${v}</div><div class="s">${s}</div></div>`;

function monthly(id='monthly', H=220){
  const svg=$(id), W=svg.clientWidth||640, pr=48, pb=24, pt=8;
  const ms=lastMonths(12, nowM), vals=ms.map(totals);
  const max=Math.max(1,...vals.map(v=>Math.max(v.i,v.o)));
  const nice=niceMax(max*1.04);
  const y=v=>pt+(H-pt-pb)*(1-v/nice), gw=(W-pr)/ms.length, bw=Math.max(3,Math.min(14,(gw-12)/2));
  let s='';
  for(const v of [0,nice/2,nice]){ const yy=y(v); s+=`<line x1="0" x2="${W-pr+8}" y1="${yy}" y2="${yy}" stroke="var(--line)"${v?' stroke-dasharray="2 4"':''}/><text x="${W}" y="${yy+4}" text-anchor="end">${compact(v)}</text>`; }
  ms.forEach((m,idx)=>{ const cx=gw*idx+gw/2, v=vals[idx];
    const bar=(val,x,col)=>{ const h=(H-pt-pb)-(y(val)-pt); if(h<=0) return ''; const r=Math.min(4,h,bw/2);
      return `<path d="M${x},${H-pb} V${y(val)+r} q0,-${r} ${r},-${r} h${bw-2*r} q${r},0 ${r},${r} V${H-pb} Z" fill="${col}"/>`; };
    const on=m===sel;
    s+=`<g data-m="${m}" style="cursor:pointer">`+
       `<rect x="${cx-gw/2+2}" y="0" width="${gw-4}" height="${H-pb+2}" fill="${on?'var(--chip)':'transparent'}" rx="8"/>`+
       bar(v.i,cx-bw-1,'var(--in)')+bar(v.o,cx+1,'var(--out)')+
       `<text x="${cx}" y="${H-6}" text-anchor="middle" style="${on?'fill:var(--text);font-weight:600':''}">${MON[+m.slice(5,7)-1]}</text></g>`; });
  svg.innerHTML=s;
  svg.querySelectorAll('g[data-m]').forEach(g=>{ const m=g.dataset.m, v=totals(m);
    g.onmousemove=e=>showTip(`<b>${monthName(m)}</b>${MODE==='pay'?`<br><span class="t2">${cycleRange(m)}</span>`:''}<br>Money in ${money(v.i)}<br>Spent ${money(v.o)}<br>Saved ${money(v.i-v.o)}`,e);
    g.onmouseleave=hideTip; g.onclick=()=>{ if(!months.includes(m)) return; hideTip(); pickMonth(m); if(id!=='monthly') show('overview'); }; });
}
function niceMax(v){ v=Math.max(v,1); const p=Math.pow(10,Math.floor(Math.log10(v)));
  for(const m of [1,1.2,1.5,2,2.5,3,4,5,6,8,10]) if(m*p>=v) return m*p; return 10*p; }

// The three bar cards. Each bar is clickable: it narrows the Transactions table to exactly the
// payments behind that bar (same test, key and amount as the bar itself, so the totals match).
const PICKS={
  cats:   {test:t=>(t.dir==='out'||paidBack(t))&&counted(t), key:t=>t.c, amt:spendAmt, label:n=>`${n} · spending`},
  incomes:{test:t=>t.dir==='in'&&counted(t)&&!paidBack(t),   key:t=>t.c, amt:t=>t.a,    label:n=>`${n} · money in`},
  payees: {test:t=>t.dir==='out'&&counted(t), key:t=>t.p||'(no name in alert)', amt:t=>t.a, label:n=>`Paid to ${n}`},
};
let FOCUS=null;                                   // {src, name} of the bar picked, or null
const focusTest = () => { if(!FOCUS) return null; const p=PICKS[FOCUS.src]; return t=>p.test(t)&&(p.key(t)||'Unknown')===FOCUS.name; };
function setFocus(src, name){
  FOCUS = FOCUS&&FOCUS.src===src&&FOCUS.name===name ? null : {src,name};
  if(FOCUS){ $('q').value=''; $('catf').value=''; $('dirf').value=''; }
  breakdowns(); drawTx();
  if(FOCUS) $('tx-card').scrollIntoView({behavior:'smooth',block:'start'});
}
$('focus-clear').onclick=()=>{ FOCUS=null; breakdowns(); drawTx(); };

function barList(el, rows, total, emptyMsg, src){
  if(!rows.length){ el.innerHTML=`<div class="empty">${emptyMsg}</div>`; return; }
  const max=rows[0][1];
  el.innerHTML=rows.map(([n,v,cnt])=>{ const on=!!(src&&FOCUS&&FOCUS.src===src&&FOCUS.name===n);
    const attrs=`${src?` role="button" tabindex="0" aria-pressed="${on}"`:''} data-n="${esc(n)}" data-v="${v}" data-c="${cnt}"`;
    if(src==='payees')            // people and shops: initials, name, how often
      return `<div class="barrow person pick${on?' on':''}"${attrs}><span class="av">${esc(initials(n))}</span><span class="who"><b>${esc(disp(n))}</b><span>${cnt} payment${cnt==1?'':'s'}</span></span><span class="amt">${money(v)}</span></div>`;
    return `<div class="barrow${src?' pick':''}${on?' on':''}"${attrs}><span class="name" title="${esc(n)}">${esc(disp(n))}</span><span class="track"><span class="fill" style="width:${Math.max(2,v/max*100)}%${/^Other (spending|income)$/.test(n)?';background:var(--muted)':''}"></span></span><span class="amt">${money(v)}</span></div>`; }).join('');
  el.querySelectorAll('.barrow').forEach(r=>{ r.onmousemove=e=>showTip(`<b>${esc(r.dataset.n)}</b><br>${money(+r.dataset.v)} · ${total?Math.round(r.dataset.v/total*100):0}%<br>${r.dataset.c} transaction${r.dataset.c==1?'':'s'}${src?(r.classList.contains('on')?'<br>Click to show all':'<br>Click to see them'):''}`,e); r.onmouseleave=hideTip;
    if(src){ const go=()=>{ hideTip(); setFocus(src, r.dataset.n); };
      r.onclick=go; r.onkeydown=e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); go(); } }; } });
}
function group(filter,key,amt=t=>t.a){ const m=new Map(); for(const t of D.txns){ if(ym(t.d)!==sel||!filter(t)) continue; const k=key(t)||'Unknown'; const g=m.get(k)||[0,0]; g[0]+=amt(t); g[1]++; m.set(k,g);} return [...m].map(([k,[v,c]])=>[k,v,c]).filter(r=>r[1]>0.5).sort((a,b)=>b[1]-a[1]); }
const groupPick = src => group(PICKS[src].test, PICKS[src].key, PICKS[src].amt);

function breakdowns(){
  const cats=groupPick('cats'), tot=cats.reduce((s,r)=>s+r[1],0);
  $('cat-meta').textContent=cats.length?`${cats.length} categor${cats.length===1?'y':'ies'}`:'';
  barList($('cats'),cats.slice(0,12),tot,'No spending recorded this month.','cats');
  const inc=groupPick('incomes');
  barList($('incomes'),inc.slice(0,6),inc.reduce((s,r)=>s+r[1],0),'No income recorded this month.','incomes');
  unsortedNote($('cat-nudge'),cats);
  const pay=groupPick('payees');
  barList($('payees'),pay.slice(0,6),tot,'No spending recorded this month.','payees');
}
// "₹2,588 not sorted · Sort it out" under a category list
function unsortedNote(el, cats){ const other=cats.find(r=>r[0]==='Other spending'); el.hidden=!(LIVE&&other);
  if(LIVE&&other) el.innerHTML=`<span class="cat unset">${money(other[1])} not sorted</span><button class="link" data-goto="sortout">Sort it out<svg class="i" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></button>`; }

// Only bank balances known (no investments, other assets, loans or card dues): call it what it is.
function bankOnly(){ const n=D.nw; return !n.holdings.length&&!(n.investments||[]).length&&!n.manual.length&&!n.card_dues.length; }

function networth(){
  // Home's Accounts panel: what's in the bank, what you owe, and anything you added by hand (investments have
  // their own panel, so they're left out here).
  const n=D.nw, rows=[];
  const add=(label,v,sub,neg,title)=>rows.push(`<div class="lrow acct"${title?` title="${esc(title)}"`:''}><span>${esc(disp(label))}${sub?`<small>${esc(sub)}</small>`:''}</span><span class="val${neg?' neg':''}">${neg?'−':''}${money(v)}</span></div>`);
  n.balances.forEach(b=>add(b.name,b.value,b.since?`Estimated from ${dshort(b.as_of)}`:`As of ${dshort(b.as_of)}`,false,
      b.since?`Last reported ${money(b.reported)} on ${dshort(b.as_of)}, then ${b.since} payment${b.since>1?'s':''} in and out`
        +(b.autopay?`, incl. ${money(b.autopay)} card auto-pay the bank sent no debit alert for`:''):''));
  const byType={}; n.holdings.forEach(h=>byType[h.type]=(byType[h.type]||0)+h.value);
  if(!(n.investments||[]).length) Object.entries(byType).forEach(([k,v])=>add(k,v,'From statements'));
  n.manual.forEach(m=>add(m.name,m.value,m.as_of?`As of ${m.as_of}`:'',m.kind==='liability'));
  const day=d=>/^\d{4}-\d\d-\d\d$/.test(d||'')?dlong(d):d;
  n.card_dues.forEach(d=>add(d.name,d.value,d.due_date?`Due ${day(d.due_date)}`:day(d.as_of),true));
  $('nw-title').textContent=bankOnly()?'In the bank':'Accounts';
  $('nw-meta').textContent=n.balances.length?`${n.balances.length} bank account${n.balances.length===1?'':'s'}`:'';
  $('nwparts').innerHTML=rows.length?rows.join(''):`<div class="empty">Balances appear as bank alerts arrive. Add EPF, FDs, loans and the like to <code>assets.csv</code>; drop broker or fund holdings CSVs into <code>statements/</code>.</div>`;
}

function drawTx(){
  $('tx-title').textContent=`Transactions, ${monthName(sel)}`;
  const q=$('q').value.toLowerCase(), cf=$('catf').value, df=$('dirf').value, ft=focusTest();
  const rows=D.txns.filter(t=>ym(t.d)===sel&&(!ft||ft(t))&&(!cf||t.c===cf)&&(!df||t.dir===df)&&(!q||(t.p+' '+disp(t.p)+' '+t.acct+' '+t.c).toLowerCase().includes(q)));
  $('tx-count').textContent=`${rows.length.toLocaleString(LOC)} transaction${rows.length===1?'':'s'}`;
  $('focus').hidden=!FOCUS; $('tx-sum').hidden=!FOCUS;
  if(FOCUS){ const p=PICKS[FOCUS.src], tot=rows.reduce((s,t)=>s+p.amt(t),0);
    $('focus-label').textContent=p.label(disp(FOCUS.name));
    $('tx-sum').textContent=`${rows.length} transaction${rows.length===1?'':'s'} · ${money(tot)} in ${monthName(sel)}`; }
  if(!rows.length){ $('tx').innerHTML=`<p class="empty">Nothing here for ${monthName(sel)}.</p>`; return; }
  // grouped by day, newest first, with what went out that day
  const days=new Map(); rows.forEach(t=>{ if(!days.has(t.d)) days.set(t.d,[]); days.get(t.d).push(t); });
  const yday=addDays(today,-1);
  const dayName=d=>d===today?'Today':d===yday?'Yesterday':new Date(d+'T00:00:00').toLocaleDateString(LOC,{weekday:'short',day:'numeric',month:'short'});
  let html=''; for(const [d,ts] of days){ const out=ts.reduce((a,t)=>a+(t.dir==='out'&&counted(t)?t.a:0),0);
    html+=`<div class="day">${dayName(d)}<span>${out?money(out)+' spent':''}</span></div>`+ts.map(txHtml).join(''); }
  $('tx').innerHTML=html;
}
function txHtml(t){
  const unset=/^Other (spending|income)$/.test(t.c);
  const cat=LIVE?`<button class="cat pill-btn${unset?' unset':''}" data-id="${t.id}" aria-haspopup="listbox" aria-expanded="false" title="Change category">${esc(unset?'Choose category':t.c)}${CHEV}</button>`
                :`<span class="cat">${esc(t.c)}</span>`;
  const who=LIVE&&t.p?`<button class="nmbtn" data-id="${t.id}" title="Give this payee a name">${esc(disp(t.p))}</button>`:`<b>${esc(disp(t.p)||'No payee name')}</b>`;
  return `<div class="tx"><span class="av">${esc(initials(t.p||'?'))}</span><span class="who">${who}<span>${esc(t.acct)}${t.w?' · '+esc(disp(t.w)):''}${orig(t)}</span></span>${cat}<span class="amt${t.dir==='in'?' in':''}">${t.dir==='in'?'+':'−'}${money(t.a)}</span></div>`; }

function review(){
  $('review').innerHTML=D.review.length?D.review.map(r=>`<tr><td class="date">${esc(r.ts.slice(0,10))}</td><td>${esc(r.sender)}</td><td>${esc(r.reason)}</td><td>${esc(r.snippet)}</td></tr>`).join(''):`<tr><td colspan="4" class="empty">Nothing to review.</td></tr>`;
}

function draw(){ kpis(); if(VIEW==='overview') monthly(); breakdowns(); drawTx(); }
draw(); review();               // Home's accounts (networth) are drawn with Home

// ---- Investments + Home ----------------------------------------------------------------------------
const KC={in_stock:'var(--c1)',mf:'var(--c2)',us_stock:'var(--c3)',nps:'var(--c4)',epf:'var(--c5)',fd:'var(--c6)',ppf:'var(--c7)',
          retirement:'var(--c5)',cash:'var(--c6)',other:'var(--c7)'};   // retirement/cash share EPF/FD's colours: rarely together
const KL={in_stock:'Stocks & ETFs',mf:'Mutual funds',us_stock:'US stocks',nps:'NPS',epf:'EPF',fd:'Fixed deposits',ppf:'PPF',
          retirement:'Retirement',cash:'Savings & CDs',other:'Other investments'};
if(CUR.code==='USD') KL.us_stock='Stocks & ETFs';
if(CUR.code!=='INR'){ KL.in_stock='Indian stocks & ETFs'; KL.mf='Indian mutual funds'; }
const INV=()=>D.invest||{};
const pct=(v,d=1)=>v==null||!isFinite(v)?'–':`${v>=0?'+':'−'}${Math.abs(v).toFixed(d)}%`;
const sign=v=>v>=0?'up':'down';
const signed=v=>`${v>=0?'+':'−'}${money(Math.abs(v))}`;
const px=(v,cur)=>{ cur=cur||CUR.code; try{ return new Intl.NumberFormat(cur==='INR'?'en-IN':cur==='USD'?'en-US':LOC,{style:'currency',currency:cur,minimumFractionDigits:2,maximumFractionDigits:2}).format(v); }catch(e){ return v.toFixed(2); } };
const units=v=>v.toLocaleString(LOC,{maximumFractionDigits:v<10?4:v<1000?3:0});
const dlong=d=>new Date(d+'T00:00:00').toLocaleDateString(LOC,{day:'numeric',month:'short',year:'numeric'});

// Smooth line through points without overshooting (monotone cubic), so a rise never dips on the way up.
function monotone(P){
  const n=P.length; if(n<3) return 'M'+P.map(p=>p.join(',')).join('L');
  const dx=[], m=[], t=[];
  for(let i=0;i<n-1;i++){ dx[i]=P[i+1][0]-P[i][0]||1e-6; m[i]=(P[i+1][1]-P[i][1])/dx[i]; }
  t[0]=m[0]; t[n-1]=m[n-2];
  for(let i=1;i<n-1;i++) t[i]=m[i-1]*m[i]<=0?0:(m[i-1]+m[i])/2;
  for(let i=0;i<n-1;i++){ if(!m[i]){ t[i]=t[i+1]=0; continue; }
    const a=t[i]/m[i], b=t[i+1]/m[i], s=a*a+b*b; if(s>9){ const k=3/Math.sqrt(s); t[i]=k*a*m[i]; t[i+1]=k*b*m[i]; } }
  let d=`M${P[0][0].toFixed(1)},${P[0][1].toFixed(1)}`;
  for(let i=0;i<n-1;i++){ const h=dx[i]/3;
    d+=`C${(P[i][0]+h).toFixed(1)},${(P[i][1]+h*t[i]).toFixed(1)} ${(P[i+1][0]-h).toFixed(1)},${(P[i+1][1]-h*t[i+1]).toFixed(1)} ${P[i+1][0].toFixed(1)},${P[i+1][1].toFixed(1)}`; }
  return d;
}
// One chart for both pages: y-axis from 0 with values on the right, month ticks that never crowd, a crosshair
// with every series in the tooltip. A single series is drawn as a soft area.
function lineChart(svg, series, H, empty){
  const W=svg.clientWidth||640, pr=52, pt=10, pb=24;
  const all=series.flatMap(s=>s.pts);
  const xs=[...new Set(all.map(p=>p[0]))].sort();
  if(xs.length<2){ svg.innerHTML=`<text x="0" y="18">${esc(empty)}</text>`; return; }
  const t=d=>+new Date(d+'T00:00:00'), x0=t(xs[0]), x1=t(xs[xs.length-1]);
  const nice=niceMax(Math.max(1,...all.map(p=>p[1]))*1.04);
  const X=d=>4+(W-pr-8)*(t(d)-x0)/(x1-x0||1), Y=v=>pt+(H-pt-pb)*(1-v/nice);
  let g='';
  for(const v of [0,nice/2,nice]) g+=`<line x1="0" x2="${W-pr+8}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)"${v?' stroke-dasharray="2 4"':''}/><text x="${W}" y="${Y(v)+4}" text-anchor="end">${compact(v)}</text>`;
  const months=(x1-x0)/864e5/30.44, fit=Math.max(2,Math.floor((W-pr)/66));
  if(months>=2){ const step=[1,2,3,4,6,12].find(s=>months/s<=fit)||12;
    const d=new Date(xs[0]+'T00:00:00'); d.setDate(1); d.setMonth(d.getMonth()+1);
    while(+d<=x1){ if(d.getMonth()%step===0){ const iso=isoDay(d), x=X(iso);
        if(x>16&&x<W-pr-12) g+=`<text x="${x}" y="${H-6}" text-anchor="middle">${MON[d.getMonth()]}${d.getMonth()===0||step>=6?' ’'+String(d.getFullYear()).slice(2):''}</text>`; }
      d.setMonth(d.getMonth()+1); } }
  else [xs[0],xs[xs.length-1]].forEach((d,i)=>g+=`<text x="${X(d)}" y="${H-6}" text-anchor="${i?'end':'start'}">${dshort(d)}</text>`);
  const one=series.length===1, gid='ga-'+svg.id;
  if(one) g+=`<defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${series[0].color}" stop-opacity=".2"/><stop offset=".75" stop-color="${series[0].color}" stop-opacity=".03"/><stop offset="1" stop-color="${series[0].color}" stop-opacity="0"/></linearGradient></defs>`;
  series.forEach(s=>{ if(!s.pts.length) return;
    const P=s.pts.map(p=>[X(p[0]),Y(p[1])]), last=P[P.length-1];
    if(P.length>1){ const line=monotone(P);
      if(one) g+=`<path d="${line}L${last[0].toFixed(1)},${Y(0)}L${P[0][0].toFixed(1)},${Y(0)}Z" fill="url(#${gid})"/>`;
      g+=`<path d="${line}" fill="none" stroke="${s.color}" stroke-width="${one?2.2:1.8}" stroke-linejoin="round" stroke-linecap="round"/>`; }
    g+=`<circle cx="${last[0]}" cy="${last[1]}" r="${one?4.5:3.5}" fill="${s.color}" stroke="var(--bg)" stroke-width="2.5"/>`; });
  g+=`<line class="cx" y1="${pt}" y2="${H-pb}" stroke="var(--muted)" stroke-dasharray="3 3" opacity="0"/>`+series.map((s,i)=>`<circle class="cd" data-i="${i}" r="5" fill="${s.color}" stroke="var(--bg)" stroke-width="2.5" opacity="0"/>`).join('');
  g+=`<rect x="0" y="0" width="${W-pr+8}" height="${H}" fill="transparent" class="hit"/>`;
  svg.innerHTML=g;
  const at=(s,d)=>{ let v=null; for(const p of s.pts){ if(p[0]<=d) v=p[1]; else break; } return v; };
  svg.querySelector('.hit').onmousemove=e=>{ const b=svg.getBoundingClientRect(), mx=(e.clientX-b.left)*(W/b.width);
    let bi=0; xs.forEach((x,i)=>{ if(Math.abs(X(x)-mx)<Math.abs(X(xs[bi])-mx)) bi=i; }); const d=xs[bi];
    svg.querySelector('.cx').setAttribute('x1',X(d)); svg.querySelector('.cx').setAttribute('x2',X(d)); svg.querySelector('.cx').setAttribute('opacity',.7);
    let html=`<b>${dlong(d)}</b>`;
    series.forEach((s,i)=>{ const v=at(s,d), c=svg.querySelector(`.cd[data-i="${i}"]`);
      if(v==null){ c.setAttribute('opacity',0); return; }
      c.setAttribute('cx',X(d)); c.setAttribute('cy',Y(v)); c.setAttribute('opacity',1); html+=`<br>${one?'':esc(s.label)+' '}${money(v)}`; });
    showTip(html,e); };
  svg.querySelector('.hit').onmouseleave=()=>{ hideTip(); svg.querySelectorAll('.cx,.cd').forEach(n=>n.setAttribute('opacity',0)); };
}

const kindLabel=k=>k.label||KL[k.kind]||k.kind;
function kindTip(k, tot){
  return `<b>${esc(kindLabel(k))}</b><br>${money(k.value)} · ${Math.round(k.value/tot*100)}% of investments`+
    (k.prev_value?`<br>Last close ${signed(k.value-k.prev_value)} (${pct((k.value/k.prev_value-1)*100)})`:'')+
    (k.month_start?`<br>This month ${signed(k.value-k.month_start)} (${pct((k.value/k.month_start-1)*100)})`:'')+
    (k.cost_known&&k.cost?`<br>On cost ${signed(k.value-k.cost)} (${pct((k.value/k.cost-1)*100)})`:'')+
    `<br>${k.n} holding${k.n===1?'':'s'}`; }
function noInvestments(){ const inv=INV();
  return `<div class="empty">No investments yet.${inv.sources&&!inv.sources.pan&&CUR.code==='INR'?' Run <code>margin setup-pan</code> in Terminal so Margin can read your CDSL statement.':''}</div>`; }
// Home: one stacked bar (a gap between every type) and the list under it
function allocation(barEl, el){
  const inv=INV(), ks=(inv.kinds||[]).slice().sort((a,b)=>b.value-a.value), tot=inv.total||1;
  if(!ks.length){ barEl.innerHTML=''; el.innerHTML=noInvestments(); return; }
  barEl.innerHTML=ks.map(k=>`<i style="flex:${k.value};background:${KC[k.kind]}" data-k="${k.kind}"></i>`).join('');
  el.innerHTML=ks.map(k=>`<div class="lrow" data-k="${k.kind}"><span class="dot" style="background:${KC[k.kind]}"></span><span>${esc(kindLabel(k))}</span><span class="pct">${Math.round(k.value/tot*100)}%</span><span class="val">${money(k.value)}</span></div>`).join('');
  [...barEl.children,...el.children].forEach(r=>{ const k=ks.find(x=>x.kind===r.dataset.k); r.onmousemove=e=>showTip(kindTip(k,tot),e); r.onmouseleave=hideTip; });
}
// Investments: a tile per type, and how this month is going
function kindTiles(el){
  const inv=INV(), ks=(inv.kinds||[]).slice().sort((a,b)=>b.value-a.value), tot=inv.total||1;
  if(!ks.length){ el.innerHTML=`<div class="tile" style="grid-column:1/-1">${noInvestments()}</div>`; return; }
  const ret=k=>k.cost_known&&k.cost?`<span class="${sign(k.value-k.cost)}">${pct((k.value/k.cost-1)*100)}</span>`:`<span>${k.n} holding${k.n===1?'':'s'}</span>`;
  const ms=inv.month_base, mch=inv.month_change;
  el.innerHTML=ks.map(k=>`<div class="tile" data-k="${k.kind}"><div class="n"><i style="background:${KC[k.kind]}"></i>${esc(kindLabel(k))}</div><div class="v">${compact(k.value)}</div><div class="s"><span>${Math.round(k.value/tot*100)}% of total</span>${ret(k)}</div></div>`).join('')+
    `<div class="tile alt"><div class="n">This month</div><div class="v ${ms?sign(mch):''}">${ms?signed(mch):'–'}</div><div class="s"><span>${ms?pct(mch/ms*100)+' since the 1st':'Starts next month'}</span></div></div>`;
  el.querySelectorAll('.tile[data-k]').forEach(r=>{ const k=ks.find(x=>x.kind===r.dataset.k); r.onmousemove=e=>showTip(kindTip(k,tot),e); r.onmouseleave=hideTip; });
}

function takeHtml(r, short){
  if(!r) return `<p class="empty">The AI writes a short analysis every Monday for the week before, and a fuller one when your monthly statement arrives. It describes and measures; it doesn’t tell you what to buy or sell.</p>`;
  const bul=short?r.summary.slice(0,3):r.summary, watch=short?r.watch.slice(0,1):r.watch;
  return `<div class="rv-head">${esc(r.headline)}</div>
    <div class="rv-meta">${esc(r.label)} · ${r.by_ai?'by AI':'automatic summary'+(r.note?' ('+esc(r.note)+')':'')}${short?'':' · '+new Date(r.created).toLocaleString(LOC,{dateStyle:'medium',timeStyle:'short'})}</div>
    <ul class="bul">${bul.map(b=>`<li>${esc(b)}</li>`).join('')}</ul>
    ${watch.map(w=>`<div class="watch"><b>${esc(w.title)}</b>${esc(w.why)}</div>`).join('')}
    <p class="fine">A description of your numbers, not financial advice. The decisions are yours.</p>`;
}

function groupMonth(m, src){ const p=PICKS[src], g=new Map();
  for(const t of D.txns){ if(ym(t.d)!==m||!p.test(t)) continue; const k=p.key(t)||'Unknown'; const x=g.get(k)||[0,0]; x[0]+=p.amt(t); x[1]++; g.set(k,x); }
  return [...g].map(([k,[v,c]])=>[k,v,c]).filter(r=>r[1]>0.5).sort((a,b)=>b[1]-a[1]); }

let homeRange=365;
function home(){
  const inv=INV(), nw=D.nw, m=totals(nowM), tot=inv.total||0;
  const bank=nw.balances.reduce((a,b)=>a+b.value,0), dues=nw.card_dues.reduce((a,d)=>a+d.value,0);
  const day=tot&&inv.prev_total?tot-inv.prev_total:null;
  // the headline: net worth and how it moved since the 1st (bank + investments, like for like)
  $('home-eyebrow').textContent=bankOnly()?'In the bank':'Net worth';
  $('home-big').innerHTML=bigMoney(nw.net);
  const pts=(inv.networth||[]).slice(); pts.push({date:today,bank,invest:tot});
  const base=[...pts].reverse().find(p=>p.date<today.slice(0,7)+'-01'&&p.bank!=null);
  if(base){ const b0=base.bank+base.invest, ch=bank+tot-b0;
    $('home-delta').innerHTML=`<span class="${sign(ch)}">${arrow(ch)}${money(Math.abs(ch))}${b0>0?' · '+Math.abs(ch/b0*100).toFixed(1)+'%':''}</span><span>this month</span>`; }
  else $('home-delta').innerHTML=day!=null?`<span class="${sign(day)}">${arrow(day)}${money(Math.abs(day))}</span><span>investments at the last close</span>`:'<span>Updated every night</span>';
  document.querySelectorAll('#home-range button').forEach(b=>b.setAttribute('aria-pressed',+b.dataset.r===homeRange));
  const from=homeRange?isoDay(new Date(Date.now()-homeRange*864e5)):'0000';
  let line=pts.filter(p=>p.bank!=null&&p.date>=from).map(p=>[p.date,p.bank+p.invest]), label='Net worth';
  if(line.length<2){ line=pts.filter(p=>p.invest&&p.date>=from).map(p=>[p.date,p.invest]); label='Investments'; }
  lineChart($('home-nw'),[{label,color:'var(--accent)',pts:line}],240,'The trend appears as bank balances and statements build up.');
  const gap=label==='Net worth'&&pts.some(p=>p.bank==null&&p.invest&&p.date>=from);
  $('home-nw-note').hidden=!(gap||label!=='Net worth');
  $('home-nw-note').textContent=label!=='Net worth'?'Showing investments only: no bank balances on record for this period yet.':'Months without a bank balance on record are left out.';
  // key figures
  const pm=lastMonths(2,nowM)[0], pmn=MON[+pm.slice(5,7)-1], cyc=MODE==='pay'?'cycle':'month';
  const so=spentSoFar(pm,daysApart(monthStart(nowM),today)+1), dp=so?Math.round((m.o-so)/so*100):null;
  $('home-kpis').innerHTML=[
    ['Investments',money(tot),day==null?'Updated every night':`<span class="${sign(day)}">${signed(day)}</span> at the last close`],
    ['Cash in bank',money(bank),`${nw.balances.length} account${nw.balances.length===1?'':'s'}${dues?` · ${compact(dues)} card due`:''}`],
    [`Spent this ${cyc}`,money(m.o),dp==null?esc(monthName(nowM)):dp===0?`Same as ${pmn} by now`:`<span class="${dp<0?'pos':'neg'}">${Math.abs(dp)}% ${dp<0?'less':'more'}</span> than ${pmn} by now`],
    ['Saved',money(m.i-m.o),m.i>0?`${Math.round((m.i-m.o)/m.i*100)}% of income`:'No income recorded yet'],
  ].map(statHtml).join('');
  allocation($('home-alloc'),$('home-kinds'));
  $('home-cat-title').textContent=`Spending this ${cyc}`;
  const cats=groupMonth(nowM,'cats');
  barList($('home-cats'),cats.slice(0,6),cats.reduce((a,r)=>a+r[1],0),'No spending recorded this month.');
  unsortedNote($('home-cat-nudge'),cats);
  networth();
  $('home-take').innerHTML=takeHtml((inv.reviews||[])[0],true);
  const hits=(inv.holdings||[]).filter(h=>h.stop&&h.stop.hit_at), sb=$('home-stops'); sb.hidden=!hits.length;
  sb.innerHTML=hits.length?`<span class="bdot"></span><b>${hits.length} price alert${hits.length>1?'s':''} reached</b><span class="muted">${hits.map(h=>esc(h.name)).join(', ')}</span><button class="link" data-goto="invest">Open Investments<svg class="i" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></button>`:'';
}
document.querySelectorAll('#home-range button').forEach(b=>b.onclick=()=>{ homeRange=+b.dataset.r; home(); });

let invMode='total', invRange=365, invFilter='all', invRKind='weekly', stopEdit=null, HMAXW=1;
function invest(){
  const inv=INV();
  if(inv.error){ $('inv-kpis').innerHTML=`<div class="tile" style="grid-column:1/-1">Investments couldn’t load: ${esc(inv.error)}</div>`; return; }
  const src=inv.sources||{}, setup=[];
  if(CUR.code==='INR'&&!src.pan) setup.push('Indian stocks and mutual funds appear once Margin can open your monthly CDSL statement: run <code>margin setup-pan</code> in Terminal.');
  if(CUR.code==='INR'&&!src.pran) setup.push('NPS appears once Margin can open its monthly statement: run <code>margin setup-pran</code> in Terminal.');
  (inv.notes||[]).slice(0,3).forEach(n=>setup.push(esc(n)));
  $('inv-setup').hidden=!setup.length; $('inv-setup').innerHTML=setup.map(x=>`<li>${x}</li>`).join('');
  const tot=inv.total||0, day=inv.prev_total?tot-inv.prev_total:null, gain=inv.cost?inv.cost_value-inv.cost:null;
  $('inv-big').innerHTML=bigMoney(tot);
  $('inv-delta').innerHTML=(day!=null?`<span class="${sign(day)}">${arrow(day)}${money(Math.abs(day))} · ${Math.abs(day/inv.prev_total*100).toFixed(1)}%</span><span>last close</span>`:'')+
    (gain!=null?`<span class="${sign(gain)}"${inv.cost_value<tot-1?' title="On the holdings whose cost is known"':''}>${arrow(gain)}${money(Math.abs(gain))} · ${Math.abs(gain/inv.cost*100).toFixed(1)}%</span><span>all-time return</span>`:'')||'<span>Values update every night</span>';
  kindTiles($('inv-kpis'));
  // value over time
  document.querySelectorAll('#inv-mode button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.m===invMode));
  document.querySelectorAll('#inv-range button').forEach(b=>b.setAttribute('aria-pressed',+b.dataset.r===invRange));
  const from=invRange?isoDay(new Date(Date.now()-invRange*864e5)):'0000', h=(inv.history||[]).filter(r=>r.date>=from);
  const present=Object.keys(KL).filter(k=>h.some(r=>r[k]!=null));
  const series=invMode==='total'?[{label:'Total',color:'var(--accent)',pts:h.map(r=>[r.date,r.total])}]
    :present.map(k=>({label:KL[k],color:KC[k],pts:h.filter(r=>r[k]!=null).map(r=>[r.date,r[k]])}));
  $('inv-legend').hidden=invMode==='total';
  $('inv-legend').innerHTML=series.map(s=>`<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join('');
  lineChart($('inv-line'),series,240,'The line starts after a second day of values (and fills in further back once your statements are read).');
  const starts=present.map(k=>[k,(inv.history||[]).find(r=>r[k]!=null)?.date]).filter(([,d])=>d&&d>((inv.history||[])[0]||{}).date);
  $('inv-line-note').textContent=starts.length?starts.map(([k,d])=>`${KL[k]} tracked from ${dlong(d)}`).join(' · ')+'.':'';
  // markets
  const bm=Object.values(inv.benchmarks||{}).map(b=>{ const s=b.series; if(!s.length) return '';
    const last=s[s.length-1], back=d=>{ const c=isoDay(new Date(new Date(last[0]+'T00:00:00')-d*864e5)); let v=null; for(const p of s){ if(p[0]<=c) v=p[1]; } return v; };
    const r=d=>{ const v=back(d); return v?(last[1]/v-1)*100:null; };
    return `<div class="bench"><span>${esc(b.name)} <span class="muted">${last[1].toLocaleString(LOC,{maximumFractionDigits:0})}</span></span><span class="r"><span class="${sign(r(30))}">${pct(r(30))}</span><span class="${sign(r(365))}">${pct(r(365))}</span></span></div>`; }).join('');
  $('inv-bench').innerHTML=bm||'<div class="empty">Index prices arrive with the nightly sync.</div>';
  // holdings
  const hs=inv.holdings||[], kinds=Object.keys(KL).filter(k=>hs.some(x=>x.kind===k));
  if(invFilter!=='all'&&!kinds.includes(invFilter)) invFilter='all';
  $('inv-filter').innerHTML=(kinds.length>1?[['all','All'],...kinds.map(k=>[k,KL[k]])]:[]).map(([k,l])=>`<button data-f="${k}" aria-pressed="${k===invFilter}">${esc(l)}</button>`).join('');
  $('inv-filter').querySelectorAll('button').forEach(b=>b.onclick=()=>{ invFilter=b.dataset.f; invest(); });
  const rows=hs.filter(x=>invFilter==='all'||x.kind===invFilter);
  HMAXW=Math.max(.0001,...hs.map(x=>x.weight||0));
  $('inv-count').textContent=hs.length?String(rows.length):'';
  $('inv-rows').innerHTML=rows.length?rows.map(rowHtml).join(''):`<tr><td colspan="5" class="empty">No holdings yet.</td></tr>`;
  bindStops();
  // analysis
  document.querySelectorAll('#inv-rkind button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.k===invRKind));
  $('inv-review').innerHTML=takeHtml((inv.reviews||[]).find(r=>r.kind===invRKind),false);
  // sources
  const kv=(a,b)=>`<div class="kv"><span>${a}</span><span>${b}</span></div>`;
  $('inv-sources').innerHTML=
    kv('Indian stocks and mutual funds',src.cas?`CDSL statement as of ${dlong(src.cas)}, valued at the latest closing prices`:src.pan?'Waiting for the next CDSL statement':'Needs <code>margin setup-pan</code>')+
    kv('US stocks (INDmoney)',src.us?`Your INDmoney snapshot of ${dlong(src.us)} + order emails after it${src.us_orders?` (last order ${dlong(src.us_orders.slice(0,10))})`:''}`:'Order emails only')+
    kv('NPS',src.nps?`KFintech statement as of ${dlong(src.nps)}`:src.pran?'Waiting for the next NPS statement':'Needs <code>margin setup-pran</code>')+
    kv('EPF',src.epf?`${src.epf.source==='passbook'?'Your EPFO passbook':'EPFO’s text'} of ${dlong(src.epf.as_of)} (${esc(src.epf.member)}${src.epf.transferred.length?'; '+src.epf.transferred.map(esc).join(', ')+' transferred into it':''}), then EPFO’s texts`+(src.epf.age_days>45?` · <span class="warn">nothing newer for ${src.epf.age_days} days: drop a fresh passbook into the statements folder</span>`:''):'EPFO’s texts after each contribution, or a passbook in the statements folder')+
    kv('Fixed deposits',hs.some(h=>h.kind==='fd')?'Your bank’s deposit advice emails (opened with your PAN); interest accrues daily up to the maturity amount':'Deposit advice emails from your bank')+
    kv('PPF',(()=>{ const p=hs.find(h=>h.kind==='ppf'); if(!p) return 'Add your balance to <code>settings.json</code> (see README)';
      return p.extra.from_statement?`The bank’s statement of ${dlong(p.as_of)} (every deposit and interest credit since the account opened) + deposits texted since, with interest at ${p.extra.rate}%`
        :'Your balance in <code>settings.json</code> + deposits since (bank alerts), with interest at the PPF rate'; })())+
    kv('Prices',src.prices?`Closing prices up to ${dlong(src.prices)}: NSE/BSE, AMFI NAVs, Yahoo Finance (US), ECB dollar rate`:'Fetched each night');
}
// One holding: name and what it is, value (and the day's move), return on cost, weight, price alert
function rowHtml(h){
  const L=h.label||KL[h.kind], gainOf=x=>x.cost?x.value-x.cost:null;
  const ret=g=>g==null?'<span class="muted">—</span>':`<span class="${sign(g)}">${signed(g)}</span><small class="${sign(g)}">${pct(g/h.cost*100)}</small>`;
  const name=(sub,title)=>`<td><div class="hname"><i style="background:${KC[h.kind]}"></i><div><b>${esc(h.name)}</b><small${title?` title="${esc(title)}"`:''}>${sub}</small></div></div></td>`;
  const wt=`<td class="wide"><span class="wcell"><span class="wbar"><i style="width:${Math.max(2,(h.weight||0)/HMAXW*100)}%"></i></span><span style="min-width:44px">${((h.weight||0)*100).toFixed(1)}%</span></span></td>`;
  const val=small=>`<td>${money(h.value)}${small||'<small>&nbsp;</small>'}</td>`;
  const tr=cells=>`<tr data-key="${esc(h.key)}">${cells}</tr>`, x=h.extra||{};
  if(['retirement','cash','other'].includes(h.kind))
    return tr(name(`${esc(L)} · as of ${dlong(h.as_of)}`)+val()+`<td>${ret(gainOf(h))}</td>`+wt+`<td class="wide al">${stopCell(h)}</td>`);
  if(h.kind==='epf')
    return tr(name(`Updated ${dlong(h.day)}`,`${x.source==='passbook'?'EPFO passbook':'EPFO text'} of ${dlong(h.day)}${x.due_month?', after the '+x.due_month+' contribution':''}. This year’s interest is added once EPFO credits it.`)
      +val()+`<td><span class="muted">—</span></td>`+wt+`<td class="wide al">${stopCell(h)}</td>`);
  if(h.kind==='ppf')
    return tr(name(`${x.rate}% a year · ${money(x.accrued)} interest so far this year`,`Balance ${money(x.balance)} + ${money(x.accrued)} interest this year (credited 31 Mar). ${x.from_statement?'From the bank statement of '+dlong(h.as_of)+' + deposits since':'From your balance of '+dlong(h.as_of)}.`)
      +val()+`<td>${ret(gainOf(h))}</td>`+wt+`<td class="wide al">${stopCell(h)}</td>`);
  if(h.kind==='fd')
    return tr(name(`${x.rate}% · ${x.assumed_renewal?'renewal assumed':`matures ${dlong(x.maturity)}`}${x.renews?' · renews':''}`,x.assumed_renewal?'No new deposit advice yet, so the renewal is assumed':`Pays ${money(x.maturity_amount)} at maturity; valued with interest to date`)
      +val()+`<td>${ret(gainOf(h))}</td>`+wt+`<td class="wide al">${stopCell(h)}</td>`);
  const dayp=h.prev?(h.price/h.prev-1)*100:null, word=h.kind==='mf'||h.kind==='nps'?'units':'shares', mv=h.prev_value!=null?h.value-h.prev_value:null;
  return tr(name(`${esc(L)}${h.kind==='us_stock'?' · '+esc(h.key):''} · ${units(h.units)} ${word} · ${px(h.price,h.cur)}`,
      `${dayp==null?'Price of '+dshort(h.day):'Price '+pct(dayp,2)+' at the last close'}${x.folio?' · folio '+x.folio:''}`)
    +val(mv==null?`<small>${dshort(h.day)}</small>`:`<small class="${sign(mv)}">${signed(mv)} on the day</small>`)
    +`<td>${ret(gainOf(h))}</td>`+wt+`<td class="wide al">${stopCell(h)}</td>`);
}
const BELL='<svg class="i" viewBox="0 0 24 24"><path d="M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/></svg>';
function stopCell(h){
  const s=h.stop;
  if(['nps','epf','fd','ppf','retirement','cash','other'].includes(h.kind)) return '<span class="muted" title="No daily price to check">—</span>';
  if(LIVE&&stopEdit===h.key) return `<span class="stopedit"><input type="number" step="any" min="0" inputmode="decimal" aria-label="Alert price" value="${s?s.price:''}" placeholder="${h.cur||CUR.code} price"><button class="btn primary" data-a="save">Save</button>${s?'<button class="btn" data-a="clear">Remove</button>':''}<button class="btn" data-a="cancel">Cancel</button></span>`;
  if(!s) return LIVE?`<button class="alert add" data-a="edit" title="Get a Mac notification if it closes at or below a price">+ Alert</button>`:'<span class="muted">—</span>';
  const gap=(h.price/s.price-1)*100;
  const label=s.hit_at?`Reached ${dshort(s.hit_at)} · ${px(s.price,h.cur)}`:`Below ${px(s.price,h.cur)}`;
  const title=s.hit_at?'Closed at or below your alert price':`${gap.toFixed(1)}% above your alert price`;
  return LIVE?`<button class="alert ${s.hit_at?'hit':'set'}" data-a="edit" title="${title} · click to change">${BELL}${label}</button>`:`<span class="alert ${s.hit_at?'hit':'set'}" title="${title}">${BELL}${label}</span>`;
}
function bindStops(){
  if(!LIVE) return;
  document.querySelectorAll('#inv-rows [data-a]').forEach(b=>{ const tr=b.closest('tr'), key=tr.dataset.key, h=INV().holdings.find(x=>x.key===key);
    const save=async price=>{ try{ apply(await api('/api/stop',{key,kind:h.kind,name:h.name,price})); stopEdit=null; invest();
        toast(price==null?`Stop-loss removed for ${h.name}`:`Alert when ${h.name} closes at or below ${px(price,h.cur)}. Nothing is ever sold.`); }
      catch(e){ toast(e.message); } };
    b.onclick=()=>{ const a=b.dataset.a;
      if(a==='edit'){ stopEdit=key; invest(); const i=document.querySelector(`#inv-rows tr[data-key="${CSS.escape(key)}"] input`); if(i){ i.focus(); i.select(); } }
      else if(a==='cancel'){ stopEdit=null; invest(); }
      else if(a==='clear') save(null);
      else if(a==='save'){ const v=parseFloat(tr.querySelector('input').value); if(!(v>0)){ toast('Enter a price above zero'); return; } save(v); } }; });
  const i=document.querySelector('#inv-rows input');
  if(i) i.onkeydown=e=>{ if(e.key==='Enter') i.closest('tr').querySelector('[data-a="save"]').click(); if(e.key==='Escape'){ stopEdit=null; invest(); } };
}
document.querySelectorAll('#inv-mode button').forEach(b=>b.onclick=()=>{ invMode=b.dataset.m; invest(); });
document.querySelectorAll('#inv-range button').forEach(b=>b.onclick=()=>{ invRange=+b.dataset.r; invest(); });
document.querySelectorAll('#inv-rkind button').forEach(b=>b.onclick=()=>{ invRKind=b.dataset.k; invest(); });

// ---- Pages ------------------------------------------------------------------------------------------
const VIEWS=['home','overview','invest',...(LIVE?['sortout','reviews']:[])];
function show(v, push=true){ if(!VIEWS.includes(v)) v='home'; VIEW=v; hideTip();
  document.querySelectorAll('#tabs button, #tabbar button').forEach(b=>b.setAttribute('aria-selected',b.dataset.view===v));
  ['home','overview','invest','sortout','reviews'].forEach(x=>$('view-'+x).hidden=x!==v);
  if(push) history.replaceState(null,'',location.pathname+location.search+(v==='home'?'':'#'+v));
  if(v==='overview') monthly(); if(v==='home') home(); if(v==='invest') invest(); scrollTo(0,0); }
document.querySelectorAll('#tabs button, #tabbar button').forEach(b=>b.onclick=()=>show(b.dataset.view));
document.addEventListener('click',e=>{ const g=e.target.closest('[data-goto]'); if(g) show(g.dataset.goto); });
let api=null, toast=m=>{}, apply=()=>{};
let resizeT=null;
addEventListener('resize',()=>{ clearTimeout(resizeT); resizeT=setTimeout(()=>{ if(VIEW==='overview') monthly(); if(VIEW==='home') home(); if(VIEW==='invest') invest(); },120); });

// ---- Live mode (served by the Margin app): Sort out, Reviews, Sync now ---------------------------
if(LIVE){
  api=async(path,body)=>{ let r;
    try{ r=await fetch(path,{method:body?'POST':'GET',headers:{'X-Margin-Key':LIVE.key,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined}); }
    catch(e){ offline(); throw new Error('Can’t reach Margin right now.'); }
    if(r.status===423){ location.reload(); throw new Error('Margin is locked'); }
    const j=await r.json().catch(()=>({error:'Margin has stopped. Open it again from the Margin app.'}));
    if(!r.ok||j.error) throw new Error(j.error||'Something went wrong'); return j; };
  toast=(msg,undo)=>{ const t=$('toast'); t.innerHTML=`<span>${esc(msg)}</span>`+(undo?'<button class="linkbtn" id="toast-undo">Undo</button>':'');
    t.classList.add('on'); clearTimeout(toast.t); toast.t=setTimeout(()=>t.classList.remove('on'), undo?7000:3200);
    if(undo) $('toast-undo').onclick=async()=>{ t.classList.remove('on'); try{ await undo(); toast('Undone'); }catch(e){ toast(e.message); } }; };
  const act=async(path,body,msg,undo)=>{ try{ apply(await api(path,body)); if(msg) toast(msg,undo); return true; }catch(e){ toast(e.message); return false; } };
  let S=null;
  // If the server restarts (same address and key) the page just reconnects; if it's gone, say so plainly.
  function offline(){ const b=$('problems'); b.hidden=false;
    b.innerHTML='<li>Margin isn’t running right now. <button class="linkbtn" id="reconnect">Reconnect</button> · or open the Margin app.</li>';
    $('reconnect').onclick=()=>api('/api/state').then(st=>{ b.hidden=true; apply(st); }).catch(()=>{}); }
  document.querySelectorAll('.live-only').forEach(x=>x.hidden=false); $('syncbtn').hidden=false; $('rv-card').hidden=true;
  $('tx-hint').innerHTML='Wrong category? Click it to change it. Payments with a payee become a rule for past and future ones.';

  apply=function(state){ S=state; D=state.data; controls(); status(); draw(); networth(); review(); sortout(); reviews(); badges();
    if(VIEW==='home') home(); if(VIEW==='invest') invest(); };
  $('inv-run').onclick=async()=>{ const b=$('inv-run'); b.disabled=true; b.textContent='Analysing… (about 20–60 s)';
    try{ const st=await api('/api/invest/review',{kind:invRKind}); apply(st); toast(st.ran.note||'Analysis written'); }
    catch(e){ toast(e.message); } b.disabled=false; b.textContent='Analyse now'; };
  function badges(){ const so=S.payees.length+S.senders.length, rv=S.open_flags;
    ['so-badge','so-badge2'].forEach(id=>{ $(id).hidden=!so; $(id).textContent=so>999?'999+':so; });
    ['rv-badge','rv-badge2'].forEach(id=>{ $(id).hidden=!rv; $(id).textContent=rv; });
    const fb=$('flagbar'); fb.hidden=!rv;
    fb.innerHTML=rv?`<span class="bdot"></span><b>${rv} thing${rv>1?'s':''} to check</b><span class="muted">from your reviews</span><button class="link" data-goto="reviews">Open Reviews<svg class="i" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></button>`:''; }

  // ---------- Category menu (one component for Transactions and Sort out) ----------
  const menu={el:$('catmenu'), q:$('catq'), list:$('catlist'), items:[], active:0, onPick:null, anchor:null};
  function openMenu(anchor, dir, current, onPick){
    if(menu.anchor===anchor){ closeMenu(); return; }
    closeMenu(); Object.assign(menu,{anchor,onPick,dir,current}); anchor.setAttribute('aria-expanded','true');
    menu.q.value=''; renderMenu(); menu.el.hidden=false; placeMenu(); menu.q.focus({preventScroll:true}); }
  function placeMenu(){ const r=menu.anchor.getBoundingClientRect(), h=menu.el.offsetHeight, w=menu.el.offsetWidth;
    let top=r.bottom+6; if(top+h>innerHeight-8) top=Math.max(8,r.top-h-6);
    menu.el.style.top=top+'px'; menu.el.style.left=Math.max(8,Math.min(r.left,innerWidth-w-8))+'px'; }
  function renderMenu(){ const q=menu.q.value.trim().toLowerCase(); menu.items=[]; let html='';
    for(const [label,cats] of S.groups[menu.dir]){ const list=cats.filter(c=>!q||c.toLowerCase().includes(q)); if(!list.length) continue;
      html+=`<div class="grp">${esc(label)}</div>`;
      for(const c of list){ const i=menu.items.length; menu.items.push(c);
        html+=`<button class="it" role="option" data-i="${i}" aria-selected="false">${esc(c)}${c===menu.current?'<span class="ck">✓</span>':''}</button>`; } }
    menu.list.innerHTML=html||'<div class="none">No category matches.</div>';
    menu.active=0; highlight();
    menu.list.querySelectorAll('.it').forEach(b=>{ b.onmousemove=()=>{ if(menu.active!==+b.dataset.i){ menu.active=+b.dataset.i; highlight(false); } }; b.onclick=()=>pick(+b.dataset.i); }); }
  function highlight(scroll=true){ menu.list.querySelectorAll('.it').forEach(b=>b.setAttribute('aria-selected',+b.dataset.i===menu.active));
    const a=menu.list.querySelector(`.it[data-i="${menu.active}"]`); if(a&&scroll) a.scrollIntoView({block:'nearest'}); }
  function pick(i){ const c=menu.items[i], f=menu.onPick; closeMenu(); if(c&&f) f(c); }
  function closeMenu(){ if(menu.el.hidden) return; menu.el.hidden=true;
    if(menu.anchor&&menu.anchor.isConnected){ menu.anchor.setAttribute('aria-expanded','false'); menu.anchor.focus({preventScroll:true}); } menu.anchor=null; }
  menu.q.addEventListener('input',renderMenu);
  document.addEventListener('keydown',e=>{ if(e.key==='Escape'&&!menu.el.hidden){ closeMenu(); e.preventDefault(); } });
  menu.q.addEventListener('keydown',e=>{
    if(e.key==='ArrowDown'){ menu.active=Math.min(menu.items.length-1,menu.active+1); highlight(); e.preventDefault(); }
    else if(e.key==='ArrowUp'){ menu.active=Math.max(0,menu.active-1); highlight(); e.preventDefault(); }
    else if(e.key==='Enter'){ pick(menu.active); e.preventDefault(); }
    else if(e.key==='Escape'||e.key==='Tab'){ closeMenu(); e.preventDefault(); } });
  document.addEventListener('mousedown',e=>{ if(!menu.el.hidden&&!menu.el.contains(e.target)&&!(menu.anchor&&menu.anchor.contains(e.target))) closeMenu(); });
  addEventListener('scroll',e=>{ if(!menu.el.hidden&&!menu.el.contains(e.target)) placeMenu(); },true);
  addEventListener('resize',closeMenu);

  // Transactions: click a category to change it
  $('tx').addEventListener('click',e=>{ const b=e.target.closest('.pill-btn'); if(!b||!S) return;
    const t=D.txns.find(x=>x.id==b.dataset.id); if(!t) return;
    openMenu(b, t.dir, t.c, cat=>{ if(cat===t.c) return;
      if(t.p){ let changed=[];
        api('/api/rule',{match:t.p,category:cat,dir:t.dir,txn:t.id}).then(st=>{ changed=st.changed||[]; apply(st);
          toast(`All payments to ${t.p} → ${cat}`,async()=>{ await api('/api/rule/delete',{i:0}); apply(await api('/api/txns/restore',{changes:changed})); }); })
          .catch(e=>toast(e.message)); }
      else act('/api/txn',{id:t.id,category:cat},`This payment → ${cat} (it has no payee name, so just this one)`); }); });

  // Names you give payees (payees.py): every payment to that payee, past and future, shows it. key: the payee as
  // the page groups it (its name, lowercased); '' takes the name back. Undo puts the names back as they were.
  const namePayee=(key,name,shown,el)=>{ name=name.trim(); if(el) el.disabled=true;
    return api('/api/payee/name',{key,name}).then(st=>{ const before=st.before||{}; apply(st);
      if(Object.keys(before).length) toast(name?`Named “${name}”: every payment to ${shown}`:`${shown} has its own name back`,
        ()=>api('/api/payee/names',{names:before}).then(apply)); })
      .catch(e=>{ if(el) el.disabled=false; toast(e.message); }); };
  // Transactions: click a payee's name to name it (Enter saves, Esc leaves it)
  $('tx').addEventListener('click',e=>{ const b=e.target.closest('.nmbtn'); if(!b||!S) return;
    const t=D.txns.find(x=>x.id==b.dataset.id); if(!t) return;
    const was=t.w?t.p:'', inp=document.createElement('input');
    inp.className='nmedit'; inp.value=was; inp.placeholder=disp(t.p); inp.maxLength=48; inp.setAttribute('aria-label','Name this payee');
    b.replaceWith(inp); inp.focus(); inp.select();
    let done=false; const finish=save=>{ if(done) return; done=true; const v=inp.value.trim();
      if(save&&v!==was) namePayee(t.p.toLowerCase(),v,disp(t.w||t.p)); else drawTx(); };
    inp.onkeydown=ev=>{ if(ev.key==='Enter'||ev.key==='Escape'){ ev.preventDefault(); finish(ev.key==='Enter'); } };
    inp.onblur=()=>finish(true); });

  // ---------- Sort out ----------
  const SPARK_I='<svg class="i" viewBox="0 0 24 24"><path d="M12 3.5l1.9 5 5 1.9-5 1.9-1.9 5-1.9-5-5-1.9 5-1.9z"/></svg>';
  // the AI the person chose (llm.py): "Claude", "Codex", "Grok", "LM Studio"…; "AI" in the demo. AIC starts a sentence.
  const AI=()=>(S&&S.ai&&S.ai.name)||'AI', AIC=()=>{ const n=AI(); return n.charAt(0).toUpperCase()+n.slice(1); };
  // why a category is suggested (suggest.py says where it came from; 'claude' is the id the AI's guesses have always had)
  const sugWhy=s=>s.src==='claude'?`${AIC()}’s guess${s.reason?': '+s.reason:''}`:s.src==='similar'?`Like ${disp(s.like)}, which you sorted`
    :s.src==='name'?`The name says “${s.word}”`:s.src==='monthly'?'About the same amount every month'
    :s.src==='person'?'Looks like a person':'One you use often';
  const expanded=new Set();
  function sortout(){
    // progress meter: share of the last 12 months' spending (by amount) that has a real category
    const since=lastMonths(12,nowM)[0]; let tot=0, unc=0, n=0;
    for(const t of D.txns){ if(t.dir!=='out'||!counted(t)||ym(t.d)<since) continue; tot+=t.a; if(t.c==='Other spending'){ unc+=t.a; n++; } }
    const pct=tot?(unc?Math.min(99,Math.floor((tot-unc)/tot*100)):100):100;   // never says 100% while something is left
    $('so-pct').textContent=pct+'%'; $('so-fill').style.width=pct+'%';
    $('so-meter-text').textContent=unc?`${n.toLocaleString(LOC)} payment${n===1?'':'s'} (${money(unc)}) in the last 12 months still have no category.`+(S.senders.length?` ${S.senders.length} sender${S.senders.length>1?'s':''} to decide on below.`:''):'Every payment in the last 12 months has a category.';

    aiSort();
    const c=S.counts;
    $('so-p-title').textContent=`Payees to sort · ${S.payees.length.toLocaleString(LOC)}`;
    const nOut=S.payees.filter(p=>p.dir==='out').length, nIn=S.payees.length-nOut, dsel=$('so-dir');
    dsel.options[0].text=`Spending (${nOut})`; dsel.options[1].text=`Income (${nIn})`; dsel.options[2].text=`Both (${S.payees.length})`;
    // open on the side that has something to sort (the badge counts both)
    if(!dsel.dataset.touched){ dsel.value=nOut?'out':nIn?'in':'out'; }
    $('so-nameless').textContent=c.nameless?`${c.nameless} more payments have no payee name in the alert, so no rule can match them. Change those one by one from Transactions on the Overview.`:'';
    const q=$('so-q').value.toLowerCase(), df=$('so-dir').value, by=$('so-sort').value;
    const list=S.payees.filter(p=>(!df||p.dir===df)&&(!q||p.name.toLowerCase().includes(q)))
      .sort((a,b)=>by==='n'?(b.n-a.n||b.total-a.total):(b.total-a.total||b.n-a.n)).slice(0,300);
    // one row per payee: who, how often, one-tap suggestions (why the first one, on hover for all), "Other…" for the
    // full list, and what "Matches" (shorten it to cover variations)
    const rows=list.map(p=>{ const i=S.payees.indexOf(p), k=p.key+'|'+p.dir, open=expanded.has(k), sg=p.sugs||[], best=sg[0];
      const pays=open?D.txns.filter(t=>t.dir===p.dir&&t.p.toLowerCase()===p.key).slice(0,8):[];
      return `<div class="sorow pay" data-i="${i}"><span class="av">${esc(initials(p.name))}</span>
        <div class="main"><div class="top2"><button class="namebtn" aria-expanded="${open}" title="Show its payments"><span>${esc(disp(p.name))}</span><span class="car">▶</span></button>
            <span class="total${p.dir==='in'?' in-amt':''}">${p.dir==='in'?'+':'−'}${money(p.total)}</span></div>
          <div class="meta">${p.n} payment${p.n==1?'':'s'} · last on ${dyear(p.last)}${p.dir==='in'?' · money in':''}${p.was?` · <span title="What the bank’s alert calls it">${esc(p.was.slice(0,2).map(disp).join(', '))}${p.was.length>2?` +${p.was.length-2}`:''}</span>`:''}</div>
          <div class="sugs">${sg.map((s,j)=>`<button class="sug${j===0&&s.src!=='often'?' best':''}" data-cat="${esc(s.cat)}" title="${esc(sugWhy(s))}">${s.src==='claude'?SPARK_I:''}${esc(s.cat)}</button>`).join('')}<button class="sug more" aria-haspopup="listbox" aria-expanded="false">${sg.length?'Other…':'Choose category'}</button></div>
          <div class="adv">${best&&best.src!=='often'?`<span>${esc(sugWhy(best))}</span>`:''}<label class="matchf">Name <input class="nm" value="${p.was?esc(p.name):''}" placeholder="Who is this?" maxlength="48" aria-label="Name this payee" title="Shown on every payment to this payee, past and future. Stays on this Mac."></label><label class="matchf">Matches <input class="m" value="${esc(p.name)}" aria-label="Match on" title="Shorten it to cover variations, e.g. swiggy"></label></div>
          ${open?`<div class="pays">${pays.map(t=>`<span>${dyear(t.d)} · ${money(t.a)}${orig(t)} · ${esc(t.acct)}</span>`).join('')||'No payments found.'}</div>`:''}</div></div>`; });
    $('so-payees').innerHTML=rows.length?rows.join(''):`<p class="empty">${S.payees.length?'No payees match the filter.':'Every payee has a category.'}</p>`;
    $('so-payees').querySelectorAll('.namebtn').forEach(b=>b.onclick=()=>{ const p=S.payees[+b.closest('.sorow').dataset.i], k=p.key+'|'+p.dir;
      expanded.has(k)?expanded.delete(k):expanded.add(k); sortout(); });
    // a tap (or a pick from the full list) becomes a rule: past and future payments to that payee follow
    const sortPayee=(row,cat)=>{ const p=S.payees[+row.dataset.i], match=row.querySelector('input.m').value.trim();
      if(!match||!p.name.toLowerCase().includes(match.toLowerCase())){ toast('“Matches” must be part of the payee name'); return; }
      const btns=row.querySelectorAll('.sug'); btns.forEach(b=>b.disabled=true);
      act('/api/rule',{match,category:cat,dir:p.dir},`${disp(match)} → ${cat}`,()=>api('/api/rule/delete',{i:0}).then(apply))
        .then(ok=>{ if(!ok) btns.forEach(b=>b.disabled=false); }); };
    $('so-payees').querySelectorAll('.sug[data-cat]').forEach(b=>b.onclick=()=>sortPayee(b.closest('.sorow'),b.dataset.cat));
    $('so-payees').querySelectorAll('input.nm').forEach(inp=>{ const p=S.payees[+inp.closest('.sorow').dataset.i];
      inp.onkeydown=e=>{ if(e.key==='Enter'){ e.preventDefault(); inp.blur(); } };
      inp.onchange=()=>namePayee(p.key,inp.value,p.was?p.was.map(disp).join(', '):disp(p.name),inp); });
    $('so-payees').querySelectorAll('.sug.more').forEach(btn=>btn.onclick=()=>{ const row=btn.closest('.sorow');
      openMenu(btn, S.payees[+row.dataset.i].dir, null, cat=>sortPayee(row,cat)); });

    $('so-s-title').textContent=`Not counted yet · ${S.senders.length}`;
    $('so-s-card').hidden=!S.senders.length;
    const hideAll=$('so-hideall'); hideAll.hidden=S.senders.length<2; hideAll.textContent=`Hide all ${S.senders.length}`;
    hideAll.onclick=()=>{ const keys=S.senders.map(s=>s.key); hideAll.disabled=true;
      act('/api/senders',{keys,action:'ignore'},`${keys.length} senders hidden (still not counted)`,()=>api('/api/senders',{keys,action:'unignore'}).then(apply))
        .then(()=>{ hideAll.disabled=false; }); };
    $('so-senders').innerHTML=S.senders.length?S.senders.map((s,i)=>`<div class="sorow" data-i="${i}"><span class="av">${esc(initials(s.key))}</span>
      <div class="main"><b>${esc(s.key)}</b><div class="meta">${s.n} message${s.n==1?'':'s'} · ${s.pay} with an amount</div>${S.sorting.bank_hints[s.key]?`<div class="ai-hint">${esc(AIC())}: may be a bank. ${esc(S.sorting.bank_hints[s.key])}</div>`:''}<div class="example">“${esc(s.example)}”</div></div>
      <div class="right acts"><button class="btn" data-a="ignore">Hide</button><button class="btn" data-a="trust">It's my bank</button></div></div>`).join('')
      :`<p class="empty">Nothing to decide.</p>`;
    $('so-senders').querySelectorAll('button').forEach(b=>b.onclick=()=>{ const s=S.senders[+b.closest('.sorow').dataset.i], a=b.dataset.a;
      b.closest('.acts').querySelectorAll('button').forEach(x=>x.disabled=true);
      act('/api/sender',{key:s.key,action:a},a==='trust'?`${s.key} counts from now on; its messages are read on the next sync`:`${s.key} hidden (still not counted)`,
          ()=>api('/api/sender',{key:s.key,action:a==='trust'?'untrust':'unignore'}).then(apply)); });
    $('so-queued').innerHTML=S.queued?`${S.queued} messages from senders you counted are waiting for the next sync. <button class="linkbtn" id="sync2">Sync now</button>`:'';
    if($('sync2')) $('sync2').onclick=startSync;

    const unread=D.review.filter(r=>!r.reason.startsWith("sender isn"));
    $('so-unread-card').hidden=!unread.length; $('so-unread-title').textContent=`Bank alerts Margin couldn't read · ${unread.length}`;
    $('so-unread').innerHTML=unread.map(r=>`<tr><td class="date">${dyear(r.ts)}</td><td>${esc(r.sender)}</td><td class="ex">${esc(r.snippet)}</td></tr>`).join('');

    $('so-choices').hidden=!(S.rules.length||S.trusted.length||S.ignored.length);
    $('so-rules').innerHTML=S.rules.length?S.rules.map(r=>`<tr><td>${esc(r.match)}</td><td><span class="pill">${esc(r.category)}</span></td><td class="num"><button class="btn" data-i="${r.i}">Remove</button></td></tr>`).join('')
      :`<tr><td colspan="3" class="empty">No rules yet.</td></tr>`;
    $('so-rules').querySelectorAll('button').forEach(b=>b.onclick=()=>act('/api/rule/delete',{i:+b.dataset.i},'Rule removed'));
    const chips=(el,list,action)=>{ el.innerHTML=list.length?list.map(k=>`<span class="chip">${esc(k)}<button title="Undo" aria-label="Undo ${esc(k)}" data-k="${esc(k)}">×</button></span>`).join(''):'<span class="hint">None</span>';
      el.querySelectorAll('button').forEach(b=>b.onclick=()=>act('/api/sender',{key:b.dataset.k,action})); };
    chips($('so-trusted'),S.trusted,'untrust'); chips($('so-ignored'),S.ignored,'unignore');
    if(S.ignored.length>12){ const el=$('so-ignored'), inner=el.innerHTML;
      el.innerHTML=`<details><summary>${S.ignored.length} senders · show</summary><div class="chips">${inner}</div></details>`;
      el.querySelectorAll('button').forEach(b=>b.onclick=()=>act('/api/sender',{key:b.dataset.k,action:'unignore'})); }
  }
  // ---------- AI sorting ----------
  const AIJOBS=[['audit','Mistakes fixed'],['unread','Payments read from alerts Margin couldn’t read'],
    ['doubles','Counted once (the same payment was reported twice)'],['payees','Payees found in the message'],['splits','Split bills (friends’ shares reduce the bill)'],['merged','Counted twice again (were two different payments)'],
    ['categories','Categories picked'],['senders','Senders hidden (not a bank)']];
  let aiPoll=null;
  function aiSort(){ const so=S.sorting, st=$('ai-status'), btn=$('ai-run');
    btn.hidden=!so.on||!S.ai.found; btn.disabled=so.running; btn.textContent=so.running?'AI is sorting…':'Ask AI to sort now';
    const when=so.last?dtime(so.last.at.replace('T',' ')):'';
    const who=esc(AIC()), here=S.ai.local?', on this Mac':'';
    st.innerHTML=!so.on?'AI sorting is off. To turn it on, run <code>margin setup-ai</code> (or set <code>"ai_sort": true</code> in settings.json).'
      :!S.ai.found?(why=>esc(why)+(/setup-ai/.test(why)?'':' Choose or fix it with <code>margin setup-ai</code>.'))(S.ai.why||`${AIC()} isn’t set up yet.`)
      :so.running?`${who} is checking your senders, unread alerts, double counts, categories and every transaction against its message. The first full check takes a while; you can keep using Margin.`
      :(so.last?`${who} sorts what rules can’t at every nightly sync${here}. Last run ${esc(when)}: ${so.last.applied?`${so.last.applied} change${so.last.applied===1?'':'s'}`:'nothing new to change'}${(()=>{ const t=so.decisions.filter(d=>d.applied&&!d.undone).length; return t?` (${t} so far)`:''; })()}. What it wasn’t sure about is left for you below. Undo anything it got wrong and it won’t do it again.`
        +(so.last.notes.length?`<br><span class="warn">${so.last.notes.map(esc).join(' · ')}</span>`:'')
        :`${who} sorts what rules can’t at every nightly sync${here}: hides shops and ads, reads alerts Margin couldn’t, counts double-reported payments once and picks categories. Undo anything it gets wrong.`);
    const L=so.decisions.filter(d=>d.applied), SG=so.decisions.filter(d=>d.verdict==='suggest');
    $('ai-list').innerHTML=(SG.length?`<details class="ai-group" open><summary><b>The AI suggests changing your own choices</b> · ${SG.length}</summary>
        <p class="hint">You set these yourself, so the AI didn’t change them.</p>
        <table class="ai-t"><tbody>${SG.map(d=>`<tr><td>${esc(d.label||'')}</td><td class="ex">${esc(d.reason||'')}</td>
          <td class="num"><div class="acts"><button class="btn" data-acc="${d.id}">Apply</button><button class="btn" data-dis="${d.id}">Keep mine</button></div></td></tr>`).join('')}</tbody></table></details>`:'')
      +AIJOBS.map(([job,title])=>{ const rows=L.filter(d=>d.job===job); if(!rows.length) return '';
      const live=rows.filter(d=>!d.undone).length;
      return `<details class="ai-group"><summary><b>${title}</b> · ${live}${live!==rows.length?` <span class="status">(${rows.length-live} undone)</span>`:''}</summary>
        <table class="ai-t"><tbody>${rows.map(d=>`<tr class="${d.undone?'undone':''}"><td>${esc(d.label||'')}</td><td class="ex">${esc(d.reason||'')}</td>
          <td class="num">${d.undone?'<span class="status">Undone</span>':`<button class="btn" data-ai="${d.id}">Undo</button>`}</td></tr>`).join('')}</tbody></table></details>`; }).join('');
    $('ai-list').querySelectorAll('[data-acc]').forEach(b=>b.onclick=()=>{ b.disabled=true; act('/api/ai/accept',{id:+b.dataset.acc},'Changed'); });
    $('ai-list').querySelectorAll('[data-dis]').forEach(b=>b.onclick=()=>{ b.disabled=true; act('/api/ai/dismiss',{id:+b.dataset.dis},'Kept yours'); });
    $('ai-list').querySelectorAll('[data-ai]').forEach(b=>b.onclick=()=>{ b.disabled=true; act('/api/ai/undo',{id:+b.dataset.ai},'Undone. The AI won’t make that change again.'); });
    clearInterval(aiPoll); aiPoll=null;
    if(so.running) aiPoll=setInterval(async()=>{ try{ const st2=await api('/api/state'); if(!st2.sorting.running){ apply(st2); toast(`${AIC()} finished: ${st2.sorting.last?st2.sorting.last.applied:0} changes`); } }catch(e){} },5000);
  }
  $('ai-run').onclick=()=>{ $('ai-run').disabled=true; act('/api/ai/run',{},null); };

  $('so-dir').addEventListener('input',()=>{ $('so-dir').dataset.touched='1'; });
  ['so-q','so-dir','so-sort'].forEach(id=>$(id).addEventListener('input',()=>S&&sortout()));

  // ---------- Reviews ----------
  let rvKind='daily', rvPick=null;
  const SEVL={high:'High',medium:'Worth a look',low:'FYI'};
  const flagHtml=f=>`<div class="flag ${f.fine?'fine':''}" data-k="${esc(f.key)}">
      <span class="ficon ${f.severity}" title="${SEVL[f.severity]||f.severity}"><svg class="i" viewBox="0 0 24 24"><path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17.5v.01"/></svg></span>
      <div><div class="t">${esc(f.title)}</div><div class="w">${esc(f.why)}</div>
        <div class="m">${esc(dtime(f.date.replace('T',' ')))} · ${esc(disp(f.payee)||'no payee name')} · ${money(f.amount)}${f.account?' · '+esc(f.account):''}</div></div>
      <div class="acts">${f.settled?'<span class="status">Counted once now</span>':f.fine?'<button class="btn" data-s="open">Undo “fine”</button>':'<button class="btn" data-s="fine">Looks fine</button>'}</div></div>`;
  function bindFlags(el){ el.querySelectorAll('.flag button').forEach(b=>b.onclick=()=>{ const k=b.closest('.flag').dataset.k;
    act('/api/flag',{key:k,state:b.dataset.s},b.dataset.s==='fine'?'Marked as fine':null,b.dataset.s==='fine'?()=>api('/api/flag',{key:k,state:'open'}).then(apply):null); }); }
  function reviews(){
    const ai=S.ai;
    $('rv-ai').innerHTML=!ai.on?'AI reviews are off (automatic checks only).'
      :!ai.found?`${esc(AIC())} isn’t set up, so reviews use the automatic checks only (<code>margin setup-ai</code>).`
      :`Written by ${esc(AI())} after the midnight sync${ai.local?', on this Mac: nothing leaves it':''}`;
    const seen=new Set(), open=[];
    for(const r of S.reviews) for(const f of r.flags) if(!f.fine&&f.severity!=='low'&&!seen.has(f.key)){ seen.add(f.key); open.push(f); }
    $('rv-h1').textContent=open.length?`${open.length} thing${open.length>1?'s':''} to check`:'All clear';
    $('rv-open').innerHTML=open.length?open.map(flagHtml).join(''):`<p class="empty" style="padding:0">Nothing to check. Anything a review flags shows up here until you mark it as fine.</p>`;
    bindFlags($('rv-open'));
    document.querySelectorAll('#rv-kind button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.k===rvKind));
    const mine=S.reviews.filter(r=>r.kind===rvKind);
    if(!mine.find(r=>r.id===rvPick)) rvPick=mine[0]?.id??null;
    $('rv-pick').innerHTML=mine.length?mine.map(r=>`<option value="${r.id}">${esc(r.label)}</option>`).join(''):'<option>No reviews yet</option>';
    $('rv-pick').disabled=!mine.length; if(rvPick!=null) $('rv-pick').value=rvPick;
    $('rv-run').textContent=`Review ${rvKind==='daily'?'yesterday':rvKind==='weekly'?'last week':'last month'} now`;
    const r=mine.find(x=>x.id===rvPick);
    if(!r){ $('rv-body').innerHTML=`<p class="empty">The first ${rvKind} review is written after the next midnight sync${rvKind==='weekly'?' on a Monday':rvKind==='monthly'?' on the 1st':''}. Or press “Review … now”.</p>`; return; }
    $('rv-body').innerHTML=`<div class="rv-head">${esc(r.headline)}</div>
      <div class="rv-meta">${esc(r.label)} · ${r.by_ai?'by AI':'automatic checks only'+(r.note?' ('+esc(r.note)+')':'')} · written ${new Date(r.created).toLocaleString(LOC,{dateStyle:'medium',timeStyle:'short'})}</div>
      <ul class="bul">${r.summary.map(b=>`<li>${esc(b)}</li>`).join('')}</ul>
      ${r.flags.length?`<div class="rv-flags"><h4>Flagged in this review · ${r.flags.length}</h4>${r.flags.map(flagHtml).join('')}</div>`:'<p class="hint">Nothing flagged in this period.</p>'}`;
    bindFlags($('rv-body'));
  }
  document.querySelectorAll('#rv-kind button').forEach(b=>b.onclick=()=>{ rvKind=b.dataset.k; rvPick=null; reviews(); });
  $('rv-pick').onchange=e=>{ rvPick=+e.target.value; reviews(); };
  $('rv-run').onclick=async()=>{ const b=$('rv-run'), label=b.textContent; b.disabled=true; b.textContent='Reviewing… (about 10–40 s)';
    try{ const st=await api('/api/review/run',{kind:rvKind}); rvPick=null; apply(st); toast(st.ran.note?`Written from automatic checks: ${st.ran.note}`:'Review written'); }
    catch(e){ toast(e.message); } b.disabled=false; b.textContent=label; };

  // ---------- Sync now, with progress ----------
  let polling=false;
  async function startSync(){ try{ await api('/api/sync',{}); }catch(e){ toast(e.message); return; } poll(); }
  async function poll(){ if(polling) return; polling=true; $('syncbtn').disabled=true; $('syncbar').hidden=false;
    const sleep=ms=>new Promise(r=>setTimeout(r,ms)); let seen=false, waited=0;
    while(true){ let p; try{ p=await api('/api/progress'); }catch(e){ toast(e.message); break; }
      if(p.running){ seen=true; const g=p.progress||{phase:'Starting',done:0,total:0};
        $('sync-text').textContent=g.total?`${g.phase} ${g.done.toLocaleString(LOC)}/${g.total.toLocaleString(LOC)}`:`${g.phase}…`;
        $('sync-fill').style.width=(g.total?Math.round(g.done/g.total*100):5)+'%'; }
      else if(seen||++waited>3) break;          // allow a few seconds for the job to start
      await sleep(1000); }
    polling=false; $('syncbtn').disabled=false; $('syncbar').hidden=true;
    try{ apply(await api('/api/state')); toast(D.status.ok?'Synced':'Sync finished with problems: see the top of the page'); }
    catch(e){ toast(e.message); } }
  $('syncbtn').onclick=startSync;

  // ---------- Lock: LIVE.lock minutes after you leave the page (tab, app, sleep), then Touch ID ----------
  if(LIVE.lock>0){
    const AWAY=LIVE.lock*60e3; let leftAt=null, timer=null;
    const here=()=>!document.hidden&&document.hasFocus();
    const lockNow=async()=>{ try{ await fetch('/api/lock',{method:'POST',headers:{'Content-Type':'application/json','X-Margin-Key':LIVE.key},body:'{}'}); }catch(e){}
      document.body.innerHTML=''; location.reload(); };          // nothing stays on the page
    const beat=()=>{ if(!here()) return; fetch('/api/heartbeat',{method:'POST',headers:{'Content-Type':'application/json','X-Margin-Key':LIVE.key},body:'{}'})
      .then(r=>r.json()).then(j=>{ if(j.locked) lockNow(); }).catch(()=>{}); };
    const away=()=>{ if(leftAt) return; leftAt=Date.now(); timer=setTimeout(lockNow,AWAY); };
    const back=()=>{ if(!here()) return; clearTimeout(timer); const long=leftAt&&Date.now()-leftAt>=AWAY; leftAt=null; long?lockNow():beat(); };
    addEventListener('blur',away); addEventListener('focus',back);
    document.addEventListener('visibilitychange',()=>document.hidden?away():back());
    setInterval(()=>{ if(leftAt&&Date.now()-leftAt>=AWAY) lockNow(); else beat(); },30e3);   // also catches sleep
    if(!here()) away(); else beat();
    $('lockbtn').hidden=false; $('lockbtn').onclick=lockNow;
  }

  api('/api/state').then(apply).catch(e=>toast(e.message));
  api('/api/progress').then(p=>{ if(p.running) poll(); }).catch(()=>{});
  setInterval(()=>{ api('/api/ping').catch(()=>{}); },60000);   // keeps Margin running while this tab is open
}
show(location.hash.slice(1)||'home', false);
</script></body></html>
"""
