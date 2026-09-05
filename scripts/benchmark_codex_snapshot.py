#!/usr/bin/env python3
"""Reproducible benchmark for Codex provider snapshot vs legacy two-pass collection (#174 PR C).

Generates a deterministic synthetic Codex workspace with root sessions, resumes,
and subagents, and measures:
  - sources enumerated
  - file open operations
  - records parsed
  - wall-clock elapsed time
  - peak memory usage

Usage:
  python -m scripts.benchmark_codex_snapshot [--sessions 150]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import tracemalloc
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ccstory.providers.base import _usage_windows_utc
from ccstory.providers.codex import CodexProvider
from ccstory.token_usage import ModelUsage


def generate_synthetic_codex_store(
    root: Path,
    num_sessions: int = 150,
) -> tuple[dict[str, tuple[datetime, datetime]], list[Path]]:
    """Generate a deterministic synthetic ~/.codex store."""
    base_time = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    windows = {
        "previous": (base_time - timedelta(days=7), base_time),
        "current": (base_time, base_time + timedelta(days=7)),
    }

    sessions_dir = root / "sessions" / "2026" / "07"
    archived_dir = root / "archived_sessions" / "2026" / "07"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    archived_dir.mkdir(parents=True, exist_ok=True)

    generated_paths: list[Path] = []

    for i in range(num_sessions):
        # Stagger sessions across previous and current windows
        day_offset = (i % 14) - 7
        sess_time = base_time + timedelta(days=day_offset, hours=(i % 24), minutes=(i % 60))
        target_dir = archived_dir if i % 5 == 0 else sessions_dir

        rollout_id = f"019f{i:04x}-0000-7000-8000-{i:012x}"
        file_path = target_dir / f"rollout-{sess_time.strftime('%Y-%m-%dT%H-%M-%S')}-{rollout_id}.jsonl"

        is_subagent = (i % 7 == 0 and i > 0)
        parent_id = f"019f{(i - 1):04x}-0000-7000-8000-{(i - 1):012x}" if is_subagent else None

        records = [
            {
                "timestamp": sess_time.isoformat().replace("+00:00", "Z"),
                "type": "session_meta",
                "payload": {
                    "id": rollout_id,
                    "session_id": parent_id or rollout_id,
                    **(
                        {
                            "parent_thread_id": parent_id,
                            "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                        }
                        if is_subagent
                        else {}
                    ),
                    "cwd": f"/Users/test/project-{(i % 5)}",
                },
            },
            {
                "timestamp": (sess_time + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-terra" if i % 2 == 0 else "gpt-5.6-sol"},
            },
            {
                "timestamp": (sess_time + timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
                "type": "event_msg",
                "payload": {"type": "user_message", "message": f"User prompt for session {i}"},
            },
            {
                "timestamp": (sess_time + timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"Assistant response for session {i}"}],
                },
            },
            {
                "timestamp": (sess_time + timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 500 + i * 10,
                            "cached_input_tokens": 100 + i * 2,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 50 + i * 3,
                        }
                    },
                },
            },
            {
                "timestamp": (sess_time + timedelta(seconds=40)).isoformat().replace("+00:00", "Z"),
                "type": "event_msg",
                "payload": {"type": "user_message", "message": f"Follow-up prompt for session {i}"},
            },
            {
                "timestamp": (sess_time + timedelta(seconds=50)).isoformat().replace("+00:00", "Z"),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"Follow-up response for session {i}"}],
                },
            },
            {
                "timestamp": (sess_time + timedelta(seconds=50)).isoformat().replace("+00:00", "Z"),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 1200 + i * 15,
                            "cached_input_tokens": 300 + i * 5,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 150 + i * 8,
                        }
                    },
                },
            },
        ]

        with file_path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

        generated_paths.append(file_path)

    return windows, generated_paths


def run_benchmark(num_sessions: int = 150) -> None:
    temp_dir = Path(tempfile.mkdtemp(prefix="ccstory_codex_bench_"))
    try:
        windows, paths = generate_synthetic_codex_store(temp_dir, num_sessions=num_sessions)
        provider = CodexProvider(codex_dir=temp_dir)

        # 1. Benchmark single-pass collect_snapshot
        open_counts_snapshot = Counter()
        original_open = Path.open

        def counted_open_snap(self, *args, **kwargs):
            open_counts_snapshot[self] += 1
            return original_open(self, *args, **kwargs)

        tracemalloc.start()
        Path.open = counted_open_snap
        start_snap = time.perf_counter()
        snapshot = provider.collect_snapshot(windows)
        elapsed_snap = time.perf_counter() - start_snap
        Path.open = original_open
        _, peak_snap = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # 2. Benchmark legacy separate collection (sessions + usage)
        open_counts_legacy = Counter()

        def counted_open_leg(self, *args, **kwargs):
            open_counts_legacy[self] += 1
            return original_open(self, *args, **kwargs)

        tracemalloc.start()
        Path.open = counted_open_leg
        start_leg = time.perf_counter()
        norm_windows = _usage_windows_utc(windows)
        earliest = min(s for s, _ in norm_windows.values())
        latest = max(u for _, u in norm_windows.values())
        leg_sessions = provider.collect_sessions(earliest, latest, engaged_only=True)
        leg_models = {k: {} for k in norm_windows}
        leg_turns = provider.collect_usage_for_windows(norm_windows, leg_models)
        elapsed_leg = time.perf_counter() - start_leg
        Path.open = original_open
        _, peak_leg = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Verify equivalence
        snap_current_ids = {s.session_id for s in snapshot.sessions_by_window["current"]}
        leg_current_ids = {
            s.session_id
            for s in leg_sessions
            if s.end >= norm_windows["current"][0] and s.start < norm_windows["current"][1]
        }
        assert snap_current_ids == leg_current_ids, "Session IDs mismatch"
        assert snapshot.assistant_turns_by_window == leg_turns, "Assistant turns mismatch"

        total_opens_snap = sum(open_counts_snapshot.values())
        total_opens_leg = sum(open_counts_legacy.values())

        print("=================================================================")
        print(f"Codex Snapshot Benchmark: {num_sessions} files generated")
        print("=================================================================")
        print(f"{'Metric':<30} | {'Legacy (2-pass)':<16} | {'Snapshot (1-pass)':<16}")
        print("-" * 69)
        print(f"{'Sources Enumerated':<30} | {len(paths):<16} | {snapshot.metrics.sources_enumerated:<16}")
        print(f"{'Source Opens':<30} | {total_opens_leg:<16} | {total_opens_snap:<16}")
        print(f"{'Records Parsed':<30} | {'N/A (untracked)':<16} | {snapshot.metrics.records_parsed:<16}")
        print(f"{'Elapsed Time (ms)':<30} | {elapsed_leg * 1000:<16.2f} | {elapsed_snap * 1000:<16.2f}")
        print(f"{'Peak Memory (KB)':<30} | {peak_leg / 1024:<16.2f} | {peak_snap / 1024:<16.2f}")
        print("-" * 69)
        reduction = (1 - total_opens_snap / total_opens_leg) * 100
        speedup = (elapsed_leg / elapsed_snap) if elapsed_snap > 0 else 1.0
        print(f"I/O Open Reduction: {reduction:.1f}% ({total_opens_leg} -> {total_opens_snap})")
        print(f"Throughput Speedup: {speedup:.2f}x")
        print("Inventory Complete:", snapshot.metrics.record_inventory_complete)
        print("=================================================================")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Codex Provider Snapshot Benchmark")
    parser.add_argument("--sessions", type=int, default=150, help="Number of synthetic sessions to generate")
    args = parser.parse_args()
    run_benchmark(num_sessions=args.sessions)
