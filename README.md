# questioning

A Claude Code (and Grok CLI) skill plus hook pack that stops an agent from starting substantial work on an ambiguous prompt. It interviews you one question at a time, shows a stop check of what is already clear, and keeps living Q/A records that later sessions inherit.

## What is in the pack

- `skills/questioning/SKILL.md`: the interview skill. Use `/questioning`, or let the gate trigger it.
- `skills/questioning/scripts/record.py`: writes global and per-project Q/A records (latest answer wins, deduped, compacted).
- `hooks/delegation-gate.py`: UserPromptSubmit and Stop hook. One batched [TypeSafe Jev](https://docs.typesafe.ai) call scores each prompt (substantial work? unresolved intent? isolable labor for delegation?) and injects a `QUESTIONING GATE` or `DELEGATION GATE` block. The Stop hook blocks once if the block was ignored.
- `hooks/questioning-session.py`: SessionStart hook that injects the records into each new session.
- `hooks/envelope.py`: shared stdin parsing (Claude snake_case and Grok camelCase) and outbound redaction.
- `vendor/smart-subagents/scripts/ssa/jev.py`: stdlib Jev client from [smart-subagents](https://github.com/m-esm/smart-subagents).

Everything fails open: no key, no network, or a timeout means no block.

## Install

```bash
./install.sh
```

Copies the files into `~/.claude` (override with `CLAUDE_HOME`) and runs each selftest. Then set `TYPESAFE_API_KEY` in your environment and add the hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 \"$HOME/.claude/hooks/delegation-gate.py\" prompt", "timeout": 8}]}],
    "SessionStart": [{"hooks": [{"type": "command", "command": "python3 \"$HOME/.claude/hooks/questioning-session.py\"", "timeout": 5}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "python3 \"$HOME/.claude/hooks/delegation-gate.py\" stop", "timeout": 8}]}]
  }
}
```

Grok CLI has no SessionStart or UserPromptSubmit, so wire both into PreToolUse in `~/.grok/hooks/guards.json`:

```json
{"hooks": {"PreToolUse": [
  {"hooks": [{"type": "command", "command": "python3 \"$HOME/.claude/hooks/delegation-gate.py\" inject", "timeout": 5}]},
  {"hooks": [{"type": "command", "command": "python3 \"$HOME/.claude/hooks/questioning-session.py\" inject", "timeout": 5}]}
]}}
```

## Privacy

Prompt text is redacted (IPs, emails, hosts, key=value secrets, PEM and token shapes, home paths) and clipped before it reaches Jev. A repo containing `.claude/no-questioning-context` sends no conversation context. `DELEGATION_GATE=0` turns the gate off.

## Tests

```bash
python3 hooks/delegation-gate.py --test
python3 hooks/questioning-session.py --test
python3 skills/questioning/scripts/record.py --test
```

MIT licensed.
