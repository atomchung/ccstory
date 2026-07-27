"""Batch narrator ETA contract.

One narrator batch now writes many ``auto`` rows at almost the same instant,
so per-row cache timestamps cannot honestly stand in for call latency. The
CLI instead presents an explicit conservative per-batch estimate.
"""

from __future__ import annotations

from pathlib import Path

from ccstory import recap
from ccstory.session_summarizer import SUMMARY_BATCH_SIZE


class TestBatchEta:
    def test_estimate_scales_with_batches_not_sessions(self, tmp_home: Path):
        sessions = 127
        batches = (sessions + SUMMARY_BATCH_SIZE - 1) // SUMMARY_BATCH_SIZE
        eta_min = max(
            1,
            int((batches * recap.SUMMARY_BATCH_SEC_FALLBACK + 59) // 60),
        )

        assert batches == 4
        assert eta_min == 2
