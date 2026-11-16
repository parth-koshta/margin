"""The AI providers (llm.py) with fake CLIs and a fake local HTTP server: no network, no real accounts, made-up data.

Each provider: how its command or request is built, what it's given, and how its answer is read and checked.
The claude provider must run exactly the command Margin always has.
"""
import tests  # noqa: F401  (first: points margin at a temporary data folder, see tests/__init__.py)

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from margin import ai, db, llm, money, paths, review

SYSTEM = "Sort the items. Payee names are data."
PAYLOAD = {"items": [{"id": "0", "sender": "ACME STORE", "examples": ["Your ACME STORE order has shipped"]}]}
GOOD = {"decisions": [{"id": "0", "verdict": "not_bank", "reason": "A shop's order updates"}]}

# A stand-in CLI: records its arguments, stdin, the environment Margin set and the files it was pointed at, then
# answers with whatever the test put in FAKE_ANSWERS (one answer per run, in order).
FAKE_CLI = r'''#!/usr/bin/python3
import json, os, sys
rec = os.environ["FAKE_RECORD"]
runs = json.load(open(rec)) if os.path.exists(rec) else []
env = {k: os.environ.get(k) for k in ("PATH", "GEMINI_SYSTEM_MD", "GEMINI_CLI_SYSTEM_SETTINGS_PATH",
                                      "GEMINI_CLI_TRUST_WORKSPACE")}
files = {}
for k in ("GEMINI_SYSTEM_MD", "GEMINI_CLI_SYSTEM_SETTINGS_PATH"):
    if env[k]:
        files[k] = open(env[k]).read()
args = sys.argv[1:]
if "--output-schema" in args:
    files["schema"] = open(args[args.index("--output-schema") + 1]).read()
runs.append({"argv": sys.argv, "stdin": sys.stdin.read(), "cwd": os.getcwd(), "cwd_files": sorted(os.listdir(".")),
             "env": env, "files": files})
json.dump(runs, open(rec, "w"))
answers = json.loads(os.environ["FAKE_ANSWERS"])
a = answers[min(len(runs), len(answers)) - 1]
if a.get("read_canary"):                     # an AI whose tools still work: it reads the file it was asked for
    got = open("canary.txt").read().strip() if os.path.exists("canary.txt") else ""
    a = {"file": json.dumps({"contents": got, "reason": "read it"}),
         "stdout": json.dumps({"response": json.dumps({"contents": got, "reason": "read it"})})}
if "--output-last-message" in args and "file" in a:
    open(args[args.index("--output-last-message") + 1], "w").write(a["file"])
sys.stdout.write(a.get("stdout", ""))
sys.stderr.write(a.get("stderr", ""))
sys.exit(a.get("exit", 0))
'''

SENDERS = ai.SENDERS_SCHEMA


class FakeCLI(unittest.TestCase):
    """Base: a fake executable named like the real CLI, in its own folder."""
    prog = "claude"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="margin-fake-cli-")
        self.exe = Path(self.dir) / self.prog
        self.exe.write_text(FAKE_CLI)
        self.exe.chmod(0o755)
        self.record = Path(self.dir) / "record.json"

    def answers(self, *answers):
        return mock.patch.dict(os.environ, {"FAKE_RECORD": str(self.record), "FAKE_ANSWERS": json.dumps(answers)})

    def runs(self) -> list:
        return json.loads(self.record.read_text()) if self.record.exists() else []


class ClaudeProvider(FakeCLI):
    prog = "claude"

    def settings(self, **ai_):
        return {"claude_path": str(self.exe), "ai_model": "sonnet", **({"ai": ai_} if ai_ else {})}

    def test_the_command_is_exactly_what_margin_always_ran(self):
        out = {"is_error": False, "structured_output": GOOD}
        with self.answers({"stdout": json.dumps(out)}):
            got, err = review.ask_ai(PAYLOAD, "sort", self.settings(), system=SYSTEM, schema=SENDERS, timeout=60)
        self.assertEqual((got, err), (GOOD, ""))
        run, = self.runs()
        exe = str(self.exe)
        self.assertEqual(run["argv"], [                          # as review.ask_claude built it before llm.py
            exe, "-p", "--no-session-persistence", "--tools", "", "--strict-mcp-config",
            "--settings", json.dumps({"disableAllHooks": True}), "--model", "sonnet",
            "--output-format", "json", "--json-schema", json.dumps(SENDERS), "--system-prompt", SYSTEM])
        self.assertEqual(run["stdin"], json.dumps(PAYLOAD, ensure_ascii=False))
        self.assertEqual(run["env"]["PATH"], f"{self.dir}:/usr/bin:/bin:/usr/sbin:/sbin")
        # it starts in an empty folder outside the data folder, with nothing next to it to find
        cwd, data = Path(run["cwd"]).resolve(), paths.DATA_DIR.resolve()
        self.assertNotIn(data, (cwd, *cwd.parents))
        self.assertEqual(run["cwd_files"], [])

    def test_the_review_uses_its_own_system_and_schema_and_ai_model(self):
        out = {"structured_output": {"headline": "Quiet week", "summary": ["Spent less"], "flags": []}}
        with self.answers({"stdout": json.dumps(out)}):
            got, err = review.ask_ai({"transactions": []}, "weekly", {**self.settings(), "ai_model": "opus"})
        self.assertEqual(err, "")
        argv = self.runs()[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--json-schema") + 1], json.dumps(review.SCHEMA))
        self.assertEqual(argv[argv.index("--system-prompt") + 1], review.system_for("weekly", self.settings()))

    def test_ai_model_setting_wins_when_set(self):
        with self.answers({"stdout": json.dumps({"structured_output": GOOD})}):
            llm.ask(SYSTEM, PAYLOAD, SENDERS, self.settings(provider="claude", model="haiku"))
        argv = self.runs()[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")

    def test_errors_read_as_before(self):
        with self.answers({"stdout": json.dumps({"is_error": True, "result": "Not logged in"})}):
            self.assertEqual(review.ask_ai(PAYLOAD, "sort", self.settings(), SYSTEM, SENDERS),
                             (None, "Claude Code: Not logged in"))
        with self.answers({"stdout": "oops", "exit": 3}):
            got, err = review.ask_ai(PAYLOAD, "sort", self.settings(), SYSTEM, SENDERS)
        self.assertIsNone(got)
        self.assertTrue(err.startswith("Claude Code didn't answer (exit 3)."))
        self.assertEqual(len(self.runs()), 2)              # failures to answer aren't retried (one run each)

    def test_not_installed(self):
        s = {"claude_path": "/nonexistent/claude"}
        with mock.patch.object(llm, "find_cli", return_value=None):
            self.assertEqual(review.ask_ai(PAYLOAD, "sort", s, SYSTEM, SENDERS), (None, llm.missing(s)))
            self.assertIn("Claude Code isn't installed", llm.missing(s))
            self.assertFalse(llm.ready(s))

    def test_a_bad_answer_is_asked_once_more(self):
        bad = {"stdout": json.dumps({"structured_output": {"decisions": [{"id": "0", "verdict": "maybe"}]}})}
        with self.answers(bad, {"stdout": json.dumps({"structured_output": GOOD})}):
            self.assertEqual(llm.ask(SYSTEM, PAYLOAD, SENDERS, self.settings()), GOOD)
        self.assertEqual(len(self.runs()), 2)

    def test_two_bad_answers_give_up(self):
        bad = {"stdout": json.dumps({"structured_output": "not an object"})}
        with self.answers(bad, bad, bad):
            with self.assertRaises(llm.BadAnswer):
                llm.ask(SYSTEM, PAYLOAD, SENDERS, self.settings())
        self.assertEqual(len(self.runs()), 2)


class CodexProvider(FakeCLI):
    prog = "codex"

    def settings(self, model=None):
        return {"ai": {"provider": "codex", "model": model, "path": str(self.exe)}}

    def test_command_runs_read_only_with_tools_and_history_off(self):
        with self.answers({"file": json.dumps(GOOD), "stdout": '{"type":"turn.completed"}\n'}):
            self.assertEqual(llm.ask(SYSTEM, PAYLOAD, SENDERS, self.settings("gpt-5-codex")), GOOD)
        run, = self.runs()
        argv = run["argv"]
        self.assertEqual(argv[1], "exec")
        for flag in ("--skip-git-repo-check", "--ephemeral", "--json"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5-codex")
        self.assertEqual(argv[-1], "-")                       # the data comes on stdin, never on the command line
        overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
        for kv in ('approval_policy="never"', 'web_search="disabled"', 'history.persistence="none"', "mcp_servers={}",
                   "features.shell_tool=false", "features.unified_exec=false", "features.apps=false",
                   "features.plugins=false", "features.hooks=false", "project_doc_max_bytes=0"):
            self.assertIn(kv, overrides)
        instructions = next(kv for kv in overrides if kv.startswith("developer_instructions="))
        self.assertEqual(json.loads(instructions.split("=", 1)[1]), SYSTEM)      # a TOML (= JSON) string
        self.assertTrue(run["stdin"].startswith(json.dumps(PAYLOAD, ensure_ascii=False)))
        schema = json.loads(run["files"]["schema"])
        self.assertEqual(schema, llm.strict_schema(SENDERS))
        self.assertFalse(schema["properties"]["decisions"]["items"]["additionalProperties"])
        self.assertNotIn("PAYEE", " ".join(argv))

    def test_no_model_means_codex_default(self):
        with self.answers({"file": json.dumps(GOOD)}):
            llm.ask(SYSTEM, PAYLOAD, SENDERS, self.settings())
        self.assertNotIn("--model", self.runs()[0]["argv"])

    def test_strict_nulls_are_dropped(self):
        answer = {"decisions": [{"id": "0", "verdict": "payment", "amount": 12.5, "currency": "USD",
                                 "direction": "out", "payee": "DEMO CAFE", "last4": None, "card": None,
                                 "reason": "A card spend"}]}
        with self.answers({"file": json.dumps(answer)}):
            got = llm.ask(SYSTEM, PAYLOAD, ai.UNREAD_SCHEMA, self.settings())
        self.assertNotIn("last4", got["decisions"][0])
        self.assertNotIn("card", got["decisions"][0])

    def test_an_error_event_is_reported(self):
        err = {"stdout": '{"type":"error","message":"You are not signed in. Run codex login."}\n', "exit": 1}
        with self.answers(err):
            with self.assertRaises(llm.AIError) as e:
                llm.ask(SYSTEM, PAYLOAD, SENDERS, self.settings())
        self.assertIn("not signed in", str(e.exception))
        self.assertNotIsInstance(e.exception, llm.BadAnswer)

    def test_names_and_where(self):
        s = self.settings()
        self.assertEqual((llm.name(s), llm.where(s), llm.is_local(s)), ("Codex", "OpenAI's servers", False))


class GeminiProvider(FakeCLI):
    prog = "gemini"

    def settings(self, model=None):
        return {"ai": {"provider": "gemini", "model": model, "path": str(self.exe)}}

    def test_command_and_lockdown(self):
        out = {"response": "```json\n" + json.dumps(GOOD) + "\n```", "stats": {}}
        with self.answers({"stdout": "Loaded cached credentials.\n" + json.dumps(out)}):
            self.assertEqual(llm.ask(SYSTEM, PAYLOAD, SENDERS, self.settings("flash")), GOOD)
        run, = self.runs()
        argv = run["argv"]
        self.assertEqual(argv[argv.index("--prompt") + 1], llm.ASK)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--extensions") + 1], "none")
        self.assertEqual(argv[argv.index("--allowed-mcp-server-names") + 1], "none")
        self.assertEqual(argv[argv.index("--model") + 1], "flash")
        self.assertEqual(run["stdin"], json.dumps(PAYLOAD, ensure_ascii=False))
        self.assertEqual(run["env"]["GEMINI_CLI_TRUST_WORKSPACE"], "true")
        cfg = json.loads(run["files"]["GEMINI_CLI_SYSTEM_SETTINGS_PATH"])
        self.assertEqual(cfg["tools"]["core"], [])                  # no built-in tools at all
        for tool in ("run_shell_command", "read_file", "google_web_search", "web_fetch", "write_file"):
            self.assertIn(tool, cfg["tools"]["exclude"])
        self.assertFalse(cfg["hooksConfig"]["enabled"])
        system_md = run["files"]["GEMINI_SYSTEM_MD"]
        self.assertTrue(system_md.startswith(SYSTEM))
        self.assertIn(json.dumps(SENDERS), system_md)
        self.assertFalse(Path(run["env"]["GEMINI_SYSTEM_MD"]).exists())     # its files are gone afterwards
        self.assertEqual(run["cwd_files"], [])                               # and weren't in its start folder
        self.assertNotIn(paths.DATA_DIR.resolve(), Path(run["env"]["GEMINI_SYSTEM_MD"]).resolve().parents)

    def test_error(self):
        with self.answers({"stdout": json.dumps({"error": {"message": "Quota exceeded"}}), "exit": 1}):
            with self.assertRaises(llm.AIError) as e:
                llm.ask(SYSTEM, PAYLOAD, SENDERS, self.settings())
        self.assertEqual(str(e.exception), "Gemini: Quota exceeded")

    def test_prose_instead_of_json_is_retried_then_given_up(self):
        prose = {"stdout": json.dumps({"response": "Sure! ACME STORE looks like a shop."})}
        with self.answers(prose, prose):
            with self.assertRaises(llm.BadAnswer):
                llm.ask(SYSTEM, PAYLOAD, SENDERS, self.settings())
        self.assertEqual(len(self.runs()), 2)


class _Server:
    """A fake OpenAI-compatible / Ollama server on 127.0.0.1 that records requests and plays back responses."""

    def __init__(self, *responses, routes=None):
        self.requests, self.responses, self.routes = [], list(responses), routes or {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self):
                q = outer.routes.get(self.path, outer.responses)
                code, body, headers = q.pop(0) if len(q) > 1 else q[0]
                data = json.dumps(body).encode() if not isinstance(body, bytes) else body
                self.send_response(code)
                for k, v in {"Content-Type": "application/json", **headers}.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({"path": self.path, "headers": dict(self.headers),
                                       "body": json.loads(self.rfile.read(n) or b"{}")})
                self._reply()

            def do_GET(self):
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": None})
                self._reply()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def chat(content) -> tuple:
    return 200, {"choices": [{"message": {"role": "assistant", "content": content}}]}, {}


class OpenAICompatible(unittest.TestCase):
    def test_local_server_gets_schema_and_no_key(self):
        srv = _Server(chat(json.dumps(GOOD)))
        try:
            s = {"ai": {"provider": "openai-compatible", "base_url": srv.url + "/v1", "model": "demo-model"}}
            with mock.patch.object(llm, "api_key", return_value=None):
                self.assertEqual(llm.ask(SYSTEM, PAYLOAD, SENDERS, s), GOOD)
        finally:
            srv.close()
        req, = srv.requests
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertNotIn("Authorization", req["headers"])
        body = req["body"]
        self.assertEqual(body["model"], "demo-model")
        self.assertEqual(body["messages"][1], {"role": "user", "content": json.dumps(PAYLOAD, ensure_ascii=False)})
        self.assertTrue(body["messages"][0]["content"].startswith(SYSTEM))
        fmt = body["response_format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertTrue(fmt["json_schema"]["strict"])
        self.assertEqual(fmt["json_schema"]["schema"], llm.strict_schema(SENDERS))
        self.assertFalse(llm.is_local(s))                  # a server on this Mac may pass it on: never "on this Mac"
        self.assertEqual(llm.where(s), "a server on this Mac, which may pass it on elsewhere (Margin can't tell)")

    def test_falls_back_when_a_service_takes_no_schema(self):
        refuse = (400, {"error": {"message": "response_format json_schema is not supported for this model"}}, {})
        srv = _Server(refuse, chat("Here you go:\n" + json.dumps(GOOD)))
        try:
            s = {"ai": {"provider": "openai-compatible", "base_url": srv.url + "/v1", "model": "small"}}
            with mock.patch.object(llm, "api_key", return_value=None):
                self.assertEqual(llm.ask(SYSTEM, PAYLOAD, SENDERS, s), GOOD)
        finally:
            srv.close()
        self.assertEqual([r["body"].get("response_format", {}).get("type") for r in srv.requests],
                         ["json_schema", "json_object"])

    def test_other_http_errors_are_errors_without_what_the_service_sent(self):
        srv = _Server((401, {"error": {"message": "Incorrect API key provided: xai-FAKE…0000. Your input: DEMO STORE"}},
                       {}))
        try:
            s = {"ai": {"provider": "openai-compatible", "base_url": srv.url + "/v1", "model": "m"}}
            with mock.patch.object(llm, "api_key", return_value=None):
                with self.assertRaises(llm.AIError) as e:
                    llm.ask(SYSTEM, PAYLOAD, SENDERS, s)
        finally:
            srv.close()
        self.assertEqual(str(e.exception), "your local model: HTTP 401, the API key was refused.")
        self.assertEqual(len(srv.requests), 1)

    def test_redirects_are_never_followed(self):
        srv = _Server((307, b"", {"Location": "http://127.0.0.1:9/elsewhere"}))
        try:
            s = {"ai": {"provider": "openai-compatible", "base_url": srv.url + "/v1", "model": "m"}}
            with mock.patch.object(llm, "api_key", return_value=None):
                with self.assertRaises(llm.AIError):
                    llm.ask(SYSTEM, PAYLOAD, SENDERS, s)
        finally:
            srv.close()

    def test_grok_over_https_with_the_key_from_the_keychain(self):
        s = {"ai": {"provider": "openai-compatible", "base_url": "https://api.x.ai/v1", "model": "grok-demo"}}
        sent = []

        def post(url, body, headers, timeout, who):
            sent.append((url, body, headers, who))
            return {"choices": [{"message": {"content": json.dumps(GOOD)}}]}
        with mock.patch.object(llm, "api_key", return_value="xai-FAKE-KEY-0000") as key, \
                mock.patch.object(llm, "_post", post):
            self.assertEqual(llm.ask(SYSTEM, PAYLOAD, SENDERS, s), GOOD)
        key.assert_called_with("https://api.x.ai/v1")
        url, body, headers, who = sent[0]
        self.assertEqual(url, "https://api.x.ai/v1/chat/completions")
        self.assertEqual(headers, {"Authorization": "Bearer xai-FAKE-KEY-0000"})
        self.assertEqual(who, "Grok")
        self.assertNotIn("xai-FAKE-KEY", json.dumps(body))
        self.assertEqual((llm.name(s), llm.where(s), llm.is_local(s)), ("Grok", "xAI's servers", False))

    def test_needs_https_a_model_and_a_key(self):
        base = {"provider": "openai-compatible", "model": "m"}
        self.assertIn("isn't an https address", llm.missing({"ai": {**base, "base_url": "http://api.example.com/v1"}}))
        self.assertIn("No model", llm.missing({"ai": {**base, "model": None, "base_url": "https://openrouter.ai/api/v1"}}))
        with mock.patch.object(llm, "api_key", return_value=None):
            s = {"ai": {**base, "base_url": "https://api.groq.com/openai/v1"}}
            self.assertEqual(llm.missing(s), "")             # the page's cheap check doesn't open the Keychain
            self.assertIn("No API key for api.groq.com", llm.missing(s, keys=True))
            with self.assertRaises(llm.AIError):
                llm.ask(SYSTEM, PAYLOAD, SENDERS, s)

    def test_names(self):
        def name(url):
            return llm.name({"ai": {"provider": "openai-compatible", "base_url": url, "model": "m"}})
        self.assertEqual([name(u) for _, u in llm.SERVICES], ["Grok", "OpenAI", "OpenRouter", "Groq", "Mistral",
                                                               "LM Studio"])
        self.assertEqual(name("https://api.example.com/v1"), "api.example.com")


class Ollama(unittest.TestCase):
    def setUp(self):
        llm._OLLAMA_SEEN.clear()

    def test_request_and_answer(self):
        srv = _Server(routes={"/api/show": [(200, {"model_info": {}, "details": {"family": "llama"}}, {})],
                              "/api/chat": [(200, {"message": {"role": "assistant", "content": json.dumps(GOOD)},
                                                   "done": True}, {})]})
        try:
            s = {"ai": {"provider": "ollama", "model": "llama3.1:8b", "base_url": srv.url}}
            self.assertEqual(llm.ask(SYSTEM, PAYLOAD, SENDERS, s), GOOD)
            self.assertEqual((llm.name(s), llm.where(s), llm.is_local(s)), ("Ollama", "this Mac", True))
        finally:
            srv.close()
        show, req = srv.requests[:2]
        self.assertEqual((show["path"], show["body"]["model"]), ("/api/show", "llama3.1:8b"))   # asked first
        self.assertEqual(req["path"], "/api/chat")
        body = req["body"]
        self.assertEqual((body["model"], body["stream"], body["format"]), ("llama3.1:8b", False, SENDERS))
        self.assertGreaterEqual(body["options"]["num_ctx"], 8192)       # room for the whole batch
        self.assertEqual(body["messages"][1]["content"], json.dumps(PAYLOAD, ensure_ascii=False))

    def test_only_on_this_mac(self):
        self.assertIn("pick a local one", llm.missing({"ai": {"provider": "ollama", "model": "gpt-oss:120b-cloud"}}))
        self.assertIn("must be on this Mac", llm.missing({"ai": {"provider": "ollama", "model": "m",
                                                                 "base_url": "https://ollama.example.com"}}))
        self.assertIn("No Ollama model", llm.missing({"ai": {"provider": "ollama"}}))

    def test_model_list_leaves_out_cloud_models(self):
        srv = _Server((200, {"models": [{"name": "llama3.1:8b"}, {"name": "gpt-oss:120b-cloud"},
                                        {"name": "qwen3:4b"}]}, {}))
        try:
            self.assertEqual(llm.ollama_models(srv.url), ["llama3.1:8b", "qwen3:4b"])
        finally:
            srv.close()
        self.assertEqual(llm.ollama_models("http://127.0.0.1:9"), [])    # not running


class Checking(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(llm.parse('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(llm.parse('Here it is: {"a": {"b": 2}} Hope that helps'), {"a": {"b": 2}})
        for bad in ("", "no json here", "{broken"):
            with self.assertRaises(ValueError):
                llm.parse(bad)

    def test_check(self):
        s = ai.UNREAD_SCHEMA
        ok = {"decisions": [{"id": 3, "verdict": "unsure", "amount": None, "reason": "Masked amount"}]}
        self.assertEqual(llm.check(ok, s), {"decisions": [{"id": "3", "verdict": "unsure", "reason": "Masked amount"}]})
        for bad in ({}, {"decisions": {}}, {"decisions": [{"id": "1", "reason": ""}]},
                    {"decisions": [{"id": "1", "verdict": "probably", "reason": ""}]},
                    {"decisions": [{"id": "1", "verdict": "payment", "amount": "12", "reason": ""}]},
                    {"decisions": [{"id": "1", "verdict": "payment", "card": "yes", "reason": ""}]}):
            with self.assertRaises(ValueError):
                llm.check(bad, s)
        flags = {"headline": "h", "summary": [], "flags": [{"txn_ids": [4.0, 7], "severity": "low", "title": "t",
                                                            "why": "w"}]}
        self.assertEqual(llm.check(flags, review.SCHEMA)["flags"][0]["txn_ids"], [4, 7])

    def test_strict_schema(self):
        s = llm.strict_schema(ai.UNREAD_SCHEMA)
        item = s["properties"]["decisions"]["items"]
        self.assertEqual(sorted(item["required"]), sorted(item["properties"]))
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(item["properties"]["verdict"]["type"], "string")            # required: not nullable
        self.assertEqual(item["properties"]["amount"]["type"], ["number", "null"])  # optional: may be null
        self.assertIn(None, item["properties"]["direction"]["enum"])
        self.assertEqual(ai.UNREAD_SCHEMA["properties"]["decisions"]["items"]["required"], ["id", "verdict", "reason"])


class Choosing(unittest.TestCase):
    def test_defaults_to_claude(self):
        self.assertEqual(llm.config({})["provider"], "claude")
        self.assertEqual(llm.config({"ai": {"provider": "claude", "model": None}})["model"], None)
        self.assertEqual(llm.name({}), "Claude")
        self.assertEqual(llm.where({}), "Anthropic's servers")
        self.assertIn("Unknown AI", llm.missing({"ai": {"provider": "skynet"}}))

    def test_the_cli_path_setting(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "gemini"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            self.assertEqual(llm.find_cli("gemini", {"ai": {"provider": "gemini", "path": str(exe)}}), str(exe))
            self.assertTrue(llm.ready({"ai": {"provider": "gemini", "path": str(exe)}}))

    def test_setup_without_a_terminal(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as said:
            exe = Path(d) / "codex"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            with mock.patch("sys.stdin.isatty", return_value=False), mock.patch.object(llm, "find_cli",
                                                                                      return_value=str(exe)):
                self.assertEqual(llm.setup(), 1)                                   # nothing to ask with
                self.assertEqual(llm.setup("codex", "gpt-demo", run_test=False), 0)
            saved = json.loads(paths.SETTINGS_PATH.read_text())
            self.assertEqual(saved["ai"], {"provider": "codex", "model": "gpt-demo"})
            with mock.patch("sys.stdin.isatty", return_value=False), mock.patch.object(llm, "api_key",
                                                                                      return_value=None):
                self.assertEqual(llm.setup("openai-compatible", "grok-demo", "https://api.x.ai/v1", False), 1)
            self.assertEqual(json.loads(paths.SETTINGS_PATH.read_text())["ai"]["provider"], "codex")   # unchanged
            with mock.patch("sys.stdin.isatty", return_value=False):
                self.assertEqual(llm.setup("none"), 0)
            saved = json.loads(paths.SETTINGS_PATH.read_text())
            self.assertEqual((saved["ai_sort"], saved["ai_review"], saved["ai"]["provider"]), (False, False, "codex"))
            with mock.patch("sys.stdin.isatty", return_value=False), mock.patch.object(llm, "find_cli",
                                                                                      return_value=str(exe)):
                self.assertEqual(llm.setup("codex", run_test=False), 0)      # AI was off: setting one up turns it on
            saved = json.loads(paths.SETTINGS_PATH.read_text())
            self.assertEqual((saved["ai_sort"], saved["ai_review"], saved["ai"]["model"]), (True, True, "gpt-demo"))
            paths.SETTINGS_PATH.unlink()
        self.assertIn("No API key saved for api.x.ai", said.getvalue())
        self.assertIn("Margin will ask Codex. What it's handed goes to OpenAI's servers.", said.getvalue())


class SortingWithBadAnswers(unittest.TestCase):
    def test_given_up_items_are_left_to_you_as_unsure(self):
        items = {"items": [{"id": "0"}, {"id": "1"}]}
        with mock.patch.object(llm, "ask", side_effect=llm.BadAnswer("Codex's answer didn't match, twice.")):
            ai.NOTES.clear()
            for schema in (ai.SENDERS_SCHEMA, ai.UNREAD_SCHEMA, ai.DOUBLES_SCHEMA, ai.CATEGORIES_SCHEMA,
                           ai.PAYEES_SCHEMA, ai.SPLITS_SCHEMA, ai.AUDIT_SCHEMA, ai.MERGED_SCHEMA):
                decisions, err = ai._ask(items, ai.BASE, schema, {})
                self.assertEqual(err, "")
                self.assertEqual([d["id"] for d in decisions], ["0", "1"])
                d = decisions[0]
                self.assertNotIn(d.get("verdict"), ("bank", "not_bank", "payment", "not_payment", "same", "different"))
                self.assertIn(d.get("confidence", "low"), ("low",))
                self.assertEqual(d.get("payee", ""), "")
                self.assertEqual(d.get("bill_id", ""), "")
        self.assertIn("2 items left for you", ai.NOTES[0])

    def test_failing_to_ask_leaves_items_for_next_time(self):
        with mock.patch.object(llm, "ask", side_effect=llm.AIError("Codex took too long (10 minutes).")):
            self.assertEqual(ai._ask({"items": [{"id": "0"}]}, ai.BASE, ai.SENDERS_SCHEMA, {}),
                             ([], "Codex took too long (10 minutes)."))

    def test_a_payees_batch_given_up_is_recorded_and_changes_nothing(self):
        conn = db.connect()
        try:
            conn.execute("DELETE FROM transactions")
            conn.execute("DELETE FROM ai_decisions")
            conn.execute("INSERT INTO transactions(uid, source, ts, amount, direction, raw, category) VALUES "
                         "('t-demo-1','sms','2026-11-02T10:00:00',450,'out','Rs.450 debited from a/c XX1234','Other spending')")
            with mock.patch.object(llm, "ask", side_effect=llm.BadAnswer("bad twice")):
                n, err = ai.job_payees(conn, {"currency": "INR"})
            self.assertEqual((n, err), (0, ""))
            self.assertEqual(conn.execute("SELECT verdict, applied FROM ai_decisions WHERE job='payees'").fetchall()[0][:],
                             ("none", 0))
            self.assertIsNone(conn.execute("SELECT counterparty FROM transactions").fetchone()[0])
        finally:
            conn.execute("DELETE FROM transactions")
            conn.execute("DELETE FROM ai_decisions")
            conn.commit()
            conn.close()

    def test_run_reports_when_the_ai_is_missing(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        s = {"ai_sort": True, "ai": {"provider": "gemini", "path": "/nonexistent/gemini"}}
        with mock.patch.object(llm, "find_cli", return_value=None):
            r = ai.run(conn, s)
        self.assertEqual(r["applied"], 0)
        self.assertEqual(r["notes"], [llm.missing(s)])
        self.assertIn("Gemini isn't installed", r["notes"][0])


class ReadByNotes(unittest.TestCase):
    """Payments the AI read start "[read by AI]"; ones stored before start "[read by Claude]". Both are read."""

    def test_both_forms(self):
        note = ai.fx_note("USD", 21.43, 1810.84, 84.5, "ECB rate for 2026-11-02")
        for who in ("[read by Claude]", "[read by AI]"):
            raw = who + note + " Transaction of USD 21.43 on Card XX9876 at DEMO STORE."
            m = ai.FX_NOTE_RE.match(raw)
            self.assertEqual((m.group(1), m.group(2), m.group(3)), (who, "USD", "21.43"))
            self.assertEqual(money.original(raw), ("USD", 21.43))
            self.assertTrue(review._foreign(raw, "INR"))
        self.assertEqual(ai.READ_BY_AI, "[read by AI]")

    def test_reconvert_keeps_each_rows_own_note(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        note = ai.fx_note("USD", 10, 800, 80, "an old flat rate")
        for i, who in enumerate(("[read by Claude]", "[read by AI]")):
            conn.execute("INSERT INTO transactions(uid, source, ts, amount, direction, raw, category) VALUES "
                         "(?,?,?,?,?,?,?)", (f"u{i}", "sms", "2026-11-02T10:00:00", 800, "out",
                                             f"{who}{note} Card XX9876 at DEMO STORE.", "Shopping"))
        with mock.patch("margin.fx.rate", return_value=(84.0, "ECB rate for 2026-11-02")):
            self.assertEqual(ai.reconvert(conn, {"currency": "INR"}), 2)
        rows = [r[0] for r in conn.execute("SELECT raw FROM transactions ORDER BY uid")]
        self.assertTrue(rows[0].startswith("[read by Claude] [USD 10 ≈ ₹840.00 at ₹84/USD, ECB rate for 2026-11-02"))
        self.assertTrue(rows[1].startswith("[read by AI] [USD 10 ≈ ₹840.00 at ₹84/USD, ECB rate for 2026-11-02"))
        self.assertTrue(all(r.endswith(" Card XX9876 at DEMO STORE.") for r in rows))


class SecurityFixes(FakeCLI):
    """The security review's points, each on made-up data."""
    prog = "codex"

    def setUp(self):
        super().setUp()
        llm._OLLAMA_SEEN.clear()
        if paths.SETTINGS_PATH.exists():
            paths.SETTINGS_PATH.unlink()

    def codex(self, model="gpt-demo"):
        return {"ai": {"provider": "codex", "model": model, "path": str(self.exe)}}

    def test_only_three_names_are_this_mac(self):
        for url in ("http://localhost:1234/v1", "http://127.0.0.1:1234/v1", "http://[::1]:1234/v1"):
            self.assertTrue(llm._local(url), url)
        for url in ("http://demo.localhost:1234/v1", "http://localhost.example.com/v1", "http://10.0.0.5:1234/v1"):
            self.assertFalse(llm._local(url), url)
            self.assertIn("isn't an https address",
                          llm.missing({"ai": {"provider": "openai-compatible", "base_url": url, "model": "m"}}))

    def test_an_ollama_model_that_ollama_says_is_remote_is_refused_before_anything_is_sent(self):
        srv = _Server(routes={"/api/show": [(200, {"remote_host": "https://ollama.com:443", "remote_model": "big"}, {})],
                              "/api/chat": [(200, {"message": {"content": json.dumps(GOOD)}}, {})]})
        try:
            s = {"ai": {"provider": "ollama", "model": "my-copy", "base_url": srv.url}}      # an `ollama cp` of one
            self.assertEqual(llm.missing(s), "")                                       # its name looks local
            self.assertIn("not this Mac", llm.missing(s, keys=True))
            self.assertFalse(llm.is_local(s))
            self.assertIn("not this Mac", llm.where(s))
            with self.assertRaises(llm.AIError):
                llm.ask(SYSTEM, PAYLOAD, SENDERS, s)
        finally:
            srv.close()
        self.assertNotIn("/api/chat", [r["path"] for r in srv.requests])

    def test_ollama_missing_down_and_a_remote_answer(self):
        srv = _Server(routes={"/api/show": [(404, {"error": "model 'nope' not found"}, {})]})
        try:
            self.assertIn("isn't in this Mac's Ollama",
                          llm.missing({"ai": {"provider": "ollama", "model": "nope", "base_url": srv.url}}, keys=True))
        finally:
            srv.close()
        self.assertIn("isn't answering",
                      llm.missing({"ai": {"provider": "ollama", "model": "m", "base_url": "http://127.0.0.1:9"}}, True))
        srv = _Server(routes={"/api/show": [(200, {"details": {}}, {})],
                              "/api/chat": [(200, {"remote_host": "https://ollama.com:443",
                                                   "message": {"content": json.dumps(GOOD)}}, {})]})
        try:
            with self.assertRaises(llm.AIError) as e:
                llm.ask(SYSTEM, PAYLOAD, SENDERS, {"ai": {"provider": "ollama", "model": "m", "base_url": srv.url}})
        finally:
            srv.close()
        self.assertIn("another computer", str(e.exception))
        self.assertNotIsInstance(e.exception, llm.BadAnswer)

    def test_no_answer_is_an_error_to_retry_not_unsure(self):
        bad = {"file": "Sure! Here is my answer"}
        with self.answers(bad, {"stdout": ""}):                 # second run: exits 0 but writes no answer
            ai.NOTES.clear()
            decisions, err = ai._ask({"items": [{"id": "0"}]}, ai.BASE, ai.SENDERS_SCHEMA, self.codex())
        self.assertEqual((decisions, err), ([], "Codex gave no answer."))
        self.assertEqual(ai.NOTES, [])
        with self.answers({"stdout": "", "exit": 0}):
            with self.assertRaises(llm.AIError) as e:
                llm.ask(SYSTEM, PAYLOAD, SENDERS, self.codex())
        self.assertNotIsInstance(e.exception, llm.BadAnswer)
        with self.answers({"stdout": "", "exit": 2}):
            decisions, err = ai._ask({"items": [{"id": "0"}]}, ai.BASE, ai.SENDERS_SCHEMA, self.codex())
        self.assertEqual(decisions, [])
        self.assertTrue(err.startswith("Codex didn't answer (exit 2)"))

    def test_claude_without_structured_output_is_an_error_as_before(self):
        with mock.patch.object(llm, "find_cli", return_value=str(self.exe)):
            with self.answers({"stdout": json.dumps({"subtype": "error_max_turns", "result": None})}):
                with self.assertRaises(llm.AIError) as e:
                    llm.ask(SYSTEM, PAYLOAD, SENDERS, {})
        self.assertEqual(str(e.exception), "Claude Code: error_max_turns")
        self.assertNotIsInstance(e.exception, llm.BadAnswer)

    def test_a_refusal_is_reported_without_its_words(self):
        def post(url, body, headers, timeout, who):
            return {"choices": [{"message": {"refusal": "I won't sort DEMO STORE for Jordan Lee"}}]}
        s = {"ai": {"provider": "openai-compatible", "base_url": "https://api.x.ai/v1", "model": "m"}}
        with mock.patch.object(llm, "api_key", return_value="xai-FAKE-KEY-0000"), mock.patch.object(llm, "_post", post):
            with self.assertRaises(llm.AIError) as e:
                llm.ask(SYSTEM, PAYLOAD, SENDERS, s)
        self.assertEqual(str(e.exception), "Grok declined to answer.")

    def test_the_address_is_checked_before_the_key_is_asked_for(self):
        def no_key(*a, **k):
            raise AssertionError("asked for a key")
        with contextlib.redirect_stdout(io.StringIO()) as said, mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("getpass.getpass", no_key), mock.patch("margin.keychain.set_secret", no_key):
            for url in ("http://api.example.com/v1", "http://demo.localhost:1234/v1", "ftp://api.example.com/v1"):
                self.assertEqual(llm.setup("openai-compatible", "m", url), 1)
        self.assertIn("isn't an https address", said.getvalue())
        self.assertFalse(paths.SETTINGS_PATH.exists() and "openai-compatible" in paths.SETTINGS_PATH.read_text())

    def test_setup_refuses_a_cli_that_can_read_files(self):
        answered = {"file": json.dumps({"verdict": "shop", "reason": "Order updates"})}
        with contextlib.redirect_stdout(io.StringIO()) as said, mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch.object(llm, "find_cli", return_value=str(self.exe)):
            with self.answers(answered, {"read_canary": True}):
                self.assertEqual(llm.setup("codex", "gpt-demo"), 1)
        self.assertIn("read a file on this Mac", said.getvalue())
        self.assertNotIn('"codex"', paths.SETTINGS_PATH.read_text())                  # nothing changed
        canary_run = self.runs()[1]
        self.assertEqual(canary_run["cwd_files"], ["canary.txt"])                     # it was asked for the file
        self.assertIn("canary.txt", canary_run["stdin"])
        self.assertFalse(Path(canary_run["cwd"]).exists())                            # and it's gone again

    def test_setup_saves_a_cli_that_cant(self):
        answered = {"file": json.dumps({"verdict": "shop", "reason": "Order updates"})}
        blind = {"file": json.dumps({"contents": "", "reason": "I can't read files"})}
        with contextlib.redirect_stdout(io.StringIO()) as said, mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch.object(llm, "find_cli", return_value=str(self.exe)):
            with self.answers(answered, blind):
                self.assertEqual(llm.setup("codex", "gpt-demo"), 0)
        self.assertIn("couldn't read the made-up file", said.getvalue())
        self.assertEqual(json.loads(paths.SETTINGS_PATH.read_text())["ai"], {"provider": "codex", "model": "gpt-demo"})
        self.assertEqual(len(self.runs()), 2)

    def test_setup_changes_nothing_when_the_test_question_fails(self):
        with contextlib.redirect_stdout(io.StringIO()) as said, mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch.object(llm, "find_cli", return_value=str(self.exe)):
            with self.answers({"stdout": '{"type":"error","message":"Not signed in"}\n', "exit": 1}):
                self.assertEqual(llm.setup("codex", "gpt-demo"), 1)
        self.assertIn("Nothing changed", said.getvalue())
        self.assertNotIn('"codex"', paths.SETTINGS_PATH.read_text())

    def test_claude_is_looked_for_only_where_it_always_was(self):
        claude = " ".join(llm.cli_places("claude", {}))
        for new in (".npm-global", ".bun", ".volta", ".nvm"):
            self.assertNotIn(new, claude)
        self.assertIn(".npm-global", " ".join(llm.cli_places("codex", {})))

    def test_the_start_folder_is_empty_and_outside_the_data_folder(self):
        data = paths.DATA_DIR.resolve()
        with llm.start_folder() as first:
            self.assertEqual(list(first.iterdir()), [])
            self.assertNotIn(data, (first.resolve(), *first.resolve().parents))
            (first / "left-behind.txt").write_text("made up")
            try:
                with llm.start_folder() as second:                  # not empty any more: a fresh one instead
                    self.assertNotEqual(second, first)
                    self.assertEqual(list(second.iterdir()), [])
            finally:
                (first / "left-behind.txt").unlink()


if __name__ == "__main__":
    unittest.main()
