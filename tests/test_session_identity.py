"""Tests for the 0.8 public-vs-evidence session identity seam (#188, 0.8-D1).

Every test builds ``SessionSlice`` objects directly. No bundled provider emits
slices yet, so these prove the seam is safe *before* the D2/D3/D4 adapters
start producing them.

Two invariants are load-bearing:

* a human ``record`` written against the physical session stays authoritative
  for every window slice of that session;
* an internal slice id never reaches a public surface.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ccstory import session_summarizer as ss
from ccstory.report import (
    _session_summary_text,
    build_report_json,
    render_report,
    top_focus_narrative,
)
from ccstory.session_identity import (
    evidence_session_id,
    evidence_session_ids,
    public_session_id,
    public_session_ids,
    resolve_session_summaries,
    session_identity,
)
from ccstory.time_tracking import (
    CategoryRollup,
    ProjectRollup,
    SessionStat,
    rollup_by_category,
    session_slice_for_window,
    wall_clock_active_sec,
)
from ccstory.token_usage import ModelUsage, UsageReport
from ccstory.trends import _resolve_sessions_from_cache, compare_to_previous

UTC = timezone.utc
BASE = datetime(2026, 7, 28, 12, tzinfo=UTC)
PHYSICAL_ID = "physical-session-1"


def _at(offset: int) -> datetime:
    return BASE + timedelta(seconds=offset)


def _stat(
    *offsets: int,
    session_id: str = PHYSICAL_ID,
    project: str = "-Users-t-myapp",
    category: str = "coding",
) -> SessionStat:
    timestamps = [_at(offset).timestamp() for offset in offsets]
    return SessionStat(
        project=project,
        category=category,
        session_id=session_id,
        start=datetime.fromtimestamp(min(timestamps), tz=UTC),
        end=datetime.fromtimestamp(max(timestamps), tz=UTC),
        active_sec=int(max(timestamps) - min(timestamps)),
        msg_count=len(offsets),
        user_msg_count=2,
        first_user_text="fix the login bug",
        agent="claude",
        timestamps=timestamps,
    )


def _clipped_slice(**kwargs):
    """A slice of a session that starts before the window and ends after it."""
    stat = _stat(-60, 60, 180, **kwargs)
    slice_ = session_slice_for_window(stat, _at(0), _at(120))
    assert slice_ is not None
    assert slice_.boundary_clipped is True
    assert slice_.session_id != slice_.physical_session_id
    return slice_


def _contained_slice(**kwargs):
    stat = _stat(0, 60, 90, **kwargs)
    slice_ = session_slice_for_window(stat, _at(-30), _at(120))
    assert slice_ is not None
    assert slice_.boundary_clipped is False
    return slice_


def _summary(session_id: str, source: str, text: str) -> ss.SessionSummary:
    return ss.SessionSummary(
        session_id=session_id,
        summary=text,
        source=source,
        prompt_version=ss.PROMPT_VERSION if source == "auto" else 0,
        evidence_fingerprint="fp" if source == "auto" else "",
        observed_evidence_fingerprint="fp" if source == "auto" else "",
    )


def _fake_fetch(rows: dict[str, ss.SessionSummary], calls: list[list[str]]):
    def _fetch(session_ids: list[str]) -> dict[str, ss.SessionSummary]:
        calls.append(list(session_ids))
        return {sid: rows[sid] for sid in session_ids if sid in rows}

    return _fetch


class TestIdentityLanes:
    def test_plain_session_stat_has_one_identity(self):
        stat = _stat(0, 60)
        identity = session_identity(stat)

        assert identity.public_id == PHYSICAL_ID
        assert identity.evidence_id == PHYSICAL_ID
        assert identity.is_window_slice is False
        assert identity.boundary_clipped is False

    def test_contained_slice_keeps_the_physical_identity(self):
        slice_ = _contained_slice()

        assert public_session_id(slice_) == PHYSICAL_ID
        assert evidence_session_id(slice_) == PHYSICAL_ID
        assert session_identity(slice_).is_window_slice is False

    def test_clipped_slice_separates_public_and_evidence_identity(self):
        slice_ = _clipped_slice()
        identity = session_identity(slice_)

        assert identity.public_id == PHYSICAL_ID
        assert identity.evidence_id == slice_.session_id
        assert identity.evidence_id.startswith("slice-")
        assert identity.is_window_slice is True
        assert identity.boundary_clipped is True

    def test_id_list_helpers_skip_objects_without_an_id(self):
        slice_ = _clipped_slice()
        sessions = [_stat(0, 60), slice_, _stat(0, 60, session_id="")]

        assert public_session_ids(sessions) == [PHYSICAL_ID, PHYSICAL_ID]
        assert evidence_session_ids(sessions) == [
            PHYSICAL_ID,
            slice_.session_id,
        ]


class TestSummaryResolution:
    def test_human_record_on_the_physical_session_covers_every_slice(self):
        slice_ = _clipped_slice()
        calls: list[list[str]] = []
        rows = {PHYSICAL_ID: _summary(PHYSICAL_ID, "record", "human wrote this")}

        resolved = resolve_session_summaries(
            [slice_], fetch=_fake_fetch(rows, calls),
        )

        assert resolved[slice_.session_id].source == "record"
        assert resolved[slice_.session_id].summary == "human wrote this"

    def test_generated_full_session_prose_never_leaks_into_a_clipped_slice(self):
        slice_ = _clipped_slice()
        calls: list[list[str]] = []
        rows = {PHYSICAL_ID: _summary(PHYSICAL_ID, "auto", "whole-session prose")}

        resolved = resolve_session_summaries(
            [slice_], fetch=_fake_fetch(rows, calls),
        )

        # No entry at all: the caller falls back to this slice's own evidence.
        assert resolved == {}

    @pytest.mark.parametrize("source", ["auto", "fallback", "skipped"])
    def test_no_non_record_physical_source_crosses_a_window_boundary(self, source):
        slice_ = _clipped_slice()
        rows = {PHYSICAL_ID: _summary(PHYSICAL_ID, source, "whole-session text")}

        resolved = resolve_session_summaries(
            [slice_], fetch=_fake_fetch(rows, []),
        )

        assert resolved == {}

    def test_slice_uses_its_own_generated_summary(self):
        slice_ = _clipped_slice()
        rows = {
            PHYSICAL_ID: _summary(PHYSICAL_ID, "auto", "whole-session prose"),
            slice_.session_id: _summary(slice_.session_id, "auto", "window prose"),
        }

        resolved = resolve_session_summaries(
            [slice_], fetch=_fake_fetch(rows, []),
        )

        assert resolved[slice_.session_id].summary == "window prose"

    def test_human_record_outranks_the_slice_own_generated_summary(self):
        slice_ = _clipped_slice()
        rows = {
            PHYSICAL_ID: _summary(PHYSICAL_ID, "record", "human wrote this"),
            slice_.session_id: _summary(slice_.session_id, "auto", "window prose"),
        }

        resolved = resolve_session_summaries(
            [slice_], fetch=_fake_fetch(rows, []),
        )

        assert resolved[slice_.session_id].summary == "human wrote this"

    def test_plain_session_resolution_is_unchanged(self):
        stat = _stat(0, 60)
        for source in ("auto", "fallback", "skipped", "record"):
            rows = {PHYSICAL_ID: _summary(PHYSICAL_ID, source, "text")}

            resolved = resolve_session_summaries(
                [stat], fetch=_fake_fetch(rows, []),
            )

            assert resolved[PHYSICAL_ID].source == source

    def test_both_slices_of_one_session_resolve_in_a_single_query(self):
        stat = _stat(-60, 60, 180)
        current = session_slice_for_window(stat, _at(0), _at(120))
        previous = session_slice_for_window(stat, _at(-120), _at(0))
        assert current is not None and previous is not None
        calls: list[list[str]] = []

        resolve_session_summaries(
            [current, previous], fetch=_fake_fetch({}, calls),
        )

        assert len(calls) == 1
        # Ordered dedupe: two evidence ids plus the one shared physical id.
        assert calls[0] == [
            current.session_id,
            PHYSICAL_ID,
            previous.session_id,
        ]

    def test_two_slices_of_one_session_do_not_share_generated_prose(self):
        stat = _stat(-60, 60, 180)
        current = session_slice_for_window(stat, _at(0), _at(120))
        previous = session_slice_for_window(stat, _at(-120), _at(0))
        assert current is not None and previous is not None
        assert current.session_id != previous.session_id
        rows = {
            current.session_id: _summary(current.session_id, "auto", "current work"),
        }

        resolved = resolve_session_summaries(
            [current, previous], fetch=_fake_fetch(rows, []),
        )

        assert resolved[current.session_id].summary == "current work"
        assert previous.session_id not in resolved


class TestHumanRecordAuthorityInBackfill:
    @pytest.mark.parametrize("use_llm", [False, True])
    def test_a_human_recorded_slice_is_never_queued_for_regeneration(self, use_llm):
        """A record about the physical session settles every slice of it.

        Without the seam the default (non-LLM) path has no second lookup to
        recover from a miss, so it would queue narrator work — and re-open the
        transcript — for a session the user has already described.
        """
        slice_ = _clipped_slice()
        ss.upsert(PHYSICAL_ID, "human wrote this", "record")

        plan = ss.prepare_backfill_plan([slice_], use_llm=use_llm)

        assert plan.ids == [slice_.session_id]
        assert plan.todo == []
        assert plan.prepared == {}
        assert plan.existing[slice_.session_id].source == "record"

    def test_backfill_writes_a_slice_summary_without_touching_the_physical_row(
        self, monkeypatch,
    ):
        slice_ = _clipped_slice()
        ss.upsert(PHYSICAL_ID, "whole-session prose", "auto")
        monkeypatch.setattr(
            ss, "_extract_excerpt", lambda path, provider=None: ("proj", "window text"),
        )
        monkeypatch.setattr(ss, "resolve_transcript_path", lambda sess: None)
        monkeypatch.setattr(
            "ccstory.providers.TranscriptResolver.path_for",
            lambda self, sess: sess.path,
        )
        monkeypatch.setattr(
            "ccstory.providers.TranscriptResolver.provider_for",
            lambda self, sess: None,
        )
        slice_.path = None  # no transcript → deterministic "skipped" write

        ss.backfill_for_sessions([slice_], use_llm=False)

        assert ss.get(PHYSICAL_ID).summary == "whole-session prose"
        assert ss.get(PHYSICAL_ID).source == "auto"
        assert ss.get(slice_.session_id).source == "skipped"

    def test_plain_session_backfill_behavior_is_unchanged(self):
        stat = _stat(0, 60)
        ss.upsert(PHYSICAL_ID, "human wrote this", "record")

        plan = ss.prepare_backfill_plan([stat], use_llm=True)

        assert plan.ids == [PHYSICAL_ID]
        assert plan.todo == []

    def test_a_clipped_slice_is_never_summarized_from_its_full_transcript(
        self, tmp_path, monkeypatch,
    ):
        """Fail closed rather than describe out-of-window work.

        The physical transcript is reachable from a slice (it carries ``path``
        for the 0.8-D adapters), so without an explicit guard the narrator lane
        would summarize the whole session and label it as this window's story.
        """
        transcript = tmp_path / "physical.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        slice_ = _clipped_slice()
        slice_.path = transcript
        opened: list = []

        def _boom(path, provider=None):
            opened.append(path)
            return ("proj", "ENTIRE SESSION INCLUDING OUT OF WINDOW TURNS")

        monkeypatch.setattr(ss, "_extract_excerpt", _boom)
        monkeypatch.setattr(
            "ccstory.providers.TranscriptResolver.path_for",
            lambda self, sess: transcript,
        )

        plan = ss.prepare_backfill_plan([slice_], use_llm=True)

        assert opened == []
        assert plan.prepared == {}
        assert slice_.session_id in plan.unavailable
        assert (
            plan.unavailable_summaries[slice_.session_id]
            == "(no bounded window evidence)"
        )

    def test_a_clipped_slice_uses_the_bounded_evidence_it_carries(
        self, tmp_path, monkeypatch,
    ):
        stat = _stat(-60, 60, 180)
        slice_ = session_slice_for_window(
            stat,
            _at(0),
            _at(120),
            evidence=ss_window_evidence(stat, _at(0), _at(120)),
        )
        assert slice_ is not None
        slice_.path = tmp_path / "physical.jsonl"
        monkeypatch.setattr(
            "ccstory.providers.TranscriptResolver.path_for",
            lambda self, sess: sess.path,
        )
        monkeypatch.setattr(
            ss, "_extract_excerpt", lambda path, provider=None: ("p", "FULL"),
        )

        plan = ss.prepare_backfill_plan([slice_], use_llm=True)

        item = plan.prepared[slice_.session_id]
        assert item.excerpt == "in-window opener\nin-window answer"
        assert "FULL" not in item.bounded_evidence

    def test_transcript_lookup_resolves_a_slice_by_its_physical_id(
        self, tmp_home,
    ):
        """The file on disk belongs to the session, not to one window of it.

        Adapters resolve a missing ``path`` from ``sess.session_id``. Handing
        them a clipped slice unchanged would look up ``slice-<hash>``, miss,
        and report the transcript as gone.
        """
        from ccstory.providers import TranscriptResolver

        projects = tmp_home / ".claude" / "projects" / "-Users-t-myapp"
        projects.mkdir(parents=True, exist_ok=True)
        transcript = projects / f"{PHYSICAL_ID}.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")

        slice_ = _clipped_slice()
        slice_.path = None  # force the id-based fallback

        resolved = TranscriptResolver().path_for(slice_)

        assert resolved == transcript
        # The lookup view must not escape and become a cache key.
        assert evidence_session_id(slice_).startswith("slice-")

    def test_a_contained_slice_still_reads_its_transcript(
        self, tmp_path, monkeypatch,
    ):
        slice_ = _contained_slice()
        slice_.path = tmp_path / "physical.jsonl"
        monkeypatch.setattr(
            "ccstory.providers.TranscriptResolver.path_for",
            lambda self, sess: sess.path,
        )
        monkeypatch.setattr(
            ss,
            "_extract_excerpt",
            lambda path, provider=None: ("proj", "whole session text"),
        )

        plan = ss.prepare_backfill_plan([slice_], use_llm=True)

        # Nothing was clipped away, so the transcript *is* the window evidence.
        assert plan.prepared[PHYSICAL_ID].excerpt == "whole session text"


class TestPublicSurfacesNeverLeakSliceIdentity:
    def _envelope(self, slice_, summaries):
        rollup = CategoryRollup(
            category="coding",
            active_min=slice_.active_min,
            sessions=1,
            messages=slice_.msg_count,
            top_sessions=[slice_],
            projects=[
                ProjectRollup(
                    project="myapp",
                    active_min=slice_.active_min,
                    sessions=1,
                    messages=slice_.msg_count,
                )
            ],
        )
        usage = UsageReport(since=_at(0), until=_at(120))
        usage.by_model["claude-opus-4-7"] = ModelUsage(
            model="claude-opus-4-7", turns=1, input_tokens=10, output_tokens=5,
        )
        usage.assistant_turns = 1
        return rollup, usage

    def test_json_session_id_is_the_physical_id(self):
        slice_ = _clipped_slice()
        summaries = {
            slice_.session_id: _summary(slice_.session_id, "auto", "window prose"),
        }
        rollup, usage = self._envelope(slice_, summaries)

        payload = build_report_json(
            label="2026-07-28",
            since=_at(0),
            until=_at(120),
            sessions=[slice_],
            rollups=[rollup],
            usage=usage,
            summaries=summaries,
            overall_narrative=None,
            comparison=None,
            artifacts=None,
        )

        entry = payload["sessions"][0]
        assert entry["id"] == PHYSICAL_ID
        assert entry["summary"] == "window prose"
        assert entry["summary_source"] == "auto"
        assert "slice-" not in json_dumps(payload)

    def test_markdown_report_contains_no_internal_slice_id(self):
        slice_ = _clipped_slice()
        summaries = {
            slice_.session_id: _summary(slice_.session_id, "auto", "window prose"),
        }
        rollup, usage = self._envelope(slice_, summaries)

        markdown = render_report(
            label="2026-07-28",
            since=_at(0),
            until=_at(120),
            sessions=[slice_],
            rollups=[rollup],
            usage=usage,
            summaries=summaries,
            overall_narrative=None,
            comparison=None,
        )

        assert "slice-" not in markdown
        assert "window prose" in markdown

    def test_mcp_top_sessions_expose_the_physical_id(self):
        pytest.importorskip("mcp")
        from types import SimpleNamespace

        from ccstory.mcp_server import _compact_recap

        slice_ = _clipped_slice()
        summaries = {
            slice_.session_id: _summary(slice_.session_id, "auto", "window prose"),
        }
        rollup, usage = self._envelope(slice_, summaries)
        result = SimpleNamespace(
            label="2026-07-28",
            since=_at(0),
            until=_at(120),
            sessions=[slice_],
            rollups=[rollup],
            usage=usage,
            summaries=summaries,
            overall_narrative=None,
            category_narratives={},
            comparison=None,
            artifacts=None,
            agent="all",
            narrative_provenance={},
            counts={},
            report_path=None,
        )

        payload = _compact_recap(result)

        assert payload["top_sessions"][0]["id"] == PHYSICAL_ID
        assert payload["top_sessions"][0]["summary"] == "window prose"
        assert "slice-" not in json_dumps(payload)

    def test_top_focus_ordering_uses_public_identity(self):
        slice_ = _clipped_slice()
        summaries = {
            slice_.session_id: _summary(slice_.session_id, "auto", "window prose"),
        }
        rollup, _usage = self._envelope(slice_, summaries)

        narrative = top_focus_narrative([rollup], summaries)

        assert narrative is not None
        assert narrative.strongest_session_summaries == ("window prose",)

    def test_summary_text_falls_back_to_the_slice_bounded_evidence(self):
        stat = _stat(-60, 60, 180)
        evidence = ss_window_evidence(stat, _at(0), _at(120))
        slice_ = session_slice_for_window(
            stat, _at(0), _at(120), evidence=evidence,
        )
        assert slice_ is not None

        # No cache row at all: the physical session's opening turn happened
        # before this window and must not be borrowed.
        assert _session_summary_text(slice_, {}) == "in-window opener"
        assert stat.first_user_text == "fix the login bug"


class TestDerivedLanesAcceptSlices:
    def test_classification_cache_is_keyed_by_evidence_identity(self, monkeypatch):
        slice_ = _clipped_slice()
        requested: list[list[str]] = []

        def _cache_get_many(session_ids):
            requested.append(list(session_ids))
            return {slice_.session_id: "research"}

        monkeypatch.setattr(ss, "_classify_cache_get_many", _cache_get_many)

        _resolve_sessions_from_cache([slice_], mode="hybrid", fallback="coding")

        assert requested == [[slice_.session_id]]
        assert slice_.category == "research"

    def test_a_physical_classification_row_does_not_bleed_into_a_slice(
        self, monkeypatch,
    ):
        slice_ = _clipped_slice()
        monkeypatch.setattr(
            ss,
            "_classify_cache_get_many",
            lambda session_ids: {PHYSICAL_ID: "research"},
        )

        _resolve_sessions_from_cache([slice_], mode="hybrid", fallback="coding")

        assert slice_.category == "coding"
        assert slice_.category_source == "fallback"

    def test_rollup_uses_the_clipped_active_time(self):
        slice_ = _clipped_slice()

        rollups = rollup_by_category([slice_])

        assert len(rollups) == 1
        assert rollups[0].sessions == 1
        # 120s of window-bounded activity, never the physical session's span.
        assert rollups[0].active_min == pytest.approx(2.0)
        assert wall_clock_active_sec([slice_]) == 120

    def test_comparison_publishes_public_ids_and_caches_by_evidence_id(self):
        current = _clipped_slice()
        stat = _stat(-60, 60, 180, session_id="previous-physical")
        previous = session_slice_for_window(stat, _at(-120), _at(0))
        assert previous is not None
        assert previous.boundary_clipped is True
        usage = UsageReport(since=_at(0), until=_at(120))

        comparison = compare_to_previous(
            current_sessions=[current],
            current_rollups=rollup_by_category([current]),
            current_usage=usage,
            current_label="2026-07-28",
            since=_at(0),
            until=_at(120),
            previous_sessions=[previous],
            previous_usage=UsageReport(since=_at(-120), until=_at(0)),
        )

        assert comparison is not None
        assert comparison.previous_session_ids == ["previous-physical"]
        assert comparison.previous_summary_keys() == [previous.session_id]

    def test_comparison_summary_keys_fall_back_to_the_id_list(self):
        from ccstory.trends import PeriodComparison

        comparison = PeriodComparison(
            current_label="c",
            previous_label="p",
            deltas=[],
            current_total_h=0.0,
            previous_total_h=0.0,
            current_output_tokens=0,
            previous_output_tokens=0,
            current_cost_usd=0.0,
            previous_cost_usd=0.0,
            previous_session_ids=["legacy-a", "legacy-b"],
        )

        assert comparison.previous_summary_keys() == ["legacy-a", "legacy-b"]


class TestRecapPipelineAcceptsSlices:
    """End-to-end: every recap lane runs on slices and publishes physical ids."""

    def _run(self, monkeypatch, tmp_home):
        from ccstory import recap as recap_mod
        from ccstory.providers import ProviderSnapshot

        stat = _stat(-60, 60, 180)
        current = session_slice_for_window(
            stat, _at(0), _at(120),
            evidence=ss_window_evidence(stat, _at(0), _at(120)),
        )
        previous = session_slice_for_window(stat, _at(-120), _at(0))
        assert current is not None and previous is not None
        assert current.boundary_clipped and previous.boundary_clipped

        current_usage = UsageReport(since=_at(0), until=_at(120))
        current_usage.by_model["claude-opus-4-7"] = ModelUsage(
            model="claude-opus-4-7", turns=2, input_tokens=200, output_tokens=100,
        )
        current_usage.assistant_turns = 2
        previous_usage = UsageReport(since=_at(-120), until=_at(0))

        def _snapshot(windows, *, engaged_only=True, agent="all"):
            sessions = {"current": [current]}
            usage = {"current": current_usage}
            if "previous" in windows:
                sessions["previous"] = [previous]
                usage["previous"] = previous_usage
            return ProviderSnapshot(
                windows=dict(windows),
                records=(),
                sessions_by_window=sessions,
                usage_by_window=usage,
            )

        monkeypatch.setattr(recap_mod, "collect_provider_snapshot", _snapshot)

        result = recap_mod.build_recap(
            window="week",
            artifacts=False,
            compare_narrative=False,
            write_report=False,
        )
        return current, previous, result

    def test_public_surfaces_only_ever_show_the_physical_id(
        self, monkeypatch, tmp_home,
    ):
        current, previous, result = self._run(monkeypatch, tmp_home)

        payload = result.to_json()
        assert [s["id"] for s in payload["sessions"]] == [PHYSICAL_ID]
        assert "slice-" not in json_dumps(payload)
        assert "slice-" not in result.markdown
        assert current.session_id.startswith("slice-")

    def test_rollups_use_window_bounded_active_time(self, monkeypatch, tmp_home):
        current, _previous, result = self._run(monkeypatch, tmp_home)

        assert [r.category for r in result.rollups]
        total_min = sum(r.active_min for r in result.rollups)
        # The physical session spans 240s; only its 120s inside the window count.
        assert total_min == pytest.approx(2.0)
        assert current.active_sec == 120

    def test_summaries_are_cached_under_the_slice_evidence_identity(
        self, monkeypatch, tmp_home,
    ):
        current, _previous, result = self._run(monkeypatch, tmp_home)

        assert set(result.summaries) == {current.session_id}
        assert ss.get(current.session_id) is not None
        # The physical session itself never gets a row from a clipped slice.
        assert ss.get(PHYSICAL_ID) is None

    def test_comparison_publishes_physical_ids_for_both_windows(
        self, monkeypatch, tmp_home,
    ):
        _current, previous, result = self._run(monkeypatch, tmp_home)

        assert result.comparison is not None
        assert result.comparison.previous_session_ids == [PHYSICAL_ID]
        assert result.comparison.previous_summary_keys() == [previous.session_id]

    def test_carried_previous_sessions_stay_private(self, monkeypatch, tmp_home):
        """``previous_sessions`` holds transcript paths — it must not serialize.

        Every ccstory serializer builds its dict field by field rather than
        from ``asdict``, so this guards against a future shortcut turning an
        internal plumbing field into published local-filesystem data.
        """
        _current, previous, result = self._run(monkeypatch, tmp_home)
        previous.path = tmp_home / ".claude" / "projects" / "secret.jsonl"
        previous.cwd = "/Users/someone/private-repo"

        rendered = json_dumps(result.to_json()) + result.markdown
        pytest.importorskip("mcp")
        from ccstory.mcp_server import _compact_comparison

        rendered += json_dumps(_compact_comparison(result.comparison))

        assert "secret.jsonl" not in rendered
        assert "private-repo" not in rendered


def json_dumps(payload) -> str:
    import json

    return json.dumps(payload, default=str)


def ss_window_evidence(stat: SessionStat, since: datetime, until: datetime):
    """Bounded evidence a 0.8-D provider adapter would supply for this window."""
    from ccstory.time_tracking import (
        active_intervals_for_timestamps,
        clip_active_intervals,
        make_window_evidence,
    )

    bounded = [
        ts
        for ts in stat.timestamps
        if since.timestamp() <= ts < until.timestamp()
    ]
    return make_window_evidence(
        timestamps=bounded,
        active_intervals=clip_active_intervals(
            active_intervals_for_timestamps(stat.timestamps), since, until,
        ),
        msg_count=len(bounded),
        user_msg_count=1,
        first_user_text="in-window opener",
        latest_user_text="in-window opener",
        final_assistant_text="in-window answer",
        excerpt="in-window opener\nin-window answer",
    )
