"""The `--llm-narrative` ETA measures this machine instead of trusting a
constant (#113).

The pre-#113 ETA multiplied the session count by a hard-coded 40s — a figure
profiled once, on one M1 Pro, back in #13. On a machine that actually runs
~7s/session that over-states by ~6x, which inverts the warning's purpose: #13
added it to save users from a silent 3-hour job, and it ended up scaring them
away from a 15-minute one.

`_sec_per_session()` reads the gaps between `auto` rows already sitting in the
cache. A backfill writes one row per `claude -p` call, so those gaps are real
timings for exactly the work being predicted — no new schema, no new probe.
"""

from __future__ import annotations

from pathlib import Path

from ccstory import recap
from ccstory import session_summarizer as ss


def _seed_auto_summaries(
    gaps_sec: list[float], start: float = 1_700_000_000.0
) -> None:
    """Write `auto` rows whose `created_at` are spaced by `gaps_sec`."""
    conn = ss._connect()
    try:
        stamp = start
        for i, gap in enumerate([0.0] + list(gaps_sec)):
            stamp += gap
            conn.execute(
                """INSERT INTO session_summaries
                   (session_id, summary, source, project, created_at,
                    prompt_version)
                   VALUES (?, ?, 'auto', 'proj', ?, 1)""",
                (f"sess-{i}", f"did thing {i}", stamp),
            )
        conn.commit()
    finally:
        conn.close()


class TestSecPerSession:
    def test_first_run_has_no_history_and_admits_it(self, tmp_home: Path):
        sec, measured = recap._sec_per_session()
        assert sec == recap.CLAUDE_P_SEC_FALLBACK
        assert measured is False, "a guess must not be labeled a measurement"

    def test_learns_the_median_from_history(self, tmp_home: Path):
        _seed_auto_summaries([7.0] * 12)
        sec, measured = recap._sec_per_session()
        assert sec == 7.0
        assert measured is True

    def test_idle_gap_between_runs_is_not_a_session(self, tmp_home: Path):
        # Ten back-to-back calls, a two-hour break, then two more. The break
        # is the user going to lunch, not a session that took two hours.
        _seed_auto_summaries([6.0] * 10 + [7200.0] + [6.0] * 2)
        sec, measured = recap._sec_per_session()
        assert sec == 6.0
        assert measured is True

    def test_too_few_samples_keeps_guessing(self, tmp_home: Path):
        _seed_auto_summaries([7.0] * 3)  # under _ETA_MIN_SAMPLES
        sec, measured = recap._sec_per_session()
        assert sec == recap.CLAUDE_P_SEC_FALLBACK
        assert measured is False

    def test_median_resists_a_single_stalled_call(self, tmp_home: Path):
        # One 280s call — slow, but under the run-gap cutoff so it is still a
        # session. The mean would be ~31s; the median stays honest at 6s.
        _seed_auto_summaries([6.0] * 10 + [280.0])
        sec, _ = recap._sec_per_session()
        assert sec == 6.0

    def test_fallback_rows_do_not_count_as_timings(self, tmp_home: Path):
        # `fallback` rows are written instantly and in bulk; counting them
        # would teach the ETA that `claude -p` takes ~0s.
        conn = ss._connect()
        try:
            for i in range(20):
                conn.execute(
                    """INSERT INTO session_summaries
                       (session_id, summary, source, project, created_at)
                       VALUES (?, ?, 'fallback', 'proj', ?)""",
                    (f"fb-{i}", "first user message", 1_700_000_000.0 + i * 0.01),
                )
            conn.commit()
        finally:
            conn.close()
        sec, measured = recap._sec_per_session()
        assert measured is False
        assert sec == recap.CLAUDE_P_SEC_FALLBACK


class TestEta113Regression:
    def test_measured_machine_is_not_quoted_the_m1_pro_constant(
        self, tmp_home: Path
    ):
        """#113 as observed: 127 sessions on a ~7s/session machine were
        announced as `ETA ~85 min`. The run took ~15."""
        _seed_auto_summaries([6.7] * 20)
        sec, measured = recap._sec_per_session()
        assert measured is True

        def eta_min(per_session: float) -> int:
            return max(1, int((127 * per_session + 59) // 60))

        assert eta_min(recap.CLAUDE_P_SEC_FALLBACK) >= 85  # what users saw
        assert eta_min(sec) <= 20  # what the machine actually does
