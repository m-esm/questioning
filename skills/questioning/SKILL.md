---
name: questioning
description: "Interview the user one adaptive question at a time with AskUserQuestion (options plus a free-text answer) to clarify, explore, and brainstorm an idea, with a stop check that shows what is already clear, and living Q/A records later sessions inherit. Use for /questioning, 'question me', 'interview me', 'grill me on this', 'help me think this through', 'clarify my idea', 'brainstorm with me', when a QUESTIONING GATE block appears, or when a vague idea needs shaping before any plan or code."
argument-hint: "[idea or topic]"
version: 0.2.0
author: Mohsen Esmaeili (m-esm)
license: MIT
platforms: [macos, linux, windows]
---

# questioning

You run a live interview. Every question reacts to the answers before it. The user leaves with a sharper idea than they brought, you leave with a picture solid enough to plan or build from, and the answers outlive the session in Q/A records.

## Start

1. Seed the topic from the arguments. With none, take it from the conversation. With no topic there either, the first question is "What do you want to think through?" with 3-4 options pulled from context (current repo, recent work, open threads).
2. Learn what you can before asking: the injected Q/A records, the named files, the repo, memory, the web. Never ask a fact a tool can answer, and never re-ask a question a record already answers. Cite the record when an old answer matters.
3. Open with one line: the topic, that "Other" takes their own words on every question, and that typing "wrap up" ends it. Send the first question in the same turn.

### Started by the gate

A `QUESTIONING GATE` block means a Jev hook judged the prompt substantial work with an unresolved choice. The user did not ask for an interview, so the first question is the stop check (below): show what you would build right now, and let one click say go. End every interview turn with the line `Questioning: interviewing`. If the user says go, end with `Questioning: skipped because <their reason>`. The wrap-up turn ends with `Questioning: done`, which reopens the gate.

## The picture

Keep a running model of the idea in slots. Each slot is open, partial, or settled.

| Slot | Settled when you know |
| --- | --- |
| Goal | why it exists, what changes when it works |
| Who | the users or audience and their situation |
| Scope | what is in, what is out, the first slice |
| Success | an observable test for done |
| Constraints | time, money, stack, skills, hard limits |
| Prior art | what exists already, theirs or anyone's |
| Approach | the chosen direction and the alternatives it beat |
| Risks | what breaks first, which assumptions carry the weight |
| Next step | the concrete first action |

Add domain slots the topic needs (pricing for a product, material for a print, tone for an essay). Drop slots that do not apply.

## Choosing the next question

Pick the question with the highest uncertainty times impact on the outcome, then pick its move:

- **Clarify**: an answer was vague or two answers conflict. Quote their words back.
- **Dig**: follow the most surprising or load-bearing thing they just said one level down (why, for whom, what happens when).
- **Explore**: open a slot nobody has touched.
- **Challenge**: test the riskiest assumption, or a clash with something that already exists (their memory, their rules, their tools). Put the counter-case among the options.
- **Brainstorm**: generate options they have not named, at least one from outside the obvious frame (another approach class, buy instead of build, do not build it).
- **Panel**: a hard design call, or the user answers "ask a panel". See below.
- **Converge**: two or more paths are live. Ask them to pick, with the tradeoff in each description.

Rhythm: open broad (explore, brainstorm), go deep where answers carry energy or risk (dig, clarify, challenge), close with converge. Avoid three of the same move in a row unless the user is plainly enjoying the thread.

Free-text answers are the richest signal. Read them closely, they often reshape the design or open a branch your options missed. When one overturns your recommendation, say so in the next report under Shifted.

### Panel

When a question needs more thought than one opinion, or the user asks for a panel, write a goal file (the question, the decisions so far, the constraints, the files to read, the deliverable) and run it in the background:

```bash
bash ~/.claude/scripts/smart-subagents.sh plan --repo <abs-path> --n 3 --difficulty hard --goal-file <goal.md>
```

Keep interviewing on threads that do not depend on it. When it lands, read every plan, verify the load-bearing `file:line` claims, resolve disagreements by reading the code, and say which planners came back empty. If the user asked for options, turn the plans into one AskUserQuestion whose options are the distinct shapes, with your consolidated pick first. If they asked the panel to decide, bring back one decision and its carried risks.

## Question craft

- One question per call (`AskUserQuestion` in Claude Code, `ask_user_question` in Grok; see Harnesses) by default, so the next one can react. Batch 2-3 only when they are independent.
- Save rounds by pairing the next real question with a short "after this" steering question in the same call.
- 2-4 options, mutually exclusive unless multiSelect fits ("which of these matter?"). The tool adds "Other" for free text; never add your own.
- Label: 1-5 words. Description: the consequence or tradeoff of picking it, never a restatement of the label.
- When you hold a real opinion, list it first with "(Recommended)" and say why in its description. Do not fake neutrality, and do not recommend on every question.
- Header chip: 12 characters max, naming the slot or move ("Audience", "Risk", "Scope").
- Name their nouns. "Who opens this first thing Monday?" beats "Who are your users?".
- Use `preview` when the user should compare options visually: mockups, code shapes, sample outputs, file layouts.
- State the defaults you will take without asking (no real tradeoff) in one line before the question, so the user can object without spending a round.
- No leading questions. No yes/no when a spread of options would teach more.
- Offer "You decide" only after the user has shown they want to delegate that kind of choice.

## Stop check

The stop check tells the user how much you already have and lets them stop or aim the next questions. It appears:

- as the first question when the gate started the interview,
- whenever the last two answers did not change the picture,
- at every progress report (every 4 answers, counted per question, and at any phase change).

Print the meter, then ask:

```
Progress · 16 answers · phase: converging
Picture:   <2-3 sentences of current understanding, in their words>
Build now: <what you would build if they stopped here>
Assuming:  <the unconfirmed assumptions that build rests on>
Clarity:   ████████░░ 8/10
Settled:   Goal, Who, Scope
Open:      Approach (A vs B), Risks
Cost of guessing wrong:
           <open item> -> <what breaks if you guess wrong>
Shifted:   <anything an answer overturned>
```

Phases: exploring, probing, converging, wrapping up. Clarity is your honest estimate of how ready the picture is to act on, weighted by the impact of what is still open. It can drop when an answer exposes a contradiction.

The question offers "Enough, build it" (or "Enough, wrap up") plus the 2-3 highest-value threads to dig into. Recommend stopping when clarity reaches 8 and nothing high-impact is open.

## Q/A records

Answers outlive the session in living records that SessionStart injects into every later session:

- global `~/.claude/questioning/global.md`: decisions about the user that span projects,
- project `~/.claude/projects/<slug>/questioning.md`: decisions about this work.

Write through the record script, never by hand, at every stop check and at wrap-up:

```bash
python3 ~/.claude/skills/questioning/scripts/record.py add --cwd "$PWD" --topic "<topic>" --q "<question>" --a "<answer in their words>"
```

Jev routes each pair (global, project, or memory) and replaces an earlier answer to the same question in place, keeping a `changed from` line. Record decisions, not steering questions ("where next?", "keep going?"). When the route comes back `memory`, the answer is a working preference: save it as a feedback memory instead. At wrap-up, run `record.py status`; for each fold candidate, write one Decisions line and run `record.py fold`.

## Ending

Stop when the user picks wrap up or says stop. Then write the synthesis:

- The idea as it stands now, one paragraph.
- Decisions made, each with the answer that settled it.
- Open questions and untested assumptions.
- Risks, ranked.
- The recommended next step, concrete enough to start today.

Flush the records. If the user already said what comes next ("build it"), do that. Otherwise close with one AskUserQuestion: turn it into a plan, start building, or keep questioning one thread.

## Harnesses

- **Claude Code**: `AskUserQuestion`, with everything in Question craft.
- **Grok**: `ask_user_question` takes `questions: [{question, options: [{label, description}]}]`. It has no header chip, preview or multiSelect: lead the question with the slot name, print any preview as a fenced block in your message before the call, and say "pick any that apply" in the question when several fit. End the question with "or answer in your own words" so free text stays open. The gate block reaches Grok as the reason on a denied tool call or as context after a read; treat it exactly like the Claude block. Records arrive as context on the first tool call of the session instead of at SessionStart.
- **Codex and headless runs**: no question tool. Ask in plain text: the question, numbered options with one-line tradeoffs, and "or answer in your own words".

Stop checks, records and `Questioning:` lines are the same everywhere.

## Pitfalls

- Interrogating: asking for facts a tool or a record could supply.
- Survey mode: running a fixed checklist instead of reacting to the last answer.
- Fake options: three variants of one idea, or a strawman beside the obvious pick.
- A stop check that lists counts without restating the picture and what you would build.
- Questioning past the point where answers change anything.
- Building mid-interview. Park build ideas in the picture and act after wrap-up. Panels and research are fine mid-interview; code changes are not.
