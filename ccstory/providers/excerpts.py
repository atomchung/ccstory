"""Shared formatting for provider-owned conversation excerpts."""

from __future__ import annotations

import re

N_USER_HEAD = 3
N_USER_TAIL = 2
N_ASSISTANT_TAIL = 1
USER_EVIDENCE_CHARS = 500
ASSISTANT_EVIDENCE_CHARS = 300

_MARKER_LIKE_LINE = re.compile(
    r"(?m)^[^\S\r\n]*(\[(?:USER [0-9]+|USER LATE|ASSISTANT END)\]|\.\.\.)"
)
_BLOCK_MARKER = re.compile(
    r"(?m)^(?:"
    r"\[(?P<label>USER [0-9]+|USER LATE|ASSISTANT END)\]"
    r"|(?P<ellipsis>\.\.\.)"
    r")[ \t]*(?:\n|$)"
)


def _maximum_provider_excerpt_chars() -> int:
    """Return the exact maximum produced by :func:`build_excerpt`."""
    part_lengths = [
        *(len(f"[USER {index}]\n") + USER_EVIDENCE_CHARS
          for index in range(1, N_USER_HEAD + 1)),
        len("..."),
        *(len("[USER LATE]\n") + USER_EVIDENCE_CHARS
          for _ in range(N_USER_TAIL)),
        *(len("[ASSISTANT END]\n") + ASSISTANT_EVIDENCE_CHARS
          for _ in range(N_ASSISTANT_TAIL)),
    ]
    return sum(part_lengths) + 2 * (len(part_lengths) - 1)


# build_excerpt() emits at most five user messages and one final assistant
# message from the lists supplied by bundled providers. Keep this explicit so
# callers can test the boundary rather than rely on an undocumented "bounded"
# claim.
MAX_PROVIDER_EXCERPT_CHARS = _maximum_provider_excerpt_chars()


def normalize_evidence_text(text: str) -> str:
    """Collapse whitespace and escape lines that resemble excerpt structure.

    Excerpts are a small line-oriented interchange format. A literal role
    marker in transcript prose must remain prose rather than becoming a new
    block when a downstream summarizer parses the excerpt.
    """
    normalized_newlines = text.replace("\r\n", "\n").replace("\r", "\n")
    escaped = _MARKER_LIKE_LINE.sub(r"\\\1", normalized_newlines)
    return " ".join(escaped.split())


def compact_evidence_text(text: str, budget: int) -> str:
    """Return normalized head+tail evidence no longer than ``budget``.

    Positive budgets always retain the text tail. Budgets of three or more use
    an explicit Unicode ellipsis between non-empty head and tail segments.
    """
    if budget <= 0:
        return ""
    normalized = normalize_evidence_text(text)
    if len(normalized) <= budget:
        return normalized
    if budget == 1:
        return normalized[-1:]
    if budget == 2:
        return normalized[:1] + normalized[-1:]
    head = max(1, (budget - 1) // 2)
    tail = budget - head - 1
    return f"{normalized[:head]}…{normalized[-tail:]}"


def parse_excerpt_blocks(excerpt: str) -> list[tuple[str, str]]:
    """Parse blocks emitted by :func:`build_excerpt`.

    Only full marker lines are structural. ``build_excerpt`` backslash-escapes
    marker-looking transcript lines before rendering, so they remain inside
    their owning block.
    """
    matches = list(_BLOCK_MARKER.finditer(excerpt))
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        label = match.group("label")
        if label is None:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(excerpt)
        text = " ".join(excerpt[match.end():end].split())
        if text:
            blocks.append((label, text))
    return blocks


def include_message(text: str) -> bool:
    """Whether transcript text is user-facing narrative material."""
    return bool(
        text
        and not text.startswith("<scheduled-task")
        and not text.startswith("<system-reminder>")
        and "tool_use_id" not in text
    )


def build_excerpt(user_msgs: list[str], assistant_msgs: list[str]) -> str:
    """Format first/latest/final evidence within an explicit fixed bound.

    User blocks retain normalized head+tail evidence within 500 characters.
    The final assistant block retains normalized head+tail evidence within 300
    characters, so outcome/error/test evidence at the response tail survives.
    The complete return value never exceeds ``MAX_PROVIDER_EXCERPT_CHARS``.
    """
    parts: list[str] = []
    head_set = set(user_msgs[:N_USER_HEAD])
    for index, message in enumerate(user_msgs[:N_USER_HEAD], start=1):
        text = compact_evidence_text(message, USER_EVIDENCE_CHARS)
        if text:
            parts.append(f"[USER {index}]\n{text}")
    if len(user_msgs) > N_USER_HEAD + N_USER_TAIL:
        parts.append("...")
    for message in user_msgs[-N_USER_TAIL:]:
        if message not in head_set:
            text = compact_evidence_text(message, USER_EVIDENCE_CHARS)
            if text:
                parts.append(f"[USER LATE]\n{text}")
    for message in assistant_msgs[-N_ASSISTANT_TAIL:]:
        text = compact_evidence_text(message, ASSISTANT_EVIDENCE_CHARS)
        if text:
            parts.append(f"[ASSISTANT END]\n{text}")
    excerpt = "\n\n".join(parts)
    assert len(excerpt) <= MAX_PROVIDER_EXCERPT_CHARS
    return excerpt
