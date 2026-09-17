#!/usr/bin/env python3
"""skill-drift-doctor - post-installation health checks for agent skills.

Zero-dependency CLI that guards SKILL.md-based agent skills against the
failures that happen AFTER installation:
  - structural rot   (broken frontmatter, missing description)
  - integrity drift  (files overwritten by upgrades, human edits clobbered)
  - injected-block tampering (AI-injected sections must stay append-only)
  - referential drift (docs reference files/commands that no longer exist)

Commands:
  check     validate SKILL.md structure and frontmatter
  baseline  create a byte-level integrity baseline (.skills-drift.json)
  verify    compare current tree against baseline (drift report)
  refs      referential integrity: declared files/commands vs reality
  report    run everything, emit JSON

Copyright 2026 agent-memory-doctor Open Source Team. Apache-2.0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TOOL_VERSION = "0.1.0"
BASELINE_NAME = ".skills-drift.json"

# Known AI-tool injection signatures. A block starting with one of these
# markers is treated as machine-injected content appended to a skill.
# Injected blocks MUST be append-only: edits inside them = tampering.
INJECTION_START_SIGS = (
    "<!-- sdd:injected",
    "<!-- injected",
    "## Deep Enhancement",
    "## 深度增强",
    "*This block was",
    "*本块由",
)
# End-of-block signatures close an injected region.
INJECTION_END_SIGS = (
    "<!-- /sdd:injected",
    "<!-- /injected",
    "*End of injected block*",
    "*注入块结束*",
)

SKILL_FILE = "SKILL.md"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DESC_MAX = 1024
NAME_MAX = 64


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------
@dataclass
class Finding:
    """One diagnosable issue. severity: ok | info | warn | fail"""

    skill: str
    check: str
    severity: str
    message: str

    def as_dict(self) -> dict:
        return {"skill": self.skill, "check": self.check,
                "severity": self.severity, "message": self.message}


@dataclass
class SkillResult:
    name: str
    path: str
    findings: list = field(default_factory=list)

    def add(self, check: str, severity: str, message: str) -> None:
        self.findings.append(Finding(self.name, check, severity, message))

    @property
    def ok(self) -> bool:
        return not any(f.severity == "fail" for f in self.findings)


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------
def discover_skills(root: Path) -> list:
    """Find skill dirs: a dir containing SKILL.md, searched recursively."""
    skills = []
    if (root / SKILL_FILE).is_file():
        skills.append(root)
    else:
        for p in sorted(root.rglob(SKILL_FILE)):
            skills.append(p.parent)
    return skills


# --------------------------------------------------------------------------
# check: structure & frontmatter
# --------------------------------------------------------------------------
def split_frontmatter(text: str):
    """Return (meta_lines, body, ok). Handles CRLF and LF."""
    norm = text.replace("\r\n", "\n")
    if not norm.startswith("---"):
        return None, norm, False
    end = norm.find("\n---", 3)
    if end == -1:
        return None, norm, False
    meta = norm[3:end].strip("\n").split("\n")
    body = norm[end + 4:]
    return meta, body, True


def parse_simple_yaml(meta_lines):
    """Minimal flat key: value parser (no nesting, no pip deps)."""
    out = {}
    for line in meta_lines:
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip("\"'")
    return out


def check_skill(skill_dir: Path) -> SkillResult:
    res = SkillResult(name=skill_dir.name, path=str(skill_dir))
    skill_md = skill_dir / SKILL_FILE
    if not skill_md.is_file():
        res.add("structure", "fail", f"{SKILL_FILE} missing")
        return res

    text = skill_md.read_text(encoding="utf-8", errors="replace")
    meta_lines, _body, ok = split_frontmatter(text)
    if not ok:
        res.add("frontmatter", "fail", "missing or unterminated YAML frontmatter (--- ... ---)")
        return res

    meta = parse_simple_yaml(meta_lines)
    name = meta.get("name")
    if not name:
        res.add("frontmatter", "fail", "frontmatter 'name' is required")
    elif not NAME_RE.match(name):
        res.add("frontmatter", "fail",
                f"name '{name}' invalid (lowercase alnum/hyphen, max {NAME_MAX} chars)")
    elif name != skill_dir.name:
        res.add("frontmatter", "warn",
                f"name '{name}' does not match directory name '{skill_dir.name}'")

    desc = meta.get("description")
    if not desc:
        res.add("frontmatter", "fail", "frontmatter 'description' is required")
    elif len(desc) > DESC_MAX:
        res.add("frontmatter", "fail",
                f"description too long ({len(desc)} > {DESC_MAX} chars)")
    elif len(desc) < 20:
        res.add("frontmatter", "warn", "description suspiciously short (<20 chars)")

    # referenced-but-missing local files (see refs for the full pass)
    return res


# --------------------------------------------------------------------------
# refs: referential integrity (declared vs real)
# --------------------------------------------------------------------------
REF_PATTERNS = (
    re.compile(r"`?([\w./\\-]+\.(?:py|sh|js|ts|rb|go|md|json|yaml|yml|toml|txt))`?"),
    re.compile(r"(?:python3?|node|bash|\./)\s+([\w./\\-]+\.(?:py|sh|js|ts))"),
)


def refs_skill(skill_dir: Path) -> SkillResult:
    res = SkillResult(name=skill_dir.name, path=str(skill_dir))
    skill_md = skill_dir / SKILL_FILE
    if not skill_md.is_file():
        res.add("refs", "fail", f"{SKILL_FILE} missing")
        return res

    text = skill_md.read_text(encoding="utf-8", errors="replace")
    refs = set()
    for pat in REF_PATTERNS:
        refs.update(m.replace("\\", "/") for m in pat.findall(text))

    # external URLs / template placeholders / extension wildcards are not local refs
    local_refs = {r for r in refs
                  if not r.startswith(("http://", "https://", "<", "{"))
                  and "{" not in r and "xxx" not in r.lower()
                  and not Path(r).name.startswith(".")   # ".md/.txt" style wildcard prose
                  and "*" not in r}

    for ref in sorted(local_refs):
        target = (skill_dir / ref).resolve()
        if not target.exists():
            sev = "fail" if ref.startswith("scripts/") or ref.endswith((".py", ".sh", ".js", ".ts")) else "warn"
            res.add("refs", sev, f"referenced file does not exist: {ref}")

    # orphan scan: real files never mentioned anywhere in SKILL.md
    mentioned = " ".join(local_refs)
    for p in sorted(skill_dir.rglob("*")):
        if p.is_dir() or p.name in (BASELINE_NAME, SKILL_FILE, "LICENSE"):
            continue
        rel = p.relative_to(skill_dir).as_posix()
        if rel not in mentioned and p.suffix in (".py", ".sh", ".js", ".ts"):
            res.add("refs", "info", f"orphan file (never referenced in {SKILL_FILE}): {rel}")
    return res


# --------------------------------------------------------------------------
# integrity: baseline / verify  (the core innovation)
# --------------------------------------------------------------------------
def _classify_blocks(text: str):
    """Split normalized text into [(kind, text)] with kind human|injected."""
    norm = text.replace("\r\n", "\n")
    blocks, buf, kind, lines = [], [], "human", norm.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if any(line.strip().startswith(s) for s in INJECTION_START_SIGS):
            if buf:
                blocks.append((kind, "\n".join(buf)))
                buf = []
            kind = "injected"
            buf.append(line)
            # consume until end signature
            i += 1
            while i < len(lines):
                buf.append(lines[i])
                if any(lines[i].strip().startswith(e) for e in INJECTION_END_SIGS):
                    i += 1
                    break
                i += 1
            blocks.append((kind, "\n".join(buf)))
            buf, kind = [], "human"
            continue
        buf.append(line)
        i += 1
    if buf:
        blocks.append((kind, "\n".join(buf)))
    return blocks


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strip_end_sig(block_text: str) -> str:
    """Remove the trailing end-signature line, returning the appendable core."""
    lines = block_text.replace("\r\n", "\n").split("\n")
    while lines and (not lines[-1].strip()
                     or any(lines[-1].strip().startswith(e) for e in INJECTION_END_SIGS)):
        lines.pop()
    return "\n".join(lines)


def build_baseline(skill_dir: Path) -> dict:
    """Byte-level fingerprint: per-file hash + injected-region boundaries."""
    files = {}
    for p in sorted(skill_dir.rglob("*")):
        if p.is_dir() or p.name == BASELINE_NAME:
            continue
        rel = p.relative_to(skill_dir).as_posix()
        raw = p.read_bytes()
        entry = {"sha256": _sha(raw), "bytes": len(raw)}
        if p.suffix in (".md", ""):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                entry["encoding"] = "binary"
            else:
                injected = [b for k, b in _classify_blocks(text) if k == "injected"]
                if injected:
                    entry["injected_blocks"] = [
                        {"sha256": _sha(b.encode("utf-8")),
                         "core_sha256": _sha(_strip_end_sig(b).encode("utf-8")),
                         "core_len": len(_strip_end_sig(b).encode("utf-8"))}
                        for b in injected]
        files[rel] = entry
    return {
        "tool": "skill-drift-doctor",
        "version": TOOL_VERSION,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": files,
    }


def verify_skill(skill_dir: Path, baseline: dict) -> SkillResult:
    res = SkillResult(name=skill_dir.name, path=str(skill_dir))
    old = baseline.get("files", {})
    seen = set()
    for p in sorted(skill_dir.rglob("*")):
        if p.is_dir() or p.name == BASELINE_NAME:
            continue
        rel = p.relative_to(skill_dir).as_posix()
        seen.add(rel)
        raw = p.read_bytes()
        cur_sha = _sha(raw)
        if rel not in old:
            res.add("integrity", "info", f"new file since baseline: {rel}")
            continue
        prev = old[rel]
        if prev.get("sha256") == cur_sha:
            continue  # untouched
        # file changed - classify how
        if "injected_blocks" in prev:
            try:
                text = raw.decode("utf-8")
                cur_blocks = [b for k, b in _classify_blocks(text) if k == "injected"]
                prev_blocks = prev["injected_blocks"]
            except UnicodeDecodeError:
                cur_blocks, prev_blocks = [], prev["injected_blocks"]
            # append-only check: every baseline block's core must survive as a
            # byte-prefix of some current block's core (growth allowed at end).
            cur_cores = [_strip_end_sig(b).encode("utf-8") for b in cur_blocks]
            surviving = 0
            for pb in prev_blocks:
                if "core_sha256" not in pb:  # legacy full-block hash
                    continue
                prefix_ok = any(
                    len(cc) >= pb["core_len"] and _sha(cc[:pb["core_len"]]) == pb["core_sha256"]
                    for cc in cur_cores)
                if prefix_ok:
                    surviving += 1
            if prev_blocks and surviving == len(prev_blocks) and len(cur_blocks) >= len(prev_blocks):
                if len(cur_blocks) > len(prev_blocks) or any(
                        _sha(b.encode("utf-8")) != prev_blocks[i].get("sha256")
                        for i, b in enumerate(cur_blocks[:len(prev_blocks)])):
                    res.add("integrity", "ok",
                            f"{rel}: injected content append-only growth (surviving {surviving}/{len(prev_blocks)} blocks)")
                    continue
            if prev_blocks and surviving < len(prev_blocks):
                res.add("integrity", "fail",
                        f"{rel}: previously injected block modified or removed (append-only violation, {surviving}/{len(prev_blocks)} survive)")
                continue
        res.add("integrity", "fail",
                f"{rel}: content modified since baseline ({prev.get('bytes', '?')}B -> {len(raw)}B)")
    for rel in old:
        if rel not in seen:
            res.add("integrity", "fail", f"file deleted since baseline: {rel}")
    if not res.findings:
        res.add("integrity", "ok", "byte-identical to baseline")
    return res


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def sev_rank(s):
    return {"fail": 0, "warn": 1, "info": 2, "ok": 3}.get(s, 4)


def run_all(root: Path, baseline_root: Path | None):
    skills = discover_skills(root)
    report = {"tool": TOOL_VERSION, "root": str(root), "skills": [], "summary": {}}
    counts = {"fail": 0, "warn": 0, "info": 0, "ok": 0}
    for sd in skills:
        entry = {"name": sd.name, "path": str(sd), "findings": []}
        checks = [check_skill(sd), refs_skill(sd)]
        bfile = (baseline_root or root) / BASELINE_NAME
        if bfile.is_file():
            try:
                base = json.loads(bfile.read_text(encoding="utf-8"))
                per = base.get("skills", {}).get(sd.name)
                if per:
                    checks.append(verify_skill(sd, per))
                else:
                    checks.append(_mk_finding(sd.name, "integrity", "warn",
                                              "no baseline entry for this skill (run: sdd baseline)"))
            except (json.JSONDecodeError, KeyError):
                checks.append(_mk_finding(sd.name, "integrity", "warn",
                                          "baseline file unreadable"))
        else:
            checks.append(_mk_finding(sd.name, "integrity", "info",
                                      "no baseline yet (run: sdd baseline)"))
        for c in checks:
            entry["findings"].extend(f.as_dict() for f in c.findings)
        for f in entry["findings"]:
            counts[f["severity"]] += 1
        entry["ok"] = not any(f["severity"] == "fail" for f in entry["findings"])
        report["skills"].append(entry)
    report["summary"] = {**counts,
                         "skills": len(skills),
                         "healthy": sum(1 for s in report["skills"] if s["ok"])}
    return report


def _mk_finding(name, check, sev, msg):
    r = SkillResult(name=name, path="")
    r.add(check, sev, msg)
    return r


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _print_report(report) -> int:
    s = report["summary"]
    print(f"skill-drift-doctor v{TOOL_VERSION} | {s['skills']} skills | "
          f"{s['healthy']} healthy | fail={s['fail']} warn={s['warn']} info={s['info']}")
    for sk in report["skills"]:
        mark = "OK " if sk["ok"] else "ERR"
        print(f"\n[{mark}] {sk['name']} ({sk['path']})")
        for f in sorted(sk["findings"], key=lambda x: sev_rank(x["severity"])):
            print(f"  {f['severity'].upper():4} [{f['check']}] {f['message']}")
    return 1 if s["fail"] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="skill-drift-doctor",
                                 description="post-installation health checks for agent skills")
    ap.add_argument("command", choices=["check", "baseline", "verify", "refs", "report"])
    ap.add_argument("path", nargs="?", default=".", help="skill dir or skills repo root")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    root = Path(args.path).resolve()
    if not root.exists():
        print(f"error: path not found: {root}", file=sys.stderr)
        return 2

    if args.command in ("check", "refs"):
        skills = discover_skills(root)
        out = []
        for sd in skills:
            r = check_skill(sd) if args.command == "check" else refs_skill(sd)
            out.append({"name": sd.name, "path": str(sd),
                        "findings": [f.as_dict() for f in r.findings],
                        "ok": r.ok})
        if args.json:
            print(json.dumps(out, indent=1, ensure_ascii=False))
        else:
            for o in out:
                print(f"[{'OK ' if o['ok'] else 'ERR'}] {o['name']}")
                for f in sorted(o["findings"], key=lambda x: sev_rank(x["severity"])):
                    print(f"  {f['severity'].upper():4} {f['message']}")
        return 0 if all(o["ok"] for o in out) else 1

    if args.command == "baseline":
        bfile = root / BASELINE_NAME
        base = {"tool": "skill-drift-doctor", "version": TOOL_VERSION,
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "skills": {}}
        if bfile.is_file():
            try:
                base = json.loads(bfile.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        n = 0
        for sd in discover_skills(root):
            base["skills"][sd.name] = build_baseline(sd)
            n += 1
        base["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        bfile.write_text(json.dumps(base, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"baseline written: {bfile} ({n} skills fingerprinted)")
        return 0

    if args.command == "verify":
        bfile = root / BASELINE_NAME
        if not bfile.is_file():
            print(f"no baseline at {bfile}; run: sdd baseline", file=sys.stderr)
            return 2
        base = json.loads(bfile.read_text(encoding="utf-8"))
        fails = 0
        for sd in discover_skills(root):
            per = base.get("skills", {}).get(sd.name)
            if not per:
                print(f"[WARN] {sd.name}: no baseline entry (run: sdd baseline)")
                continue
            r = verify_skill(sd, per)
            for f in sorted(r.findings, key=lambda x: sev_rank(x.severity)):
                print(f"  {f.severity.upper():4} [{sd.name}] {f.message}")
            if not r.ok:
                fails += 1
        return 1 if fails else 0

    # report
    report = run_all(root, root)
    if args.json:
        print(json.dumps(report, indent=1, ensure_ascii=False))
    else:
        return _print_report(report)
    return 1 if report["summary"]["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
