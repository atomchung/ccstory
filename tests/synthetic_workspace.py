"""Deterministic synthetic cross-provider fixture for ccstory #188, slice 0.8-G.

``build_synthetic_workspace()`` writes a small but rich multi-provider
workspace into a fake ``$HOME`` (the same layout the ``tmp_home`` fixture in
``tests/conftest.py`` isolates every test into) and returns a
:class:`SyntheticWorkspace` describing, in plain data, exactly what it wrote
and what a correct reader of it should observe. It exists so slice 0.8-F (the
sampling-plan consumers that replace ``session_summarizer.py``'s
``[:6000]``/``[:3000]`` prefix truncation) has one shared, trustworthy corpus
to assert against, without every test re-deriving its own small transcript.

Contents, per the #188 0.8-G acceptance list:

* Claude, Codex, and Antigravity sessions across four synthetic projects
  (``atlas-dashboard``, ``borealis-api``, ``cascade-etl``, ``delta-mobile``)
  spanning twelve distinct local calendar days;
* one physical session (Claude) whose messages straddle the window
  ``boundary``, so it must come back as a ``SessionSlice`` in both windows;
* a mix of "long" (many turns, hours of active time) and "short" (two turns,
  minutes of active time) sessions;
* two kinds of duplication: a thematically-linked bug-report/fix pair
  (``auth-incident``) with different wording, and a literal, byte-identical
  duplicate first message (``onboarding-copy-dup``) planted on two different
  providers/projects/days;
* at least one session whose bounded evidence triggers each
  ``ccstory.sampling.SALIENCE_CODES`` entry;
* comfortably more than 6000 characters of total joined evidence (see
  ``SyntheticWorkspace.total_evidence_chars``), so a ``[:6000]`` prefix
  truncation demonstrably drops sessions an explicit sampling plan would keep.

Determinism is the whole point: every timestamp is anchored to the
``boundary`` parameter (a fixed constant by default, never ``datetime.now()``)
and every piece of generated text varies only by a fixed, explicit index —
never by the ``random`` module or wall-clock time. Two calls with the same
arguments write byte-identical files; see
``tests/test_synthetic_workspace.py`` for the self-checks that prove it.

This module is deliberately not named ``test_*.py`` (see
``[tool.pytest.ini_options] python_files`` in ``pyproject.toml``) so pytest
never collects it directly; it is a plain library module imported by tests.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ccstory.providers.excerpts import build_excerpt
from ccstory.providers.projects import encode_project_dir, worktree_origin
from ccstory.sampling import SALIENCE_CODES

from tests.conftest import make_assistant_msg, make_user_msg, write_jsonl

# ---------------------------------------------------------------------------
# Fixed vocabulary. Every value below is a constant, never derived from the
# clock or from `random` — see the module docstring's determinism guarantee.
# ---------------------------------------------------------------------------

#: Noon UTC, comfortably before this repository's present day so that a real
#: test run's file mtimes are never mistaken for "before the window" by the
#: Codex/Antigravity providers' `stat().st_mtime < since_ts` collection guard
#: (`ccstory/providers/codex.py` and `ccstory/providers/antigravity.py`
#: `collect_sessions()`). Any fixed date safely in the past works forever;
#: this one was chosen for no reason beyond being unambiguously "already
#: happened" relative to when this fixture was written.
DEFAULT_BOUNDARY = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

DEFAULT_WINDOW_SPAN = timedelta(days=7)

PROVIDERS: tuple[str, ...] = ("claude", "codex", "antigravity")

PROJECT_SLUGS: tuple[str, ...] = (
    "atlas-dashboard",
    "borealis-api",
    "cascade-etl",
    "delta-mobile",
)

# One (provider, project) pair per combination, nested provider-then-project.
# `build_synthetic_workspace` walks this in order and gives each pair exactly
# one "previous"-window session and one "current"-window session (24 total).
PAIRS: tuple[tuple[str, str], ...] = tuple(
    (provider, project) for provider in PROVIDERS for project in PROJECT_SLUGS
)

# Six distinct previous-window days and six distinct current-window days,
# each an exact whole-day multiple of `boundary` (never an hour/minute
# offset from the boundary itself). Two different whole-day offsets from the
# same anchor land on different local calendar days under *any* fixed-offset
# host timezone, matching the same `_at(day_offset, hour=12, ...)` shape
# `tests/test_sampling.py` already relies on for `_local_day`.
PREVIOUS_DAY_OFFSETS: tuple[int, ...] = (-6, -5, -4, -3, -2, -1)
CURRENT_DAY_OFFSETS: tuple[int, ...] = (1, 2, 3, 4, 5, 6)

SALIENCE_ROTATION: tuple[str, ...] = ("error", "test", "security", "artifact", "outcome")
assert set(SALIENCE_ROTATION) == set(SALIENCE_CODES), (
    "SALIENCE_ROTATION must name exactly ccstory.sampling.SALIENCE_CODES"
)

# Keyword-bearing sentence templates, one per salience code. Deliberately
# plain vocabulary lifted straight from `ccstory/sampling.py`'s
# `_SALIENCE_PATTERNS` (error/exception/traceback/crash..., pytest/regression/
# coverage..., vulnerab.../credential/secret/leak..., screenshot/deliverable/
# export/report..., fixed/resolved/shipped/merged/deployed/completed/done/
# success...) so a real parse is expected to detect exactly the intended code
# (extra incidental matches are fine; the self-check only asserts
# containment, never exact equality — see tests/test_synthetic_workspace.py).
_SALIENCE_SENTENCES: dict[str, str] = {
    "error": (
        "Investigate the {project} exception with a full traceback that "
        "keeps crashing the {provider} pipeline on session {seq:03d}."
    ),
    "test": (
        "Add pytest coverage and a regression test for the {project} suite "
        "uncovered while pairing on session {seq:03d}."
    ),
    "security": (
        "Rotate the leaked {project} credential and patch the vulnerability "
        "the scanner flagged for session {seq:03d}."
    ),
    "artifact": (
        "Export a screenshot deliverable and compile the {project} report "
        "artifact requested for session {seq:03d}."
    ),
    "outcome": (
        "Confirmed the {project} patch is resolved and shipped; the "
        "deployment completed successfully for session {seq:03d}."
    ),
}

# Keyword-free filler used only to pad a first user message past 200 raw
# characters (so a contained session's `first_user_text` — capped at 200 by
# every bundled provider's `parse_session()` — always lands on exactly 200).
# Checked by hand against every `_SALIENCE_PATTERNS` entry in
# `ccstory/sampling.py`: no root word here matches any of the five patterns.
_FILLER = "This synthetic fixture line adds bounded filler text for deterministic length."

# Keyword-free follow-up lines for the second/third/... turn of a session.
# Only a session's *first* user message ever becomes `first_user_text`, so
# these never affect evidence_chars or salience — they exist only so a
# "long" session has a plausible multi-turn shape.
_ASSISTANT_LINES: tuple[str, ...] = (
    "Looking into this now.",
    "That matches what the logs show.",
    "Noted, thanks for the update.",
)
_NEUTRAL_USER_LINES: tuple[str, ...] = (
    "Continuing the discussion with additional context from the session.",
    "Here is another follow-up note worth keeping on file.",
    "Wrapping up this thread with one final observation.",
)

def _pad_to(text: str, minimum: int) -> str:
    """Deterministically extend ``text`` past ``minimum`` characters.

    Padding only ever repeats the fixed, keyword-free ``_FILLER`` sentence,
    so it never changes which ``SALIENCE_CODES`` a caller's chosen prefix
    triggers — it only guarantees the *raw* text is long enough that a
    provider's ``text[:200]`` cap lands on exactly 200, not on however long
    the hand-written sentence happened to be.
    """

    out = text
    while len(out) < minimum:
        out = f"{out} {_FILLER}"
    return out


# The literal, byte-identical text planted on both `onboarding-copy-dup`
# sessions (different provider, project, and day) to exercise a real
# duplicate rather than only a thematically-linked one. Padded to the same
# 200-character floor as every other contained session's `first_user_text`
# (see `_pad_to`) so `total_evidence_chars`'s `200 * len(contained_sessions)`
# term holds for these two sessions too, not just the salience-bearing ones.
_DUPLICATE_FIRST_TEXT = _pad_to(
    "Quick sync on the onboarding flow copy changes and button labels "
    "before the design review.",
    200,
)


def _iso(value: datetime) -> str:
    """Render ``value`` the way every bundled provider's fixtures do."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _project_cwd(slug: str) -> str:
    return f"/Users/synthetic/{slug}"


def _project_name(slug: str) -> str:
    """The ``project`` string every provider reports for one logical project.

    Codex and Antigravity derive ``project`` from
    ``encode_project_dir(worktree_origin(cwd))``; Claude Code's project is
    the literal directory name under ``.claude/projects/``. Using this same
    encoded string as that directory name too means all three providers
    report the identical ``project`` value for "the same" synthetic
    repository, exactly like three real coding agents pointed at one
    checkout would.
    """

    return encode_project_dir(worktree_origin(_project_cwd(slug)))


Turn = tuple[str, str, datetime]  # (role, text, timestamp) — role is "user" or "assistant"


def _short_pattern(base: datetime, first_text: str) -> list[Turn]:
    """Two user turns + one assistant turn, ~4 minutes of wall time."""

    return [
        ("user", first_text, base),
        ("assistant", _ASSISTANT_LINES[0], base + timedelta(minutes=2)),
        ("user", _NEUTRAL_USER_LINES[0], base + timedelta(minutes=4)),
    ]


def _long_pattern(base: datetime, first_text: str) -> list[Turn]:
    """Four user turns + three assistant turns, ~200 minutes of wall time."""

    return [
        ("user", first_text, base),
        ("assistant", _ASSISTANT_LINES[0], base + timedelta(minutes=20)),
        ("user", _NEUTRAL_USER_LINES[0], base + timedelta(minutes=80)),
        ("assistant", _ASSISTANT_LINES[1], base + timedelta(minutes=95)),
        ("user", _NEUTRAL_USER_LINES[1], base + timedelta(minutes=150)),
        ("assistant", _ASSISTANT_LINES[2], base + timedelta(minutes=165)),
        ("user", _NEUTRAL_USER_LINES[2], base + timedelta(minutes=200)),
    ]


# ---------------------------------------------------------------------------
# Per-provider transcript writers. Each mirrors the on-disk shape already
# proven out by tests/conftest.py's `jsonl_factory`/`codex_factory` and
# tests/test_window_integration.py's local `antigravity_factory` — this
# module writes those exact shapes directly (as plain functions, not pytest
# fixtures) since `build_synthetic_workspace` takes `tmp_home` as a plain
# argument rather than requesting it through fixture injection.
# ---------------------------------------------------------------------------


def _write_claude_session(
    tmp_home: Path, *, project_dir: str, session_id: str, turns: Sequence[Turn],
) -> Path:
    records: list[dict] = []
    assistant_seq = 0
    for role, text, ts in turns:
        iso = _iso(ts)
        if role == "user":
            records.append(make_user_msg(text, iso))
        else:
            assistant_seq += 1
            records.append(
                make_assistant_msg(text, iso, f"{session_id}-a{assistant_seq}")
            )
    path = tmp_home / ".claude" / "projects" / project_dir / f"{session_id}.jsonl"
    return write_jsonl(path, records)


def _write_codex_session(
    tmp_home: Path, *, session_id: str, cwd: str, turns: Sequence[Turn],
) -> Path:
    first_ts = turns[0][2]
    root = (
        tmp_home
        / ".codex"
        / "sessions"
        / f"{first_ts.year:04d}"
        / f"{first_ts.month:02d}"
        / f"{first_ts.day:02d}"
    )
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"rollout-{first_ts.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"

    records: list[dict] = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": cwd}},
    ]
    for role, text, ts in turns:
        iso = _iso(ts)
        if role == "user":
            records.append(
                {
                    "timestamp": iso,
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": text},
                }
            )
        else:
            records.append(
                {
                    "timestamp": iso,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": text,
                    },
                }
            )
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def _write_antigravity_session(
    tmp_home: Path, *, session_id: str, cwd: str, turns: Sequence[Turn],
) -> Path:
    brain_dir = (
        tmp_home
        / ".gemini"
        / "antigravity"
        / "brain"
        / session_id
        / ".system_generated"
        / "logs"
    )
    brain_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = brain_dir / "transcript.jsonl"

    records: list[dict] = []
    for step_index, (role, text, ts) in enumerate(turns, start=1):
        iso = _iso(ts)
        if role == "user":
            records.append(
                {
                    "step_index": step_index,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": iso,
                    "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>",
                }
            )
        else:
            records.append(
                {
                    "step_index": step_index,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": iso,
                    "content": text,
                }
            )
    with transcript_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")

    # The cwd companion DB. Rebuilding an existing workspace must stay
    # byte-identical, and `CREATE TABLE` is not idempotent, so a stale file
    # from a previous build is removed first rather than reused.
    conv_dir = tmp_home / ".gemini" / "antigravity" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    db_path = conv_dir / f"{session_id}.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB);")
        blob = f"\x0a(file://{cwd}".encode("utf-8")
        cur.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (blob,))
        conn.commit()
    finally:
        conn.close()
    return transcript_path


def _write_session(
    tmp_home: Path,
    *,
    provider: str,
    session_id: str,
    project_dir: str,
    cwd: str,
    turns: Sequence[Turn],
) -> Path:
    if provider == "claude":
        return _write_claude_session(
            tmp_home, project_dir=project_dir, session_id=session_id, turns=turns,
        )
    if provider == "codex":
        return _write_codex_session(tmp_home, session_id=session_id, cwd=cwd, turns=turns)
    if provider == "antigravity":
        return _write_antigravity_session(
            tmp_home, session_id=session_id, cwd=cwd, turns=turns,
        )
    raise ValueError(f"unknown synthetic provider {provider!r}")


# ---------------------------------------------------------------------------
# Public description of what was written.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SyntheticSession:
    """One physical session this fixture wrote, and the facts it implies.

    ``local_day`` is computed exactly the way ``ccstory.sampling._local_day``
    computes it (``start.astimezone().date()``) — at build time, on whatever
    host runs the test — so it is always correct relative to that host's
    timezone rather than a value hardcoded against an assumed timezone.

    ``expected_salience`` is the union of ``SALIENCE_CODES`` this session's
    evidence is *intended* to trigger; ``expected_salience_by_window`` is
    only populated for a session that crosses ``boundary`` (only there does
    "which window" change which codes should show up).
    """

    session_id: str
    provider: str
    project: str
    theme: str
    length: str  # "long" | "short"
    start: datetime
    end: datetime
    local_day: date
    crosses_boundary: bool
    expected_windows: tuple[str, ...]
    expected_salience: tuple[str, ...]
    expected_salience_by_window: Mapping[str, tuple[str, ...]] | None
    path: Path


@dataclass(frozen=True)
class SyntheticWorkspace:
    """Everything ``build_synthetic_workspace`` wrote, as plain data.

    A consumer should be able to answer "which sessions belong in the
    previous window", "which one crosses", "which carry the `error` code",
    and "how many characters of evidence exist in total" from this object
    alone — never by re-parsing the transcripts it describes. See
    ``tests/test_synthetic_workspace.py`` for the self-checks that prove this
    description matches what the real bundled providers actually parse.
    """

    boundary: datetime
    windows: dict[str, tuple[datetime, datetime]]
    sessions: tuple[SyntheticSession, ...]
    total_evidence_chars: int
    crossing_session_id: str
    projects: tuple[str, ...]
    local_days: tuple[date, ...]

    def session(self, session_id: str) -> SyntheticSession:
        for candidate in self.sessions:
            if candidate.session_id == session_id:
                return candidate
        raise KeyError(session_id)

    def by_provider(self, provider: str) -> tuple[SyntheticSession, ...]:
        return tuple(s for s in self.sessions if s.provider == provider)

    def by_window(self, window_key: str) -> tuple[SyntheticSession, ...]:
        """Sessions that should appear (fully or as a slice) in one window."""

        return tuple(s for s in self.sessions if window_key in s.expected_windows)

    def with_salience(self, code: str) -> tuple[SyntheticSession, ...]:
        return tuple(s for s in self.sessions if code in s.expected_salience)

    def theme_group(self, theme: str) -> tuple[SyntheticSession, ...]:
        return tuple(s for s in self.sessions if s.theme == theme)


def _special_case(
    idx: int, window_key: str,
) -> tuple[str | None, str | None, str | None]:
    """Theme/code/literal-text override for one of the four planted entries.

    Returns ``(theme, code, override_text)``. ``theme`` is ``None`` only for
    an ordinary (non-special) entry — the caller then assigns a generated
    theme and the next rotating salience code. A special entry always
    returns a real theme string; its ``code`` may legitimately be ``None``
    (the duplicate-text pair deliberately carries no salience keyword).
    """

    if idx == 0 and window_key == "previous":
        return "auth-incident", "error", None
    if idx == 0 and window_key == "current":
        return "auth-incident", "outcome", None
    if idx == 2 and window_key == "current":
        return "onboarding-copy-dup", None, _DUPLICATE_FIRST_TEXT
    if idx == 9 and window_key == "current":
        return "onboarding-copy-dup", None, _DUPLICATE_FIRST_TEXT
    return None, None, None


def _build_contained_sessions(
    tmp_home: Path, *, boundary: datetime,
) -> list[SyntheticSession]:
    sessions: list[SyntheticSession] = []
    seq_counter = 0
    rotation_counter = 0

    for idx, (provider, project_slug) in enumerate(PAIRS):
        day_group = idx // 2
        minute_offset = 0 if idx % 2 == 0 else 20
        day_by_window = {
            "previous": PREVIOUS_DAY_OFFSETS[day_group],
            "current": CURRENT_DAY_OFFSETS[day_group],
        }

        for window_key in ("previous", "current"):
            seq_counter += 1
            length = "long" if seq_counter % 2 == 0 else "short"

            theme, code, override_text = _special_case(idx, window_key)
            if theme is None:
                theme = f"{project_slug}-{provider}-{window_key}"
                code = SALIENCE_ROTATION[rotation_counter % len(SALIENCE_ROTATION)]
                rotation_counter += 1

            if override_text is not None:
                first_text = override_text
            else:
                first_text = _pad_to(
                    _SALIENCE_SENTENCES[code].format(
                        project=project_slug, provider=provider, seq=seq_counter,
                    ),
                    200,
                )

            base_time = boundary + timedelta(
                days=day_by_window[window_key], minutes=minute_offset,
            )
            turns = (
                _long_pattern(base_time, first_text)
                if length == "long"
                else _short_pattern(base_time, first_text)
            )

            session_id = f"synthetic-{provider}-{project_slug}-{window_key}"
            cwd = _project_cwd(project_slug)
            project_dir = _project_name(project_slug)

            path = _write_session(
                tmp_home,
                provider=provider,
                session_id=session_id,
                project_dir=project_dir,
                cwd=cwd,
                turns=turns,
            )

            sessions.append(
                SyntheticSession(
                    session_id=session_id,
                    provider=provider,
                    project=project_dir,
                    theme=theme,
                    length=length,
                    start=turns[0][2],
                    end=turns[-1][2],
                    local_day=turns[0][2].astimezone().date(),
                    crosses_boundary=False,
                    expected_windows=(window_key,),
                    expected_salience=() if code is None else (code,),
                    expected_salience_by_window=None,
                    path=path,
                )
            )

    return sessions


_CROSSING_PROVIDER = "claude"
_CROSSING_PROJECT_SLUG = "delta-mobile"
_CROSSING_SESSION_ID = "synthetic-claude-crossing-release-freeze"


def _crossing_turns(boundary: datetime) -> tuple[list[Turn], list[Turn]]:
    """The one physical session's turns, split at ``boundary``.

    Three user messages before the boundary (an "error"+"test" salient bug
    investigation) and three after it (an "outcome" salient fix), each side
    with one assistant reply. Every side keeps its user-message count at
    exactly three so ``build_excerpt`` puts all of them in its "head" block
    (``N_USER_HEAD == 3``) with no "..." gap or "[USER LATE]" block — see
    ``ccstory/providers/excerpts.py`` — which is what lets
    ``_crossing_excerpt_lengths`` predict the *exact* excerpt text
    ``extract_window_evidence`` will produce, by calling the real
    ``build_excerpt`` on these same message lists rather than re-implementing
    its truncation rules by hand.

    This is the single source of truth for the crossing session's content:
    both the file writer and the excerpt-length predictor call this same
    function, so they can never drift apart.
    """

    # User messages are padded (with the same keyword-free `_FILLER` used for
    # the contained sessions' `first_user_text`) well past the 200-character
    # floor, and assistant messages past 150 — both comfortably under the
    # 500/300-character per-block caps `build_excerpt` applies, so
    # `compact_evidence_text` passes each one through unchanged. This is what
    # gives the crossing session's two excerpts enough real content to keep
    # `SyntheticWorkspace.total_evidence_chars` comfortably, not just barely,
    # past 6000 (see `_crossing_excerpt_lengths` and
    # `tests/test_synthetic_workspace.py`, which measures the real total
    # rather than trusting this comment's arithmetic).
    pre_turns: list[Turn] = [
        (
            "user",
            _pad_to(
                _SALIENCE_SENTENCES["error"].format(
                    project=_CROSSING_PROJECT_SLUG, provider=_CROSSING_PROVIDER, seq=901,
                ),
                350,
            ),
            boundary - timedelta(hours=3),
        ),
        (
            "assistant",
            _pad_to("Pulling the relevant logs to narrow down the timeline.", 150),
            boundary - timedelta(hours=2),
        ),
        (
            "user",
            _pad_to(
                _SALIENCE_SENTENCES["test"].format(
                    project=_CROSSING_PROJECT_SLUG, provider=_CROSSING_PROVIDER, seq=902,
                ),
                350,
            ),
            boundary - timedelta(minutes=90),
        ),
        (
            "user",
            _pad_to(
                "Pairing with the on-call engineer to narrow down which "
                "recent release is responsible.",
                350,
            ),
            boundary - timedelta(minutes=40),
        ),
    ]
    post_turns: list[Turn] = [
        (
            "user",
            _pad_to(
                _SALIENCE_SENTENCES["outcome"].format(
                    project=_CROSSING_PROJECT_SLUG, provider=_CROSSING_PROVIDER, seq=903,
                ),
                350,
            ),
            boundary + timedelta(minutes=40),
        ),
        (
            "user",
            _pad_to(
                "Documented the timeline in the incident retro notes for "
                "the on-call handoff.",
                350,
            ),
            boundary + timedelta(minutes=90),
        ),
        (
            "assistant",
            _pad_to(
                "Will monitor the service for the next few hours to "
                "confirm stability.",
                150,
            ),
            boundary + timedelta(hours=2),
        ),
        (
            "user",
            _pad_to(
                "Closing out the follow-up checklist for this release freeze.",
                350,
            ),
            boundary + timedelta(hours=3),
        ),
    ]
    return pre_turns, post_turns


def _build_crossing_session(
    tmp_home: Path, *, boundary: datetime,
) -> SyntheticSession:
    """Write the boundary-crossing session and describe it."""

    pre_turns, post_turns = _crossing_turns(boundary)

    path = _write_session(
        tmp_home,
        provider=_CROSSING_PROVIDER,
        session_id=_CROSSING_SESSION_ID,
        project_dir=_project_name(_CROSSING_PROJECT_SLUG),
        cwd=_project_cwd(_CROSSING_PROJECT_SLUG),
        turns=pre_turns + post_turns,
    )

    return SyntheticSession(
        session_id=_CROSSING_SESSION_ID,
        provider=_CROSSING_PROVIDER,
        project=_project_name(_CROSSING_PROJECT_SLUG),
        theme="release-freeze",
        length="long",
        start=pre_turns[0][2],
        end=post_turns[-1][2],
        local_day=pre_turns[0][2].astimezone().date(),
        crosses_boundary=True,
        expected_windows=("previous", "current"),
        expected_salience=("error", "test", "outcome"),
        expected_salience_by_window={
            "previous": ("error", "test"),
            "current": ("outcome",),
        },
        path=path,
    )


def _crossing_excerpt_lengths(boundary: datetime) -> tuple[int, int]:
    """Exact ``build_excerpt`` output length for each side of the crossing session.

    Calls the real, public ``build_excerpt`` — the same function
    ``ClaudeCodeProvider.extract_window_evidence`` calls — on the exact
    turns ``_crossing_turns`` produces, rather than hand-deriving its
    head/tail/truncation arithmetic. This keeps
    ``SyntheticWorkspace.total_evidence_chars`` correct by construction: any
    change to ``build_excerpt``'s formatting is picked up here automatically
    instead of silently drifting out of sync with a hardcoded estimate. No
    file is written here — this only formats in-memory text.
    """

    pre_turns, post_turns = _crossing_turns(boundary)
    pre_user = [text for role, text, _ts in pre_turns if role == "user"]
    pre_assistant = [text for role, text, _ts in pre_turns if role == "assistant"]
    post_user = [text for role, text, _ts in post_turns if role == "user"]
    post_assistant = [text for role, text, _ts in post_turns if role == "assistant"]
    previous_excerpt = build_excerpt(pre_user, pre_assistant)
    current_excerpt = build_excerpt(post_user, post_assistant)
    return len(previous_excerpt), len(current_excerpt)


def _validate_intent(workspace: SyntheticWorkspace) -> None:
    """Fail loudly at build time if the manifest stops matching its own spec.

    This checks the generator's *intent* only (arithmetic on what it meant
    to write) — it is not a substitute for ``tests/test_synthetic_workspace.py``,
    which re-derives every one of these facts by actually parsing the files
    through the real bundled providers.
    """

    if len(workspace.projects) < 4:
        raise AssertionError(
            f"expected >= 4 distinct projects, got {workspace.projects!r}"
        )
    if len(workspace.local_days) < 6:
        raise AssertionError(
            f"expected several distinct local days, got {workspace.local_days!r}"
        )
    if workspace.total_evidence_chars <= 6000:
        raise AssertionError(
            "expected total_evidence_chars to comfortably exceed 6000, got "
            f"{workspace.total_evidence_chars}"
        )
    crossing = [s for s in workspace.sessions if s.crosses_boundary]
    if len(crossing) != 1:
        raise AssertionError(f"expected exactly one crossing session, got {len(crossing)}")
    planted_codes: set[str] = set()
    for session in workspace.sessions:
        planted_codes.update(session.expected_salience)
    missing = set(SALIENCE_CODES) - planted_codes
    if missing:
        raise AssertionError(f"manifest never plants salience code(s): {sorted(missing)}")
    dup_pair = workspace.theme_group("onboarding-copy-dup")
    if len(dup_pair) != 2 or len({s.provider for s in dup_pair}) < 2:
        raise AssertionError("onboarding-copy-dup theme must span two different providers")
    theme_pair = workspace.theme_group("auth-incident")
    if len(theme_pair) != 2:
        raise AssertionError("auth-incident theme must have exactly two sessions")


def build_synthetic_workspace(
    tmp_home: Path,
    *,
    boundary: datetime = DEFAULT_BOUNDARY,
    window_span: timedelta = DEFAULT_WINDOW_SPAN,
) -> SyntheticWorkspace:
    """Write the whole synthetic cross-provider workspace into ``tmp_home``.

    ``tmp_home`` is expected to be the fake ``$HOME`` the ``tmp_home`` pytest
    fixture (``tests/conftest.py``) points ``$HOME``/``$USERPROFILE`` at, but
    this function itself only ever writes under the given path — it does not
    read or depend on any pytest fixture, so it can also be called with a
    bare ``tmp_path`` to prove the determinism/rebuild self-checks without
    the rest of the autouse fixture machinery.

    Calling this twice with the same arguments (even against two different
    directories) writes byte-identical files, because every timestamp is
    derived from ``boundary`` and every piece of text varies only by a fixed
    index — never by ``datetime.now()`` or the ``random`` module.
    """

    if boundary.tzinfo is None:
        boundary = boundary.replace(tzinfo=timezone.utc)
    else:
        boundary = boundary.astimezone(timezone.utc)

    windows: dict[str, tuple[datetime, datetime]] = {
        "previous": (boundary - window_span, boundary),
        "current": (boundary, boundary + window_span),
    }

    contained_sessions = _build_contained_sessions(tmp_home, boundary=boundary)
    crossing_session = _build_crossing_session(tmp_home, boundary=boundary)
    sessions = tuple(contained_sessions) + (crossing_session,)

    previous_excerpt_len, current_excerpt_len = _crossing_excerpt_lengths(boundary)
    total_evidence_chars = (
        200 * len(contained_sessions) + previous_excerpt_len + current_excerpt_len
    )

    projects = tuple(sorted({s.project for s in sessions}))
    local_days = tuple(sorted({s.local_day for s in sessions}))

    workspace = SyntheticWorkspace(
        boundary=boundary,
        windows=windows,
        sessions=sessions,
        total_evidence_chars=total_evidence_chars,
        crossing_session_id=crossing_session.session_id,
        projects=projects,
        local_days=local_days,
    )
    _validate_intent(workspace)
    return workspace


__all__ = [
    "DEFAULT_BOUNDARY",
    "DEFAULT_WINDOW_SPAN",
    "PROVIDERS",
    "PROJECT_SLUGS",
    "SyntheticSession",
    "SyntheticWorkspace",
    "build_synthetic_workspace",
]
