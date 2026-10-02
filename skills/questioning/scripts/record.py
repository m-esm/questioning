#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hooks"))
from envelope import redact_outbound  # noqa: E402

SSA_SCRIPTS = Path(__file__).resolve().parents[3] / "vendor" / "smart-subagents" / "scripts"
DEDUPE_MIN = 0.70
DEDUPE_CAP = 40
CAP_BYTES = 8000
FOLD_CANDIDATES = 5
TIMEOUT_S = 6.0
UNCHECKED = " (unchecked)"


def today() -> str:
    return date.today().isoformat()


def records_root() -> Path:
    return Path(os.environ.get("QUESTIONING_ROOT") or Path.home() / ".claude").expanduser()


def slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd))


def global_path() -> Path:
    return records_root() / "questioning" / "global.md"


def project_path(cwd: str) -> Path:
    return records_root() / "projects" / slug(cwd) / "questioning.md"


TITLE = "# Questioning record: "
DECISIONS = "Decisions"
HEADER = re.compile(r"^## (.+?)(?: \((\d{4}-\d{2}-\d{2})\))?\s*$")
CHANGED = re.compile(r"^_changed from: .*_\s*$")
ROUTES = {
    "global": "A fact or standing decision about the user that holds across all of their projects",
    "project": "A decision about this specific piece of work: its goal, scope, users, design or constraints",
    "memory": "A working preference or feedback about how the user wants Claude to work: process, communication, workflow rules",
    "none": "None of the other options fits",
}
MEMORY_NOTE = (
    "Route is memory: this answer is a working preference about how the user wants Claude to work. "
    "Save it as a feedback memory instead; nothing was written to the Q/A records."
)


class JevFailed(Exception):
    pass


def one_line(text: str) -> str:
    return " ".join((text or "").split())


def strip_unchecked(question: str) -> str:
    return question[: -len(UNCHECKED)] if question.endswith(UNCHECKED) else question


def label_for(scope: str, cwd: str) -> str:
    return "global" if scope == "global" else os.path.abspath(cwd)


def path_for(scope: str, cwd: str) -> Path:
    return global_path() if scope == "global" else project_path(cwd)


def is_decisions(section: dict) -> bool:
    return section["name"].casefold() == DECISIONS.casefold() and not section.get("date")


def parse(text: str) -> dict:
    doc: dict = {"preamble": [], "sections": []}
    current = None
    titled = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if current is None and not titled and line.startswith("# "):
            titled = True
            continue
        header = HEADER.match(line)
        if header:
            current = {"name": header.group(1).strip(), "date": header.group(2), "items": []}
            doc["sections"].append(current)
            continue
        if current is None:
            doc["preamble"].append(raw)
            continue
        items = current["items"]
        last = items[-1] if items and isinstance(items[-1], dict) else None
        if line.startswith("**Q:**"):
            items.append({"q": line[len("**Q:**"):].strip(), "a": None, "changed": []})
        elif line.startswith("**A:**") and last is not None and last["a"] is None:
            last["a"] = line[len("**A:**"):].strip()
        elif CHANGED.match(line) and last is not None and last["a"] is not None:
            last["changed"].append(line)
        else:
            items.append(raw)
    return doc


def render(doc: dict, label: str) -> str:
    out = [TITLE + label, *doc["preamble"]]
    for section in doc["sections"]:
        out.append("")
        out.append(f"## {section['name']} ({section['date']})" if section.get("date") else f"## {section['name']}")
        for index, item in enumerate(section["items"]):
            if isinstance(item, dict):
                if index:
                    out.append("")
                out.append(f"**Q:** {item['q']}")
                if item["a"] is not None:
                    out.append(f"**A:** {item['a']}")
                out.extend(item["changed"])
            else:
                out.append(item)
    return "\n".join(out) + "\n"


def load(path: Path) -> tuple[dict, str]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return parse(""), ""
    return parse(text), text


def make_dirs(path: Path) -> None:
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)


def write_atomic(path: Path, text: str) -> None:
    import tempfile
    make_dirs(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(path: Path, doc: dict, label: str, original: str) -> bool:
    text = render(doc, label)
    if text == original or (not original and not doc["sections"] and not doc["preamble"]):
        return False
    write_atomic(path, text)
    return True


class Locked:
    def __init__(self, *paths: Path):
        self.paths = sorted({str(p) for p in paths})
        self.handles: list = []

    def __enter__(self):
        import fcntl
        for name in self.paths:
            path = Path(name)
            make_dirs(path.parent)
            fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.handles.append(fd)
        return self

    def __exit__(self, *exc):
        import fcntl
        for fd in reversed(self.handles):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return False


def entries(doc: dict) -> list:
    found = []
    for si, section in enumerate(doc["sections"]):
        if is_decisions(section):
            continue
        for ii, item in enumerate(section["items"]):
            if isinstance(item, dict):
                found.append((section.get("date") or "", si, ii, section, item))
    return found


def newest_first(doc: dict) -> list:
    return sorted(entries(doc), key=lambda e: (e[0], e[1], e[2]), reverse=True)


def remove_item(doc: dict, section: dict, item: dict) -> None:
    section["items"] = [x for x in section["items"] if x is not item]
    if not section["items"]:
        doc["sections"] = [s for s in doc["sections"] if s is not section]


def topic_section(doc: dict, topic: str) -> dict:
    for section in doc["sections"]:
        if not is_decisions(section) and section["name"].casefold() == topic.casefold():
            return section
    section = {"name": topic, "date": today(), "items": []}
    doc["sections"].append(section)
    return section


def same_question(key: str) -> dict:
    return {
        "type": "noul",
        "instructions": f"Does `question` ask for the same decision or fact as `existing.{key}`, so that an answer to one also answers the other?",
        "criteria": {
            "true": "Both ask for the same decision or fact, even if worded differently",
            "false": "They ask about different things, or one is narrower or broader in a way that needs its own answer",
        },
    }


def judge(q: str, a: str, topic: str, cwd: str, docs: dict) -> dict:
    existing = {}
    for prefix, scope in (("g", "global"), ("p", "project")):
        for i, entry in enumerate(newest_first(docs[scope])[:DEDUPE_CAP]):
            existing[f"{prefix}{i}"] = entry
    state = {
        "question": redact_outbound(q),
        "answer": redact_outbound(a),
        "topic": redact_outbound(topic),
        "project": redact_outbound(os.path.abspath(cwd)),
        "existing": {k: redact_outbound(strip_unchecked(e[4]["q"])) for k, e in existing.items()},
    }
    questions = {
        "route": {
            "type": "choice",
            "instructions": "Where should the answer in `answer` to `question` be recorded?",
            "criteria": ROUTES,
        },
        **{"same_" + k: same_question(k) for k in existing},
    }
    try:
        sys.path.insert(0, str(SSA_SCRIPTS))
        from ssa import jev
        answers = jev.ask(state, questions, timeout=TIMEOUT_S).get("answers") or {}
        route_answer = answers.get("route") or {}
        choice = route_answer.get("choice")
        confidence = float(route_answer.get("confidence") or 0.0)
        same = {k: float((answers.get("same_" + k) or {}).get("noul", 0.0)) for k in existing}
    except Exception as exc:
        raise JevFailed(type(exc).__name__) from exc
    route = choice if choice in ("global", "project", "memory") else "project"
    return {"route": route, "choice": choice, "confidence": confidence, "existing": existing, "same": same}


def apply_answer(q: str, a: str, topic: str, judged: dict, docs: dict, cwd: str) -> dict:
    route = judged["route"]
    scores = {"route": {"choice": judged["choice"], "confidence": round(judged["confidence"], 2)}, "match": None}
    if route == "memory":
        return {"route": "memory", "action": "memory", "path": None, "note": MEMORY_NOTE, "scores": scores}
    doc = docs[route]
    prefix = "g" if route == "global" else "p"
    candidates = [(p, k) for k, p in judged["same"].items() if k.startswith(prefix)]
    best_p, best_k = max(candidates) if candidates else (0.0, None)
    if best_k is not None:
        scores["match"] = {"question": strip_unchecked(judged["existing"][best_k][4]["q"]), "p": round(best_p, 2)}
    if best_k is not None and best_p >= DEDUPE_MIN:
        section, item = judged["existing"][best_k][3], judged["existing"][best_k][4]
        if one_line(item["a"] or "") == one_line(a):
            section["date"] = today()
            action = "unchanged"
        else:
            item["changed"].insert(0, f"_changed from: {item['a'] or ''} ({today()})_")
            item["a"] = a
            item["q"] = strip_unchecked(item["q"])
            if section["name"].casefold() != topic.casefold():
                remove_item(doc, section, item)
                section = topic_section(doc, topic)
                section["items"].append(item)
            section["date"] = today()
            action = "replaced"
    else:
        section = topic_section(doc, topic)
        section["items"].append({"q": q, "a": a, "changed": []})
        section["date"] = today()
        action = "appended"
    return {"route": route, "action": action, "path": str(path_for(route, cwd)), "scores": scores}


def load_both(cwd: str) -> tuple[dict, dict]:
    docs, originals = {}, {}
    for scope in ("global", "project"):
        docs[scope], originals[scope] = load(path_for(scope, cwd))
    return docs, originals


def save_both(cwd: str, docs: dict, originals: dict) -> None:
    for scope in ("global", "project"):
        save(path_for(scope, cwd), docs[scope], label_for(scope, cwd), originals[scope])


def cmd_add(cwd: str, topic: str, q: str, a: str) -> dict:
    q, a, topic = one_line(q), one_line(a), one_line(topic)
    with Locked(global_path(), project_path(cwd)):
        docs, originals = load_both(cwd)
        try:
            judged = judge(q, a, topic, cwd, docs)
        except JevFailed as exc:
            section = topic_section(docs["project"], topic)
            section["items"].append({"q": q + UNCHECKED, "a": a, "changed": []})
            section["date"] = today()
            save_both(cwd, docs, originals)
            return {"route": "unchecked", "action": "appended", "path": str(project_path(cwd)),
                    "scores": None, "error": str(exc)}
        result = apply_answer(q, a, topic, judged, docs, cwd)
        if result["action"] != "memory":
            save_both(cwd, docs, originals)
        return result


def cmd_dedupe(cwd: str) -> list:
    import copy
    results: list = []
    with Locked(global_path(), project_path(cwd)):
        docs, originals = load_both(cwd)
        while True:
            pending = sorted(
                (e for e in entries(docs["project"]) if e[4]["q"].endswith(UNCHECKED)),
                key=lambda e: (e[0], e[1], e[2]),
            )
            if not pending:
                break
            _, si, ii, section, item = pending[0]
            trial = copy.deepcopy(docs)
            trial_section = trial["project"]["sections"][si]
            remove_item(trial["project"], trial_section, trial_section["items"][ii])
            q, a, topic = strip_unchecked(item["q"]), item["a"] or "", section["name"]
            try:
                judged = judge(q, a, topic, cwd, trial)
            except JevFailed as exc:
                results.append({"question": q, "action": "kept", "error": str(exc)})
                break
            result = apply_answer(q, a, topic, judged, trial, cwd)
            if result["action"] == "memory":
                result.update(question=q, answer=a)
            results.append(result)
            docs = trial
        save_both(cwd, docs, originals)
    return results


def cmd_fold(cwd: str, scope: str, topic: str, decision: str) -> tuple[int, dict]:
    path = path_for(scope, cwd)
    with Locked(path):
        doc, original = load(path)
        match = [s for s in doc["sections"] if not is_decisions(s) and s["name"].casefold() == one_line(topic).casefold()]
        if not match:
            return 1, {"error": f"no topic named {topic!r} in the {scope} record", "path": str(path)}
        doc["sections"] = [s for s in doc["sections"] if s is not match[0]]
        decisions = [s for s in doc["sections"] if is_decisions(s)]
        if decisions:
            target = decisions[0]
        else:
            target = {"name": DECISIONS, "date": None, "items": []}
            doc["sections"].insert(0, target)
        target["items"].append("- " + one_line(decision))
        save(path, doc, label_for(scope, cwd), original)
    return 0, {"action": "folded", "scope": scope, "topic": match[0]["name"], "path": str(path)}


def cmd_status(cwd: str) -> dict:
    out = {}
    for scope in ("global", "project"):
        path = path_for(scope, cwd)
        doc, text = load(path)
        found = entries(doc)
        topics = [s for s in doc["sections"] if not is_decisions(s)]
        size = len(text.encode())
        info = {
            "path": str(path),
            "exists": path.exists(),
            "bytes": size,
            "entries": len(found),
            "unchecked": sum(1 for e in found if e[4]["q"].endswith(UNCHECKED)),
            "topics": len(topics),
            "over_cap": size > CAP_BYTES,
        }
        if size > CAP_BYTES:
            oldest = sorted(topics, key=lambda s: s.get("date") or "")[:FOLD_CANDIDATES]
            info["fold_candidates"] = [
                {"topic": s["name"], "date": s.get("date"),
                 "entries": sum(1 for x in s["items"] if isinstance(x, dict))}
                for s in oldest
            ]
        out[scope] = info
    return out


def inject_text(cwd: str) -> str:
    parts = []
    for path in (global_path(), project_path(cwd)):
        try:
            parts.append(path.read_text().rstrip("\n") + "\n")
        except (FileNotFoundError, NotADirectoryError):
            continue
    return "\n".join(parts)


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="record.py", description="Living questioning Q/A records.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    for flag in ("--cwd", "--topic", "--q", "--a"):
        add.add_argument(flag, required=True)
    for name in ("dedupe", "status", "inject"):
        sub.add_parser(name).add_argument("--cwd", required=True)
    fold = sub.add_parser("fold")
    fold.add_argument("--cwd", required=True)
    fold.add_argument("--scope", required=True, choices=("global", "project"))
    fold.add_argument("--topic", required=True)
    fold.add_argument("--decision", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "add":
        print(json.dumps(cmd_add(args.cwd, args.topic, args.q, args.a)))
    elif args.cmd == "dedupe":
        print(json.dumps(cmd_dedupe(args.cwd)))
    elif args.cmd == "fold":
        rc, result = cmd_fold(args.cwd, args.scope, args.topic, args.decision)
        print(json.dumps(result))
        return rc
    elif args.cmd == "status":
        print(json.dumps(cmd_status(args.cwd)))
    else:
        sys.stdout.write(inject_text(args.cwd))
    return 0


def selftest() -> int:
    import tempfile
    from contextlib import redirect_stdout
    from io import StringIO
    global today, CAP_BYTES
    sys.path.insert(0, str(SSA_SCRIPTS))
    from ssa import jev
    old_ask, old_today, old_cap = jev.ask, today, CAP_BYTES
    old_root = os.environ.get("QUESTIONING_ROOT")
    bad: list = []
    calls: list = []
    knobs: dict = {"route": "project", "same": {}, "raise": False}
    day = ["2026-01-01"]

    def fake(state, questions, **kwargs):
        calls.append((json.loads(json.dumps(state)), sorted(questions), kwargs.get("timeout")))
        if knobs["raise"]:
            raise jev.JevUnavailable("down")
        answers = {"route": {"choice": knobs["route"], "confidence": 0.9}}
        for qid in questions:
            if qid.startswith("same_"):
                answers[qid] = {"noul": knobs["same"].get(state["existing"][qid[5:]], 0.05)}
        return {"model": "jev-test", "answers": answers}

    def fake_today() -> str:
        return day[0]

    def check(name, fn):
        try:
            fn()
        except Exception as exc:
            bad.append((name, type(exc).__name__, str(exc)))

    jev.ask, today = fake, fake_today
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["QUESTIONING_ROOT"] = tmp
            cwd = os.path.join(tmp, "work", "door")

            def run(*argv):
                buf = StringIO()
                with redirect_stdout(buf):
                    rc = main(list(argv))
                return rc, buf.getvalue()

            def add(topic, q, a):
                rc, out = run("add", "--cwd", cwd, "--topic", topic, "--q", q, "--a", a)
                assert rc == 0, out
                return json.loads(out)

            def project_text():
                return project_path(cwd).read_text()

            def slug_check():
                assert slug("/Users/moshe/.claude") == "-Users-moshe--claude"
                assert project_path(cwd) == Path(tmp) / "projects" / slug(cwd) / "questioning.md"
            check("slug", slug_check)

            def route_global():
                knobs["route"] = "global"
                out = add("Profile", "Which editor do you use?", "Neovim")
                assert out["route"] == "global" and out["action"] == "appended", out
                assert out["path"] == str(global_path())
                text = global_path().read_text()
                assert text.startswith("# Questioning record: global\n")
                assert "## Profile (2026-01-01)\n**Q:** Which editor do you use?\n**A:** Neovim\n" in text
                assert global_path().stat().st_mode & 0o777 == 0o600
                assert global_path().parent.stat().st_mode & 0o777 == 0o700
                assert not project_path(cwd).exists()
            check("route global append", route_global)

            def route_project():
                knobs["route"] = "project"
                out = add("Audience", "Who is the\naudience?", "Makers\nwith printers")
                assert out["route"] == "project" and out["action"] == "appended" and out["path"] == str(project_path(cwd))
                text = project_text()
                assert text.startswith("# Questioning record: " + os.path.abspath(cwd) + "\n")
                assert "**Q:** Who is the audience?\n**A:** Makers with printers\n" in text
                assert project_path(cwd).parent.stat().st_mode & 0o777 == 0o700
                assert (Path(tmp) / "projects").stat().st_mode & 0o777 == 0o700
                state = calls[-1][0]
                assert "same_g0" in calls[-1][1] and "same_p0" not in calls[-1][1]
                assert state["existing"] == {"g0": "Which editor do you use?"} and calls[-1][2] == TIMEOUT_S
            check("route project append", route_project)

            def planted_email():
                out = add("Contact", "Who signs off?", "Ada at ada@example.com")
                assert out["action"] == "appended"
                sent = json.dumps(calls[-1][0])
                assert "ada@example.com" not in sent and "[email]" in sent, sent
                assert "Ada at ada@example.com" in project_text()
                assert set(calls[-1][0]["existing"]) == {"g0", "p0"}
                assert calls[-1][0]["existing"]["p0"] == "Who is the audience?"
            check("planted email", planted_email)

            def replace_and_move():
                day[0] = "2026-01-05"
                knobs["same"] = {"Who is the audience?": 0.9}
                out = add("Users", "Who are the users?", "Print farms")
                assert out["action"] == "replaced" and out["scores"]["match"]["p"] == 0.9, out
                assert out["scores"]["match"]["question"] == "Who is the audience?"
                text = project_text()
                assert "## Audience" not in text
                assert ("## Users (2026-01-05)\n**Q:** Who is the audience?\n**A:** Print farms\n"
                        "_changed from: Makers with printers (2026-01-05)_\n") in text, text
            check("replace with topic move", replace_and_move)

            def unchanged():
                day[0] = "2026-01-06"
                before = project_text()
                out = add("Users", "Who is the audience?", "Print   farms")
                assert out["action"] == "unchanged", out
                after = project_text()
                assert after == before.replace("## Users (2026-01-05)", "## Users (2026-01-06)")
            check("unchanged", unchanged)

            def replace_again():
                day[0] = "2026-01-07"
                add("Users", "Who is the audience?", "Schools")
                text = project_text()
                assert ("**A:** Schools\n_changed from: Print farms (2026-01-07)_\n"
                        "_changed from: Makers with printers (2026-01-05)_\n") in text, text
            check("replace keeps older changed-from lines", replace_again)

            def memory_route():
                knobs.update(route="memory", same={})
                before = (global_path().read_text(), project_text())
                out = add("Workflow", "How should updates be reported?", "Short bullets")
                assert out["action"] == "memory" and out["route"] == "memory" and "memory" in out["note"]
                assert (global_path().read_text(), project_text()) == before
            check("memory route writes nothing", memory_route)

            def none_route():
                knobs["route"] = "none"
                out = add("Scope", "Is billing in scope?", "No")
                assert out["route"] == "project" and out["action"] == "appended"
            check("none route falls to project", none_route)

            def fail_open():
                knobs["raise"] = True
                out = add("Scope", "What is out of scope?", "Payments")
                assert out["route"] == "unchecked" and out["action"] == "appended", out
                assert "**Q:** What is out of scope?" + UNCHECKED + "\n**A:** Payments" in project_text()
            check("fail-open unchecked", fail_open)

            def dedupe_failure_keeps():
                before = project_text()
                rc, out = run("dedupe", "--cwd", cwd)
                assert rc == 0 and project_text() == before, out
                assert json.loads(out)[0]["action"] == "kept"
            check("dedupe keeps entry when Jev fails", dedupe_failure_keeps)

            def dedupe_clears():
                knobs.update({"raise": False, "route": "project", "same": {}})
                rc, out = run("dedupe", "--cwd", cwd)
                results = json.loads(out)
                assert rc == 0 and [r["action"] for r in results] == ["appended"], results
                text = project_text()
                assert UNCHECKED not in text and "**Q:** What is out of scope?\n**A:** Payments" in text
            check("dedupe clears the marker", dedupe_clears)

            def dedupe_memory():
                knobs["raise"] = True
                add("Workflow", "How terse should replies be?", "Very")
                knobs.update({"raise": False, "route": "memory"})
                rc, out = run("dedupe", "--cwd", cwd)
                results = json.loads(out)
                assert results[0]["action"] == "memory" and results[0]["answer"] == "Very", results
                assert "How terse" not in project_text() and "## Workflow" not in project_text()
                knobs["route"] = "project"
            check("dedupe memory route", dedupe_memory)

            def unknown_lines_survive():
                path = project_path(cwd)
                path.write_text(path.read_text().replace(
                    "## Contact (2026-01-01)\n", "## Contact (2026-01-01)\nNote: keep this line\n"))
                add("Contact", "Who pays?", "The client")
                text = project_text()
                assert "## Contact (2026-01-07)\nNote: keep this line\n" in text, text
                assert "**Q:** Who pays?" in text
            check("unknown lines survive", unknown_lines_survive)

            def fold():
                rc, out = run("fold", "--cwd", cwd, "--scope", "project", "--topic", "users",
                              "--decision", "Audience\nis schools")
                assert rc == 0, out
                text = project_text()
                assert "## Users" not in text and "Who is the audience?" not in text
                title = "# Questioning record: " + os.path.abspath(cwd)
                assert text.startswith(title + "\n\n## Decisions\n- Audience is schools\n"), text
                before = project_text()
                rc, out = run("fold", "--cwd", cwd, "--scope", "project", "--topic", "Nope", "--decision", "x")
                assert rc == 1 and "error" in json.loads(out) and project_text() == before
                rc, out = run("fold", "--cwd", cwd, "--scope", "global", "--topic", "Profile", "--decision", "Uses Neovim")
                assert rc == 0 and "- Uses Neovim" in global_path().read_text()
            check("fold", fold)

            def status():
                global CAP_BYTES
                CAP_BYTES = 100
                rc, out = run("status", "--cwd", cwd)
                info = json.loads(out)
                proj = info["project"]
                assert rc == 0 and proj["exists"] and proj["over_cap"] and proj["bytes"] == len(project_text().encode())
                assert proj["entries"] == 4 and proj["unchecked"] == 0 and proj["topics"] == 2, proj
                dates = [c["date"] for c in proj["fold_candidates"]]
                assert dates == sorted(dates) and len(dates) <= FOLD_CANDIDATES
                assert {"topic", "date", "entries"} <= set(proj["fold_candidates"][0])
                CAP_BYTES = 10 ** 6
                rc, out = run("status", "--cwd", cwd)
                assert "fold_candidates" not in json.loads(out)["project"]
                assert json.loads(out)["global"]["exists"] is True
            check("status", status)

            def inject():
                rc, out = run("inject", "--cwd", cwd)
                assert rc == 0 and out == inject_text(cwd)
                assert out.startswith("# Questioning record: global\n")
                assert "\n\n# Questioning record: " + os.path.abspath(cwd) + "\n" in out
                other = os.path.join(tmp, "elsewhere")
                assert inject_text(other) == global_path().read_text()
                os.environ["QUESTIONING_ROOT"] = os.path.join(tmp, "empty-root")
                rc, out = run("inject", "--cwd", cwd)
                assert rc == 0 and out == "" and inject_text(cwd) == ""
                os.environ["QUESTIONING_ROOT"] = tmp
            check("inject", inject)
    finally:
        jev.ask, today, CAP_BYTES = old_ask, old_today, old_cap
        if old_root is None:
            os.environ.pop("QUESTIONING_ROOT", None)
        else:
            os.environ["QUESTIONING_ROOT"] = old_root
    for b in bad:
        print("WRONG:", b)
    print("selftest", "FAIL" if bad else "OK", "questioning record")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(selftest() if "--test" in sys.argv else main(sys.argv[1:]))
