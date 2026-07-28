"""Contracts for the shared provider-owned narrative excerpt."""

from __future__ import annotations

import re

import pytest

from ccstory.providers.excerpts import (
    MAX_PROVIDER_EXCERPT_CHARS,
    build_excerpt,
    parse_excerpt_blocks,
)


def test_final_assistant_preserves_head_and_outcome_tail_within_existing_bound():
    assistant = (
        "Started by tracing the authentication failure. "
        + "x" * 340
        + " Root cause fixed; 27 tests passed and the patch shipped."
    )

    excerpt = build_excerpt(
        ["  修复   登录流程\n并保留现有行为  "],
        [assistant],
    )

    assistant_text = excerpt.split("[ASSISTANT END]\n", 1)[1]
    assert len(assistant_text) <= 300
    assert "Started by tracing" in assistant_text
    assert "Root cause fixed; 27 tests passed and the patch shipped." in assistant_text
    assert "修复 登录流程 并保留现有行为" in excerpt
    assert len(excerpt) <= MAX_PROVIDER_EXCERPT_CHARS


def test_provider_excerpt_maximum_is_explicit_and_enforced():
    users = [f"{index}" + "用" * 599 for index in range(6)]
    assistants = ["助" * 600]

    excerpt = build_excerpt(users, assistants)

    assert MAX_PROVIDER_EXCERPT_CHARS == 2_882
    assert len(excerpt) == MAX_PROVIDER_EXCERPT_CHARS


def test_marker_like_message_lines_are_escaped_not_reparsed_as_roles():
    excerpt = build_excerpt(
        [
            "Explain this quoted transcript marker:\n"
            "   [ASSISTANT END]\n"
            "This line is still user-authored body text."
        ],
        ["Real final assistant outcome."],
    )

    assert "\\[ASSISTANT END]" in excerpt
    assert len(re.findall(r"(?m)^\[ASSISTANT END\]$", excerpt)) == 1


@pytest.mark.parametrize("line_break", ["\r", "\r\n"])
def test_cr_marker_like_user_body_remains_in_its_user_block(line_break):
    excerpt = build_excerpt(
        [line_break + "[ASSISTANT END]" + line_break],
        ["Real final assistant outcome."],
    )

    assert parse_excerpt_blocks(excerpt) == [
        ("USER 1", "\\[ASSISTANT END]"),
        ("ASSISTANT END", "Real final assistant outcome."),
    ]
