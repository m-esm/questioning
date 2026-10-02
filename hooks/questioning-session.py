#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))
from envelope import HookInput, is_worker_context  # noqa: E402

RECORD_SCRIPTS = HOOKS.parent / "skills" / "questioning" / "scripts"
STATE_DIR = Path.home() / ".claude" / "cache" / "questioning-session"
STATE_TTL_S = 30 * 86400
OFF = ("0", "false", "off")
NOTE = (
    "Questioning records for you and this project follow; never re-ask what they answer "
    "(skill: ~/.claude/skills/questioning/SKILL.md, writer: ~/.claude/skills/questioning/scripts/record.py)."
)


def records_context(cwd: str) -> str:
    sys.path.insert(0, str(RECORD_SCRIPTS))
    import record
    text = record.inject_text(cwd)
    return NOTE + "\n\n" + text if text else ""


def emit(event: str, context: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}))


def enabled(env: HookInput) -> bool:
    return os.environ.get("QUESTIONING_SESSION", "1") not in OFF and not is_worker_context(env.cwd)


def marker_path(session_id: str) -> Path:
    return STATE_DIR / (re.sub(r"[^\w.-]", "_", session_id) + ".json")


def prune_markers() -> None:
    cutoff = time.time() - STATE_TTL_S
    for marker in STATE_DIR.glob("*.json"):
        try:
            if marker.stat().st_mtime < cutoff:
                marker.unlink()
        except OSError:
            pass


def claim_session(session_id: str) -> bool:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    try:
        fd = os.open(marker_path(session_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as fh:
        json.dump({"ts": time.time()}, fh)
    prune_markers()
    return True


def on_session_start(env: HookInput) -> int:
    if not enabled(env):
        return 0
    context = records_context(env.cwd)
    if context:
        emit("SessionStart", context)
    return 0


def on_inject(env: HookInput) -> int:
    if env.harness != "grok" or not env.session_id or marker_path(env.session_id).exists():
        return 0
    if not enabled(env) or not claim_session(env.session_id):
        return 0
    context = records_context(env.cwd)
    if context:
        emit("PreToolUse", context)
    return 0


def main(argv: list) -> int:
    if "--test" in argv:
        return selftest()
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return 0
        env = HookInput(data)
        if argv[1:2] == ["inject"]:
            return on_inject(env)
        return on_session_start(env)
    except Exception:
        return 0


def selftest() -> int:
    import tempfile
    from contextlib import redirect_stdout
    from io import StringIO
    global STATE_DIR, records_context
    bad: list = []
    old_state, old_context = STATE_DIR, records_context
    saved_env = {k: os.environ.get(k) for k in ("QUESTIONING_ROOT", "QUESTIONING_SESSION")}

    def run(payload, *mode):
        buf = StringIO()
        old_stdin = sys.stdin
        sys.stdin = StringIO(payload if isinstance(payload, str) else json.dumps(payload))
        try:
            with redirect_stdout(buf):
                rc = main(["questioning-session.py", *mode])
        finally:
            sys.stdin = old_stdin
        return rc, buf.getvalue()

    def check(name, fn):
        try:
            fn()
        except Exception as exc:
            bad.append((name, type(exc).__name__, str(exc)))

    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["QUESTIONING_ROOT"] = tmp
            os.environ.pop("QUESTIONING_SESSION", None)
            STATE_DIR = Path(tmp) / "state"
            sys.path.insert(0, str(RECORD_SCRIPTS))
            import record
            cwd = os.path.join(tmp, "proj")
            claude = {"session_id": "c1", "cwd": cwd, "hook_event_name": "SessionStart", "source": "startup"}
            grok = {"sessionId": "g1", "promptId": "g1-p", "workspaceRoot": cwd, "cwd": cwd,
                    "hookEventName": "pre_tool_use", "toolName": "read_file", "toolInput": {"target_file": "x"}}
            global_text = "\n".join(["# Questioning record: global", "", "## Profile (2026-01-01)",
                                     "**Q:** Which editor?", "**A:** Neovim", ""])
            project_text = "\n".join(["# Questioning record: " + cwd, "", "## Scope (2026-01-02)",
                                      "**Q:** Is billing in?", "**A:** No", ""])

            def no_records():
                assert run(claude) == (0, "")
                assert run(grok, "inject") == (0, "")
                assert run(grok, "inject") == (0, "")
                assert (STATE_DIR / "g1.json").exists()
            check("no records", no_records)

            def global_only():
                record.write_atomic(record.global_path(), global_text)
                rc, out = run(claude)
                hso = json.loads(out)["hookSpecificOutput"]
                assert rc == 0 and hso["hookEventName"] == "SessionStart"
                assert hso["additionalContext"] == NOTE + "\n\n" + global_text
            check("global only", global_only)

            def both():
                record.write_atomic(record.project_path(cwd), project_text)
                rc, out = run(claude)
                ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
                assert ctx == NOTE + "\n\n" + global_text + "\n" + project_text, ctx
                assert ctx.index("Which editor?") < ctx.index("Is billing in?")
            check("both records", both)

            def worker():
                assert run({**claude, "cwd": os.path.join(tmp, "smart-subagents", "wt")}) == (0, "")
                assert run({**claude, "cwd": "/x/.claude/worktrees/agent-1"}) == (0, "")
            check("worker cwd", worker)

            def env_off():
                os.environ["QUESTIONING_SESSION"] = "0"
                try:
                    assert run(claude) == (0, "")
                    assert run({**grok, "sessionId": "g-off"}, "inject") == (0, "")
                finally:
                    os.environ.pop("QUESTIONING_SESSION", None)
            check("env off", env_off)

            def malformed():
                assert run("{not json") == (0, "")
                assert run("[]") == (0, "")
                assert run("") == (0, "")
                assert run("{not json", "inject") == (0, "")
            check("malformed stdin", malformed)

            def grok_inject_once():
                env = {**grok, "sessionId": "g2"}
                rc, out = run(env, "inject")
                hso = json.loads(out)["hookSpecificOutput"]
                assert rc == 0 and hso["hookEventName"] == "PreToolUse"
                assert hso["additionalContext"] == NOTE + "\n\n" + global_text + "\n" + project_text
                marker = STATE_DIR / "g2.json"
                assert marker.stat().st_mode & 0o777 == 0o600 and STATE_DIR.stat().st_mode & 0o777 == 0o700
                assert run({**env, "toolName": "search_replace"}, "inject") == (0, "")
                assert run(env, "inject") == (0, "")
                assert json.loads(run({**grok, "sessionId": "g3"}, "inject")[1])["hookSpecificOutput"]
            check("grok inject once per session", grok_inject_once)

            def claude_inject_noop():
                assert run({**claude, "tool_name": "Read", "tool_input": {}}, "inject") == (0, "")
                assert not list(STATE_DIR.glob("c1*"))
            check("claude inject is a no-op", claude_inject_noop)

            def grok_worker_inject():
                env = {**grok, "sessionId": "g4", "cwd": os.path.join(tmp, "smart-subagents", "wt")}
                assert run(env, "inject") == (0, "")
            check("grok worker inject", grok_worker_inject)

            def exception_is_silent():
                global records_context

                def boom(cwd):
                    raise RuntimeError("boom")
                records_context = boom
                try:
                    assert run(claude) == (0, "")
                    assert run({**grok, "sessionId": "g5"}, "inject") == (0, "")
                finally:
                    records_context = old_context
            check("exception is silent", exception_is_silent)
    finally:
        STATE_DIR, records_context = old_state, old_context
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    for b in bad:
        print("WRONG:", b)
    print("selftest", "FAIL" if bad else "OK", "questioning-session")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
