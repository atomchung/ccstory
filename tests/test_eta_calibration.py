"""Bounded narrator budget contract."""

from __future__ import annotations

from ccstory import session_summarizer as ss


class TestNarrativeBudget:
    def test_default_budget_matches_cli_contract(self):
        budget = ss.NarrativeBudget()
        assert budget.total_sec == 90
        assert budget.batch_deadline_sec == 45

    def test_timeout_marks_budget_status_partial(self):
        budget = ss.NarrativeBudget()
        budget.finish_call(0.0, timed_out=True)
        assert budget.status()["partial"] is True
