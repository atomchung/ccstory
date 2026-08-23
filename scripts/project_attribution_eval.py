#!/usr/bin/env python3
"""Private rule suggestion and evaluation harness for ccstory issue #223."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Prefer the checkout that owns this script over an older installed ccstory.
# The documented ``python scripts/...`` invocation otherwise puts only the
# scripts directory at the front of sys.path.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ccstory.project_attribution import (
    ProjectAttributionError,
    evaluate_profiles,
    load_evidence_jsonl,
    load_profiles_toml,
    render_profiles_toml,
    suggest_rules,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Mine inspectable session-to-project rules from owner-labelled "
            "local evidence, then evaluate them without model calls."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    suggest = subparsers.add_parser(
        "suggest",
        help="emit suggested Project Profile rules from a labelled train split",
    )
    suggest.add_argument("evidence", type=Path, help="private JSONL evidence file")
    suggest.add_argument(
        "--split", default="train", help="labelled split used to mine rules"
    )
    suggest.add_argument("--min-support", type=int, default=2)
    suggest.add_argument("--min-precision", type=float, default=0.9)
    suggest.add_argument(
        "-o",
        "--output",
        type=Path,
        help="write TOML here; defaults to stdout",
    )

    evaluate = subparsers.add_parser(
        "evaluate",
        help="replay deterministic rules and print per-session traces plus metrics",
    )
    evaluate.add_argument("profiles", type=Path, help="Project Profile TOML")
    evaluate.add_argument("evidence", type=Path, help="private JSONL evidence file")
    evaluate.add_argument("--split", default="test")
    evaluate.add_argument("--min-score", type=float, default=2.0)
    evaluate.add_argument("--min-margin", type=float, default=1.0)
    evaluate.add_argument(
        "--include-suggested",
        action="store_true",
        help=(
            "experimentally apply suggested rules; default evaluation applies "
            "accepted rules only"
        ),
    )
    evaluate.add_argument(
        "--format",
        choices=("jsonl", "json"),
        default="jsonl",
        help="jsonl streams one trace per line; json emits one document",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    sessions = load_evidence_jsonl(args.evidence)
    if args.command == "suggest":
        profiles = suggest_rules(
            sessions,
            split=args.split,
            min_support=args.min_support,
            min_precision=args.min_precision,
        )
        rendered = render_profiles_toml(profiles)
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            args.output.write_text(rendered, encoding="utf-8")
        return 0

    profiles = load_profiles_toml(args.profiles)
    rows, summary = evaluate_profiles(
        profiles,
        sessions,
        split=args.split,
        include_suggested=args.include_suggested,
        min_score=args.min_score,
        min_margin=args.min_margin,
    )
    if args.format == "json":
        print(
            json.dumps(
                {"results": rows, "summary": summary},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for row in [*rows, summary]:
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    try:
        return _run(_parser().parse_args())
    except (ProjectAttributionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
