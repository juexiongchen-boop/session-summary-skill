#!/usr/bin/env python3
"""Idempotent installer for the session-summary-card skill.

Usage:
    install.sh                # installs into $HOME
    install.sh --home PATH    # installs into PATH instead (for testing)

What it does:
1. Copies scripts/*.py to <home>/.claude/scripts/ (chmod +x)
2. Deep-merges SessionEnd + Stop hooks into <home>/.claude/settings.json
   - Preserves all other top-level keys and all other hook events
   - Skips adding duplicate hook entries (matched by command path)
3. Creates <home>/.claude/projects/-root/memory/ if missing
4. Adds a `<!-- BEGIN/END daily-summary:auto -->` pointer block to
   <home>/.claude/CLAUDE.md if not already present
5. Uses atomic writes (write-temp-then-rename) to avoid corruption

Re-running is safe: each step checks current state and only adds what's missing.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

# ---- Constants for the skill's runtime footprint ---------------------------

# Marker commands the installer writes into settings.json. The canonical form
# uses ~/.claude/scripts/... so that the path is portable across machines.
# Windows doesn't ship `python3` (only `python`); also `~/.claude/...` works
# in bash but not cmd.exe, so on Windows we use the absolute path explicitly.
if os.name == "nt":
    HOOK_COMMAND = (
        f'python "{Path.home() / ".claude" / "scripts" / "daily-summary.py"}"'
    )
else:
    HOOK_COMMAND = "python3 ~/.claude/scripts/daily-summary.py"
HOOK_TIMEOUT = 120

# CLAUDE.md marker block — must match the BEGIN/END strings that
# daily-summary.py:_set_claude_md_pointer() emits.
CLAUDE_MD_BEGIN = "<!-- BEGIN daily-summary:auto -->"
CLAUDE_MD_END = "<!-- END daily-summary:auto -->"
CLAUDE_MD_POINTER_LINE = (
    "📇 Session history → see `~/.claude/projects/-root/memory/MEMORY.md`"
)

# Files we own (so uninstall.sh knows what to remove)
OWNED_SCRIPT_NAMES = ["daily-summary.py", "stress-test-dedup.py"]


# ---- Atomic write helper ----------------------------------------------------

def atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically: write to <path>.tmp then rename.
    Avoids partial writes if the process is interrupted mid-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


# ---- Settings.json merge ----------------------------------------------------

def load_settings(home: Path) -> dict:
    """Load ~/.claude/settings.json, returning {} if missing or malformed."""
    path = home / ".claude" / "settings.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"❌ {path} is not valid JSON: {e}", file=sys.stderr)
        print(f"   Fix it manually, then re-run this installer.", file=sys.stderr)
        sys.exit(1)


def hook_entry_already_present(hooks_list: list, command: str) -> bool:
    """True if any entry already runs daily-summary.py.

    Matches by EITHER exact command string OR by script basename, so:
      - `python3 /root/.claude/scripts/daily-summary.py`
      - `python3 ~/.claude/scripts/daily-summary.py`
      - `python /home/foo/.claude/scripts/daily-summary.py`
    all collapse to the same "already installed" entry. Prevents duplicates
    when the user previously installed with a different path form.
    """
    target_name = Path(command).name
    for entry in hooks_list:
        for sub in entry.get("hooks", []):
            existing_cmd = sub.get("command", "")
            if existing_cmd == command:
                return True
            if Path(existing_cmd).name == target_name:
                return True
    return False


def merge_settings(settings: dict, dry_run: bool) -> tuple[dict, list[str]]:
    """Add SessionEnd + Stop hooks if missing. Returns (new_settings, notes)."""
    notes = []
    hooks = settings.setdefault("hooks", {})

    for event in ("SessionEnd", "Stop", "PostToolUseFailure"):
        existing = hooks.get(event, [])
        if hook_entry_already_present(existing, HOOK_COMMAND):
            notes.append(f"  hooks.{event}: already present, skipped")
            continue
        new_entry = {
            "hooks": [
                {
                    "type": "command",
                    "command": HOOK_COMMAND,
                    "timeout": HOOK_TIMEOUT,
                }
            ]
        }
        existing.append(new_entry)
        hooks[event] = existing
        notes.append(f"  hooks.{event}: added (1 entry)")
    return settings, notes


# ---- CLAUDE.md pointer block ------------------------------------------------

def ensure_claude_md_pointer(home: Path, dry_run: bool) -> list[str]:
    """Add the BEGIN/END pointer block to ~/.claude/CLAUDE.md if missing.

    Preserves any user-written content outside the markers. If the BEGIN/END
    block already exists (with any content), leave it alone — this is
    idempotent and respects user edits to the pointer line itself.
    """
    path = home / ".claude" / "CLAUDE.md"
    notes = []
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""

    if CLAUDE_MD_BEGIN in existing and CLAUDE_MD_END in existing:
        notes.append(f"  CLAUDE.md: pointer block already present, skipped")
        return notes

    block = (
        f"\n{CLAUDE_MD_BEGIN}\n"
        f"{CLAUDE_MD_POINTER_LINE}\n"
        f"{CLAUDE_MD_END}\n"
    )
    new_content = existing.rstrip() + block if existing.strip() else block.lstrip()
    if not dry_run:
        atomic_write(path, new_content)
    notes.append(f"  CLAUDE.md: pointer block appended ({len(block)} bytes)")
    return notes


# ---- Scripts copy -----------------------------------------------------------

def install_scripts(home: Path, skill_dir: Path, dry_run: bool) -> list[str]:
    """Copy scripts/*.py to ~/.claude/scripts/ and chmod +x."""
    notes = []
    src_dir = skill_dir / "scripts"
    dst_dir = home / ".claude" / "scripts"

    if not dry_run:
        dst_dir.mkdir(parents=True, exist_ok=True)

    for name in OWNED_SCRIPT_NAMES:
        src = src_dir / name
        dst = dst_dir / name
        if not src.is_file():
            notes.append(f"  ⚠️  missing {src}, skipped")
            continue
        if dst.is_file() and dst.read_bytes() == src.read_bytes():
            notes.append(f"  scripts/{name}: already up-to-date")
            continue
        if not dry_run:
            shutil.copy2(src, dst)
            dst.chmod(0o755)
        notes.append(f"  scripts/{name}: copied + chmod 755")
    return notes


# ---- Memory dir init --------------------------------------------------------

def ensure_memory_dir(home: Path, project_name: str, dry_run: bool) -> list[str]:
    """Create ~/.claude/projects/-<project_name>/memory/ if missing.

    We always use `root` (the script's hardcoded default) regardless of the
    installer's cwd, because daily-summary.py writes to ~/.claude/projects/-root/memory.
    Mismatched names create orphan directories.
    """
    notes = []
    project_name = "root"  # align with daily-summary.py: MEMORY_DIR
    mem_dir = home / ".claude" / "projects" / f"-{project_name}" / "memory"
    if mem_dir.is_dir():
        notes.append(f"  memory/{project_name}: already exists")
        return notes
    if not dry_run:
        mem_dir.mkdir(parents=True, exist_ok=True)
        # Seed MEMORY.md if absent
        idx = mem_dir / "MEMORY.md"
        if not idx.is_file():
            idx.write_text("", encoding="utf-8")
    notes.append(f"  memory/{project_name}: created (seeded empty MEMORY.md)")
    return notes


# ---- Main -------------------------------------------------------------------

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

    skill_dir = Path(__file__).parent.resolve()
    if not (skill_dir / "SKILL.md").is_file():
        print(f"❌ {skill_dir} doesn't look like the skill directory (no SKILL.md)",
              file=sys.stderr)
        return 1

    cwd_name = Path.cwd().name or "root"
    print(f"=== Installing session-summary-card v2 ===")
    print(f"  home:    {home}")
    print(f"  skill:   {skill_dir}")
    print(f"  project: root (aligned with daily-summary.py default)")
    if args.dry_run:
        print(f"  mode:    DRY RUN")
    print()

    # 1. Scripts
    print("• Scripts:")
    for n in install_scripts(home, skill_dir, args.dry_run):
        print(n)

    # 2. Settings
    print("\n• Settings:")
    settings = load_settings(home)
    new_settings, notes = merge_settings(settings, args.dry_run)
    for n in notes:
        print(n)
    if not args.dry_run:
        # Atomic write of settings.json
        sp = home / ".claude" / "settings.json"
        atomic_write(sp, json.dumps(new_settings, indent=2, ensure_ascii=False) + "\n")
        print(f"  settings.json: written ({sp.stat().st_size} bytes)")

    # 3. Memory dir
    print("\n• Memory:")
    for n in ensure_memory_dir(home, cwd_name, args.dry_run):
        print(n)

    # 4. CLAUDE.md
    print("\n• CLAUDE.md:")
    for n in ensure_claude_md_pointer(home, args.dry_run):
        print(n)

    print(f"\n✅ Done. Restart Claude Code for hooks to take effect.")
    print(f"   Validate: cat {home}/.claude/CLAUDE.md | grep '{CLAUDE_MD_BEGIN}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())