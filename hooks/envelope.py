#!/usr/bin/env python3
"""Normalize Claude (snake_case) and Grok (camelCase) hook stdin.

Grok scans ~/.claude/settings.json by default, but its envelope uses
toolInput/toolName/sessionId/stopHookActive and read_file's path is
target_file. Every guard should go through HookInput so a silent miss
cannot look like an allow.

Self-test: python3 envelope.py --test
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sys
import time
from pathlib import Path

# Long mixed letter+digit runs: keys, tokens, hashes. Shared by every hook that
# writes text to disk or sends it off the machine.
TOKENISH = re.compile(r"(?<![\w/.-])(?=[\w+/=-]*\d)(?=[\w+/=-]*[A-Za-z])[\w+/=-]{24,}")
SECRET_SHAPES = re.compile(
    r"(?is)\b(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----)"
)
PEM_BLOCK = re.compile(r"-----BEGIN [^\n]*?-----.*?-----END [^\n]*?-----", re.S)
SECRET_KEYS = re.compile(
    r"(?i)\b([\w-]*?(?:api_key|apikey|secret|password|passwd|token|authorization|auth))\b"
    r"([\"']?[ \t]*[:=][ \t]*)(?:bearer[ \t]+)?(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]+)"
)
URL_QUERY = re.compile(r"(https?://[^\s?\"'<>]+)\?[^\s\"'<>]*")
IPV4_CANDIDATE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
IPV6_CANDIDATE = re.compile(r"(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
FQDN = re.compile(r"(?<![\w.@-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+([A-Za-z]{2,24})(?![\w-]|\.\w)")
FILE_SUFFIXES = frozenset("py js ts tsx jsx md json yml yaml txt go rs toml sh css html".split())
HOME_PREFIX = re.compile(r"(?<![\w\].~-])/(?:Users|home)/[^/\s\"'`]+")
SENSITIVE_HOME = re.compile(r"~/\.(?:ssh|aws|config|gnupg)\b[^\s\"'`]*")
SAMPLE_DIR = Path.home() / ".claude" / "cache" / "jev-samples"
SAMPLE_CLIP = 2000
SAMPLE_KEEP = 200
GROK_KEYS = ("toolName", "toolInput", "sessionId", "hookEventName", "promptId", "stopHookActive")


def pick(data: dict, *keys, default=None):
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    return default


class HookInput:
    def __init__(self, data: dict | None = None):
        self.raw = data if isinstance(data, dict) else {}
        r = self.raw
        self.tool_name = str(pick(r, "tool_name", "toolName") or "")
        self.tool_input = pick(r, "tool_input", "toolInput") or {}
        if not isinstance(self.tool_input, dict):
            self.tool_input = {}
        self.cwd = str(pick(r, "cwd") or os.getcwd())
        self.session_id = str(
            pick(r, "session_id", "sessionId")
            or os.environ.get("GROK_SESSION_ID")
            or os.environ.get("CLAUDE_SESSION_ID")
            or ""
        )
        self.transcript_path = str(pick(r, "transcript_path", "transcriptPath") or "")
        self.workspace_root = str(pick(r, "workspaceRoot", "workspace_root") or "")
        self.stop_hook_active = bool(pick(r, "stop_hook_active", "stopHookActive"))
        self.prompt = str(pick(r, "prompt") or "")
        self.prompt_id = str(pick(r, "prompt_id", "promptId") or "")
        self.last_assistant = str(pick(r, "lastAssistantMessage", "last_assistant_message") or "")
        self.reason = str(pick(r, "reason") or "")
        self.hook_event = str(pick(r, "hook_event_name", "hookEventName") or "")
        self.harness = "grok" if any(k in r for k in GROK_KEYS) else "claude"

    def command(self) -> str:
        return str(self.tool_input.get("command") or "")

    def file_path(self) -> str:
        ti = self.tool_input
        return str(ti.get("file_path") or ti.get("target_file") or ti.get("path") or "")

    def tool_result(self) -> str:
        """PostToolUse payload: Claude tool_response, Grok toolResult."""
        r = self.raw
        val = pick(r, "tool_response", "toolResult", "tool_output", "toolOutput")
        if val is None:
            return ""
        if isinstance(val, str):
            return val
        try:
            return json.dumps(val)
        except (TypeError, ValueError):
            return str(val)

    def edit_kind(self) -> str:
        """Map harness tool names onto prose-guard's Write/Edit branches."""
        t = self.tool_name
        low = t.lower()
        if low in ("write",):
            return "Write"
        if low in ("edit", "multiedit", "search_replace"):
            return "Edit"
        return t


def redact(text: str) -> str:
    return TOKENISH.sub("[redacted]", text or "")


def is_worker_context(cwd: str, prompt: str = "") -> bool:
    low = cwd.lower()
    if "smart-subagents" in low or "/.claude/worktrees/" in low or "/.overnight" in low:
        return True
    head = prompt[:600]
    return "BRIEF.md" in head or "brief.md" in head or "You are a dispatched worker" in head


def _ip_or_keep(match: re.Match) -> str:
    try:
        ipaddress.ip_address(match.group(0))
    except ValueError:
        return match.group(0)
    return "[ip]"


def _host_or_keep(match: re.Match) -> str:
    return match.group(0) if match.group(1).lower() in FILE_SUFFIXES else "[host]"


def redact_outbound(text: str) -> str:
    out = PEM_BLOCK.sub("[redacted]", text or "")
    out = SECRET_SHAPES.sub("[redacted]", out)
    out = SECRET_KEYS.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", out)
    out = URL_QUERY.sub(lambda m: m.group(1) + "?[redacted]", out)
    out = IPV4_CANDIDATE.sub(_ip_or_keep, out)
    out = IPV6_CANDIDATE.sub(_ip_or_keep, out)
    out = EMAIL.sub("[email]", out)
    out = FQDN.sub(_host_or_keep, out)
    out = HOME_PREFIX.sub("~", out)
    out = SENSITIVE_HOME.sub("[path]", out)
    return TOKENISH.sub("[redacted]", out)


def audit_fields(env: "HookInput", content: str) -> dict:
    """Who/when/what for a Jev log row, with no content: a hash joins it to a sample."""
    return {
        "v": 2,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "session": hashlib.sha256(env.session_id.encode()).hexdigest()[:12] if env.session_id else "",
        "harness": env.harness,
        "tool": env.tool_name,
        "sha": hashlib.sha256((content or "").encode("utf-8", "replace")).hexdigest()[:16],
        "chars": len(content or ""),
    }


def save_sample(row: dict, text: str, sample_dir: Path | None = None) -> None:
    """Keep the redacted text behind a near-miss or a flag so thresholds can be
    tuned on real data. 0700 dir, 0600 files, newest SAMPLE_KEEP only. Silent on failure."""
    d = sample_dir or SAMPLE_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        path = d / f"{row.get('event', 'x')}-{row['sha']}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({**row, "text": redact(text)[:SAMPLE_CLIP]}, fh)
        old = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime)[:-SAMPLE_KEEP]
        for f in old:
            f.unlink()
    except (OSError, KeyError):
        pass


def load_stdin() -> HookInput:
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    return HookInput(data if isinstance(data, dict) else {})


def deny(reason: str, extra: str = "") -> int:
    """Block a PreToolUse call on both harnesses. Exit 2 + JSON deny + stderr."""
    sys.stdout.write(json.dumps({"decision": "deny", "reason": reason}) + "\n")
    print(reason, file=sys.stderr)
    if extra:
        print(extra, file=sys.stderr)
    return 2


def selftest() -> int:
    claude = HookInput({
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/a.png", "command": "x"},
        "session_id": "s1",
        "transcript_path": "/tmp/t.jsonl",
        "stop_hook_active": False,
        "prompt": "hello",
        "cwd": "/tmp",
    })
    grok = HookInput({
        "toolName": "read_file",
        "toolInput": {"target_file": "/tmp/a.png", "command": "gh repo create x --public"},
        "sessionId": "s2",
        "transcriptPath": "/tmp/u.jsonl",
        "stopHookActive": True,
        "prompt": "hello",
        "promptId": "p1",
        "lastAssistantMessage": "Delegation: dispatched",
        "reason": "end_turn",
        "cwd": "/tmp",
        "hook_event_name": "PreToolUse",
        "hookEventName": "pre_tool_use",
        "workspaceRoot": "/tmp/ws",
    })
    bad = []
    if claude.file_path() != "/tmp/a.png":
        bad.append(("claude path", claude.file_path()))
    if grok.file_path() != "/tmp/a.png":
        bad.append(("grok path", grok.file_path()))
    if grok.command() != "gh repo create x --public":
        bad.append(("grok cmd", grok.command()))
    if grok.session_id != "s2" or not grok.stop_hook_active:
        bad.append(("grok session", grok.session_id, grok.stop_hook_active))
    if grok.edit_kind() != "read_file":
        bad.append(("read kind", grok.edit_kind()))
    if HookInput({"toolName": "write"}).edit_kind() != "Write":
        bad.append("write kind")
    if HookInput({"toolName": "search_replace"}).edit_kind() != "Edit":
        bad.append("search_replace kind")
    if HookInput({"tool_name": "Write"}).edit_kind() != "Write":
        bad.append("Write kind")
    if grok.reason != "end_turn" or "Delegation:" not in grok.last_assistant:
        bad.append("stop fields")
    if grok.workspace_root != "/tmp/ws" or claude.workspace_root != "":
        bad.append(("workspace_root", grok.workspace_root, claude.workspace_root))
    if claude.harness != "claude" or grok.harness != "grok":
        bad.append(("harness", claude.harness, grok.harness))
    token = "sk" + "-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3"
    row = audit_fields(grok, "cat key " + token)
    if not (row["v"] == 2 and row["harness"] == "grok" and row["tool"] == "read_file"
            and len(row["sha"]) == 16 and len(row["session"]) == 12 and row["ts"]
            and "s2" not in json.dumps(row) and token not in json.dumps(row)):
        bad.append(("audit_fields", row))
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        d = Path(t) / "samples"
        save_sample({**row, "event": "output"}, "cat key " + token, d)
        files = list(d.glob("*.json"))
        if len(files) != 1 or files[0].stat().st_mode & 0o777 != 0o600 or d.stat().st_mode & 0o777 != 0o700:
            bad.append(("sample perms", [f.name for f in files]))
        elif token in files[0].read_text() or "[redacted]" not in files[0].read_text():
            bad.append("sample redaction")
        for i in range(SAMPLE_KEEP + 5):
            save_sample({"event": "command", "sha": "%016d" % i}, "x", d)
        if len(list(d.glob("*.json"))) != SAMPLE_KEEP:
            bad.append(("sample cap", len(list(d.glob("*.json")))))
    sk20 = "sk" + "-" + "A1b2C3d4E5f6G7h8I9j0"
    pem = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34\n-----END " + "RSA PRIVATE KEY-----"
    outbound = (
        ("ping 203.0.113.50 now", ("203.0.113.50",), ("[ip]",)),
        ("dial 2001:db8::1 and fe80::1%en0", ("2001:db8::1", "fe80::1"), ("[ip]",)),
        ("mail ada@example.com today", ("ada@example.com", "example.com"), ("[email]",)),
        ("set password=s3cret then", ("s3cret",), ("password=[redacted]",)),
        ("key " + sk20 + " here", (sk20,), ("[redacted]",)),
        ("deploy my-inventory.example and github.com", ("my-inventory.example", "github.com"), ("[host]",)),
        ("edit server.py and src/door.py", (), ("server.py", "src/door.py")),
        ("open /Users/alice/proj/x.py", ("/Users/alice", "alice"), ("~/proj/x.py",)),
        ("see /home/bob/work", ("bob",), ("~/work",)),
        ("read ~/.ssh/id_rsa and ~/.aws/credentials", ("id_rsa", ".ssh", "credentials"), ("[path]",)),
        ("author: Ada wrote it", (), ("author: Ada",)),
        ("meet at 12:30:45 sharp", (), ("12:30:45",)),
        ("Authorization: Bearer abc.def-ghi", ("abc.def-ghi", "Bearer"), ("Authorization: [redacted]",)),
        ("db_password: 'two words' and auth_token=xyz;", ("two words", "xyz"), ("db_password: [redacted]", "auth_token=[redacted];")),
        ('{"api_key": "k-1"}', ("k-1",), ('"api_key": [redacted]',)),
        ("go https://github.com/a/b?token=abc&x=1 now", ("token=abc", "x=1", "github.com"), ("?[redacted]",)),
        ("key " + pem + " end", ("MIIBOgIBAAJBAKj34", "PRIVATE KEY"), ("key [redacted] end",)),
        ("dump " + "Zx9" * 9, ("Zx9Zx9Zx9",), ("[redacted]",)),
        ("hosts 10.1.2.3:8080 and 1.2.3.4.5", ("10.1.2.3",), ("[ip]:8080", "1.2.3.4.5")),
    )
    for text, gone, kept in outbound:
        got = redact_outbound(text)
        if any(g in got for g in gone) or not all(k in got for k in kept):
            bad.append(("redact_outbound", text, got))
    if redact_outbound(redact_outbound(sk20 + " a@b.co")) != redact_outbound(sk20 + " a@b.co"):
        bad.append("redact_outbound idempotent")
    if not SECRET_SHAPES.search("x " + sk20) or SECRET_SHAPES.search("sk-short"):
        bad.append("SECRET_SHAPES")
    if not (is_worker_context("/x/smart-subagents/wt") and is_worker_context("/tmp", "see BRIEF.md")
            and not is_worker_context("/tmp")):
        bad.append("is_worker_context")
    for b in bad:
        print("WRONG:", b)
    print("selftest", "FAIL" if bad else "OK", "envelope")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(selftest() if "--test" in sys.argv else 0)
