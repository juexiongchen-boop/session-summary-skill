---
name: session-summary-card
description: "Configure Claude Code to auto-summarize each session into a colored terminal card, persist curated knowledge into memory/ files, and inject a single-line pointer into CLAUDE.md so the next session remembers prior work. Uses an atomic lock file to prevent TOCTOU races between concurrent Stop hooks. Use when the user asks for 'session-end summary', 'daily summary', 'auto-summarize', 'remember past sessions', '跨 session 记忆', 'memory layer', 'atomic lock', 'TOCTOU', or wants a recap card when exiting a session."
version: 2.0.0
license: MIT
---

# Session Summary Card (v2 — memory/ + atomic lock)

Auto-summarize each Claude Code session as a colored ANSI terminal card and **persist the curated knowledge into a three-layer memory architecture** so the next session can recall it without bloating CLAUDE.md.

## What you get

After installing, every session with ≥5 user messages produces:

1. **`~/daily-summaries/YYYY-MM-DD-HHMMSS-<sid>.md`** — full structured summary (title, recap, decisions, metadata).
2. **`~/daily-summaries/YYYY-MM-DD-HHMMSS-<sid>.ans`** — same digest rendered as a colored box-drawing card with ANSI escapes; `cat` it from a truecolor terminal.
3. **A per-session memory file** at `~/.claude/projects/-root/memory/auto-summary-{ts}-{sid}.md` — frontmatter + curated summary + decisions + links.
4. **A single-line pointer appended to `~/.claude/CLAUDE.md`** — visible in the next session's context automatically, pointing to the MEMORY.md index.
5. **The MEMORY.md index** — auto-curated list of all session memory files (max 20 entries).

The displayable card never works in the TUI (it tears down at session exit), so persistence-into-memory is the reliable channel.

## Install (≤30 seconds)

```bash
# From the skill directory:
./install.sh                  # installs into $HOME
./install.sh --home /tmp/foo  # installs into a custom HOME (for testing)

# Idempotent — re-running is safe.
```

The installer:

1. Copies `scripts/daily-summary.py` and `stress-test-dedup.py` to `~/.claude/scripts/`, marks them executable.
2. Deep-merges SessionEnd + Stop hooks into `~/.claude/settings.json` (preserves all other settings, including any pre-existing hook entries on other events).
3. Creates `~/.claude/projects/-root/memory/` if missing.
4. Appends a `<!-- BEGIN/END daily-summary:auto -->` pointer block to `~/.claude/CLAUDE.md` if not already present.

Restart Claude Code, then end any session with `/exit` or Ctrl-D after ≥5 user messages.

## Architecture (three-layer persistence)

```
        ┌────────────┐
        │  Stop hook │  fires after each Claude response
        │  SessionEnd│  fires on session exit
        └─────┬──────┘
              │ stdin = {transcript_path, session_id, …}
              ▼
   ┌──────────────────────────────────────────────┐
   │ daily-summary.py                             │
   │  1. acquire atomic lock (.summary.lock)      │ ← O_CREAT|O_EXCL,
   │  2. dedup check (.md mtime, 180s window)     │   prevents TOCTOU
   │  3. parse JSONL, skip if < 5 user msgs       │   between concurrent
   │  4. claude -p "<prompt>"  (sandboxed $HOME)  │   Stop hooks
   │  5. parse sections (TITLE/SUMMARY/DECISIONS) │
   │  6. write .md + .ans  → ~/daily-summaries/   │
   │  7. write memory file → ~/.claude/projects/- │
   │     root/memory/auto-summary-{ts}-{sid}.md   │
   │  8. append to MEMORY.md index (≤ 20 entries) │
   │  9. ensure CLAUDE.md has single pointer line │
   │  10. release lock                            │
   └──────────────────────────────────────────────┘
              │              │              │
              ▼              ▼              ▼
   ┌──────────────┐  ┌────────────┐  ┌──────────────┐
   │  daily-      │  │  memory/   │  │  CLAUDE.md   │
   │  summaries/  │  │  auto-*.md │  │  (1-line ptr)│
   │  (full       │  │  (curated) │  │  (auto-load  │
   │   archive)   │  │  + MEMORY  │  │   next sess) │
   │              │  │   .md idx  │  │              │
   └──────────────┘  └────────────┘  └──────────────┘
                                              │
                                              ▼
                                    Next session reads
                                    MEMORY.md → loads
                                    curated knowledge
```

### Why three layers, not one big CLAUDE.md

The old v1 design stuffed every session's summary into CLAUDE.md until `CLAUDE_MD_MAX_KEEP=10` rolled the oldest out. That loses knowledge and bloats context.

The v2 design splits responsibility:

| Layer | Path | Who writes | Capacity | Purpose |
|---|---|---|---|---|
| `CLAUDE.md` | `~/.claude/CLAUDE.md` | user + skill | 1 pointer block | user-controlled global instructions + breadcrumb |
| `memory/` | `~/.claude/projects/-root/memory/` | hook auto + user-curated | `MEMORY_INDEX_MAX_KEEP=20` | distilled, structured, persisted knowledge |
| `daily-summaries/` | `~/daily-summaries/` | hook auto | unbounded | full archive (every session ever) |

CLAUDE.md only carries a pointer line, so context stays small. Memory files are loaded on demand. Daily-summaries are for users who want to grep their history.

### Why an atomic lock (and not just mtime dedup)

The v1 dedup checked `time.time() - latest_mtime < 180s`. This is a TOCTOU race: multiple Stop hooks firing concurrently all read the same old mtime, all judge it as expired, and all run `claude -p` + write new files. Symptom: 1-second-spaced duplicate .md files.

The v2 dedup adds an `O_CREAT|O_EXCL` lock file (`~/.claude/daily-summaries/.summary.lock`) BEFORE the mtime check. Only one process wins; others `FileExistsError` and return 0 immediately. Stale locks (>10 min) are broken automatically — for crash recovery.

Validate locally: `python3 scripts/stress-test-dedup.py 20` — expect 1 winner, 19 fast skips.

## Configuration (env vars)

Set these before running the script (e.g., in `~/.bashrc`):

| Var | Default | Effect |
|---|---|---|
| `DAILY_SUMMARY_MIN_MSG` | `5` | Skip if user messages < N |
| `DAILY_SUMMARY_DEDUP_SECS` | `180` | Skip if a fresh .md exists |
| `DAILY_SUMMARY_LOCK_STALE_SECS` | `600` | Break locks older than N seconds |
| `DAILY_SUMMARY_DIR` | `~/daily-summaries` | Override SUMMARY_DIR (useful for hermetic testing) |

## Uninstall

```bash
./uninstall.sh                 # removes from $HOME
./uninstall.sh --home /tmp/foo # removes from a custom HOME
```

The uninstaller:

1. Removes SessionEnd + Stop hook entries from `~/.claude/settings.json` (only if they were added by this skill; preserves any others).
2. Strips the `<!-- BEGIN/END daily-summary:auto -->` block from `~/.claude/CLAUDE.md`.
3. Deletes `~/.claude/scripts/daily-summary.py` and `stress-test-dedup.py`.
4. **Preserves** `~/daily-summaries/` and `~/.claude/projects/-root/memory/` — these may contain real history the user wants.

## Customization

- **Card style**: edit the `CLR_*` constants and `build_card()` in `scripts/daily-summary.py`. Current style is bold-magenta double-line borders, 24-bit near-white title, magenta `✦` bullets, italic-green filename.
- **Summary language**: edit the `run_claude_summarize()` prompt (currently Chinese).
- **Memory structure**: edit the frontmatter and body shape in `_write_session_memory()`.
- **Pointer wording**: edit `_set_claude_md_pointer()`.

## Troubleshooting

**Q: Card doesn't appear in TUI when session ends.**
A: Expected. TUI tears down before any rendering. Check `~/daily-summaries/<ts>-<sid>.ans` (the file is written regardless). Read it with `cat` or `less -R`.

**Q: New session doesn't know what we did before.**
A: Check that CLAUDE.md has the pointer line. If it does, MEMORY.md should be auto-loaded next session. If MEMORY.md is empty, no prior sessions have been summarized.

**Q: Many duplicate .md files appear in quick succession.**
A: This was the v1 TOCTOU bug — fixed by the atomic lock in v2. If you still see this, the lock file may be stuck (stale). Delete `~/.claude/daily-summaries/.summary.lock` manually. Run `python3 scripts/stress-test-dedup.py 10` to verify the fix is in place.

**Q: I edited settings.json by hand and broke JSON syntax.**
A: Run `python3 -c "import json; json.load(open('$HOME/.claude/settings.json'))"` to validate. The installer also validates before writing.

## Distribution

To install on another machine:

```bash
# Tarball:
tar czf session-summary-card.tar.gz -C ~/.claude/skills session-summary-card
# transfer…
mkdir -p ~/.claude/skills && tar xzf session-summary-card.tar.gz -C ~/.claude/skills
./~/.claude/skills/session-summary-card/install.sh

# Or git:
git clone <repo> /tmp/ssc && /tmp/ssc/install.sh

# Or one-liner curl (if published):
curl -L https://example.com/session-summary-card.tar.gz | tar xz -C ~/.claude/skills && \
  ~/.claude/skills/session-summary-card/install.sh
```

To publish as a Claude Code marketplace plugin, see `https://docs.claude.com/en/docs/claude-code/plugins` — add a `.claude-plugin/marketplace.json` to the directory.

## Related

- `~/.claude/projects/-root/memory/session-summarization-preference.md` — feedback memory with the same architectural guidance.
- `~/.claude/projects/-root/memory/auto-summary-architecture-v2.md` — the changelog / architecture-decision record.