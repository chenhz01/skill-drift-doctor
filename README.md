# skill-drift-doctor

**Post-installation health checks for agent skills.** Zero dependencies. One file. CLI + GitHub Action.

[![CI](https://github.com/chenhz01/skill-drift-doctor/actions/workflows/ci.yml/badge.svg)](https://github.com/chenhz01/skill-drift-doctor/actions) [![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

> Skills rot after installation. Everyone checks skills *before* install — nobody guards them *after*.

Agent skills (SKILL.md packages for Claude Code, OpenClaw, Hermes, and every harness adopting the format) are being installed at massive scale. Security scanners vet what you install. Linters vet what you publish. But the moment a skill lands in `.claude/skills/` or any skills directory, it enters a hostile lifecycle nobody covers:

- **Upgrades rewrite files** — a skill update silently clobbers your local customizations.
- **Other tools edit skill files** — harnesses append guidance blocks, agents append learned sections; an in-place edit inside those blocks breaks assumptions nobody re-checks.
- **Docs drift from reality** — SKILL.md says "run `scripts/analyze.py`" but the script was renamed or deleted upstream.
- **Structure rots** — frontmatter edits break the `name`/`description` contract that skill routers depend on.

`skill-drift-doctor` is the verification layer for that lifecycle. It is the skills-side companion to [agent-memory-doctor](https://github.com/chenhz01/agent-memory-doctor) (which verifies agent *memory* stores): one guards what agents remember, the other guards what agents are taught.

---

## What it checks

| Command | Failure class | What it catches |
|---------|--------------|-----------------|
| `check` | structural rot | missing/invalid frontmatter, bad `name` format, name/dir mismatch, oversized or missing `description` |
| `refs`  | referential drift | scripts/files referenced in SKILL.md that no longer exist; orphan scripts never referenced |
| `baseline` + `verify` | integrity drift | byte-level drift since baseline: modified files, deleted files; **AI-injected blocks must stay append-only** — any in-place edit inside an injected block is a violation |
| `report` | everything | all of the above, human or JSON |

### The append-only guarantee (the core idea)

Harnesses and agents increasingly *inject* content into skill files (enhancement sections, learned behaviors). The contract those systems rely on is simple: **injected blocks may grow, never mutate**. This tool enforces it byte-for-byte:

1. `sdd baseline` fingerprints every file (SHA-256) and every injected block (detected via known injection signatures, e.g. `<!-- sdd:injected -->`, `## Deep Enhancement`).
2. `sdd verify` later compares: identical = OK; block grew at the end = OK (append-only growth); a byte changed inside a previously-injected block = **append-only violation**; file deleted = fail.

This is a property check, not a security scan — it complements (not replaces) tools like prompt-injection scanners.

## Quick start

```bash
# no install, no deps — just run it
python skill_drift_doctor.py report ~/.claude/skills

# or per-command
python skill_drift_doctor.py check  ./my-skills
python skill_drift_doctor.py refs   ./my-skills
python skill_drift_doctor.py baseline ./my-skills   # fingerprint once
python skill_drift_doctor.py verify   ./my-skills   # check for drift any time
```

### In CI

```yaml
name: skills-health
on: [push, schedule]
jobs:
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python skill_drift_doctor.py verify ./skills
```

Commit the generated `.skills-drift.json` baseline — `verify` then fails CI the moment any skill file is rewritten, deleted, or an injected block is edited in place.

## How it compares

| Tool | Focus | Gap this fills |
|------|-------|----------------|
| static linters (e.g. agnix) | validate SKILL.md *format* | one-shot, no lifecycle state |
| security scanners (skill-sentinel, clawsec, …) | malicious code *pre-install* | nothing after install |
| conflict/precedence inspectors | overlapping triggers between skills | no per-file integrity |
| crypto signing (provenance tools) | who published a skill | heavy ecosystem; no drift semantics |

skill-drift-doctor owns the **post-install window**: baseline once, verify forever, append-only contract for injected content. All four compose fine — run them together.

## Design principles

- **Zero dependencies** — Python 3.9+ stdlib only. Runs anywhere Python runs.
- **Read-only** — never modifies your skills; only writes its own `.skills-drift.json`.
- **Exit codes you can gate on** — 0 healthy, 1 findings, 2 usage error.
- **JSON everywhere** — every command takes `--json` for tooling integration.

## Status

- Test suite: 13/13 green (unit tests cover all four commands and the append-only contract).
- Apache-2.0. Contributions welcome — especially new injection signatures for other harnesses.

## Sister project

**[agent-memory-doctor](https://github.com/chenhz01/agent-memory-doctor)** — zero-dependency health checks for agent *memory* stores (tamper/freshness/mojibake/marker-rot). Together: verify what your agents *remember* and what they were *taught*.
