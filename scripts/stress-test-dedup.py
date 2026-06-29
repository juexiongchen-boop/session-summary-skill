#!/usr/bin/env python3
"""Stress test for daily-summary.py dedup lock.

Spawns N concurrent invocations of the (mocked) summarize flow and verifies
that exactly ONE process wins the lock and writes a .md file. Run with
`python3 stress-test-dedup.py [N]` (default N=10).

Mocks claude -p via monkey-patching run_claude_summarize so the test runs
in seconds, not minutes. For a full end-to-end test that actually invokes
claude -p, see scripts/run-real-stress.sh (takes ~60s).
"""
import importlib.util
import io
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE / "daily-summary.py"

# Load daily-summary as a module
spec = importlib.util.spec_from_file_location("ds", str(SCRIPT))
ds = importlib.util.module_from_spec(spec)

# Patch out the slow claude -p call BEFORE the module body runs, so the
# patch is in place when child processes inherit the module via fork.
def fake_summarize(conv_file: Path) -> str:
    """Pretend claude -p returned a fixed summary. Sleep briefly so the
    lock-holder stays in claude -p long enough for racing processes to
    actually contend for the lock."""
    time.sleep(2)
    return (
        "<<<TITLE:::\n"
        "并发压测 mock 总结\n"
        "<<<SUMMARY:::\n"
        "这是 stress test 的 mock 输出，验证 lock 防止 TOCTOU 竞态。\n"
        "<<<DECISIONS:::\n"
        "- lock 测试通过：10 个并发只有 1 个写文件\n"
        "<<<END:::"
    )

# Inject the patch before module load
import types
_orig_run = None  # placeholder; will be replaced post-load via attribute set
spec.loader.exec_module(ds)
ds.run_claude_summarize = fake_summarize

# Build a small fake transcript that the script will parse.
# Must have ≥ MIN_USER_MESSAGES (=5) user-type entries to pass the threshold.
fake_transcript = HERE / "stress-transcript.jsonl"
lines = []
for i in range(7):
    lines.append(json.dumps({
        "type": "user",
        "message": {"role": "user", "content": f"压力测试 user 消息 {i+1}"},
    }))
    lines.append(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": f"压力测试 assistant 响应 {i+1}"},
    }))
fake_transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")


def child_main(_ignore):
    """Worker entry point. Runs main() in a forked child. Each child has
    its own copy of sys.stdin so we don't need to coordinate input."""
    hook_input = json.dumps({
        "transcript_path": str(fake_transcript),
        "session_id": "stress-test-sid-12345",
    })
    sys.stdin = io.StringIO(hook_input)
    t0 = time.time()
    rc = ds.main()
    return {"rc": rc, "elapsed": time.time() - t0}


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    print(f"=== Stress test: {n} concurrent invocations ===")

    # Use a hermetic SUMMARY_DIR so the test doesn't interfere with
    # production state and isn't tripped by recent .md files.
    import tempfile
    test_dir = Path(tempfile.mkdtemp(prefix="stress-summary-"))
    ds.SUMMARY_DIR = test_dir
    ds.LOCK_PATH = test_dir / ".summary.lock"
    print(f"Using hermetic SUMMARY_DIR: {test_dir}")

    # Spawn N concurrent processes via fork (Linux default).
    # Each child runs main() in parallel.
    ctx = mp.get_context("fork")
    t_start = time.time()
    with ctx.Pool(n) as pool:
        results = pool.map(child_main, range(n))
    t_total = time.time() - t_start

    # Snapshot results
    md_after = sorted(test_dir.glob("*.md"))
    new_files = md_after  # all .md in the test dir are new
    print(f"NEW .md files written: {len(new_files)}")
    for f in new_files:
        print(f"  + {f.name}")

    # Lock file should be cleaned up
    if ds.LOCK_PATH.exists():
        print(f"⚠️  Lock file still present: {ds.LOCK_PATH}")
    else:
        print(f"✅ Lock file cleaned up")

    # Per-process stats
    print(f"\nPer-process results ({len(results)} children, total {t_total:.2f}s):")
    fast_skips = [r for r in results if r["elapsed"] < 1.0]
    slow_winners = [r for r in results if r["elapsed"] >= 1.0]
    print(f"  Fast skips (<1s, locked-out or dedup-skipped): {len(fast_skips)}")
    print(f"  Slow winners (≥1s, ran the full flow):       {len(slow_winners)}")

    # Assertions
    print()
    if len(new_files) == 1:
        print(f"✅ PASS: exactly 1 .md written under contention")
        if len(slow_winners) == 1:
            print(f"✅ PASS: exactly 1 process held the lock and ran the full flow")
        rc = 0
    elif len(new_files) == 0:
        print(f"❌ FAIL: 0 .md files — no one won the race (likely a bug)")
        rc = 1
    else:
        print(f"❌ FAIL: {len(new_files)} .md files written — lock is broken")
        rc = 1

    # Cleanup test dir
    import shutil
    shutil.rmtree(test_dir, ignore_errors=True)
    fake_transcript.unlink(missing_ok=True)
    print(f"\nCleaned up test dir: {test_dir}")

    sys.exit(rc)


if __name__ == "__main__":
    main()