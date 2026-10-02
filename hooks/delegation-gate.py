#!/usr/bin/env python3
"""Delegation gate: make "should this be delegated?" a computed question.

Three hook events, one script. Stdin is Claude snake_case or Grok camelCase
(see envelope.py).

UserPromptSubmit (`delegation-gate.py prompt`): asks TypeSafe's Jev model a
handful of typed questions about the prompt in ONE call (about a second) and,
only when the numbers say so, adds a context block for Claude:
  - isolable labor  -> dispatch through smart-subagents, with the computed
                       --size/--difficulty/--kind flags, or state a valid
                       reason for doing it inline
  - a plan/design   -> run the planning panel, not one opinion
  - a typed judgment (classify, route, rank, dedupe, verify at volume)
                    -> build that step on Jev, not on an LLM prompt
A prompt that is none of these gets no output at all.
Follow-ups to a flagged task are deduped in the same Jev call for six hours.
`delegation-gate.py report [--days N]` summarizes the versioned outcome log.

Grok discards UserPromptSubmit additionalContext. PreToolUse
(`delegation-gate.py inject`, registered in ~/.grok/hooks) delivers the same
block after the first tool of a flagged turn.

Stop (`delegation-gate.py stop`): if the prompt was flagged as labor and the
turn neither dispatched (smart-subagents agent or script) nor wrote a
`Delegation:` line with its reason, the stop is blocked once with that ask.
Grok session-end Stop fires (reason != end_turn) are ignored.

Everything fails open and silent: no key, no network, a timeout, an import
error or DELEGATION_GATE=0 all exit 0 with no output. The prompt is redacted
(long token-shaped strings) and clipped before it leaves the machine. Scores
and latency, never prompt text, are logged to ~/.claude/cache/delegation-gate.jsonl
so the thresholds can be tuned from data.

Dispatched workers are labor, not supervisors: inside an SSA worktree, or when
the prompt is a brief, the gate stays out of the way (anti-recursion). A
`<scheduled-task>` prompt is skipped too (logged as event=skip): it is the same
SKILL.md every run and nobody is present to act on the block.

Questioning stage: a `substantial` question rides in the same batch (prompts of
12 to 59 characters get it alone, with no delegation questions). When it fires,
a second Jev call asks `unresolved_intent` over the prompt plus a small window
of recent human messages and in-repo file paths (never tool bodies, commands,
thinking or assistant prose). A fire adds a QUESTIONING GATE block that sends
Claude to the `questioning` skill, drops the labor dispatch line, and opens an
interview flag that mutes the gate until a turn ends with `Questioning: done`
or `Questioning: skipped` (or two hours pass). The Stop hook reads only
assistant-authored records for those markers. A `.claude/no-questioning-context`
marker in cwd or a parent keeps the window on this machine (a local block
fires instead); QUESTIONING_GATE=0 turns the stage off. Everything sent to Jev
goes through envelope.redact_outbound.
"""

from __future__ import annotations

import fnmatch
import json
import hashlib
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from envelope import HookInput, deny, is_worker_context, redact_outbound  # noqa: E402

HOME = Path.home()
SSA_SCRIPTS = Path(__file__).resolve().parents[1] / "vendor" / "smart-subagents" / "scripts"
STATE_DIR = HOME / ".claude" / "cache" / "delegation-gate"
LOG = HOME / ".claude" / "cache" / "delegation-gate.jsonl"
GROK_SESSIONS = HOME / ".grok" / "sessions"

LABOR_MIN = 0.70  # isolable labor
TRIVIAL_MAX = 0.50  # ...that is not a one-liner
PLAN_MIN = 0.70
JEV_MIN = 0.70
MIN_PROMPT_CHARS = 60
MAX_PROMPT_CHARS = 4000
TIMEOUT_S = 3.5
LAST_FLAG_TTL_S = 6 * 3600
SAME_TASK_MIN = 0.70
SUBSTANTIAL_MIN = 0.40
AMBIG_MIN = 0.40
QUESTIONING_MIN_CHARS = 12
INTERVIEW_TTL_S = 7200
STAGE2_TIMEOUT_S = 1.2
STAGE2_BUDGET_S = 3.0
WINDOW_TAIL_BYTES = 512 * 1024
CONTEXT_USER_TURNS = 4
CONTEXT_USER_TAIL_CHARS = 800
CONTEXT_MAX_PATHS = 8
CONTEXT_INCLUDE_ASSISTANT = False
CONTEXT_ASSISTANT_TAIL_CHARS = 600
PATH_KEYS = ("file_path", "target_file", "path")
SENSITIVE_SEGMENTS = (".ssh", ".aws", ".config", ".gnupg")
SENSITIVE_BASENAMES = ("*.env", "*.pem", "id_*", "Cookies", "admin.env", "keystore*")
OPT_OUT_MARKER = Path(".claude") / "no-questioning-context"
GIT_TIMEOUT_S = 0.3
QUESTIONING_OFF = ("0", "false", "off")
GROK_TOOL_ALIASES = {
    "Bash": "run_terminal_command", "Read": "read_file", "Edit": "search_replace", "Write": "search_replace",
    "MultiEdit": "search_replace", "Grep": "grep", "Glob": "list_dir", "ListDir": "list_dir",
    "WebSearch": "web_search", "Task": "spawn_subagent",
}
READ_ONLY_TOOLS = frozenset(
    [GROK_TOOL_ALIASES[t] for t in ("Read", "Grep", "Glob", "ListDir", "WebSearch")] + ["web_fetch", "ask_user_question"]
)

clock = time.monotonic
SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
USER_QUERY = re.compile(r"<user_query>(.*?)</user_query>", re.S)

# A routine's prompt is its own SKILL.md, the same text every run, and nobody
# is present to act on a delegation block. 59 of the first 87 fires were two of them.
AUTOMATED_PROMPT = re.compile(r"\A\s*<scheduled-task\b")

QUESTIONS = {
    "labor": {
        "type": "noul",
        "instructions": "Does `brief` ask for software or document work that touches several files and could be written down as a brief with acceptance criteria, then carried out by another engineer with no further conversation?",
        "criteria": {
            "true": "Multi-file implementation, refactor, port, test writing, debugging, review, audit, docs or analysis with a checkable outcome",
            "false": "A question, a chat reply, a single small edit, a decision for the person, or work that depends on live back-and-forth",
        },
    },
    "trivial": {
        "type": "noul",
        "instructions": "Can everything `brief` asks for be finished with a short answer or one small edit in one file?",
        "criteria": {
            "true": "A quick factual answer, a one-line change, a rename in one place, running one command",
            "false": "Needs reading or changing several files, or several steps of work",
        },
    },
    "plan": {
        "type": "noul",
        "instructions": "Does `brief` ask for a plan, a design, an architecture, or a comparison of approaches for non-trivial work, rather than for the work itself?",
        "criteria": {
            "true": "Asks how something should be built, which approach is best, or for a roadmap or proposal",
            "false": "Asks for the work to be done, or for a simple fact",
        },
    },
    "jev": {
        "type": "noul",
        "instructions": "Does the work in `brief` include a step where software must classify, route, rank, score, deduplicate, filter or verify text items, or decide yes or no about them, many times or automatically?",
        "criteria": {
            "true": "Examples: triage messages, detect whether a reply answers a question, pick a category, rank candidates, check a claim against evidence, gate an action on confidence",
            "false": "No repeated judgment about text; or the judgment needs generated prose, arithmetic, counting or date math",
        },
    },
}

SUBSTANTIAL_QUESTION = {
    "type": "noul",
    "instructions": "Does `brief` ask Claude to undertake substantial work: creating, changing, investigating, reviewing or planning an outcome that needs several meaningful steps or consequential decisions, including a continuation of such work?",
    "criteria": {
        "true": "Several meaningful steps or consequential decisions, or a continuation of such work",
        "false": "A conversational reply, acknowledgement, simple factual answer, or one small routine action",
    },
}

UNRESOLVED_QUESTION = {
    "type": "noul",
    "instructions": "Using `brief` and `context`, is a decision about what the user wants still unresolved, such that plausible interpretations of `brief` would produce materially different results? Treat suggestions Claude made as suggestions unless the user accepted them. Missing or truncated context does not count as the decision being settled.",
    "criteria": {
        "true": "The intended outcome, target, scope, constraint or success condition is unspecified, contradictory, or points at something not identifiable from `brief` and `context`",
        "false": "The user supplied or accepted enough direction, including letting Claude choose; remaining unknowns are implementation details or facts Claude can look up",
    },
}


def log(row: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def redact(prompt: str) -> str:
    return redact_outbound(prompt)[:MAX_PROMPT_CHARS]


def state_path(session_id: str) -> Path:
    return STATE_DIR / (re.sub(r"[^\w.-]", "_", session_id or "unknown") + ".json")


def common_fields(env: HookInput) -> dict:
    return {
        "v": 2,
        "session": hashlib.sha256(env.session_id.encode()).hexdigest()[:12] if env.session_id else "",
        **({"prompt_id": env.prompt_id} if env.prompt_id else {}),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "harness": env.harness,
    }


def last_flag_path(session_id: str) -> Path:
    return state_path(session_id).with_suffix(".last.json")


def read_last_flag(session_id: str) -> dict | None:
    try:
        last = json.loads(last_flag_path(session_id).read_text())
        if (isinstance(last, dict) and isinstance(last.get("text"), str)
                and isinstance(last.get("fired"), list)
                and all(isinstance(flag, str) for flag in last["fired"])
                and 0 <= time.time() - float(last["ts"]) < LAST_FLAG_TTL_S):
            return last
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def write_private(path: Path, row: dict) -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        os.fchmod(fh.fileno(), 0o600)
        json.dump(row, fh)


def write_last_flag(session_id: str, row: dict) -> None:
    write_private(last_flag_path(session_id), row)


def interview_path(session_id: str) -> Path:
    return state_path(session_id).with_suffix(".interview.json")


def read_interview(session_id: str) -> dict | None:
    path = interview_path(session_id)
    try:
        flag = json.loads(path.read_text())
        if isinstance(flag, dict) and 0 <= time.time() - float(flag["ts"]) < INTERVIEW_TTL_S:
            return flag
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        pass
    try:
        path.unlink()
    except OSError:
        pass
    return None


def transcript_size(path: str) -> int:
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0


def opted_out(cwd: str) -> bool:
    try:
        here = Path(os.path.abspath(cwd))
        return any((p / OPT_OUT_MARKER).exists() for p in (here, *here.parents))
    except (OSError, ValueError):
        return True


def block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"] for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""


def human_text(rec: dict) -> str:
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return ""
    msg = rec.get("message")
    if isinstance(msg, dict):
        origin = rec.get("origin")
        if origin is not None and (not isinstance(origin, dict) or origin.get("kind") != "human"):
            return ""
        text = block_text(msg.get("content"))
    elif "message" not in rec and "content" in rec:
        if rec.get("synthetic_reason"):
            return ""
        text = block_text(rec.get("content"))
        if "prompt_index" not in rec and "<user_query>" not in text:
            return ""
        query = USER_QUERY.search(text)
        if query:
            text = query.group(1)
    else:
        return ""
    text = SYSTEM_REMINDER.sub("", text).strip()
    return "" if not text or text.startswith("<") else text


def path_values(args: dict) -> list:
    return [args[k] for k in PATH_KEYS if isinstance(args.get(k), str) and args[k]]


def assistant_parts(rec: dict) -> tuple[list, list]:
    if rec.get("type") != "assistant" or rec.get("isSidechain") or rec.get("isMeta"):
        return [], []
    prose, paths = [], []
    msg = rec.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            prose.append(content)
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                prose.append(block["text"])
            elif block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                paths.extend(path_values(block["input"]))
    elif "message" not in rec:
        if isinstance(rec.get("content"), str):
            prose.append(rec["content"])
        calls = rec.get("tool_calls")
        for call in calls if isinstance(calls, list) else []:
            try:
                args = json.loads(call["arguments"])
            except (TypeError, KeyError, ValueError):
                continue
            if isinstance(args, dict):
                paths.extend(path_values(args))
    return prose, paths


def load_tail_records(transcript_path: str) -> list:
    with open(transcript_path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        start = max(0, fh.tell() - WINDOW_TAIL_BYTES)
        fh.seek(start)
        lines = fh.read().split(b"\n")
    if start > 0:
        lines = lines[1:]
    records = []
    for line in lines:
        try:
            rec = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def scoped_path(raw: str, roots: list, cwd: str) -> str:
    raw = os.path.expanduser(raw)
    full = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(cwd, raw))
    parts = Path(full).parts
    if any(seg in SENSITIVE_SEGMENTS for seg in parts):
        return ""
    if parts and any(fnmatch.fnmatch(parts[-1], pattern) for pattern in SENSITIVE_BASENAMES):
        return ""
    for root in roots:
        if full == root:
            return "."
        if full.startswith(root.rstrip(os.sep) + os.sep):
            return redact_outbound(os.path.relpath(full, root))
    return ""


def empty_window() -> dict:
    window = {"user_messages": [], "paths": [], "prior_user_turns": 0, "coverage": "empty"}
    if CONTEXT_INCLUDE_ASSISTANT:
        window["assistant_reply"] = ""
    return window


def build_window(records: list, roots: list, prompt: str, prompt_id: str,
                 cwd: str = "", truncated: bool = False) -> dict:
    current = prompt.strip()
    messages, touched, prose = [], [], ""
    for rec in records:
        if prompt_id and rec.get("promptId") == prompt_id:
            continue
        text = human_text(rec)
        if text and text != current:
            messages.append(text)
        said, paths = assistant_parts(rec)
        if said:
            prose = said[-1]
        touched.extend(paths)
    base = cwd or (roots[-1] if roots else "")
    kept: list = []
    for raw in reversed(touched):
        rel = scoped_path(raw, roots, base)
        if rel and rel not in kept:
            kept.append(rel)
        if len(kept) >= CONTEXT_MAX_PATHS:
            break
    window = {
        "user_messages": [redact_outbound(m)[-CONTEXT_USER_TAIL_CHARS:] for m in messages[-CONTEXT_USER_TURNS:]],
        "paths": kept,
        "prior_user_turns": len(messages),
        "coverage": "empty" if not messages and not kept else "transcript-tail" if truncated else "transcript",
    }
    if CONTEXT_INCLUDE_ASSISTANT:
        window["assistant_reply"] = redact_outbound(prose)[-CONTEXT_ASSISTANT_TAIL_CHARS:]
    return window


def window_roots(cwd: str) -> list:
    roots = []
    try:
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
        if top.returncode == 0 and top.stdout.strip():
            roots.append(os.path.realpath(top.stdout.strip()))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    real = os.path.realpath(cwd)
    if real not in roots:
        roots.append(real)
    return [root for root in roots if root != os.sep]


def stage2_window(transcript_path: str, cwd: str, prompt: str, prompt_id: str) -> dict:
    try:
        records = load_tail_records(transcript_path)
        truncated = transcript_size(transcript_path) > WINDOW_TAIL_BYTES
        return build_window(records, window_roots(cwd), prompt, prompt_id, cwd=cwd, truncated=truncated)
    except Exception:
        return empty_window()


def window_context(window: dict) -> dict:
    return {k: window[k] for k in ("user_messages", "paths", "assistant_reply") if k in window}


def questioning_lines(substantial: float, unresolved: float | None, local: bool) -> list:
    if local:
        return [
            "QUESTIONING GATE (local check: Jev saw only this prompt; your conversation context stayed on this machine):",
            f"- Reads as substantial work (p={substantial:.2f}). Check against your own conversation context whether "
            "a choice that changes the outcome (target, scope, constraint, success condition) is still unresolved. "
            "If it is, load the `questioning` skill (Skill tool in Claude Code, /questioning in Grok) and open with the stop check, ending every "
            "interview turn with `Questioning: interviewing`. If it is not, end this turn with "
            "`Questioning: skipped because <what the request resolves to>`.",
        ]
    return [
        "QUESTIONING GATE (computed by Jev from this prompt and recent context, not a guess):",
        f"- Reads as substantial work (p={substantial:.2f}) with an unresolved choice about what you want "
        f"(p={unresolved or 0.0:.2f}). Before building, load the `questioning` skill (Skill tool in Claude Code, /questioning in Grok) and open "
        "with the stop check: what you would build right now and the assumptions it rests on, clarity with the "
        "settled and open slots, the cost of guessing wrong, and options to go now or dig.",
        "- End every interview turn with the line `Questioning: interviewing`. If the user says go, end with "
        "`Questioning: skipped because <reason>`. The wrap-up turn ends with `Questioning: done`.",
    ]


def delegation_lines(n: dict, cls: list, flags: dict, questioning: bool) -> list:
    size, diff, kind = cls
    lines = []
    if flags["plan"] and questioning:
        lines.append(
            f"- Also reads as a planning/design ask (p={n['plan']:.2f}). Interview first, then run the panel with the "
            "project Q/A record as the goal file: `bash ~/.claude/scripts/smart-subagents.sh plan --repo <abs> --n 3 "
            "--difficulty hard --goal-file ~/.claude/projects/<slug>/questioning.md`, and consolidate into ONE plan."
        )
    elif flags["plan"]:
        lines.append(
            f"- Reads as a planning/design ask (p={n['plan']:.2f}). Default is the panel, not one opinion: "
            "`bash ~/.claude/scripts/smart-subagents.sh plan --repo <abs> --n 3 --difficulty hard --goal-file <goal.md>`, "
            "then consolidate into ONE plan."
        )
    if flags["labor"] and not questioning:
        lines.append(
            f"- Reads as isolable labor (p={n['labor']:.2f}, trivial p={n['trivial']:.2f}), class "
            f"`--size {size} --difficulty {diff} --kind {kind}`. Spawn the `smart-subagents` agent with a brief "
            "(goal, absolute workdir, in/out of scope, acceptance criteria, verify commands) and supervise. "
            "To do it inline instead, your reply must carry a line `Delegation: inline because <reason>`, and the "
            "only valid reasons are: trivial, needs this session's MCP tools or live steering, commit/destructive. "
            "If you dispatch, write `Delegation: dispatched`. The stop hook checks for one or the other. "
            "trivial/routine dispatches land on the `cerebras` worker by registry default; do not --prefer around it."
        )
    if flags["jev"]:
        lines.append(
            f"- Contains a repeated typed judgment about text (p={n['jev']:.2f}). Build that step on Jev "
            "(`typesafe-ai` skill; key in ~/.config/typesafe/env; client in "
            "~/.claude/vendor/smart-subagents/scripts/ssa/jev.py): literal noul/choice/score questions with criteria, "
            "thresholds in code, fail-open. Not an LLM prompt-and-parse, and not regexes that grow one exception at a time."
        )
    return ["DELEGATION GATE (computed by Jev from this prompt, not a guess):", *lines] if lines else []


def grok_transcript(env: HookInput) -> str:
    if env.harness != "grok" or not re.fullmatch(r"[\w.-]+", env.session_id or "") or env.session_id in (".", ".."):
        return ""
    for root in (env.workspace_root, env.cwd):
        if root:
            path = GROK_SESSIONS / urllib.parse.quote(root, safe="") / env.session_id / "chat_history.jsonl"
            if path.is_file():
                return str(path)
    return ""


def resolve_transcript(env: HookInput) -> str:
    return env.transcript_path or grok_transcript(env)


def on_prompt(data: dict) -> int:
    t0 = clock()
    env = HookInput(data)
    prompt = env.prompt
    session = env.session_id
    sp = state_path(session)
    try:
        sp.unlink()
    except OSError:
        pass
    tp = resolve_transcript(env)
    open_flag = read_interview(session)
    if open_flag is not None:
        try:
            write_private(interview_path(session), {**open_flag, "offset": transcript_size(tp)})
        except OSError:
            pass
    if prompt.lstrip().startswith("/"):
        return 0
    if is_worker_context(env.cwd, prompt):
        return 0
    if AUTOMATED_PROMPT.match(prompt):
        log({**common_fields(env), "event": "skip", "why": "scheduled-task", "chars": len(prompt)})
        return 0
    if len(prompt) < QUESTIONING_MIN_CHARS:
        return 0
    if open_flag is not None:
        log({**common_fields(env), "v": 3, "event": "skip", "why": "interview-open"})
        return 0
    questioning_on = os.environ.get("QUESTIONING_GATE", "1") not in QUESTIONING_OFF
    short = len(prompt) < MIN_PROMPT_CHARS
    if short and not questioning_on:
        return 0

    sys.path.insert(0, str(SSA_SCRIPTS))
    from ssa import jev  # the one Jev client on this machine

    last = None
    state = {"brief": redact(prompt)}
    if short:
        questions = {"substantial": SUBSTANTIAL_QUESTION}
    else:
        questions = dict(QUESTIONS)
        last = read_last_flag(session)
        if last is not None:
            state["previous"] = redact_outbound(last["text"])
            questions["same_task"] = {
                "type": "noul",
                "instructions": "Is `brief` a follow-up that continues the same task as `previous`, rather than a new task?",
                "criteria": {
                    "true": "Continues, refines, corrects or asks about the same piece of work described in `previous`",
                    "false": "Asks for a different piece of work, or a new task in the same repo",
                },
            }
        questions["size"] = jev._score_question(
            "How much of the codebase does the work described in `brief` change?", jev.SIZE_LEVELS
        )
        questions["difficulty"] = jev._score_question(
            "How much reasoning does the work described in `brief` demand, regardless of how many files it touches?",
            jev.DIFFICULTY_LEVELS,
        )
        questions["kind"] = {
            "type": "choice",
            "instructions": "Which kind of task does `brief` describe?",
            "criteria": jev.KIND_OPTIONS,
        }
        questions["substantial"] = SUBSTANTIAL_QUESTION

    t1 = clock()
    out = jev.ask(state, questions, timeout=TIMEOUT_S)
    ms = int((clock() - t1) * 1000)
    ans = out.get("answers") or {}
    substantial = float((ans.get("substantial") or {}).get("noul", 0.0))
    row = {**common_fields(env), "v": 3, "event": "prompt", "ms": ms, "chars": len(prompt)}
    n: dict = {}
    flags = {"labor": False, "plan": False, "jev": False}
    fired: list = []
    deduped = False
    cls: list = [None, None, None]
    if not short:
        n = {k: float((ans.get(k) or {}).get("noul", 0.0)) for k in QUESTIONS}
        size, size_c = jev._score_label(ans.get("size") or {}, jev.SIZE_LEVELS)
        diff, diff_c = jev._score_label(ans.get("difficulty") or {}, jev.DIFFICULTY_LEVELS)
        kind = (ans.get("kind") or {}).get("choice")
        kind = kind if kind in jev.KIND_OPTIONS else "default"
        conf = {"size": size_c, "difficulty": diff_c, "kind": float((ans.get("kind") or {}).get("confidence") or 0)}
        low = sorted(k for k, v in conf.items() if v < jev.LOW_CONFIDENCE_BY.get(k, jev.LOW_CONFIDENCE))
        rivals = {
            "size": jev._runner_up(ans.get("size") or {}, jev.SIZE_LEVELS),
            "difficulty": jev._runner_up(ans.get("difficulty") or {}, jev.DIFFICULTY_LEVELS),
        }
        runner_up = {k: rivals[k] for k in low if rivals.get(k)}
        eff = jev.effective_class(size, diff, kind, low, runner_up)
        cls = [eff["size"], eff["difficulty"], eff["kind"]]

        flags = {
            "labor": n["labor"] >= LABOR_MIN and n["trivial"] < TRIVIAL_MAX,
            "plan": n["plan"] >= PLAN_MIN and n["trivial"] < TRIVIAL_MAX,
            "jev": n["jev"] >= JEV_MIN,
        }
        fired = [x for x in ("labor", "plan", "jev") if flags[x]]
        same_task = float((ans.get("same_task") or {}).get("noul", 0.0)) if last else 0.0
        deduped = bool(fired and last and same_task >= SAME_TASK_MIN and set(fired) <= set(last["fired"]))
        row.update({
            **{k: round(v, 2) for k, v in n.items()},
            "class": cls,
            "conf": {k: round(v, 2) for k, v in conf.items()},
            "fired": fired,
            "deduped": deduped,
            **({"same_task": round(same_task, 2)} if last else {}),
        })

    stage2 = "skipped" if questioning_on else "off"
    stage2_ms, unresolved, window, local = 0, None, None, False
    model = out.get("model")
    if questioning_on and substantial >= SUBSTANTIAL_MIN:
        if opted_out(env.cwd):
            stage2, local = "opted-out", True
        elif clock() - t0 <= STAGE2_BUDGET_S:
            t2 = clock()
            window = stage2_window(tp, env.cwd, prompt, env.prompt_id)
            try:
                out2 = jev.ask(
                    {"brief": redact(prompt), "context": window_context(window),
                     "coverage": window["coverage"], "prior_user_turns": window["prior_user_turns"]},
                    {"unresolved_intent": UNRESOLVED_QUESTION},
                    timeout=STAGE2_TIMEOUT_S,
                )
                unresolved = float(((out2.get("answers") or {}).get("unresolved_intent") or {}).get("noul", 0.0))
                stage2 = "asked"
                model = model or out2.get("model")
            except Exception as exc:
                stage2 = "error"
                log({**common_fields(env), "v": 3, "event": "error", "stage": 2, "error": type(exc).__name__})
            stage2_ms = int((clock() - t2) * 1000)
    questioning = local or (stage2 == "asked" and unresolved is not None and unresolved >= AMBIG_MIN)
    row.update({
        "substantial": round(substantial, 2),
        "short": short,
        "stage2": stage2,
        "stage2_ms": stage2_ms,
        **({"unresolved_intent": round(unresolved, 2)} if stage2 == "asked" and unresolved is not None else {}),
        "coverage": window["coverage"] if window else None,
        "window_chars": len(json.dumps(window_context(window))) if window else 0,
        "prior_user_turns": window["prior_user_turns"] if window else 0,
        "interview": questioning,
        "local": local,
        **({"model": model} if isinstance(model, str) and model else {}),
    })
    log(row)

    show_delegation = bool(fired) and not deduped
    lines = questioning_lines(substantial, unresolved, local) if questioning else []
    if show_delegation:
        lines += delegation_lines(n, cls, flags, questioning)
    if not lines:
        return 0
    if show_delegation:
        saved = [flag for flag in fired if not (questioning and flag == "labor")]
        if saved:
            try:
                write_last_flag(session, {
                    "text": redact(prompt)[:1500], "fired": saved,
                    "class": cls, "ts": time.time(),
                })
            except OSError:
                pass
    offset = transcript_size(tp)
    if questioning:
        try:
            write_private(interview_path(session), {
                "ts": time.time(), "offset": offset, "prompt_id": env.prompt_id, "local": local,
            })
        except OSError:
            pass
    context = "\n".join(lines)
    if questioning or (show_delegation and flags["labor"]):
        try:
            STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            sp.write_text(
                json.dumps(
                    {
                        "offset": offset,
                        "labor": round(n.get("labor", 0.0), 2),
                        "prompt_id": env.prompt_id,
                        "context": context,
                        "injected": False,
                        "interview": questioning,
                        "local": local,
                        "substantial": round(substantial, 2),
                        "unresolved_intent": round(unresolved, 2) if unresolved is not None else None,
                    }
                )
            )
        except OSError:
            pass
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
        )
    )
    return 0


DISPATCH_MARKS = ("smart-subagents.sh", '"subagent_type":"smart-subagents"', '"subagent_type": "smart-subagents"')


def tool_is_read_only(tool: str) -> bool:
    return GROK_TOOL_ALIASES.get(tool, tool) in READ_ONLY_TOOLS


def on_inject(data: dict) -> int:
    """Grok PreToolUse: UserPromptSubmit additionalContext is discarded."""
    env = HookInput(data)
    sp = state_path(env.session_id)
    if not sp.exists():
        return 0
    try:
        state = json.loads(sp.read_text())
    except (OSError, ValueError):
        return 0
    context = state.get("context") or ""
    if (context and state.get("interview") and env.harness == "grok" and not state.get("denied")
            and not tool_is_read_only(env.tool_name)):
        state["denied"] = state["injected"] = True
        try:
            sp.write_text(json.dumps(state))
        except OSError:
            pass
        return deny(context)
    if not context or state.get("injected"):
        return 0
    state["injected"] = True
    try:
        sp.write_text(json.dumps(state))
    except OSError:
        pass
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": context,
                }
            }
        )
    )
    return 0


def assistant_turn_text(path: str, offset) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, int(offset or 0)))
            raw = fh.read()
    except (OSError, ValueError, TypeError, OverflowError):
        return ""
    parts = []
    for line in raw.decode("utf-8", "replace").split("\n"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant" or rec.get("isSidechain"):
            continue
        msg = rec.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    parts.append(json.dumps(block.get("input")))
        elif "message" not in rec:
            if isinstance(rec.get("content"), str):
                parts.append(rec["content"])
            calls = rec.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                if isinstance(call, dict) and isinstance(call.get("arguments"), str):
                    parts.append(call["arguments"])
    return "\n".join(parts)


def interview_turn_text(env: HookInput, offset) -> str:
    if env.harness == "grok" and env.last_assistant:
        return env.last_assistant
    return assistant_turn_text(resolve_transcript(env), offset) + "\n" + env.last_assistant


def close_interview(env: HookInput) -> None:
    flag = read_interview(env.session_id)
    if flag is None:
        return
    text = interview_turn_text(env, flag.get("offset", 0))
    how = "done" if "Questioning: done" in text else "skipped" if "Questioning: skipped" in text else ""
    if not how:
        return
    try:
        interview_path(env.session_id).unlink()
    except OSError:
        pass
    log({**common_fields(env), "v": 3, "event": "interview-close", "how": how})


def delegation_resolution(turn: str) -> str:
    resolution = "blocked"
    if any(m in turn for m in DISPATCH_MARKS) or "Delegation: dispatched" in turn:
        resolution = "dispatched"
    elif "Delegation:" in turn:
        resolution = "inline:other"
        match = re.search(r"Delegation: inline because([^\r\n]*)", turn)
        if match:
            reason = match.group(1).lower()
            for label, keywords in (
                ("trivial", ("trivial",)),
                ("mcp-or-steering", ("mcp", "steering", "live")),
                ("commit-destructive", ("commit", "destructive")),
            ):
                if any(word in reason for word in keywords):
                    resolution = "inline:" + label
                    break
    return resolution


def p_text(value) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


def interview_reason(state: dict) -> str:
    tail = (
        "Before building, load the `questioning` skill and open with the stop check, ending the turn with "
        "`Questioning: interviewing`. If the user already settled it or said go, end with one line: "
        "`Questioning: skipped because <reason>`."
    )
    substantial = p_text(state.get("substantial"))
    if state.get("local"):
        return (
            "Questioning gate: this prompt read as substantial work with a choice to check against your own "
            f"context (substantial p={substantial}). " + tail
        )
    return (
        "Questioning gate: this prompt read as substantial work with an unresolved choice about the outcome "
        f"(substantial p={substantial}, unresolved p={p_text(state.get('unresolved_intent'))}). " + tail
    )


def on_interview_stop(env: HookInput, sp: Path, state: dict) -> int:
    try:
        sp.unlink()
    except OSError:
        pass
    turn = interview_turn_text(env, state.get("offset", 0))
    satisfied = "Questioning:" in turn or "Delegation:" in turn or any(m in turn for m in DISPATCH_MARKS)
    if "Questioning: skipped" in turn:
        resolution = "interview-skipped"
    elif "Questioning:" in turn:
        resolution = "interview"
    elif satisfied:
        resolution = delegation_resolution(turn)
    else:
        resolution = "interview-blocked"
    log({**common_fields(env), "v": 3, "event": "stop", "satisfied": satisfied, "resolution": resolution,
         "interview": True, "local": bool(state.get("local"))})
    if satisfied:
        return 0
    print(json.dumps({"decision": "block", "reason": interview_reason(state)}))
    return 0


def on_stop(data: dict) -> int:
    env = HookInput(data)
    # Grok also fires Stop at session end; only gate a real turn completion.
    if env.reason and env.reason != "end_turn":
        return 0
    close_interview(env)
    sp = state_path(env.session_id)
    if env.stop_hook_active or not sp.exists():
        return 0
    try:
        state = json.loads(sp.read_text())
        turn = env.last_assistant or ""
        tp = env.transcript_path or str(data.get("transcript_path") or "")
        if tp and os.path.exists(tp):
            with open(tp, "rb") as fh:
                fh.seek(int(state.get("offset", 0)))
                turn = fh.read().decode("utf-8", "replace") + "\n" + turn
    except (OSError, ValueError):
        return 0
    if isinstance(state, dict) and state.get("interview"):
        return on_interview_stop(env, sp, state)
    try:
        sp.unlink()  # one ask per flagged prompt, whatever happens next
    except OSError:
        pass
    satisfied = "Delegation:" in turn or any(m in turn for m in DISPATCH_MARKS)
    resolution = delegation_resolution(turn)
    log({**common_fields(env), "event": "stop", "satisfied": satisfied, "resolution": resolution})
    if satisfied:
        return 0
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    f"Delegation gate: this prompt read as isolable labor (p={state.get('labor')}) and the turn "
                    "neither dispatched through smart-subagents nor stated why not. If work remains, dispatch it now. "
                    "If it is already done inline, end with one line: `Delegation: inline because <trivial | needs this "
                    "session's MCP tools or live steering | commit/destructive | other: ...>`."
                ),
            }
        )
    )
    return 0


INTERVIEW_LABELS = ("interview", "interview-skipped", "interview-blocked")
DELEGATION_LABELS = ("dispatched", "inline:trivial", "inline:mcp-or-steering", "inline:commit-destructive", "inline:other", "blocked")


def report(days: float | None = None) -> int:
    from collections import Counter
    from datetime import datetime
    counts = Counter()
    flags = Counter()
    resolutions = Counter()
    cutoff = time.time() - days * 86400 if days is not None else None
    try:
        with LOG.open() as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        continue
                    if cutoff is not None:
                        ts = datetime.strptime(row.get("ts", ""), "%Y-%m-%dT%H:%M:%S%z").timestamp()
                        if ts < cutoff:
                            continue
                    version = row.get("v", 1)
                    if version not in (1, 2, 3):
                        continue
                    fired = row.get("fired", [])
                    if not isinstance(fired, list) or not all(isinstance(x, str) for x in fired):
                        continue
                    resolution = row.get("resolution", "")
                    if not isinstance(resolution, str):
                        continue
                    counts[f"v{version}"] += 1
                    if row.get("event") == "prompt":
                        if row.get("short") is True:
                            counts["short_prompts"] += 1
                        else:
                            counts["prompts"] += 1
                            flags.update(set(fired))
                        if version in (2, 3) and fired:
                            counts["v2_fired"] += 1
                            counts["deduped"] += row.get("deduped") is True
                        if row.get("interview") is True:
                            counts["interview_fires"] += 1
                            counts["interview_local"] += row.get("local") is True
                    elif row.get("event") == "stop" and version in (2, 3):
                        counts["v2_stops"] += 1
                        resolutions[resolution] += 1
                        if row.get("interview") is True:
                            counts["interview_stops"] += 1
                            counts["interview_followed"] += resolution in INTERVIEW_LABELS[:2] + DELEGATION_LABELS[:-1]
                    elif row.get("event") == "error" and row.get("stage") == 2:
                        counts["stage2_errors"] += 1
                except (ValueError, TypeError, OverflowError):
                    continue
    except FileNotFoundError:
        pass
    def rate(n, d):
        return f"{n}/{d} ({n / d:.1%})" if d else f"{n}/{d} (n/a)"
    all_prompts = counts["prompts"] + counts["short_prompts"]
    rows = [
        ("rows read", str(counts["v1"] + counts["v2"] + counts["v3"])),
        ("v1 rows", str(counts["v1"])), ("v2 rows", str(counts["v2"])), ("v3 rows", str(counts["v3"])),
        ("prompts judged", str(counts["prompts"])),
        ("short prompts judged", str(counts["short_prompts"])),
        *[(f"fire {flag}", rate(flags[flag], counts["prompts"])) for flag in ("labor", "plan", "jev")],
        ("dedupe (v2 fired)", rate(counts["deduped"], counts["v2_fired"])),
    ]
    labels = DELEGATION_LABELS
    rows.extend((label, str(resolutions[label])) for label in labels)
    rows.append(("follow-through", rate(sum(resolutions[label] for label in labels[:-1]), counts["v2_stops"])))
    rows.extend([
        ("interview fires", rate(counts["interview_fires"], all_prompts)),
        ("interview local fires", rate(counts["interview_local"], all_prompts)),
        ("stage2 errors", str(counts["stage2_errors"])),
        *[(label, str(resolutions[label])) for label in INTERVIEW_LABELS],
        ("interview follow-through", rate(counts["interview_followed"], counts["interview_stops"])),
    ])
    print(f"{'Metric':<28} Count / rate")
    for label, value in rows:
        print(f"{label:<28} {value}")
    return 0


def selftest() -> int:
    import tempfile
    import hashlib
    from contextlib import redirect_stdout
    from io import StringIO
    sys.path.insert(0, str(SSA_SCRIPTS))
    from ssa import jev
    global STATE_DIR, LOG, GROK_SESSIONS
    old_state, old_log, old_ask, old_sessions = STATE_DIR, LOG, jev.ask, GROK_SESSIONS
    try:
        with tempfile.TemporaryDirectory() as root:
            STATE_DIR, LOG = Path(root) / "state", Path(root) / "log.jsonl"
            GROK_SESSIONS = Path(root) / "grok-sessions"
            return _selftest(jev, redirect_stdout, StringIO, hashlib)
    finally:
        STATE_DIR, LOG, jev.ask, GROK_SESSIONS = old_state, old_log, old_ask, old_sessions


def _selftest(jev, redirect_stdout, StringIO, hashlib) -> int:
    import tempfile
    bad = []
    with tempfile.TemporaryDirectory() as t:
        global STATE_DIR
        old = STATE_DIR
        STATE_DIR = Path(t)
        sp = state_path("sess-g")
        sp.write_text(json.dumps({"labor": 0.9, "context": "DELEGATION GATE test", "injected": False}))
        out_path = Path(t) / "out.json"
        # inject once
        from io import StringIO
        sys_stdin, sys_stdout = sys.stdin, sys.stdout
        try:
            sys.stdin = StringIO(json.dumps({"sessionId": "sess-g", "toolName": "read_file"}))
            buf = StringIO()
            sys.stdout = buf
            rc = on_inject(json.loads(sys.stdin.getvalue()))
            inj = buf.getvalue()
        finally:
            sys.stdin, sys.stdout = sys_stdin, sys_stdout
        if rc != 0 or "DELEGATION GATE test" not in inj:
            bad.append(("inject", rc, inj[:200]))
        sys.stdin = StringIO(json.dumps({"sessionId": "sess-g"}))
        buf = StringIO()
        sys.stdout = buf
        try:
            sys.stdout = buf
            rc2 = on_inject({"sessionId": "sess-g"})
        finally:
            sys.stdin, sys.stdout = sys_stdin, sys_stdout
        if rc2 != 0 or buf.getvalue():
            bad.append(("inject-second", rc2, buf.getvalue()[:200]))
        # session-end Stop must not block
        sp.write_text(json.dumps({"labor": 0.9, "offset": 0}))
        rc3 = on_stop({"sessionId": "sess-g", "reason": "shutdown", "stopHookActive": False})
        if rc3 != 0:
            bad.append(("session-end stop", rc3))
        STATE_DIR = old
    calls = []
    same = 0.9
    extra_plan = False
    def fake(state, questions, **kwargs):
        calls.append((state, questions))
        return {"answers": {
            **{k: {"noul": 0.9 if k == "labor" or (k == "plan" and extra_plan) else 0.1} for k in QUESTIONS},
            "same_task": {"noul": same},
            "size": {"probabilities": {"2": 1.0}, "confidence": 1.0},
            "difficulty": {"probabilities": {"1": 1.0}, "confidence": 1.0},
            "kind": {"choice": "impl", "confidence": 1.0},
        }}
    jev.ask = fake
    def capture(fn, *args):
        buf = StringIO()
        with redirect_stdout(buf):
            rc = fn(*args)
        assert rc == 0
        return buf.getvalue()
    def check(name, fn):
        try:
            fn()
        except Exception as exc:
            bad.append((name, type(exc).__name__, str(exc)))
    marker = "DISTINCTIVE PRIVATE PROMPT MARKER"
    data = {"session_id": "dedupe-test", "prompt_id": "p1", "cwd": "/tmp",
            "prompt": marker + " Implement the requested multi-file feature and verify the result."}
    last = STATE_DIR / "dedupe-test.last.json"
    def row():
        return json.loads(LOG.read_text().splitlines()[-1])
    def first():
        assert "additionalContext" in capture(on_prompt, data)
        saved = json.loads(last.read_text())
        assert saved["text"] == redact(data["prompt"])[:1500]
        assert saved["fired"] == ["labor"] and len(saved["class"]) == 3
        assert STATE_DIR.stat().st_mode & 0o777 == 0o700
        assert last.stat().st_mode & 0o777 == 0o600
        assert row()["v"] == 3 and row()["prompt_id"] == "p1"
        assert row()["deduped"] is False and "same_task" not in row()
    check("first flag", first)
    def followup():
        saved = last.read_text()
        before = len(calls)
        assert capture(on_prompt, data) == ""
        assert len(calls) == before + 1
        assert calls[-1][0]["previous"] == json.loads(saved)["text"]
        assert "same_task" in calls[-1][1]
        assert not state_path(data["session_id"]).exists()
        assert last.read_text() == saved
        assert row()["deduped"] is True and row()["same_task"] == 0.9
    check("same task", followup)
    same = 0.1
    def new_task():
        data["prompt"] += " Now build a different feature."
        assert "additionalContext" in capture(on_prompt, data)
        assert json.loads(last.read_text())["text"] == redact(data["prompt"])[:1500]
        assert not row()["deduped"]
    check("new task", new_task)
    def expired():
        saved = json.loads(last.read_text())
        saved["ts"] = time.time() - 6 * 3600 - 1
        last.write_text(json.dumps(saved))
        assert "additionalContext" in capture(on_prompt, data)
        assert "same_task" not in calls[-1][1] and "previous" not in calls[-1][0]
    check("expired", expired)
    same, extra_plan = 0.9, True
    def expanded_flags():
        assert capture(on_prompt, data)
        assert row()["fired"] == ["labor", "plan"] and not row()["deduped"]
    check("new flag is not suppressed", expanded_flags)
    extra_plan = False
    def subset_flags():
        assert capture(on_prompt, data) == ""
        assert row()["deduped"] and row()["fired"] == ["labor"]
    check("subset flags deduped", subset_flags)
    def unreadable():
        last.write_text("not json")
        assert capture(on_prompt, data)
        assert "same_task" not in calls[-1][1]
    check("unreadable last flag", unreadable)
    def stop(turn, resolution):
        state_path(data["session_id"]).write_text(json.dumps({"labor": 0.9}))
        output = capture(on_stop, {**data, "last_assistant_message": turn})
        assert row()["resolution"] == resolution and row()["v"] == 2
        assert row()["satisfied"] == (resolution != "blocked")
        assert (json.loads(output)["decision"] == "block") if resolution == "blocked" else not output
    for turn, resolution in (
        ("ran smart-subagents.sh", "dispatched"),
        ("Delegation: dispatched", "dispatched"),
        ("Delegation: inline because trivial edit", "inline:trivial"),
        ("Delegation: inline because MCP tools and live steering", "inline:mcp-or-steering"),
        ("Delegation: inline because commit/destructive", "inline:commit-destructive"),
        ("Delegation: inline because another reason", "inline:other"),
        ("Delegation: something else", "inline:other"),
        ("Finished the work", "blocked"),
    ):
        check(resolution, lambda: stop(turn, resolution))
    def privacy():
        assert marker not in LOG.read_text()
    check("log privacy", privacy)
    def grok():
        assert capture(on_prompt, {"sessionId": "grok-test", "promptId": "g1",
                                  "hookEventName": "UserPromptSubmit", "cwd": "/tmp",
                                  "prompt": data["prompt"]})
        assert row()["session"] == hashlib.sha256(b"grok-test").hexdigest()[:12]
        assert row()["prompt_id"] == "g1"
    check("camelCase", grok)
    def scheduled():
        before = len(calls)
        out = capture(on_prompt, {"session_id": "sched-test", "cwd": "/tmp", "prompt":
            '<scheduled-task name="x" file="/tmp/SKILL.md">\n' + data["prompt"] * 3})
        assert out == "" and len(calls) == before
        assert row()["event"] == "skip" and row()["why"] == "scheduled-task" and row()["harness"] == "claude"
        assert not state_path("sched-test").exists()
    check("scheduled-task skip", scheduled)
    def audit():
        capture(on_prompt, {**data, "session_id": "audit-test", "prompt_id": "a1"})
        assert row()["harness"] == "claude" and set(row()["conf"]) == {"size", "difficulty", "kind"}
    check("audit fields", audit)
    def report_test():
        assert "follow-through" in capture(report)
        global LOG
        saved_log = LOG
        try:
            LOG = LOG.with_name("report-fixture.jsonl")
            assert "0/0 (n/a)" in capture(report)
            current = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            fixtures = [
                {"ts": "2000-01-01T00:00:00+0000", "event": "prompt", "fired": ["labor"]},
                {"v": 2, "ts": current, "event": "prompt", "fired": ["labor"], "deduped": True},
                {"v": 2, "ts": current, "event": "stop", "resolution": "dispatched"},
                {"ts": current, "event": "stop", "satisfied": True},
            ]
            LOG.write_text("broken\n[]\n" + "\n".join(json.dumps(r) for r in fixtures))
            output = capture(report)
            assert "2/2 (100.0%)" in output and "1/1 (100.0%)" in output
            assert "rows read                    4" in output
            filtered = capture(report, 2)
            assert "rows read                    3" in filtered
            assert "prompts judged               1" in filtered
        finally:
            LOG = saved_log
    check("report", report_test)
    _questioning_selftest(jev, bad)
    print("selftest", "FAIL" if bad else "OK", "delegation-gate inject/stop/dedupe/report/questioning")
    for b in bad:
        print("WRONG:", b)
    return 1 if bad else 0


def _questioning_selftest(jev, bad: list) -> None:
    import tempfile
    from contextlib import redirect_stderr, redirect_stdout
    from io import StringIO
    global clock, GROK_SESSIONS
    old_clock, old_ask, old_sessions = clock, jev.ask, GROK_SESSIONS
    now = [1000.0]
    knobs: dict = {}
    asks: list = []
    long_prompt = "Implement the requested multi-file feature for the billing module and verify it."

    def reset(**changes):
        knobs.clear()
        knobs.update(substantial=0.9, unresolved=0.9, labor=0.9, plan=0.1, same=0.1, raise2=False, tick=0.0)
        knobs.update(changes)

    def fake(state, questions, **kwargs):
        asks.append((json.loads(json.dumps(state)), sorted(questions), kwargs.get("timeout")))
        now[0] += knobs["tick"]
        if "unresolved_intent" in questions:
            if knobs["raise2"]:
                raise jev.JevUnavailable("stage 2 down")
            return {"model": "jev-test", "answers": {"unresolved_intent": {"noul": knobs["unresolved"]}}}
        return {"model": "jev-test", "answers": {
            "labor": {"noul": knobs["labor"]}, "trivial": {"noul": 0.1},
            "plan": {"noul": knobs["plan"]}, "jev": {"noul": 0.1},
            "substantial": {"noul": knobs["substantial"]}, "same_task": {"noul": knobs["same"]},
            "size": {"probabilities": {"2": 1.0}, "confidence": 1.0},
            "difficulty": {"probabilities": {"1": 1.0}, "confidence": 1.0},
            "kind": {"choice": "impl", "confidence": 1.0},
        }}

    def capture(fn, *args):
        buf = StringIO()
        with redirect_stdout(buf):
            rc = fn(*args)
        assert rc == 0
        return buf.getvalue()

    def context_of(out):
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""

    def rows():
        return [json.loads(x) for x in LOG.read_text().splitlines()]

    def check(name, fn):
        reset()
        try:
            fn()
        except Exception as exc:
            bad.append(("questioning " + name, type(exc).__name__, str(exc)))

    def append(tp, *records):
        with open(tp, "a") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

    def human(text, pid="old"):
        return {"type": "user", "promptId": pid, "origin": {"kind": "human"},
                "message": {"role": "user", "content": text}}

    def said(*blocks):
        return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}

    def text(value):
        return {"type": "text", "text": value}

    def capture_rc(fn, *args):
        buf = StringIO()
        with redirect_stdout(buf), redirect_stderr(StringIO()):
            rc = fn(*args)
        return rc, buf.getvalue()

    clock = lambda: now[0]  # noqa: E731
    jev.ask = fake
    try:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            proj = base / "proj"
            proj.mkdir()

            def mk(sess, prompt, cwd=None):
                tp = base / (sess + ".jsonl")
                if not tp.exists():
                    append(tp, human("an earlier ask about the billing module"))
                return {"session_id": sess, "prompt_id": sess + "-p", "cwd": str(cwd or proj),
                        "prompt": prompt, "transcript_path": str(tp)}

            def interview_with_labor():
                d = mk("q1", long_prompt)
                before = len(asks)
                ctx = context_of(capture(on_prompt, d))
                assert len(asks) == before + 2 and asks[-1][1] == ["unresolved_intent"]
                assert asks[-1][2] == STAGE2_TIMEOUT_S and "substantial" in asks[-2][1]
                assert ctx.startswith("QUESTIONING GATE (computed by Jev")
                assert "Spawn the `smart-subagents` agent" not in ctx and "DELEGATION GATE" not in ctx
                lf = last_flag_path("q1")
                assert not lf.exists() or "labor" not in json.loads(lf.read_text())["fired"]
                r = rows()[-1]
                assert r["v"] == 3 and r["event"] == "prompt" and r["stage2"] == "asked" and r["short"] is False
                assert r["interview"] is True and r["local"] is False and r["fired"] == ["labor"]
                assert r["substantial"] == 0.9 and r["unresolved_intent"] == 0.9 and r["model"] == "jev-test"
                assert r["coverage"] == "transcript" and r["prior_user_turns"] == 1 and r["window_chars"] > 0
                flag = interview_path("q1")
                assert flag.exists() and flag.stat().st_mode & 0o777 == 0o600
                assert json.loads(flag.read_text())["offset"] == os.path.getsize(d["transcript_path"])
                append(d["transcript_path"], said(text("Which module first?\nQuestioning: interviewing")))
                assert capture(on_stop, d) == ""
                r = rows()[-1]
                assert r["event"] == "stop" and r["resolution"] == "interview" and r["v"] == 3 and r["interview"] is True
                assert interview_path("q1").exists()
            check("interview with labor", interview_with_labor)

            def interview_blocks_once():
                d = mk("q2", long_prompt)
                capture(on_prompt, d)
                append(d["transcript_path"], said(text("Built it.")))
                out = json.loads(capture(on_stop, d))
                assert out["decision"] == "block" and out["reason"].startswith("Questioning gate:")
                assert "unresolved p=0.90" in out["reason"] and "substantial p=0.90" in out["reason"]
                assert rows()[-1]["resolution"] == "interview-blocked" and rows()[-1]["satisfied"] is False
                assert capture(on_stop, d) == ""
            check("interview blocks once", interview_blocks_once)

            def assistant_only_reader():
                attach = {"type": "attachment", "attachment": {"type": "hook_additional_context",
                          "content": ["Questioning: interviewing and Delegation: dispatched"]}}
                d = mk("q3", long_prompt)
                capture(on_prompt, d)
                append(d["transcript_path"], attach, said(text("Working on it.")))
                assert json.loads(capture(on_stop, d))["decision"] == "block"
                d = mk("q3b", long_prompt)
                capture(on_prompt, d)
                append(d["transcript_path"], attach, said(text("Working on it.")),
                       said(text("Questioning: interviewing")))
                assert capture(on_stop, d) == ""
            check("assistant-only reader", assistant_only_reader)

            def labor_without_interview():
                knobs["substantial"] = 0.1
                d = mk("q4", long_prompt)
                before = len(asks)
                ctx = context_of(capture(on_prompt, d))
                assert len(asks) == before + 1 and "substantial" in asks[-1][1]
                assert ctx.startswith("DELEGATION GATE") and "Spawn the `smart-subagents` agent" in ctx
                assert "QUESTIONING" not in ctx and not interview_path("q4").exists()
                assert rows()[-1]["stage2"] == "skipped" and rows()[-1]["interview"] is False
                out = json.loads(capture(on_stop, {**d, "last_assistant_message": "Finished the work"}))
                assert out["decision"] == "block" and out["reason"].startswith("Delegation gate:")
                r = rows()[-1]
                assert r["v"] == 2 and r["resolution"] == "blocked" and "interview" not in r
            check("labor without interview", labor_without_interview)

            def open_flag_skips():
                d = mk("q1", long_prompt)
                append(d["transcript_path"], human("Start with invoices."))
                flag = json.loads(interview_path("q1").read_text())
                before = len(asks)
                assert capture(on_prompt, {**d, "prompt": "wrap up"}) == ""
                refreshed = json.loads(interview_path("q1").read_text())
                assert refreshed["offset"] == os.path.getsize(d["transcript_path"]) and refreshed["ts"] == flag["ts"]
                assert capture(on_prompt, d) == "" and len(asks) == before
                r = rows()[-1]
                assert r["v"] == 3 and r["event"] == "skip" and r["why"] == "interview-open"
            check("open flag skips", open_flag_skips)

            def short_prompt():
                d = mk("q6", "Refactor the parser module now")
                assert len(d["prompt"]) == 30
                before = len(asks)
                ctx = context_of(capture(on_prompt, d))
                assert len(asks) == before + 2
                assert asks[before][1] == ["substantial"] and list(asks[before][0]) == ["brief"]
                assert asks[before + 1][1] == ["unresolved_intent"]
                assert "QUESTIONING GATE" in ctx and "DELEGATION GATE" not in ctx
                assert not last_flag_path("q6").exists()
                r = rows()[-1]
                assert r["short"] is True and r["interview"] is True and "fired" not in r
            check("short prompt", short_prompt)

            def tiny_prompt():
                before = len(asks)
                assert capture(on_prompt, mk("q7", "wrap it up")) == "" and len(asks) == before
            check("tiny prompt", tiny_prompt)

            def low_substantial():
                knobs["substantial"] = 0.3
                before = len(asks)
                capture(on_prompt, mk("q8", long_prompt))
                assert len(asks) == before + 1 and rows()[-1]["stage2"] == "skipped"
            check("low substantial", low_substantial)

            def privacy_window():
                cwd = base / "priv"
                (cwd / "src").mkdir(parents=True)
                tp = base / "q9.jsonl"
                home = str(Path.home())
                planted = ["TOOLBODY_SHOULD_NOT_LEAK", "THINKING_SHOULD_NOT_LEAK", "FILEBODY_SHOULD_NOT_LEAK",
                           "SECRET_CMD", "203.0.113.50", "10.1.2.3", "ada@example.com", "hunter2xyz", "id_rsa",
                           "B and C", "META_MARKER_X", "SIDECHAIN_MARKER_X", "CURRENT_RECORD_MARKER_X"]
                append(
                    tp,
                    human("Mail the billing report to ada@example.com with password=hunter2xyz from 10.1.2.3, "
                          "now do the same for the other two", "p-old"),
                    said({"type": "thinking", "thinking": "THINKING_SHOULD_NOT_LEAK"},
                         text("Done for A; the other two are B and C on 203.0.113.50"),
                         {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo SECRET_CMD"}},
                         {"type": "tool_use", "id": "t2", "name": "Write",
                          "input": {"file_path": str(cwd / "src" / "door.py"), "content": "FILEBODY_SHOULD_NOT_LEAK"}},
                         {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": home + "/.ssh/id_rsa"}}),
                    {"type": "user", "promptId": "p-old", "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "TOOLBODY_SHOULD_NOT_LEAK"}]}},
                    {"type": "user", "isMeta": True, "message": {"role": "user", "content": "META_MARKER_X"}},
                    {"type": "user", "isSidechain": True, "origin": {"kind": "human"},
                     "message": {"role": "user", "content": "SIDECHAIN_MARKER_X"}},
                    {"type": "user", "prompt_index": 0,
                     "content": [{"type": "text", "text": "<user_query>start with the billing report</user_query>"}]},
                    human("CURRENT_RECORD_MARKER_X now do the same for the other two", "q9-p"),
                )
                d = {"session_id": "q9", "prompt_id": "q9-p", "cwd": str(cwd),
                     "prompt": "now do the same for the other two", "transcript_path": str(tp)}
                assert len(d["prompt"]) == 33
                before = len(asks)
                capture(on_prompt, d)
                mine = asks[before:]
                stage2 = [state for state, qs, _ in mine if qs == ["unresolved_intent"]]
                assert stage2, "no stage-2 ask"
                for state in stage2:
                    blob = json.dumps(state)
                    assert "src/door.py" in blob and "other two" in blob and "billing report" in blob, blob
                    assert state["prior_user_turns"] == 2 and state["coverage"] == "transcript"
                for state, _, _ in mine:
                    leaked = [m for m in planted if m in json.dumps(state)]
                    assert not leaked, leaked
                leaked = [m for m in planted if m in LOG.read_text()]
                assert not leaked, ("log", leaked)
            check("privacy window", privacy_window)

            def grok_window():
                recs = [
                    {"type": "user", "prompt_index": 0,
                     "content": [{"type": "text", "text": "<user_query>tune the door hinge</user_query>"}]},
                    {"type": "user", "content": "SYNTH_MARKER", "synthetic_reason": "system_reminder"},
                    {"type": "user", "content": [{"type": "text", "text": "plain nonhuman text"}]},
                    {"type": "assistant", "content": "grok prose", "tool_calls": [
                        {"id": "1", "name": "read_file",
                         "arguments": json.dumps({"target_file": str(proj / "src" / "hinge.py"), "content": "BODY"})},
                        {"id": "2", "name": "write", "arguments": "{not json"}]},
                    {"type": "tool_result", "content": "GROK_TOOL_RESULT"},
                    {"type": "reasoning", "summary": "GROK_REASONING"},
                ]
                w = build_window(recs, [os.path.realpath(proj)], "next step please", "", cwd=str(proj))
                assert w["user_messages"] == ["tune the door hinge"] and w["paths"] == ["src/hinge.py"], w
                blob = json.dumps(w)
                assert not [m for m in ("BODY", "SYNTH", "nonhuman", "GROK_", "grok prose") if m in blob], blob
                assert w["prior_user_turns"] == 1 and "assistant_reply" not in w
                assert build_window([], [os.path.realpath(proj)], "x", "")["coverage"] == "empty"
            check("grok window", grok_window)

            def tail_window():
                global WINDOW_TAIL_BYTES
                saved = WINDOW_TAIL_BYTES
                tp = base / "tail.jsonl"
                append(tp, human("HEAD_MARKER " + "x" * 300), human("keep this recent one"))
                try:
                    WINDOW_TAIL_BYTES = 200
                    recs = load_tail_records(str(tp))
                    assert len(recs) == 1 and "HEAD_MARKER" not in json.dumps(recs)
                    w = stage2_window(str(tp), str(proj), "something else", "")
                    assert w["coverage"] == "transcript-tail" and w["user_messages"] == ["keep this recent one"]
                    assert stage2_window(str(base / "missing.jsonl"), str(proj), "x", "")["coverage"] == "empty"
                finally:
                    WINDOW_TAIL_BYTES = saved
            check("tail window", tail_window)

            def opted_out():
                top = base / "optout"
                (top / ".claude").mkdir(parents=True)
                (top / ".claude" / "no-questioning-context").write_text("")
                cwd = top / "proj" / "sub"
                cwd.mkdir(parents=True)
                d = mk("q10", long_prompt, cwd)
                before = len(asks)
                ctx = context_of(capture(on_prompt, d))
                assert len(asks) == before + 1 and "unresolved_intent" not in asks[-1][1]
                assert ctx.startswith("QUESTIONING GATE (local check: Jev saw only this prompt")
                assert "Spawn the `smart-subagents` agent" not in ctx
                assert json.loads(interview_path("q10").read_text())["local"] is True
                r = rows()[-1]
                assert r["stage2"] == "opted-out" and r["local"] is True and r["interview"] is True
                append(d["transcript_path"], said(text("Built it.")))
                out = json.loads(capture(on_stop, d))
                assert out["decision"] == "block"
                assert "with a choice to check against your own context" in out["reason"]
                assert "unresolved p" not in out["reason"] and "substantial p=0.90" in out["reason"]
                assert rows()[-1]["local"] is True and rows()[-1]["resolution"] == "interview-blocked"
                assert capture(on_stop, d) == ""
            check("opt-out marker", opted_out)

            def stage2_error():
                knobs["raise2"] = True
                before = len(asks)
                ctx = context_of(capture(on_prompt, mk("q11", long_prompt)))
                assert len(asks) == before + 2
                assert ctx.startswith("DELEGATION GATE") and "Spawn the `smart-subagents` agent" in ctx
                errors = [r for r in rows() if r.get("event") == "error" and r.get("stage") == 2]
                assert errors and errors[-1]["error"] == "JevUnavailable" and errors[-1]["v"] == 3
                assert rows()[-1]["stage2"] == "error" and not interview_path("q11").exists()
            check("stage-2 error", stage2_error)

            def budget():
                knobs["tick"] = 3.5
                before = len(asks)
                capture(on_prompt, mk("q12", long_prompt))
                assert len(asks) == before + 1 and rows()[-1]["stage2"] == "skipped"
            check("budget", budget)

            def env_off():
                old = os.environ.get("QUESTIONING_GATE")
                os.environ["QUESTIONING_GATE"] = "0"
                try:
                    before = len(asks)
                    assert capture(on_prompt, mk("q13", "Refactor the parser module now")) == ""
                    assert len(asks) == before
                    ctx = context_of(capture(on_prompt, mk("q13b", long_prompt)))
                    assert len(asks) == before + 1
                    assert ctx.startswith("DELEGATION GATE") and "QUESTIONING" not in ctx
                    assert not interview_path("q13b").exists() and rows()[-1]["stage2"] == "off"
                finally:
                    if old is None:
                        os.environ.pop("QUESTIONING_GATE", None)
                    else:
                        os.environ["QUESTIONING_GATE"] = old
                knobs["substantial"] = 0.1
                assert ctx == context_of(capture(on_prompt, mk("q13c", long_prompt)))
            check("QUESTIONING_GATE=0", env_off)

            def dedupe_keeps_questioning():
                knobs["substantial"] = 0.1
                d = mk("q14", long_prompt)
                assert "DELEGATION GATE" in context_of(capture(on_prompt, d))
                knobs.update(substantial=0.9, same=0.9)
                ctx = context_of(capture(on_prompt, {**d, "prompt": long_prompt + " Keep going."}))
                assert "QUESTIONING GATE" in ctx and "DELEGATION GATE" not in ctx
                r = rows()[-1]
                assert r["deduped"] is True and r["interview"] is True
            check("dedupe keeps questioning", dedupe_keeps_questioning)

            def flag_close():
                d = mk("q15a", long_prompt)
                capture(on_prompt, d)
                append(d["transcript_path"], said(text("First question.\nQuestioning: interviewing")))
                assert capture(on_stop, d) == "" and interview_path("q15a").exists()
                before = len(asks)
                assert capture(on_prompt, {**d, "prompt": "Use option A and keep the old API."}) == ""
                assert len(asks) == before
                append(d["transcript_path"], said(text("Synthesis.\nQuestioning: done")))
                assert capture(on_stop, d) == "" and not interview_path("q15a").exists()
                assert rows()[-1]["event"] == "interview-close" and rows()[-1]["how"] == "done"
                d = mk("q15b", long_prompt)
                capture(on_prompt, d)
                assert capture(on_stop, {**d, "last_assistant_message": "Questioning: skipped because go"}) == ""
                assert not interview_path("q15b").exists()
                closes = [r for r in rows() if r.get("event") == "interview-close"]
                assert closes[-1]["how"] == "skipped" and rows()[-1]["resolution"] == "interview-skipped"
                d = mk("q15c", long_prompt)
                capture(on_prompt, d)
                flag = json.loads(interview_path("q15c").read_text())
                flag["ts"] -= INTERVIEW_TTL_S + 1
                interview_path("q15c").write_text(json.dumps(flag))
                before = len(asks)
                assert "QUESTIONING GATE" in context_of(capture(on_prompt, d)) and len(asks) == before + 2
                assert json.loads(interview_path("q15c").read_text())["ts"] > flag["ts"] + INTERVIEW_TTL_S
            check("flag close and expiry", flag_close)

            def plan_interview():
                knobs.update(labor=0.1, plan=0.9)
                ctx = context_of(capture(on_prompt, mk("q16", long_prompt)))
                assert ctx.startswith("QUESTIONING GATE") and "\nDELEGATION GATE" in ctx
                assert "- Also reads as a planning/design ask (p=0.90). Interview first" in ctx
                assert "--goal-file ~/.claude/projects/<slug>/questioning.md" in ctx
                assert "Default is the panel" not in ctx
                assert json.loads(last_flag_path("q16").read_text())["fired"] == ["plan"]
            check("plan with interview", plan_interview)

            def report_versions():
                global LOG
                saved = LOG
                try:
                    LOG = base / "report-v3.jsonl"
                    current = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    fixtures = [
                        {"ts": current, "event": "prompt", "fired": ["labor"]},
                        {"v": 2, "ts": current, "event": "prompt", "fired": ["labor"], "deduped": True},
                        {"v": 2, "ts": current, "event": "stop", "resolution": "dispatched"},
                        {"v": 3, "ts": current, "event": "prompt", "fired": ["labor", "plan"], "deduped": False,
                         "short": False, "interview": True, "local": False, "stage2": "asked"},
                        {"v": 3, "ts": current, "event": "prompt", "short": True, "interview": True,
                         "local": True, "stage2": "opted-out"},
                        {"v": 3, "ts": current, "event": "prompt", "short": True, "interview": False,
                         "stage2": "skipped"},
                        {"v": 3, "ts": current, "event": "error", "stage": 2, "error": "JevUnavailable"},
                        {"v": 3, "ts": current, "event": "stop", "resolution": "interview", "interview": True},
                        {"v": 3, "ts": current, "event": "stop", "resolution": "interview-skipped", "interview": True},
                        {"v": 3, "ts": current, "event": "stop", "resolution": "interview-blocked", "interview": True},
                        {"v": 3, "ts": current, "event": "stop", "resolution": "dispatched", "interview": True},
                        {"v": 3, "ts": current, "event": "skip", "why": "interview-open"},
                    ]
                    LOG.write_text("\n".join(json.dumps(r) for r in fixtures) + "\n")
                    out = capture(report)
                    expect = {
                        "rows read": "12", "v1 rows": "1", "v2 rows": "2", "v3 rows": "9",
                        "prompts judged": "3", "short prompts judged": "2",
                        "fire labor": "3/3 (100.0%)", "fire plan": "1/3 (33.3%)",
                        "dedupe (v2 fired)": "1/2 (50.0%)", "dispatched": "2", "blocked": "0",
                        "follow-through": "2/5 (40.0%)",
                        "interview fires": "2/5 (40.0%)", "interview local fires": "1/5 (20.0%)",
                        "stage2 errors": "1", "interview": "1", "interview-skipped": "1",
                        "interview-blocked": "1", "interview follow-through": "3/4 (75.0%)",
                    }
                    missing = [(k, v) for k, v in expect.items() if f"{k:<28} {v}\n" not in out]
                    assert not missing, (missing, out)
                finally:
                    LOG = saved
            check("report v1/v2/v3", report_versions)

            def sensitive_paths():
                real = os.path.realpath(proj)
                for rel in (".aws/creds.txt", ".config/app.toml", "deploy/admin.env", "keys/server.pem",
                            "id_ed25519", ".env", "keystore.jks", "Cookies"):
                    assert scoped_path(str(proj / rel), [real], real) == "", rel
                assert scoped_path("src/door.py", [real], real) == "src/door.py"
                assert scoped_path(str(proj), [real], real) == "."
                assert scoped_path("/etc/hosts", [real], real) == ""
            check("sensitive paths", sensitive_paths)

            GROK_SESSIONS = base / "grok-sessions"
            ws = base / "grokws"
            (ws / "src").mkdir(parents=True)

            def grok_history(sess, root=ws):
                path = GROK_SESSIONS / urllib.parse.quote(str(root), safe="") / sess / "chat_history.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    append(path, {"type": "user", "prompt_index": 0, "content": [
                        {"type": "text", "text": "<user_query>ship the hinge redesign</user_query>"}]},
                        {"type": "assistant", "content": "Reading it.", "tool_calls": [
                            {"id": "1", "name": "read_file",
                             "arguments": json.dumps({"target_file": str(ws / "src" / "hinge.py")})}]})
                return path

            def grok_env(sess, event, **extra):
                return {"sessionId": sess, "promptId": sess + "-p", "workspaceRoot": str(ws), "cwd": str(ws),
                        "hookEventName": event, **extra}

            def grok_transcript_lookup():
                path = grok_history("g-ws")
                assert grok_transcript(HookInput(grok_env("g-ws", "stop"))) == str(path)
                other = base / "grok-cwd"
                other.mkdir()
                fallback = grok_history("g-cwd", other)
                env = HookInput({**grok_env("g-cwd", "stop"), "cwd": str(other)})
                assert grok_transcript(env) == str(fallback)
                assert grok_transcript(HookInput(grok_env("g-none", "stop"))) == ""
                assert grok_transcript(HookInput({"session_id": "g-ws", "cwd": str(ws)})) == ""
                assert grok_transcript(HookInput(grok_env("../g-ws", "stop"))) == ""
            check("grok transcript lookup", grok_transcript_lookup)

            def grok_delivery():
                grok_history("g1")
                before = len(asks)
                capture(on_prompt, grok_env("g1", "user_prompt_submit", prompt=long_prompt))
                stage2 = [state for state, qs, _ in asks[before:] if qs == ["unresolved_intent"]]
                blob = json.dumps(stage2)
                assert stage2 and "ship the hinge redesign" in blob and "src/hinge.py" in blob, blob
                assert rows()[-1]["harness"] == "grok" and rows()[-1]["interview"] is True

                def pre(tool, tool_input=None):
                    return capture_rc(on_inject, grok_env("g1", "pre_tool_use", toolName=tool,
                                                          toolInput=tool_input or {}))
                rc, out = pre("read_file", {"target_file": "src/hinge.py"})
                assert rc == 0 and context_of(out).startswith("QUESTIONING GATE")
                assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
                assert pre("grep", {"pattern": "x"}) == (0, "")
                rc, out = pre("search_replace", {"file_path": "src/hinge.py"})
                assert rc == 2 and json.loads(out)["decision"] == "deny"
                assert json.loads(out)["reason"].startswith("QUESTIONING GATE")
                assert pre("run_terminal_command", {"command": "make"}) == (0, "")
                assert pre("search_replace", {"file_path": "src/hinge.py"}) == (0, "")

                stop = grok_env("g1", "stop", reason="end_turn", stopHookActive=False,
                                lastAssistantMessage="Here is what I would build.\nQuestioning: interviewing")
                assert capture(on_stop, stop) == "" and rows()[-1]["resolution"] == "interview"
                assert interview_path("g1").exists()
                done = {**stop, "lastAssistantMessage": "Synthesis.\nQuestioning: done"}
                assert capture(on_stop, done) == "" and not interview_path("g1").exists()
                assert rows()[-1]["event"] == "interview-close" and rows()[-1]["how"] == "done"

                capture(on_prompt, grok_env("g1", "user_prompt_submit", prompt=long_prompt + " Second round."))
                rc, out = pre("write", {"file_path": "src/new.py"})
                assert rc == 2 and json.loads(out)["reason"].startswith("QUESTIONING GATE")
            check("grok delivery and deny", grok_delivery)

            def grok_read_only_aliases():
                grok_history("g2")
                capture(on_prompt, grok_env("g2", "user_prompt_submit", prompt=long_prompt))

                def pre(tool):
                    return capture_rc(on_inject, grok_env("g2", "pre_tool_use", toolName=tool, toolInput={}))
                rc, out = pre("ask_user_question")
                assert rc == 0 and "QUESTIONING GATE" in context_of(out)
                for tool in ("Read", "list_dir", "web_fetch", "web_search", "Glob"):
                    assert pre(tool) == (0, ""), tool
                rc, out = pre("todo_write")
                assert rc == 2 and json.loads(out)["decision"] == "deny"
                assert all(tool_is_read_only(t) for t in ("read_file", "grep", "list_dir", "web_search",
                                                          "web_fetch", "ask_user_question", "Read", "Grep"))
                assert not any(tool_is_read_only(t) for t in ("search_replace", "write", "run_terminal_command",
                                                              "Bash", "Edit", "Write", "use_tool", "spawn_subagent"))
            check("grok read-only aliases", grok_read_only_aliases)

            def grok_local_variant():
                top = base / "grok-optout"
                (top / ".claude").mkdir(parents=True)
                (top / ".claude" / "no-questioning-context").write_text("")
                (top / "app").mkdir()
                env = {**grok_env("g3", "user_prompt_submit", prompt=long_prompt), "cwd": str(top / "app"),
                       "workspaceRoot": str(top / "app")}
                capture(on_prompt, env)
                rc, out = capture_rc(on_inject, {**env, "hookEventName": "pre_tool_use",
                                                 "toolName": "run_terminal_command", "toolInput": {"command": "ls"}})
                assert rc == 2 and json.loads(out)["reason"].startswith("QUESTIONING GATE (local check")
            check("grok local variant", grok_local_variant)

            def grok_stop_fallback():
                hist = grok_history("g4")
                capture(on_prompt, grok_env("g4", "user_prompt_submit", prompt=long_prompt))
                append(hist, {"type": "assistant", "content": "Stop check.\nQuestioning: interviewing"})
                stop = grok_env("g4", "stop", reason="end_turn", stopHookActive=False)
                assert capture(on_stop, stop) == "" and rows()[-1]["resolution"] == "interview"
                grok_history("g5")
                capture(on_prompt, grok_env("g5", "user_prompt_submit", prompt=long_prompt))
                out = capture(on_stop, grok_env("g5", "stop", reason="end_turn", stopHookActive=False,
                                                lastAssistantMessage="Built the thing."))
                assert json.loads(out)["decision"] == "block" and rows()[-1]["resolution"] == "interview-blocked"
            check("grok stop fallback", grok_stop_fallback)

            def claude_inject_never_denies():
                capture(on_prompt, mk("c-inject", long_prompt))
                rc, out = capture_rc(on_inject, {"session_id": "c-inject", "tool_name": "Edit", "tool_input": {}})
                assert rc == 0 and "QUESTIONING GATE" in context_of(out)
            check("claude inject never denies", claude_inject_never_denies)
    finally:
        clock, jev.ask, GROK_SESSIONS = old_clock, old_ask, old_sessions


def main() -> int:
    if "--test" in sys.argv:
        return selftest()
    mode = sys.argv[1] if len(sys.argv) > 1 else "prompt"
    if mode == "report":
        try:
            args = sys.argv[2:]
            if not args:
                return report()
            if len(args) == 2 and args[0] == "--days":
                return report(float(args[1]))
        except Exception:
            pass
        return 0
    if os.environ.get("DELEGATION_GATE", "1") in ("0", "false", "off"):
        return 0
    data = {}
    try:
        data = json.load(sys.stdin)
        if mode == "stop":
            return on_stop(data)
        if mode == "inject":
            return on_inject(data)
        return on_prompt(data)
    except Exception as exc:  # fail open, always
        try:
            common = common_fields(HookInput(data))
        except Exception:
            common = {"v": 2, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        log({**common, "event": "error", "mode": mode, "error": type(exc).__name__})
        return 0


if __name__ == "__main__":
    sys.exit(main())
