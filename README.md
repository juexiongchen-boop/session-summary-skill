# session-summary-card

> Auto-summarize every Claude Code session into a colored terminal card, persist curated knowledge into `memory/`, and inject a single-line pointer into `CLAUDE.md` so the next session remembers what happened.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Quick start

```bash
./install.sh                  # install into ~/.claude/
# restart Claude Code
# ...have a conversation with ≥5 messages...
# end the session — auto-summary fires on each Stop hook and at SessionEnd
```

Verify it worked:

```bash
ls ~/daily-summaries/                    # should have <ts>-<sid>.md and .ans
cat ~/.claude/CLAUDE.md                  # should have a pointer line
cat ~/.claude/projects/-root/memory/MEMORY.md
```

To uninstall:

```bash
./uninstall.sh
```

## What it looks like

When the hook fires, you get a colored card (best viewed with `cat` or `less -R`):

```
╔══════════════════════════════════════════════╗
║ · 2026-06-30 12:34:56                       ║
╠══════════════════════════════════════════════╣
║ ◆ Refactor dedup to use atomic lock file    ║
║                                              ║
║ User wanted to eliminate TOCTOU race…        ║
║                                              ║
║ ▰ 决策 / 待办 (4)                           ║
║   ✦ Added _try_acquire_lock with O_EXCL     ║
║   ✦ Stress test passes 10/20 concurrent     ║
║   ✦ Stop hook now writes to memory/         ║
║   ✦ CLAUDE.md reduced to single pointer     ║
║                                              ║
║ ➜ 2026-06-30-123456-stress-t.md             ║
╚══════════════════════════════════════════════╝
```

(The TUI won't show this — it tears down before render. The `.ans` file is preserved on disk.)

## Three install paths

### A. Local tarball (no network)

```bash
# Source machine:
tar czf session-summary-card.tar.gz -C ~/.claude/skills session-summary-card
# Transfer the file via scp/rsync/USB…

# Target machine:
mkdir -p ~/.claude/skills
tar xzf session-summary-card.tar.gz -C ~/.claude/skills
~/.claude/skills/session-summary-card/install.sh
```

### B. Git clone

```bash
git clone <repo-url> /tmp/session-summary-card
/tmp/session-summary-card/install.sh
```

### C. Curl pipe (when published)

```bash
curl -L https://your-host/session-summary-card.tar.gz | \
  tar xz -C ~/.claude/skills && \
  ~/.claude/skills/session-summary-card/install.sh
```

## Architecture (one-minute tour)

Three persistence layers instead of one big CLAUDE.md:

| Layer | Path | Purpose |
|---|---|---|
| `CLAUDE.md` | `~/.claude/CLAUDE.md` | User-controlled instructions + **one-line pointer** to MEMORY.md |
| `memory/` | `~/.claude/projects/-root/memory/` | Per-session structured memory + MEMORY.md index (≤20 entries) |
| `daily-summaries/` | `~/daily-summaries/` | Full archive: every session ever (unbounded) |

Dedup uses an **atomic lock file** (`O_CREAT|O_EXCL`) — prevents the TOCTOU race that v1 had with mtime-only dedup. Validate:

```bash
python3 ~/.claude/skills/session-summary-card/scripts/stress-test-dedup.py 10
# expect: 1 winner, 9 fast skips
```

Read [SKILL.md](SKILL.md) for the full design rationale, configuration env vars, troubleshooting, and customization points.

## Configuration

| Env var | Default | What |
|---|---|---|
| `DAILY_SUMMARY_MIN_MSG` | `5` | Skip if user messages < N |
| `DAILY_SUMMARY_DEDUP_SECS` | `180` | Skip if a fresh .md exists |
| `DAILY_SUMMARY_LOCK_STALE_SECS` | `600` | Break locks older than N seconds |
| `DAILY_SUMMARY_DIR` | `~/daily-summaries` | Override SUMMARY_DIR (for hermetic testing) |

## License

[MIT](LICENSE) — do whatever, just keep the copyright notice.