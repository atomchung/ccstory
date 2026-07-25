"""Shared formatting for provider-owned conversation excerpts."""

from __future__ import annotations

N_USER_HEAD = 3
N_USER_TAIL = 2
N_ASSISTANT_TAIL = 1


def include_message(text: str) -> bool:
    """Whether transcript text is user-facing narrative material."""
    return bool(
        text
        and not text.startswith("<scheduled-task")
        and not text.startswith("<system-reminder>")
        and "tool_use_id" not in text
    )


def build_excerpt(user_msgs: list[str], assistant_msgs: list[str]) -> str:
    """Format bounded first/last messages for the narrative backend."""
    parts: list[str] = []
    head_set = set(user_msgs[:N_USER_HEAD])
    for index, message in enumerate(user_msgs[:N_USER_HEAD], start=1):
        parts.append(f"[USER {index}]\n{message}")
    if len(user_msgs) > N_USER_HEAD + N_USER_TAIL:
        parts.append("...")
    for message in user_msgs[-N_USER_TAIL:]:
        if message not in head_set:
            parts.append(f"[USER LATE]\n{message}")
    for message in assistant_msgs[-N_ASSISTANT_TAIL:]:
        parts.append(f"[ASSISTANT END]\n{message[:300]}")
    return "\n\n".join(parts)
