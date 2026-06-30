#!/usr/bin/env python3
"""Clean removal of the session-summary-card skill.

Usage:
    uninstall.sh                # removes from $HOME
    uninstall.sh --home PATH    # removes from PATH instead (for testing)

What it does:
1. Removes SessionEnd + Stop hook entries from <home>/.claude/settings.json
   - Only removes entries that match this skill's command signature
   - Preserves any other hook events (PreToolUse, Notification, etc.)
   - Preserves any hooks on SessionEnd/Stop that don't match our command
2. Strips the `<!-- BEGIN/END daily-summary:auto -->` block from
   <home>/.claude/CLAUDE.md
3. Deletes <home>/.claude/scripts/daily-summary.py and stress-test-dedup.py
4. PRESERVES <home>/daily-summaries/ and <home>/.claude/projects/-*/memory/
   (these may contain real history the user wants)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Must match install.sh
HOOK_COMMAND = "python3 ~/.claude/scripts/daily-summary.py"
CLAUDE_MD_BEGIN = "<!-- BEGIN daily-summary:auto -->"
CLAUDE_MD_END = "<!-- END daily-summary:auto -->"
OWNED_SCRIPT_NAMES = ["daily-summary.py", "stress-test-dedup.py"]


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def remove_our_hooks(settings: dict) -> tuple[dict, list[str]]:
    """Strip hook entries whose command matches ours. Returns (new, notes)."""
    notes = []
    hooks = settings.get("hooks", {})
    if not hooks:
        return settings, ["  settings.json: no hooks block, nothing to do"]

    changed = False
    for event in ("SessionEnd", "Stop", "PostToolUseFailure"):
        entries = hooks.get(event, [])
        if not entries:
            continue
        kept = []
        removed = 0
        for entry in entries:
            sub_hooks = entry.get("hooks", [])
            new_subs = [sh for sh in sub_hooks if sh.get("command") != HOOK_COMMAND]
            removed += len(sub_hooks) - len(new_subs)
            if new_subs:
                kept.append({**entry, "hooks": new_subs})
        if removed:
            notes.append(f"  hooks.{event}: removed {removed} entry/entries")
            changed = True
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)

    if changed:
        settings["hooks"] = hooks
    else:
        notes.append("  hooks: no matching entries found, nothing removed")
    return settings, notes


def strip_claude_md(home: Path) -> list[str]:
    """Remove the BEGIN/END pointer block from CLAUDE.md."""
    path = home / ".claude" / "CLAUDE.md"
    notes = []
    if not path.is_file():
        return ["  CLAUDE.md: doesn't exist, nothing to do"]

    text = path.read_text(encoding="utf-8")
    # Match the block + any leading/trailing blank line on the same logical line
    pattern = re.compile(
        re.escape(CLAUDE_MD_BEGIN) + r".*?" + re.escape(CLAUDE_MD_END) + r"[ \t]*\n?",
        re.S,
    )
    new_text, n = pattern.subn("", text)
    if n == 0:
        notes.append("  CLAUDE.md: no pointer block found, nothing to do")
        return notes
    # Trim trailing extra blank lines but keep one
    new_text = new_text.rstrip() + "\n"
    atomic_write(path, new_text)
    notes.append(f"  CLAUDE.md: removed {n} pointer block(s)")
    return notes


def remove_scripts(home: Path) -> list[str]:
    notes = []
    scripts_dir = home / ".claude" / "scripts"
    for name in OWNED_SCRIPT_NAMES:
        path = scripts_dir / name
        if path.is_file():
            path.unlink()
            notes.append(f"  scripts/{name}: deleted")
        else:
            notes.append(f"  scripts/{name}: not present, skipped")
    return notes


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", default=os.environ.get("HOME", "/root"),
                   help="Target HOME directory (default: $HOME)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would change without writing")
    args = p.parse_args()

    home = Path(args.home).expanduser().resolve()
    if not home.is_dir():
        print(f"❌ --home {home} is not a directory", file=sys.stderr)
        return 1

    print(f"=== Uninstalling session-summary-card v2 ===")
    print(f"  home: {home}")
    if args.dry_run:
        print(f"  mode: DRY RUN")
    print()

    # Settings
    print("• Settings:")
    sp = home / ".claude" / "settings.json"
    if sp.is_file():
        try:
            settings = json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  ❌ settings.json is not valid JSON: {e}", file=sys.stderr)
            return 1
        new_settings, notes = remove_our_hooks(settings)
        for n in notes:
            print(n)
        if not args.dry_run:
            atomic_write(sp, json.dumps(new_settings, indent=2, ensure_ascii=False) + "\n")
            print(f"  settings.json: written ({sp.stat().st_size} bytes)")
    else:
        print("  settings.json: doesn't exist, nothing to do")

    # CLAUDE.md
    print("\n• CLAUDE.md:")
    for n in strip_claude_md(home):
        print(n)

    # Scripts
    print("\n• Scripts:")
    for n in remove_scripts(home):
        print(n)

    # Preserved (just informational)
    daily_dir = home / "daily-summaries"
    if daily_dir.is_dir():
        print(f"\n  ℹ️  Preserved: {daily_dir} ({sum(1 for _ in daily_dir.glob('*.md'))} .md files)")

    memory_root = home / ".claude" / "projects"
    if memory_root.is_dir():
        mem_count = sum(1 for _ in memory_root.glob("-*/memory/*.md"))
        if mem_count:
            print(f"  ℹ️  Preserved: {memory_root}/-*/memory/ ({mem_count} .md files)")

    print(f"\n✅ Done. Restart Claude Code to stop the hooks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())