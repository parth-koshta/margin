"""Ask an AI for a JSON answer: the one place Margin talks to an AI (sorting, reviews, investment analysis).

The person picks the AI in settings.json, "ai": {"provider": ..., "model": ...} (`margin setup-ai` writes it);
"ai_sort" and "ai_review" still turn the jobs on and off.

  claude             Claude Code (`claude -p`): no tools, no MCP servers, no hooks, no saved session. The default.
  codex              OpenAI's Codex CLI (`codex exec`): read-only sandbox, never asks for approval, its shell, web
                     search, apps, plugins, hooks, image and MCP tools off, no saved session (--ephemeral), no history.
  gemini             Google's Gemini CLI (`gemini -p`): every built-in tool, extension, MCP server, hook, skill and
                     subagent off; its coding-agent instructions replaced by Margin's. It keeps its own copy of each
                     run in ~/.gemini/tmp/…/chats (it has no switch for that; it deletes them after 30 days by default).
  openai-compatible  Any OpenAI-compatible chat endpoint: xAI (Grok), OpenAI, OpenRouter, Groq, Mistral, or a server
                     on this Mac such as LM Studio (which may pass requests on: Margin can't tell, so it never says
                     "on this Mac" for one). "base_url" and "model" in settings; the API key in the Keychain.
  ollama             A model on this Mac through Ollama (http://127.0.0.1:11434). Before each request Ollama itself is
                     asked whether the model is local (no remote_host); cloud models are refused.

The CLIs start in an empty folder outside Margin's data folder; `margin setup-ai` checks with a made-up file that a
CLI can't read files before using it. Every provider gets the same thing: Margin's instructions and the same JSON
the job built (message text with long numbers masked, payees, amounts, dates, categories). Each answer is checked
against the job's JSON schema; one that isn't JSON in that shape is asked for once more, then given up (BadAnswer),
so its items are left to the person. No answer at all (offline, signed out, a crash) is an AIError: asked again at
the next sync. Errors from a service are reported by HTTP status only, never with what it sent back.
"""
from __future__ import annotations

import contextlib
import glob
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import paths

PROVIDERS = ("claude", "codex", "gemini", "openai-compatible", "ollama")
CLI = {"claude": "claude", "codex": "codex", "gemini": "gemini"}
NAMES = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini", "ollama": "Ollama"}
COMPANY = {"claude": "Anthropic", "codex": "OpenAI", "gemini": "Google"}
OLLAMA_URL = "http://127.0.0.1:11434"
KEY_ACCOUNT = "ai_api_key"              # Keychain account, followed by the service's host: "ai_api_key api.x.ai"

# OpenAI-compatible services Margin knows by name (anything else works too, by its base URL).
SERVICES = [("xAI (Grok)", "https://api.x.ai/v1"), ("OpenAI", "https://api.openai.com/v1"),
            ("OpenRouter", "https://openrouter.ai/api/v1"), ("Groq", "https://api.groq.com/openai/v1"),
            ("Mistral", "https://api.mistral.ai/v1"), ("LM Studio, on this Mac", "http://localhost:1234/v1")]
HOSTS = {"api.x.ai": ("Grok", "xAI"), "api.openai.com": ("OpenAI", "OpenAI"), "openrouter.ai": ("OpenRouter", "OpenRouter"),
         "api.groq.com": ("Groq", "Groq"), "api.mistral.ai": ("Mistral", "Mistral AI")}


class AIError(Exception):
    """The AI couldn't be asked or didn't answer: not installed, signed out, offline, too slow."""


class BadAnswer(AIError):
    """It answered, twice, but not with JSON in the shape Margin asked for."""


# --- which AI -----------------------------------------------------------------------------------
def config(settings: dict) -> dict:
    ai = settings.get("ai") if isinstance(settings.get("ai"), dict) else {}
    return {"provider": str(ai.get("provider") or "claude").strip().lower(), "model": ai.get("model") or None,
            "base_url": str(ai.get("base_url") or "").strip(), "path": str(ai.get("path") or ""),
            "name": str(ai.get("name") or "")}


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _local(url: str) -> bool:
    """An address on this Mac: only these three names (any other name goes through the resolver)."""
    return _host(url) in ("localhost", "127.0.0.1", "::1")


def name(settings: dict) -> str:
    """What the page and messages call the AI: Claude, Codex, Gemini, Grok, Ollama, LM Studio…"""
    c = config(settings)
    if c["name"]:
        return c["name"]
    if c["provider"] == "openai-compatible":
        url = c["base_url"]
        if _host(url) in HOSTS:
            return HOSTS[_host(url)][0]
        if _local(url):
            return "LM Studio" if urllib.parse.urlsplit(url).port == 1234 else "your local model"
        return _host(url) or "AI"
    return NAMES.get(c["provider"], "AI")


def is_local(settings: dict) -> bool:
    """True only when nothing Margin hands the AI leaves this Mac: Ollama, with a model Ollama itself reports as local.
    (A server on localhost may forward requests anywhere, so an OpenAI-compatible one never counts.)"""
    c = config(settings)
    return c["provider"] == "ollama" and _ollama_where(c)[0] == "local"


def where(settings: dict) -> str:
    """Where what Margin hands the AI goes: "this Mac", "Anthropic's servers", "xAI's servers"…"""
    c = config(settings)
    if c["provider"] == "ollama":
        state, detail = _ollama_where(c)
        return {"local": "this Mac", "remote": f"{detail}, not this Mac"}.get(
            state, "Ollama on this Mac (not checked: it isn't answering)")
    if c["provider"] == "openai-compatible" and _local(c["base_url"]):
        return "a server on this Mac, which may pass it on elsewhere (Margin can't tell)"
    if c["provider"] == "openai-compatible":
        who = HOSTS.get(_host(c["base_url"]), ("", _host(c["base_url"]) or "that service"))[1]
        return f"{who}'s servers" + (", and the company running the model you picked" if who == "OpenRouter" else "")
    return f"{COMPANY.get(c['provider'], 'its company')}'s servers"


def find_cli(provider: str, settings: dict) -> str | None:
    """The CLI's path, also where the nightly job (launchd, with a bare PATH) can't look it up."""
    if provider not in CLI:
        return None
    return next((x for x in cli_places(provider, settings) if x and os.path.isfile(x) and os.access(x, os.X_OK)), None)


def cli_places(provider: str, settings: dict) -> list[str]:
    """Where to look, in order. Claude Code only where Margin has always looked: Claude is the default, so a copy
    found somewhere new would start getting data without the person choosing it."""
    prog, c, home = CLI[provider], config(settings), Path.home()
    own = c["path"] if c["provider"] == provider else ""
    if prog == "claude":
        return [own, settings.get("claude_path") or "", shutil.which("claude") or "", "/opt/homebrew/bin/claude",
                "/usr/local/bin/claude", str(home / ".claude/local/claude"), str(home / ".local/bin/claude")]
    return [own, shutil.which(prog) or "", f"/opt/homebrew/bin/{prog}", f"/usr/local/bin/{prog}",
            str(home / ".local/bin" / prog), *(str(home / d / prog) for d in (".npm-global/bin", ".bun/bin", ".volta/bin")),
            *sorted(glob.glob(str(home / ".nvm/versions/node/*/bin" / prog)), reverse=True)]


def missing(settings: dict, keys: bool = False) -> str:
    """Why the chosen AI can't be asked right now ('' when it can). keys=True also checks the Keychain."""
    c = config(settings)
    p, model, url = c["provider"], c["model"], c["base_url"]
    if p not in PROVIDERS:
        return f"Unknown AI \"{p}\" in settings.json (ai.provider: claude, codex, gemini, openai-compatible or ollama)."
    if p == "claude" and not find_cli(p, settings):
        return "Claude Code isn't installed where Margin can find it (set claude_path in settings.json)."
    if p in CLI and not find_cli(p, settings):
        return f"{NAMES[p]} isn't installed where Margin can find it (set ai.path in settings.json, or margin setup-ai)."
    if p == "openai-compatible":
        if not url:
            return "No address for the AI service: run margin setup-ai (or set ai.base_url in settings.json)."
        if urllib.parse.urlsplit(url).scheme != "https" and not _local(url):
            return f"{_host(url) or url} isn't an https address, so your data and API key would travel unencrypted."
        if not model:
            return f"No model chosen for {name(settings)}: run margin setup-ai (or set ai.model in settings.json)."
        if keys and not _local(url) and not api_key(url):
            return f"No API key for {_host(url)} in the Keychain: run margin setup-ai in Terminal."
    if p == "ollama":
        if url and not _local(url):
            return "Ollama must be on this Mac (ai.base_url localhost, 127.0.0.1 or ::1), so nothing leaves it."
        if not model:
            return "No Ollama model chosen: run margin setup-ai (or set ai.model to one from ollama list)."
        if _cloud_model(model):
            return f"{model} is an Ollama cloud model (it runs on ollama.com, not this Mac): pick a local one."
        if keys:                                           # ask Ollama itself, right before Margin sends anything
            state, detail = _ollama_where(c, fresh=True)
            if state == "remote":
                return f"{model} runs on {detail}, not this Mac (an Ollama cloud model): pick a local one."
            if state == "missing":
                return f"{model} isn't in this Mac's Ollama: ollama pull it, or run margin setup-ai."
            if state == "down":
                return "Ollama isn't answering on this Mac: open the Ollama app (or run ollama serve)."
    return ""


def ready(settings: dict) -> bool:
    """Cheap check for the page: the AI is set up (the Keychain isn't consulted)."""
    return not missing(settings)


def _cloud_model(model: str) -> bool:
    tag = str(model).lower()
    return tag.endswith(("-cloud", ":cloud")) or ":cloud-" in tag


_OLLAMA_SEEN: dict = {}


def _ollama_where(c: dict, fresh: bool = False) -> tuple[str, str]:
    """Ask Ollama about the model: ("local", ""), ("remote", host), ("missing", "") or ("down", ""). The page's answer
    is kept for a minute; right before a request (fresh) Ollama is always asked."""
    url, model = (c["base_url"] or OLLAMA_URL).rstrip("/"), c["model"]
    if not model or not _local(url):
        return "missing" if not model else "remote", _host(url)
    key, now = (url, model), time.time()
    if not fresh and key in _OLLAMA_SEEN and now - _OLLAMA_SEEN[key][0] < 60:
        return _OLLAMA_SEEN[key][1]
    try:
        info = _post(url + "/api/show", {"model": model, "name": model}, {}, 3, "Ollama")   # "name": older Ollama
        remote = info.get("remote_host") or info.get("remote_model")
        found = ("remote", str(info.get("remote_host") or "another computer")) if remote else ("local", "")
    except _HTTPProblem as e:
        found = ("missing", "") if e.code == 404 else ("down", "")
    except AIError:
        found = ("down", "")
    _OLLAMA_SEEN[key] = (now, found)
    return found


def api_key(url: str) -> str | None:
    from . import keychain
    return keychain.get_secret(f"{KEY_ACCOUNT} {_host(url)}")


# --- asking -------------------------------------------------------------------------------------
def ask(system: str, payload: dict, schema: dict, settings: dict, timeout: int = 300) -> dict:
    """The AI's answer to `payload` under `system`, checked against `schema`. Raises AIError / BadAnswer."""
    why = missing(settings, keys=True)
    if why:
        raise AIError(why)
    c = config(settings)
    call = {"claude": _claude, "codex": _codex, "gemini": _gemini, "openai-compatible": _openai,
            "ollama": _ollama}[c["provider"]]
    data = json.dumps(payload, ensure_ascii=False)
    problem = ""
    for _ in range(2):                     # a bad answer is asked for once more
        answer = call(c, system, data, schema, settings, timeout)
        if answer is None or (isinstance(answer, str) and not answer.strip()):
            raise AIError(f"{name(settings)} gave no answer.")    # nothing to judge: asked again at the next sync
        try:
            return check(parse(answer), schema)
        except ValueError as e:
            problem = str(e)
    raise BadAnswer(f"{name(settings)}'s answer didn't match what Margin asked for, twice ({problem[:120]}).")


def parse(answer) -> object:
    """JSON from what the AI said: as given, inside a ```json fence, or the outermost {...}."""
    if isinstance(answer, (dict, list)):
        return answer
    t = str(answer or "").strip()
    if not t:
        raise ValueError("no answer")
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, re.S)
    t = m.group(1) if m else t
    try:
        return json.loads(t)
    except ValueError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(t[i:j + 1])
        except ValueError:
            pass
    raise ValueError("the answer wasn't JSON")


def _is(v, t: str) -> bool:
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "integer":
        return (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, float) and v.is_integer())
    return isinstance(v, {"object": dict, "array": list, "string": str, "boolean": bool}.get(t, object))


def check(value, schema: dict, where_: str = "answer"):
    """value checked against the JSON schema (the subset Margin's schemas use) and tidied: optional fields that
    came back null are left out, whole floats become ints where an integer is asked for, a number where a string
    is asked for (an id) becomes that string. Raises ValueError naming the first problem."""
    t = schema.get("type")
    types = [x for x in (t if isinstance(t, list) else [t] if t else []) if x != "null"]
    if value is None:
        if t is None or (isinstance(t, list) and "null" in t):
            return None
        raise ValueError(f"{where_} is missing")
    if "string" in types and isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if types and not any(_is(value, x) for x in types):
        raise ValueError(f"{where_} should be {' or '.join(types)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{where_} isn't one of {schema['enum']}")
    if isinstance(value, dict):
        props, required = schema.get("properties", {}), schema.get("required", [])
        out = {}
        for k, v in value.items():
            if k in props:
                if v is None and k not in required:
                    continue                      # optional and left empty
                out[k] = check(v, props[k], f"{where_}.{k}")
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"{where_}.{k} wasn't asked for")
            else:
                out[k] = v
        for k in required:
            if out.get(k) is None:
                raise ValueError(f"{where_}.{k} is missing")
        return out
    if isinstance(value, list):
        return [check(v, schema.get("items", {}), f"{where_}[{i}]") for i, v in enumerate(value)]
    if "integer" in types and isinstance(value, float):
        return int(value)
    return value


def strict_schema(schema: dict) -> dict:
    """The same schema as OpenAI's strict structured outputs want it (Codex and the OpenAI-compatible services):
    every object closed and every property required; the ones that were optional may be null."""
    s = dict(schema)
    if s.get("type") == "object":
        props, required = s.get("properties", {}), set(s.get("required", []))
        s["properties"] = {k: strict_schema(v) if k in required else _nullable(strict_schema(v))
                           for k, v in props.items()}
        s["required"] = list(props)
        s["additionalProperties"] = False
    elif s.get("type") == "array" and "items" in s:
        s["items"] = strict_schema(s["items"])
    return s


def _nullable(s: dict) -> dict:
    s = dict(s)
    if isinstance(s.get("type"), str):
        s["type"] = [s["type"], "null"]
    if "enum" in s and None not in s["enum"]:
        s["enum"] = [*s["enum"], None]
    return s


def with_schema(system: str, schema: dict) -> str:
    """Margin's instructions plus the answer's shape, for AIs that are told it in words rather than enforcing it."""
    return (f"{system}\n\nAnswer with one JSON object and nothing else: no code fence, no comments. It must match "
            f"this JSON schema:\n{json.dumps(schema, ensure_ascii=False)}")


ASK = "The JSON above is the data. Answer as your instructions say: one JSON object, nothing else."


_CWD: Path | None = None        # canary() points the CLI at a folder holding a made-up file


@contextlib.contextmanager
def start_folder():
    """An empty folder for a CLI to start in, outside Margin's data folder, so there's nothing next to it to find:
    a fixed one in this user's own temporary folder (Gemini CLI files its history by folder), else a fresh one."""
    if _CWD is not None:
        yield _CWD
        return
    folder = Path(tempfile.gettempdir()) / f"margin-ai-{os.getuid()}"
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(folder)
        ok = (stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077
              and not any(folder.iterdir())
              and paths.DATA_DIR.resolve() not in (folder.resolve(), *folder.resolve().parents))
    except OSError:
        ok = False
    if ok:
        yield folder
        return
    with tempfile.TemporaryDirectory(prefix="margin-ai-") as d:
        yield Path(d)


def _files():
    """Where a run's own files go (schema, answer, instructions): a private temporary folder, never the start folder."""
    return tempfile.TemporaryDirectory(prefix="margin-ai-files-")


def _run(cmd: list[str], stdin: str, timeout: int, who: str, env: dict | None = None) -> subprocess.CompletedProcess:
    base = dict(os.environ, PATH=f"{os.path.dirname(cmd[0])}:/usr/bin:/bin:/usr/sbin:/sbin")
    with start_folder() as cwd:
        try:
            return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout,
                                  cwd=str(cwd), env={**base, **(env or {})})
        except subprocess.TimeoutExpired:
            raise AIError(f"{who} took too long ({timeout // 60} minutes).") from None


def _last_line(text: str) -> str:
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    return lines[-1][:200] if lines else ""


# --- Claude Code ----------------------------------------------------------------------------------
def claude_command(exe: str, system: str, schema: dict, model: str) -> list[str]:
    return [exe, "-p", "--no-session-persistence", "--tools", "", "--strict-mcp-config",
            "--settings", json.dumps({"disableAllHooks": True}), "--model", model,
            "--output-format", "json", "--json-schema", json.dumps(schema), "--system-prompt", system]


def _claude(c, system, data, schema, settings, timeout):
    model = c["model"] or settings.get("ai_model") or "sonnet"
    r = _run(claude_command(find_cli("claude", settings), system, schema, model), data, timeout, "Claude")
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise AIError(f"Claude Code didn't answer (exit {r.returncode}). {(r.stderr or r.stdout).strip()[:200]}") from None
    if not isinstance(out, dict) or out.get("is_error") or out.get("structured_output") is None:
        out = out if isinstance(out, dict) else {}
        raise AIError(f"Claude Code: {str(out.get('result') or out.get('subtype'))[:200]}")
    return out["structured_output"]              # Claude Code has already held it to the schema; checked again


# --- OpenAI Codex CLI ---------------------------------------------------------------------------
# `-c` overrides for the run only (the person's config.toml is untouched). The names are the [features] keys in
# codex-rs/features/src/lib.rs and the config schema (codex-rs/core/config.schema.json) as of 2026-11. Codex only logs
# a key it doesn't know, so an older or newer Codex could leave a tool on: `margin setup-ai` therefore checks with a
# made-up file that it can't read files (canary()) before Margin uses it.
CODEX_FEATURES_OFF = ("shell_tool", "unified_exec", "view_image", "apps", "plugins", "hooks", "image_generation",
                      "multi_agent", "multi_agent_v2", "memories", "computer_use", "browser_use", "browser_use_external",
                      "in_app_browser", "tool_suggest", "skill_search", "code_mode")
CODEX_OFF = ('approval_policy="never"', 'web_search="disabled"', 'history.persistence="none"', "mcp_servers={}",
             "project_doc_max_bytes=0", *(f"features.{f}=false" for f in CODEX_FEATURES_OFF))


def _toml_str(s: str) -> str:
    return json.dumps(s.replace("\x7f", ""), ensure_ascii=False)   # a JSON string is a TOML basic string


def codex_command(exe: str, system: str, schema_file: str, answer_file: str, model: str | None) -> list[str]:
    cmd = [exe, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "--json",
           "--output-schema", schema_file, "--output-last-message", answer_file]
    for kv in (*CODEX_OFF, "developer_instructions=" + _toml_str(system)):
        cmd += ["-c", kv]
    if model:
        cmd += ["--model", model]
    return [*cmd, "-"]                                # the data comes on stdin


def _codex_error(r: subprocess.CompletedProcess) -> str:
    for line in reversed((r.stdout or "").splitlines()):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") in ("error", "turn.failed"):
            msg = ev.get("message") or (ev.get("error") or {}).get("message")
            if msg:
                return str(msg)[:200]
    return _last_line(r.stderr)


def _codex(c, system, data, schema, settings, timeout):
    with _files() as tmp:
        schema_file, answer_file = Path(tmp) / "schema.json", Path(tmp) / "answer.json"
        schema_file.write_text(json.dumps(strict_schema(schema)))
        cmd = codex_command(find_cli("codex", settings), system, str(schema_file), str(answer_file), c["model"])
        r = _run(cmd, f"{data}\n\n{ASK}", timeout, "Codex")
        answer = answer_file.read_text() if answer_file.exists() else ""
    if r.returncode != 0:
        raise AIError(f"Codex didn't answer (exit {r.returncode}). {_codex_error(r)}".strip())
    return answer


# --- Gemini CLI -------------------------------------------------------------------------------------
GEMINI_TOOLS = ["run_shell_command", "glob", "grep_search", "search_file_content", "list_directory", "read_file",
                "read_many_files", "write_file", "replace", "ask_user", "write_todos", "save_memory", "activate_skill",
                "get_internal_docs", "enter_plan_mode", "exit_plan_mode", "update_topic", "complete_task",
                "google_web_search", "web_fetch", "list_mcp_resources", "read_mcp_resource", "tracker_create_task",
                "tracker_update_task", "tracker_get_task", "tracker_list_tasks", "tracker_add_dependency",
                "tracker_visualize"]
# Given to that one run as its system settings (GEMINI_CLI_SYSTEM_SETTINGS_PATH), which override the person's own.
GEMINI_SETTINGS = {
    "tools": {"core": [], "exclude": GEMINI_TOOLS},     # an empty allowlist: no built-in tools at all
    "mcp": {"allowed": ["none"]}, "hooksConfig": {"enabled": False}, "skills": {"enabled": False},
    "experimental": {"enableAgents": False}, "general": {"checkpointing": {"enabled": False}},
    "context": {"fileName": ["MARGIN-NO-CONTEXT.md"]},  # don't load anyone's GEMINI.md into Margin's question
    "privacy": {"usageStatisticsEnabled": False},
}


def gemini_command(exe: str, model: str | None) -> list[str]:
    cmd = [exe, "--prompt", ASK, "--output-format", "json", "--extensions", "none",
           "--allowed-mcp-server-names", "none"]
    return cmd + (["--model", model] if model else [])


def _gemini(c, system, data, schema, settings, timeout):
    with _files() as tmp:
        system_md, cfg = Path(tmp) / "system.md", Path(tmp) / "settings.json"
        system_md.write_text(with_schema(system, schema).replace("${", "$ {"))   # ${...} would be filled in
        cfg.write_text(json.dumps(GEMINI_SETTINGS))
        env = {"GEMINI_SYSTEM_MD": str(system_md), "GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(cfg),
               "GEMINI_CLI_TRUST_WORKSPACE": "true"}          # an empty folder of Margin's; headless can't ask
        r = _run(gemini_command(find_cli("gemini", settings), c["model"]), data, timeout, "Gemini", env)
    try:
        out = parse(r.stdout)
    except ValueError:
        out = None
    if not isinstance(out, dict):
        raise AIError(f"Gemini didn't answer (exit {r.returncode}). {_last_line(r.stderr or r.stdout)}".strip())
    err = out.get("error")
    if err:
        raise AIError(f"Gemini: {str(err.get('message') if isinstance(err, dict) else err)[:200]}")
    return out.get("response") or ""


# --- HTTP: OpenAI-compatible services and Ollama ----------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):      # never follow: the API key must go only where it was set up for
        return None


class _HTTPProblem(AIError):
    def __init__(self, code: int, text: str, about_format: bool = False):
        super().__init__(text)
        self.code, self.about_format = code, about_format


# What an HTTP error is reported as. Only the status and these words reach notes, sync.log and the page: never what
# the service sent back, which could echo the request.
HTTP_WORDS = {400: "the request was refused", 401: "the API key was refused", 403: "access was refused",
              404: "not found: check the address and the model's name", 408: "it timed out",
              413: "the request was too big", 422: "the request was refused", 429: "too many requests, or no credit left"}


def http_words(code: int) -> str:
    if 300 <= code < 400:
        return "it pointed somewhere else, and Margin doesn't follow that"
    return HTTP_WORDS.get(code) or ("the service had a problem" if code >= 500 else "the request was refused")


def _post(url: str, body: dict, headers: dict, timeout: int, who: str) -> dict:
    handlers = [_NoRedirect()] + ([urllib.request.ProxyHandler({})] if _local(url) else [])
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "margin", **headers})
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(4000).decode("utf-8", "replace")      # looked at only to see if it's about the JSON format
        except OSError:
            raw = ""
        about = bool(re.search(r"response_format|json_schema|json_object|structured|schema|strict", raw, re.I))
        raise _HTTPProblem(e.code, f"{who}: HTTP {e.code}, {http_words(e.code)}.", about) from None
    except TimeoutError:
        raise AIError(f"{who} took too long ({timeout // 60} minutes).") from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        if "timed out" in str(reason):
            raise AIError(f"{who} took too long ({timeout // 60} minutes).") from None
        raise AIError(f"{who} couldn't be reached ({str(reason)[:120]}).") from None
    except ValueError:
        raise AIError(f"{who} didn't answer with JSON.") from None


def openai_body(model: str, system: str, data: str, schema: dict, form: str) -> dict:
    body = {"model": model, "messages": [{"role": "system", "content": with_schema(system, schema)},
                                         {"role": "user", "content": data}]}
    if form == "json_schema":
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "answer", "schema": strict_schema(schema), "strict": True}}
    elif form == "json_object":
        body["response_format"] = {"type": "json_object"}
    return body


def _openai(c, system, data, schema, settings, timeout):
    who, url = name(settings), c["base_url"].rstrip("/") + "/chat/completions"
    key = api_key(c["base_url"])
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    forms = ("json_schema", "json_object", "plain")      # not every service or model takes a schema
    for i, form in enumerate(forms):
        try:
            out = _post(url, openai_body(c["model"], system, data, schema, form), headers, timeout, who)
            break
        except _HTTPProblem as e:
            if e.code in (400, 422) and i < len(forms) - 1 and e.about_format:
                continue
            raise AIError(str(e)) from None
    msg = ((out.get("choices") or [{}])[0] or {}).get("message") or {}
    if msg.get("refusal"):
        raise AIError(f"{who} declined to answer.")
    content = msg.get("content")
    if isinstance(content, list):                        # some services answer in parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def ollama_body(model: str, system: str, data: str, schema: dict, num_ctx: int | None = None) -> dict:
    prompt = with_schema(system, schema)
    need = (len(prompt) + len(data)) // 3 + 4096         # Ollama's small default context would cut the data off
    ctx = num_ctx or next((n for n in (8192, 16384, 32768, 65536) if n >= need), 65536)
    return {"model": model, "stream": False, "format": schema, "options": {"temperature": 0, "num_ctx": ctx},
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": data}]}


def _ollama(c, system, data, schema, settings, timeout):
    url = (c["base_url"] or OLLAMA_URL).rstrip("/") + "/api/chat"
    ai = settings.get("ai") if isinstance(settings.get("ai"), dict) else {}
    out = _post(url, ollama_body(c["model"], system, data, schema, ai.get("num_ctx")), {}, timeout, "Ollama")
    if out.get("remote_host") or out.get("remote_model"):   # checked before asking; this would mean it changed since
        raise AIError("Ollama passed the request to another computer (a cloud model), so its answer isn't used: "
                      "pick a local model with margin setup-ai.")
    if out.get("error"):
        raise AIError("Ollama couldn't answer.")
    return (out.get("message") or {}).get("content") or ""


def ollama_models(url: str = "") -> list[str]:
    """Models in the local Ollama (cloud ones left out); [] if it isn't running."""
    req = urllib.request.Request((url or OLLAMA_URL).rstrip("/") + "/api/tags")
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3) as r:
            models = json.loads(r.read().decode()).get("models", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [m["name"] for m in models if isinstance(m, dict) and m.get("name") and not _cloud_model(m["name"])
            and not m.get("remote_host")]


# --- margin setup-ai --------------------------------------------------------------------------------
TEST_SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["shop", "bank", "unsure"]},
                                                "reason": {"type": "string"}}, "required": ["verdict", "reason"]}
TEST_PAYLOAD = {"sender": "ACME STORE", "example": "Your ACME STORE order #1234 has shipped."}   # made up


def test(settings: dict) -> str:
    """Ask the AI one made-up question (none of the person's data). '' if it answered properly, else the problem."""
    try:
        ask("Say whether this sender is a shop or a bank. Answer only through the JSON schema.",
            TEST_PAYLOAD, TEST_SCHEMA, settings, timeout=180)
        return ""
    except AIError as e:
        return str(e)


CANARY_SCHEMA = {"type": "object", "properties": {"contents": {"type": "string"}, "reason": {"type": "string"}},
                 "required": ["contents", "reason"]}


def canary(settings: dict) -> str:
    """'' if the AI can't read files on this Mac; otherwise why Margin won't use it. It's asked for two made-up
    files: one in the folder it starts in and one elsewhere, by full path. Only a working tool could return them."""
    global _CWD
    token = "MARGIN-CANARY-" + secrets.token_hex(8).upper()
    with tempfile.TemporaryDirectory(prefix="margin-canary-") as here, \
            tempfile.TemporaryDirectory(prefix="margin-canary-") as away:
        far = Path(away) / "canary.txt"
        for f in (Path(here) / "canary.txt", far):
            f.write_text(token + "\n")
        _CWD = Path(here)
        try:
            answer = ask("This checks your access to files on this computer. If you can read files, read canary.txt in "
                         f"your current folder and {far}, and put their exact contents in 'contents'. If you can't "
                         "read files, set 'contents' to ''. Answer only through the JSON schema.",
                         {"files": ["canary.txt", str(far)]}, CANARY_SCHEMA, settings, timeout=180)
        except AIError as e:
            return f"Couldn't check that {name(settings)} can't read files on this Mac ({e}), so Margin won't use it."
        finally:
            _CWD = None
    if token[-16:] in json.dumps(answer):
        return (f"{name(settings)} read a file on this Mac when asked: its tools aren't all off in this version, so "
                "Margin won't hand it your data. Update it, or choose another AI. Nothing changed.")
    return ""


def _pick(prompt: str, n: int, default: int) -> int:
    while True:
        a = input(f"{prompt} [{default}]: ").strip()
        if not a:
            return default
        if a.isdigit() and 1 <= int(a) <= n:
            return int(a)
        print(f"Type a number from 1 to {n}.")


def setup(provider: str | None = None, model: str | None = None, base_url: str | None = None,
          run_test: bool = True) -> int:
    """`margin setup-ai`: choose the AI (the installed ones are detected), its model, and for an API service its
    address and key (typed hidden, kept in the Keychain). Then one made-up test question."""
    import getpass
    import sys
    from . import keychain, sync
    paths.ensure_data_dir()
    settings = sync.load_settings()
    cur = config(settings)
    interactive = sys.stdin.isatty()
    choices = ["claude", "codex", "gemini", "openai-compatible", "ollama", "off"]
    if not provider:
        if not interactive:
            print("Run margin setup-ai in your own Terminal, or pass --provider.")
            return 1
        local_models = ollama_models()
        found = {p: find_cli(p, settings) for p in CLI}
        rows = [("Claude", "Claude Code; sends to Anthropic", found["claude"]),
                ("Codex", "OpenAI's Codex CLI; sends to OpenAI", found["codex"]),
                ("Gemini", "Google's Gemini CLI; sends to Google", found["gemini"]),
                ("API service", "Grok, OpenAI, OpenRouter, Groq, Mistral or LM Studio, with an API key", None),
                ("Ollama", "a model on this Mac; nothing leaves it", None),
                ("None", "no AI: Margin's own rules and checks still run", None)]
        print("Margin can ask an AI to sort what its rules can't and to write your reviews. Which one?")
        for i, (label, what, path) in enumerate(rows, 1):
            state = ("installed" if path else "not installed") if i <= 3 else \
                (f"running, {len(local_models)} model{'s' if len(local_models) != 1 else ''}" if local_models
                 else "not running") if i == 5 else ""
            print(f"  {i}  {label:<12} {what}" + (f"  ({state})" if state else ""))
        now = choices.index(cur["provider"]) + 1 if cur["provider"] in choices and settings.get("ai_sort", True) else 1
        provider = choices[_pick("Pick a number", len(choices), now) - 1]
    provider = provider.lower()
    if provider in ("none", "off"):
        settings["ai_sort"] = settings["ai_review"] = False
        _save(settings)
        print("AI is off. Margin's own rules and checks still run. margin setup-ai turns it back on.")
        return 0
    if provider not in PROVIDERS:
        print(f"Unknown AI \"{provider}\". Choose one of: {', '.join(PROVIDERS)}, none.")
        return 1
    old = settings.get("ai") if isinstance(settings.get("ai"), dict) else {}
    ai: dict = dict(old) if cur["provider"] == provider else {}     # same AI again: keep its path, model...
    ai["provider"] = provider
    ai["model"] = model or ai.get("model") or None
    if provider in CLI:
        trial = {**settings, "ai": ai}
        if not find_cli(provider, trial):
            how = {"claude": "https://claude.com/claude-code", "codex": "npm install -g @openai/codex",
                   "gemini": "npm install -g @google/gemini-cli"}[provider]
            print(f"{NAMES[provider]} isn't installed. Install it ({how}), run `{CLI[provider]}` once to sign in, "
                  "then run margin setup-ai again.")
            return 1
        if model is None and interactive:
            default = ai["model"] or ((settings.get("ai_model") or "sonnet") if provider == "claude"
                                      else f"{NAMES[provider]}'s own default")
            ai["model"] = input(f"Model (Enter for {default}): ").strip() or ai["model"]
    elif provider == "openai-compatible":
        if not base_url and interactive:
            print("Which service?")
            for i, (label, url) in enumerate(SERVICES, 1):
                print(f"  {i}  {label:<24} {url}")
            print(f"  {len(SERVICES) + 1}  another one (type its OpenAI-compatible base URL)")
            n = _pick("Pick a number", len(SERVICES) + 1, 1)
            base_url = SERVICES[n - 1][1] if n <= len(SERVICES) else input("Base URL (ending in /v1): ").strip()
        if not base_url:
            print("Pass --base-url, e.g. https://api.x.ai/v1.")
            return 1
        scheme = urllib.parse.urlsplit(base_url).scheme
        if not _host(base_url) or scheme not in ("http", "https") or (scheme != "https" and not _local(base_url)):
            print(f"{base_url} isn't an https address (only localhost, 127.0.0.1 or ::1 may use http), so your data "
                  "and API key would travel unencrypted. Nothing saved.")
            return 1
        if _host(base_url) != _host(ai.get("base_url") or ""):
            ai["model"] = model or None                 # another service: its own model names
        ai["base_url"] = base_url.rstrip("/")
        if model is None and interactive:
            keep = f" (Enter for {ai['model']})" if ai["model"] else ""
            ai["model"] = input(f"Model name, as {name({'ai': ai})} lists it{keep}: ").strip() or ai["model"]
        if not _local(base_url):
            host = _host(base_url)
            if interactive:
                key = getpass.getpass(f"API key for {host} (typing is hidden"
                                      + ("; Enter keeps the saved one" if api_key(base_url) else "") + "): ").strip()
                if key:
                    keychain.set_secret(f"{KEY_ACCOUNT} {host}", key)
                    print(f"Saved in your macOS Keychain (service 'margin', account '{KEY_ACCOUNT} {host}').")
            if not api_key(base_url):
                print(f"No API key saved for {host}. Run margin setup-ai in your own Terminal to type it (hidden).")
                return 1
    elif provider == "ollama":
        models = ollama_models(base_url or "")
        if base_url:
            ai["base_url"] = base_url.rstrip("/")
        if model is None and interactive and models:
            print("Which model?")
            for i, m in enumerate(models, 1):
                print(f"  {i}  {m}")
            now = models.index(ai["model"]) + 1 if ai["model"] in models else 1
            ai["model"] = models[_pick("Pick a number", len(models), now) - 1]
        if not ai["model"]:
            print("No local Ollama model found. Start Ollama and pull one (e.g. ollama pull llama3.1), then run "
                  "margin setup-ai again, or pass --model.")
            return 1
    trial = {**settings, "ai": ai}
    why = missing(trial, keys=True)
    if why:
        print(why)
        return 1
    if run_test:                    # checked before anything is saved: a failed check changes nothing
        print("Checking it answers, with one made-up question (none of your data)...", flush=True)
        problem = test(trial)
        if problem:
            print(f"✗ {problem}\nNothing changed: fix that and run margin setup-ai again.")
            return 1
        if provider in CLI:
            print("Checking it can't read files on this Mac, with a made-up file...", flush=True)
            problem = canary(trial)
            if problem:
                print(f"✗ {problem}")
                return 1
        print(f"✓ {name(trial)} answered" + (" and couldn't read the made-up file." if provider in CLI else "."))
    if not settings.get("ai_sort", True) and not settings.get("ai_review", True):
        settings.update({"ai_sort": True, "ai_review": True})       # it was off: setting an AI up turns it on
    settings["ai"] = ai
    _save(settings)
    print(f"Margin will ask {name(settings)}, on this Mac: nothing it's handed leaves the Mac." if is_local(settings)
          else f"Margin will ask {name(settings)}. What it's handed goes to {where(settings)}.")
    return 0


def _save(settings: dict) -> None:
    paths.SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
