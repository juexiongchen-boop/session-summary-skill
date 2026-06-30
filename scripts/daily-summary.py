#!/usr/bin/env python3
"""
daily-summary.py — SessionEnd hook for Claude Code.

Reads the session transcript, generates a Chinese summary via `claude -p`,
writes a markdown file under ~/daily-summaries/, and prints a JSON
`systemMessage` containing a terminal text card (box-drawing chars) that
Claude Code will render in the TUI when the session ends.

Silent-skip rules (no output, no file):
  - No transcript_path in hook input, or file does not exist
  - User-message count < 5 (configurable via MIN_USER_MESSAGES)
  - claude -p fails (logged to stderr, but no user-facing output)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime
from pathlib import Path

# Windows defaults to GBK for stdout/stderr; we emit emojis (📇) and
# box-drawing chars (╔═╗) that GBK can't encode. Force UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MIN_USER_MESSAGES = int(os.environ.get("DAILY_SUMMARY_MIN_MSG", "5"))
DEDUP_SECS = int(os.environ.get("DAILY_SUMMARY_DEDUP_SECS", "60"))  # 1 min (was 180)
LOCK_STALE_SECS = int(os.environ.get("DAILY_SUMMARY_LOCK_STALE_SECS", "600"))  # 10 min
ERROR_DEDUP_SECS = int(os.environ.get("DAILY_SUMMARY_ERROR_DEDUP_SECS", "60"))
CARD_INNER_WIDTH = 44
SUMMARY_DIR = Path(os.environ.get("DAILY_SUMMARY_DIR", str(Path.home() / "daily-summaries")))
LOCK_PATH = SUMMARY_DIR / ".summary.lock"
ERROR_LOCK_PATH = SUMMARY_DIR / ".error.lock"


def _latest_mtime(pattern: str = "*.md") -> float | None:
    """Return the mtime of the newest file matching `pattern` in SUMMARY_DIR,
    or None if the dir is empty / doesn't exist. Used for the summary-mode
    dedup window (`*.md`) and the error-mode dedup window (`ERROR-*.md`).
    """
    if not SUMMARY_DIR.exists():
        return None
    newest: float | None = None
    for p in SUMMARY_DIR.glob(pattern):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if newest is None or m > newest:
            newest = m
    return newest


def _try_acquire_lock(lock_path: Path = LOCK_PATH) -> bool:
    """Atomically acquire the named lock file (default: SUMMARY_DIR/.summary.lock).

    Returns True if this process holds the lock; False if another process
    holds a fresh lock (in which case we should exit immediately).

    If the existing lock is older than LOCK_STALE_SECS, treat it as a
    crash leftover and break it before retrying. The stale-window must be
    much larger than the longest expected claude -p call, otherwise a slow
    but legitimate run would lose its lock to a racing process.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Break stale locks (crash leftovers)
    if lock_path.exists():
        try:
            age = time.time() - lock_path.stat().st_mtime
            if age > LOCK_STALE_SECS:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    # Atomic exclusive create — O_EXCL is the critical bit
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, f"pid={os.getpid()} ts={time.time():.0f}\n".encode())
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError as e:
        print(f"[daily-summary] lock acquire failed ({lock_path}): {e}", file=sys.stderr)
        return False


def _release_lock(lock_path: Path = LOCK_PATH) -> None:
    """Best-effort lock release. Safe to call even if we don't own the lock."""
    try:
        lock_path.unlink()
    except OSError:
        pass


MAX_SUMMARY_LINES = 3
MAX_DECISION_LINES = 5


def fatal(msg: str, code: int = 0) -> None:
    print(f"[daily-summary] {msg}", file=sys.stderr)
    sys.exit(code)


def read_hook_input() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        fatal("empty stdin (not running as a hook?)", code=0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        fatal(f"invalid hook JSON on stdin: {e}", code=0)


def parse_transcript(tp: Path):
    """Return (user_count, conversation_segments)."""
    user_count = 0
    segments = []
    with tp.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t == "user":
                user_count += 1
            if t not in ("user", "assistant"):
                continue
            msg = obj.get("message") or {}
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                text = "\n".join(parts)
            else:
                text = ""
            if len(text) > 500:
                text = text[:500] + "\n[…truncated…]"
            if text.strip():
                role = "user" if t == "user" else "assistant"
                segments.append(f"[{role}]\n{text}")
    return user_count, segments


def trim_segments(segments, max_turns=30, max_total_chars=12000):
    """Keep the most recent turns and cap total size to bound API latency."""
    segments = segments[-max_turns:] if len(segments) > max_turns else segments
    out, total = [], 0
    for seg in segments:
        if total + len(seg) > max_total_chars:
            seg = seg[: max(0, max_total_chars - total)] + "\n[…cut…]"
        out.append(seg)
        total += len(seg)
        if total >= max_total_chars:
            break
    return out


def run_claude_summarize(conv_file: Path) -> str:
    # Single-line prompt: cmd.exe /c treats \n as a command separator, so
    # multi-line prompts get truncated to the first line on Windows. Keep
    # this prompt on one line — `claude -p` understands it fine.
    prompt = (
        f"你是对话总结助手。请阅读文件 {conv_file} 中的对话内容 "
        f"（每段以 [user] 或 [assistant] 开头）。 "
        f"请严格按以下格式输出（不要任何额外说明、开场白、结束语、代码块包裹）: "
        f"<<<TITLE::: <一句话中文标题，不超过20字，简洁有力> "
        f"<<<SUMMARY::: <3-5句中文摘要，概括对话核心内容与结论> "
        f"<<<DECISIONS::: - 决策/待办1 - 决策/待办2 - 决策/待办3 "
        f"（如对话中无明确决策/待办，这一节写「无」） "
        f"<<<END:::"
    )
    try:
        if os.name == "nt":
            # Windows: npm-installed `claude` is a .cmd shim; Python's
            # CreateProcess doesn't resolve .cmd via PATHEXT, so route
            # through cmd.exe. subprocess.list2cmdline handles quoting.
            cmd = ["cmd.exe", "/c", "claude", "-p", prompt]
        else:
            cmd = ["claude", "-p", prompt]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=110,
        )
        return (result.stdout or "").strip()
    except subprocess.TimeoutExpired:
        fatal("claude -p timed out after 110s", code=0)
        return ""
    except FileNotFoundError:
        fatal("`claude` CLI not found in PATH", code=0)
        return ""
    except Exception as e:
        fatal(f"claude -p raised: {e}", code=0)
        return ""


def extract_section(text: str, name: str) -> str:
    start_marker = f"<<<{name}:::"
    start_idx = text.find(start_marker)
    if start_idx < 0:
        return ""
    start_idx += len(start_marker)
    end_idx = text.find("<<<", start_idx)
    if end_idx < 0:
        end_idx = len(text)
    return text[start_idx:end_idx].strip()


# ── ANSI helpers ──────────────────────────────────────────────────────────
import re
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
RESET = "\x1b[0m"


def c(code: str, text: str) -> str:
    """Wrap text in an ANSI SGR code (e.g. '1;35' = bold magenta)."""
    return f"\x1b[{code}m{text}{RESET}"


# Pre-defined color codes
CLR_BORDER      = "1;35"   # bold magenta — outer frame
CLR_DATE        = "2;36"   # dim cyan — date
CLR_TITLE       = "1;38;2;220;235;255"  # bold + RGB near-white — title
CLR_SUMMARY     = "38;5;252"            # light gray — body text
CLR_SECTION     = "1;33"   # bold yellow — section headers
CLR_BULLET      = "1;35"   # bold magenta — decision bullet
CLR_FILE        = "3;32"   # italic green — file path
CLR_EMPTY       = "2;37"   # dim white
CLR_ACCENT      = "38;5;213"  # pink — accent dots


def visible_width(s: str) -> int:
    """CJK-aware visible column count. ANSI codes are stripped first."""
    s = ANSI_RE.sub("", s)
    w = 0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w


def pad(s: str, width: int) -> str:
    pad_count = max(0, width - visible_width(s))
    return s + " " * pad_count


def clip_to_width(s: str, width: int) -> str:
    """Clip string to fit within `width` visible columns (CJK aware)."""
    s = ANSI_RE.sub("", s)
    if visible_width(s) <= width:
        return s
    out = []
    w = 0
    for ch in s:
        ch_w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + ch_w > width - 1:
            out.append("…")
            break
        out.append(ch)
        w += ch_w
    return "".join(out)


def build_card(
    title: str,
    summary: str,
    decisions: list[str],
    outfile: Path,
    ts_human: str,
) -> str:
    """Build a colored terminal text card using box-drawing chars + ANSI.

    Layout (double-line frame, bold magenta):
        ╔══...══╗
        ║  · DATE ║           (dim cyan)
        ╠══...══╣
        ║  ◆ TITLE            ║ (bold RGB near-white)
        ║                      ║
        ║  summary line ...    ║ (light gray)
        ║                      ║
        ║  ▰ 决策 / 待办 (N)   ║ (bold yellow)
        ║    ✦ decision 1      ║ (magenta bullet, gray text)
        ║    ✦ decision 2      ║
        ║                      ║
        ║  ➜ filename.md       ║ (italic green)
        ╚══...══╝
    """
    inner = CARD_INNER_WIDTH
    b = CLR_BORDER

    top = c(b, "╔" + "═" * (inner + 2) + "╗")
    sep = c(b, "╠" + "═" * (inner + 2) + "╣")
    bot = c(b, "╚" + "═" * (inner + 2) + "╝")

    def line(content: str, content_clr: str | None = None) -> str:
        """Wrap content (uncolored) with border and optional inner color."""
        padded = pad(content, inner)
        if content_clr:
            padded = c(content_clr, padded)
        return c(b, "║") + " " + padded + " " + c(b, "║")

    def empty_line() -> str:
        return c(b, "║") + " " + (" " * inner) + " " + c(b, "║")

    lines: list[str] = []

    # Date row
    date_str = f"{c(CLR_ACCENT, '·')} {ts_human}"
    lines.append(line(date_str, CLR_DATE))
    lines.append(sep)

    # Title row (clipped, glowing-white)
    title_str = f"{c(CLR_ACCENT, '◆')} {clip_to_width(title, inner - 2)}"
    lines.append(line(title_str, CLR_TITLE))
    lines.append(empty_line())

    # Summary block
    summary_text = summary.strip() if summary else ""
    if summary_text:
        for sl in summary_text.split("\n")[:MAX_SUMMARY_LINES]:
            lines.append(line(clip_to_width(sl, inner), CLR_SUMMARY))
    else:
        lines.append(line("（无摘要）", CLR_SUMMARY))
    lines.append(empty_line())

    # Decisions block
    if decisions and decisions != ["无"]:
        header = f"{c(CLR_SECTION, '▰ 决策 / 待办')} {c(CLR_SECTION, f'({len(decisions)})')}"
        lines.append(line(header, CLR_SECTION))
        for d in decisions[:MAX_DECISION_LINES]:
            bullet = c(CLR_BULLET, "✦")
            txt = clip_to_width(d, inner - 4)
            content = f"  {bullet} {txt}"
            # pad manually to keep ANSI clean
            pad_count = max(0, inner - visible_width(content))
            content = content + " " * pad_count
            lines.append(c(b, "║") + " " + content + " " + c(b, "║"))
    else:
        lines.append(line(f"{c(CLR_EMPTY, '·')} 无明确决策", CLR_EMPTY))
    lines.append(empty_line())

    # File path row
    fname = outfile.name
    if visible_width(fname) > inner - 4:
        fname = clip_to_width(fname, inner - 4)
    file_str = f"{c(CLR_FILE, '➜')} {fname}"
    lines.append(line(file_str, CLR_FILE))
    lines.append(bot)

    return "\n".join(lines) + RESET


CLAUDE_MD_PATH = Path.home() / ".claude" / "CLAUDE.md"
CLAUDE_MD_SECTION_START = "<!-- BEGIN daily-summary:auto -->"
CLAUDE_MD_SECTION_END = "<!-- END daily-summary:auto -->"
MEMORY_DIR = Path.home() / ".claude" / "projects" / "-root" / "memory"
MEMORY_INDEX_FILE = MEMORY_DIR / "MEMORY.md"
MEMORY_INDEX_MAX_KEEP = 20  # index rolls off oldest entries beyond this


def _write_session_memory(
    title: str,
    summary: str,
    decisions: list[str],
    ts_human: str,
    ts: str,
    short_sid: str,
    user_count: int,
    outfile: Path,
    transcript: Path,
) -> Path:
    """Write a session memory file under ~/.claude/projects/-root/memory/
    containing the curated summary, decisions, and links to the full record.

    Returns the path of the written file.
    """
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    summary_lines = [ln.strip() for ln in (summary or "").strip().split("\n") if ln.strip()][:5]
    summary_text = "\n".join(summary_lines)

    body = [
        "---",
        f"name: auto-summary-{ts}-{short_sid}",
        f"description: Session {short_sid} ({user_count} 条消息) — {title}",
        "metadata:",
        "  type: project",
        "  project: auto-summary",
        f"  session: {short_sid}",
        f"  ts: {ts}",
        "---",
        "",
        f"# Session `{short_sid}` — {title}",
        "",
        f"**时间**: {ts_human}  ",
        f"**消息数**: {user_count}  ",
        f"**Session ID**: `{short_sid}`",
        "",
    ]
    if summary_text:
        body.append("## 摘要")
        body.append("")
        body.append(summary_text)
        body.append("")
    if decisions and decisions != ["无"]:
        body.append("## 关键决策 / 待办")
        body.append("")
        for d in decisions[:8]:
            body.append(f"- {d}")
        body.append("")
    body.append("## 链接")
    body.append("")
    body.append(f"- 完整记录: `{outfile}`")
    body.append(f"- 原始 transcript: `{transcript}`")
    body.append("")

    memory_file = MEMORY_DIR / f"auto-summary-{ts}-{short_sid}.md"
    memory_file.write_text("\n".join(body), encoding="utf-8")
    return memory_file


def _update_memory_index(
    ts: str, short_sid: str, title: str, user_count: int
) -> None:
    """Append (or update) the MEMORY.md index, capped at MEMORY_INDEX_MAX_KEEP.

    The index file follows the convention: one bullet per session, no headers,
    no frontmatter — so the harness can load just the index into context.
    """
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    new_line = f"- [auto-summary-{ts}-{short_sid}](auto-summary-{ts}-{short_sid}.md) — {title} ({user_count}条)"

    if MEMORY_INDEX_FILE.is_file():
        try:
            existing = MEMORY_INDEX_FILE.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    else:
        existing = ""

    lines = [ln for ln in existing.split("\n") if ln.strip() and not ln.strip().startswith("#")]

    # Replace the line if short_sid already present, else append
    replaced = False
    new_lines = []
    for ln in lines:
        if short_sid in ln:
            new_lines.append(new_line)
            replaced = True
        else:
            new_lines.append(ln)
    if not replaced:
        new_lines.append(new_line)

    new_lines = new_lines[-MEMORY_INDEX_MAX_KEEP:]

    final = "\n".join(new_lines) + "\n"
    MEMORY_INDEX_FILE.write_text(final, encoding="utf-8")


def _set_claude_md_pointer() -> None:
    """Replace CLAUDE.md's auto-block with a single-line pointer to MEMORY.md.

    Preserves any user-written content outside the BEGIN/END markers.
    """
    if CLAUDE_MD_PATH.is_file():
        try:
            existing = CLAUDE_MD_PATH.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    else:
        existing = ""

    new_block = (
        f"{CLAUDE_MD_SECTION_START}\n"
        f"📇 Session history → see `{MEMORY_INDEX_FILE}`\n"
        f"{CLAUDE_MD_SECTION_END}\n"
    )

    pattern = re.compile(
        re.escape(CLAUDE_MD_SECTION_START) + r".*?" + re.escape(CLAUDE_MD_SECTION_END) + r"\n?",
        re.S,
    )

    # Strip ALL old auto-blocks first, then append exactly one new block.
    # (Previously this used pattern.sub(new_block, existing), which replaced
    # each match with a fresh copy of new_block — so 10 old blocks became 10
    # new pointer blocks. For a pointer, we always want exactly one.)
    user_content = pattern.sub("", existing).rstrip()
    new_existing = (user_content + "\n\n" + new_block) if user_content else new_block

    CLAUDE_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_MD_PATH.write_text(new_existing, encoding="utf-8")


# ── v3: Tree format & error-mode path ──────────────────────────────────────

# Secret redaction patterns for tool_input dumps. We never want to leak
# API keys, tokens, or passwords into summary files. Patterns are matched
# case-insensitively against tool_input values.
_SECRET_PATTERNS = [
    # api_key=foo / token: bar / password="baz" / secret=baz
    (re.compile(r'(?i)\b(api[_-]?key|access[_-]?token|token|password|passwd|secret)\b\s*[:=]\s*["\']?[\w.\-]+'), r'\1=<REDACTED>'),
    # Anthropic-style: sk-ant-...
    (re.compile(r'sk-ant-[A-Za-z0-9_\-]{8,}'), 'sk-ant-<REDACTED>'),
    # OpenAI-style: sk-... (20+ chars)
    (re.compile(r'(?<![A-Za-z0-9])sk-[A-Za-z0-9]{20,}'), 'sk-<REDACTED>'),
    # GitHub PATs: ghp_ / gho_ / ghu_ / ghs_ / ghr_
    (re.compile(r'\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}'), r'\1_<REDACTED>'),
    # Bearer tokens in Authorization headers
    (re.compile(r'(?i)(Bearer\s+)[A-Za-z0-9._\-]+'), r'\1<REDACTED>'),
]


def redact_secrets(s: str) -> str:
    """Mask common secret patterns in tool_input strings."""
    if not s:
        return s
    for pattern, replacement in _SECRET_PATTERNS:
        s = pattern.sub(replacement, s)
    return s


def curate_tool_input(tool_name: str, tool_input) -> str:
    """Return a curated, secret-redacted one-liner describing the tool_input.
    Avoid dumping the whole dict to keep cards compact and avoid leaks."""
    if tool_input is None:
        return ""
    if isinstance(tool_input, str):
        return redact_secrets(tool_input)[:200]
    if not isinstance(tool_input, dict):
        return redact_secrets(str(tool_input))[:200]

    # Per-tool curated fields
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        line = redact_secrets(cmd)[:200]
        if desc:
            line = f"{line}  # {desc[:60]}" if line else desc[:200]
        return line
    if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
        return tool_input.get("file_path", "")
    if tool_name == "Grep":
        return f"pattern={tool_input.get('pattern', '')!r}  path={tool_input.get('path', '')}"
    if tool_name == "Glob":
        return f"pattern={tool_input.get('pattern', '')!r}  path={tool_input.get('path', '')}"
    if tool_name in ("Agent", "Task"):
        return f"prompt={tool_input.get('prompt', tool_input.get('description', ''))!r:.200}"
    if tool_name == "WebFetch":
        return tool_input.get("url", "")
    if tool_name == "WebSearch":
        return tool_input.get("query", "")
    # Fallback: show only the first short string field
    for k, v in tool_input.items():
        if isinstance(v, str) and v:
            return f"{k}={redact_secrets(v)[:200]!r}"
    return f"<{tool_name} args>"


def build_error_ans(tool_name: str, tool_input_str: str, exit_code, stderr: str,
                     duration_ms, ts_human: str, fname: str) -> str:
    """Build a red-themed colored .ans card for a tool error."""
    inner = CARD_INNER_WIDTH
    b = "1;31"  # bold red border for errors

    top = c(b, "╔" + "═" * (inner + 2) + "╗")
    sep = c(b, "╠" + "═" * (inner + 2) + "╣")
    bot = c(b, "╚" + "═" * (inner + 2) + "╝")

    def line(content, clr="38;5;252"):
        padded = pad(content, inner)
        return c(b, "║") + " " + c(clr, padded) + " " + c(b, "║")

    def empty():
        return c(b, "║") + " " + (" " * inner) + " " + c(b, "║")

    lines = [
        line(f"{c('38;5;203', '✗')} {ts_human}", "2;31"),  # dim red
        sep,
    ]

    # Title
    title = f"{c('38;5;203', '✗')} {tool_name} failed"
    if exit_code is not None:
        title += f" (exit {exit_code})"
    lines.append(line(clip_to_width(title, inner), "1;38;2;255;180;180"))
    lines.append(empty())

    # Input line
    if tool_input_str:
        lines.append(line(clip_to_width(tool_input_str, inner), "38;5;252"))
        lines.append(empty())

    # Stderr block (capped)
    if stderr.strip():
        stderr_lines = [l for l in stderr.strip().split("\n") if l.strip()][:3]
        lines.append(line(f"{c('1;33', '▰ stderr')} {c('1;33', f'({len(stderr_lines)})')}", "1;33"))
        for sl in stderr_lines:
            lines.append(line(clip_to_width(sl, inner), "38;5;210"))
        lines.append(empty())

    # Duration
    if duration_ms is not None:
        lines.append(line(f"duration: {duration_ms} ms", "2;37"))

    # File pointer
    fpath = fname if visible_width(fname) <= inner - 4 else clip_to_width(fname, inner - 4)
    lines.append(line(f"{c(CLR_FILE, '➜')} {fpath}"))
    lines.append(bot)
    return "\n".join(lines) + RESET


def build_error_tree(tool_name: str, tool_input_str: str, exit_code, stderr: str,
                      duration_ms, ts_human: str, short_sid: str, fname: str) -> str:
    """Plain-ASCII tree diagram for a tool error. No ANSI, any terminal."""
    lines = [f"session {short_sid} — tool error ─ {ts_human}"]
    lines.append(f"├─ tool: {tool_name}")

    if tool_input_str:
        lines.append(f"│  └─ input: {tool_input_str}")

    if exit_code is not None:
        lines.append(f"├─ exit_code: {exit_code}")

    if stderr.strip():
        stderr_lines = [l for l in stderr.strip().split("\n") if l.strip()][:5]
        lines.append(f"├─ stderr ({len(stderr_lines)} line{'s' if len(stderr_lines) != 1 else ''}):")
        for i, sl in enumerate(stderr_lines):
            connector = "│  ├─" if i < len(stderr_lines) - 1 else "│  └─"
            lines.append(f"{connector} {sl[:200]}")

    if duration_ms is not None:
        lines.append(f"├─ duration_ms: {duration_ms}")

    lines.append(f"└─ ➜ {fname}")
    return "\n".join(lines) + "\n"


def build_tree_card(
    title: str,
    summary: str,
    decisions: list[str],
    outfile: Path,
    ts_human: str,
    short_sid: str,
    mode: str = "summary",
) -> str:
    """Plain-ASCII tree diagram of a session summary. Mirrors build_card()
    but uses ├─ │ └─ instead of colored borders. Readable in any terminal
    without ANSI support; easy to grep / diff."""
    label = "auto-summary" if mode == "summary" else "summary"
    lines = [f"session {short_sid} — {label} ─ {ts_human}"]
    lines.append(f"├─ 主题: {title}")

    # Summary block (multiline-aware)
    summary_text = (summary or "").strip()
    if summary_text:
        sum_lines = [l for l in summary_text.split("\n") if l.strip()][:5]
        if len(sum_lines) == 1:
            lines.append(f"├─ 摘要: {sum_lines[0]}")
        else:
            lines.append("├─ 摘要:")
            for i, sl in enumerate(sum_lines):
                connector = "│  ├─" if i < len(sum_lines) - 1 else "│  └─"
                lines.append(f"{connector} {sl[:200]}")
    else:
        lines.append("├─ 摘要: (无)")

    # Decisions
    if decisions and decisions != ["无"]:
        n = min(len(decisions), 8)
        lines.append(f"├─ 决策 / 待办 ({n}):")
        for i, d in enumerate(decisions[:n]):
            connector = "│  ├─" if i < n - 1 else "│  └─"
            lines.append(f"{connector} {d[:200]}")
    else:
        lines.append("├─ 决策 / 待办: 无")

    # File pointer (last leaf)
    fname = outfile.name
    lines.append(f"└─ ➜ {fname}")
    return "\n".join(lines) + "\n"


def _handle_error_mode(hook_input: dict, sid: str) -> int:
    """Handle a PostToolUseFailure event. Generate a structured error card
    WITHOUT calling claude -p (errors are structured data — no LLM needed).

    Uses a separate .error.lock and ERROR_DEDUP_SECS window so errors don't
    block summary generation and vice versa.
    """
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    if not _try_acquire_lock(ERROR_LOCK_PATH):
        return 0
    try:
        latest = _latest_mtime("ERROR-*.md")
        if latest is not None and (time.time() - latest) < ERROR_DEDUP_SECS:
            return 0

        tool_name = hook_input.get("tool_name", "unknown")
        tool_input = hook_input.get("tool_input")
        tool_response = hook_input.get("tool_response") or {}
        duration_ms = hook_input.get("duration_ms")

        # Curate tool_input — show only safe fields, redact secrets
        tool_input_str = curate_tool_input(tool_name, tool_input)
        exit_code = tool_response.get("exitCode")
        stderr = tool_response.get("stderr", "") or ""
        stdout = tool_response.get("stdout", "") or ""

        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        ts_human = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        short_sid = sid[:8] if sid else "unknown"
        outfile = SUMMARY_DIR / f"ERROR-{ts}-{short_sid}.md"

        # --- .md (structured, human-readable) ---
        md_lines = [
            f"# Tool Error: {tool_name}",
            "",
            f"**时间**: {ts_human}  ",
            f"**Session ID**: `{short_sid}`  ",
            f"**Tool**: `{tool_name}`  ",
        ]
        if exit_code is not None:
            md_lines.append(f"**Exit Code**: `{exit_code}`  ")
        if duration_ms is not None:
            md_lines.append(f"**Duration**: `{duration_ms}` ms")
        md_lines.append("")

        if tool_input_str:
            md_lines.append("## Input (curated, secrets redacted)")
            md_lines.append("")
            md_lines.append("```")
            md_lines.append(tool_input_str)
            md_lines.append("```")
            md_lines.append("")

        if stderr.strip():
            md_lines.append("## Stderr")
            md_lines.append("")
            md_lines.append("```")
            md_lines.append(stderr.rstrip()[:2000])  # cap at 2KB
            md_lines.append("```")
            md_lines.append("")

        if stdout.strip() and exit_code:
            # Only include stdout if there was a failure — sometimes useful
            md_lines.append("## Stdout (tail, last 1KB)")
            md_lines.append("")
            md_lines.append("```")
            md_lines.append(stdout[-1000:].rstrip())
            md_lines.append("```")
            md_lines.append("")

        md_lines.append("---")
        md_lines.append("")
        md_lines.append(f"- 时间: {ts_human}")
        md_lines.append(f"- Session: {short_sid}")
        md_lines.append(f"- Hook event: PostToolUseFailure")
        outfile.write_text("\n".join(md_lines), encoding="utf-8")

        # --- .ans (colored card, red theme) ---
        ans = build_error_ans(
            tool_name=tool_name,
            tool_input_str=tool_input_str,
            exit_code=exit_code,
            stderr=stderr,
            duration_ms=duration_ms,
            ts_human=ts_human,
            fname=outfile.name,
        )
        try:
            outfile.with_suffix(".ans").write_text(ans, encoding="utf-8")
        except OSError:
            pass

        # --- .tree (plain ASCII tree) ---
        try:
            outfile.with_suffix(".tree").write_text(
                build_error_tree(
                    tool_name=tool_name,
                    tool_input_str=tool_input_str,
                    exit_code=exit_code,
                    stderr=stderr,
                    duration_ms=duration_ms,
                    ts_human=ts_human,
                    short_sid=short_sid,
                    fname=outfile.name,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

        # Best-effort write to /dev/tty (same as summary path)
        banner = "\n\033[1;31m╭─ ⚠ Tool Error ─╮\033[0m\n"
        footer = f"\n\033[2;31m└─ 完整内容：{outfile} ─┘\033[0m\n"
        try:
            with open("/dev/tty", "w", encoding="utf-8") as tty:
                tty.write(banner + ans + footer)
                tty.flush()
        except OSError:
            pass

        # Emit systemMessage for downstream consumers
        json_payload = {
            "systemMessage": (
                f"⚠️ **Tool Error**: `{tool_name}` failed"
                + (f" (exit {exit_code})" if exit_code is not None else "")
                + f"\n\n```\n{ans}\n```\n"
                + f"完整记录: `{outfile}`"
            )
        }
        print(json.dumps(json_payload, ensure_ascii=False))
        return 0
    finally:
        _release_lock(ERROR_LOCK_PATH)


def main() -> int:
    hook_input = read_hook_input()
    sid = hook_input.get("session_id", "")

    # Dispatch: PostToolUseFailure → error mode (no transcript, no claude -p).
    # Must come BEFORE the transcript_path check, since error mode doesn't
    # need the transcript file at all.
    event = hook_input.get("hook_event_name", "")
    if event == "PostToolUseFailure":
        return _handle_error_mode(hook_input, sid)

    tp_str = hook_input.get("transcript_path", "")
    if not tp_str or not Path(tp_str).is_file():
        return 0

    # Atomic dedup via lock + mtime. The lock prevents a TOCTOU race where
    # multiple concurrent Stop-hook invocations all read the same old .md
    # mtime, all judge it as expired, and all run claude -p + write new .md.
    # After acquiring the lock, we re-check mtime so the DEDUP_SECS window
    # is enforced for processes that acquire the lock just after the
    # previous holder finishes writing.
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    if not _try_acquire_lock():
        return 0
    try:
        latest = _latest_mtime("*.md")
        if latest is not None and (time.time() - latest) < DEDUP_SECS:
            return 0

        tp = Path(tp_str)
        try:
            user_count, segments = parse_transcript(tp)
        except Exception as e:
            fatal(f"failed to read transcript {tp}: {e}", code=0)
            return 0

        if user_count < MIN_USER_MESSAGES:
            return 0

        segments = trim_segments(segments)

        # Write conversation to a temp file inside ~/.claude/ so the sandboxed
        # `claude -p` process (restricted to $HOME) can read it.
        tmp_dir = Path.home() / ".claude" / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            dir=str(tmp_dir),
            delete=False,
            encoding="utf-8",
        )
        try:
            tmp.write("\n\n".join(segments))
            tmp.close()
            conv_file = Path(tmp.name)

            raw = run_claude_summarize(conv_file)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

        if not raw:
            # claude -p failed; do not surface a card, but stay silent
            return 0

        title = extract_section(raw, "TITLE") or "对话总结"
        summary = extract_section(raw, "SUMMARY")
        decisions_raw = extract_section(raw, "DECISIONS")
        decisions = [d.strip() for d in decisions_raw.splitlines() if d.strip().startswith("-")]
        decisions = [d.lstrip("-").strip() for d in decisions]

        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        ts_human = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        short_sid = sid[:8] if sid else "unknown"
        SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
        outfile = SUMMARY_DIR / f"{ts}-{short_sid}.md"

        md_lines = [f"# {title}", "", summary or "（无摘要）", ""]
        md_lines.append("## 关键决策 / 待办")
        md_lines.append("")
        if decisions and decisions != ["无"]:
            md_lines.extend(f"- {d}" for d in decisions)
        else:
            md_lines.append("无")
        md_lines.append("")
        md_lines.append("---")
        md_lines.append("")
        md_lines.append(f"- 用户消息数：{user_count}")
        md_lines.append(f"- 总结时间：{ts}")
        md_lines.append(f"- Session ID：`{short_sid}`")
        md_lines.append(f"- Transcript：`{tp}`")
        outfile.write_text("\n".join(md_lines), encoding="utf-8")

        # Persist the curated session knowledge to memory/ so the next Claude
        # Code session auto-loads it as long-term memory. CLAUDE.md gets only a
        # pointer line — its real content lives in the per-session memory file
        # + the MEMORY.md index.
        memory_file = _write_session_memory(
            title=title,
            summary=summary,
            decisions=decisions,
            ts_human=ts_human,
            ts=ts,
            short_sid=short_sid,
            user_count=user_count,
            outfile=outfile,
            transcript=tp,
        )
        _update_memory_index(ts, short_sid, title, user_count)
        _set_claude_md_pointer()

        card = build_card(title, summary, decisions, outfile, ts_human)

        # 1. Save a .ans sidecar with raw ANSI codes (handy for `cat` / `less -R`)
        ans_path = outfile.with_suffix(".ans")
        try:
            ans_path.write_text(card, encoding="utf-8")
        except OSError:
            pass

        # 1b. Also write a plain-ASCII .tree sidecar — readable in any
        #     terminal without ANSI support, easy to grep / diff.
        tree_path = outfile.with_suffix(".tree")
        try:
            tree_path.write_text(
                build_tree_card(
                    title=title,
                    summary=summary,
                    decisions=decisions,
                    outfile=outfile,
                    ts_human=ts_human,
                    short_sid=short_sid,
                    mode="summary",
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

        # 2. Best-effort: write the card directly to the user's controlling
        #    terminal. This bypasses the TUI's systemMessage rendering, which
        #    doesn't display reliably when the session is tearing down.
        banner = "\n\033[1;35m╭─ 📇 今日对话总结 ─╮\033[0m\n"
        footer = "\n\033[2;36m└─ 完整内容：" + str(outfile) + " ─┘\033[0m\n"
        payload_text = banner + card + footer
        try:
            with open("/dev/tty", "w", encoding="utf-8") as tty:
                tty.write(payload_text)
                tty.flush()
        except OSError:
            # /dev/tty not available (sandboxed, no controlling terminal, etc.)
            pass

        # 3. Also emit the JSON systemMessage for any consumer that listens
        #    (kept for compatibility — TUI may render it in some flows).
        json_payload = {
            "systemMessage": (
                "📇 **今日对话总结已生成**\n\n"
                "```\n" + card + "\n```\n"
                f"完整内容已保存到 `{outfile}`"
            )
        }
        print(json.dumps(json_payload, ensure_ascii=False))
        return 0
    finally:
        _release_lock()


if __name__ == "__main__":
    sys.exit(main())
