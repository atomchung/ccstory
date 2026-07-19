"""Tests for ccstory.categorizer.

Focus: normalization correctness, classification rules, config override
precedence, and graceful fallback on malformed TOML (covers parts of #9).
"""

from __future__ import annotations

from pathlib import Path

from ccstory.categorizer import (
    BUCKET_COLORS,
    DEFAULT_FALLBACK_BUCKET,
    classify,
    color_for,
    colors_for,
    load_rules,
    load_settings,
    normalize_project_name,
)


class TestNormalizeProjectName:
    def test_empty(self):
        assert normalize_project_name("") == ""

    def test_strips_users_prefix(self):
        # `Side`, `project` are stem hints and get stripped along the way
        assert normalize_project_name("-Users-alice-Side-project-awesome-app") == "awesome-app"

    def test_strips_home_prefix(self):
        assert normalize_project_name("-home-bob-code-myapp") == "myapp"

    def test_strips_worktree_suffix(self):
        encoded = "-Users-alice-code-myrepo--claude-worktrees-foo-bar-abc123"
        assert normalize_project_name(encoded) == "myrepo"

    def test_strips_stem_hints(self):
        # `projects`, `code`, `workspace` are stem hints
        assert normalize_project_name("-Users-alice-workspace-mything") == "mything"

    def test_underscore_to_dash(self):
        assert normalize_project_name("-Users-alice-code-super_cool_thing") == "super-cool-thing"

    def test_top_level_fallback(self):
        # All tokens stripped → return sentinel
        assert normalize_project_name("-Users-alice-code") == "(top-level)"


class TestClassify:
    def test_investment_bucket(self):
        assert classify("-Users-alice-Side-project-portfolio-tracker") == "investment"

    def test_writing_bucket(self):
        assert classify("-Users-alice-blog-myblog") == "writing"

    def test_coding_bucket(self):
        assert classify("-Users-alice-code-cli-tool") == "coding"

    def test_other_bucket(self):
        assert classify("-Users-alice-code-playground") == "other"

    def test_unmatched_falls_back_to_coding(self):
        # `randomname` has no needle match; default fallback is `coding`
        assert classify("-Users-alice-code-randomname") == DEFAULT_FALLBACK_BUCKET

    def test_first_match_wins_investment_before_coding(self):
        # `investment-dashboard` has both "investment" and "dashboard" needles —
        # investment is listed first in DEFAULT_RULES.
        assert classify("-Users-alice-code-investment-dashboard") == "investment"

    def test_token_match_not_substring(self):
        # "cli" must not match a project named "paperclip" (no token equality)
        assert classify("-Users-alice-code-paperclip") == DEFAULT_FALLBACK_BUCKET

    def test_empty_input_returns_fallback(self):
        assert classify("") == DEFAULT_FALLBACK_BUCKET


class TestLoadRules:
    # `load_rules` and `load_settings` bind their default `config_path` arg at
    # def-time, so monkeypatching the module-level `CONFIG_PATH` doesn't
    # reach them. Tests below pass the path explicitly.

    def test_missing_config_returns_defaults_only(self, tmp_path: Path):
        nonexistent = tmp_path / "config.toml"
        rules = load_rules(nonexistent)
        assert any(r.name == "investment" for r in rules)
        assert any(r.name == "coding" for r in rules)

    def test_user_override_takes_precedence(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[categories]\n'
            '"work" = ["myrepo", "internal-tool"]\n',
            encoding="utf-8",
        )
        rules = load_rules(cfg)
        # user rule comes first
        assert rules[0].name == "work"
        assert "myrepo" in rules[0].needles

    def test_user_override_routes_classify(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[categories]\n'
            '"work" = ["myrepo"]\n',
            encoding="utf-8",
        )
        rules = load_rules(cfg)
        assert classify("-Users-alice-code-myrepo", rules) == "work"

    def test_malformed_toml_falls_back_silently(self, tmp_path: Path):
        # Today's behavior: malformed config is swallowed and defaults used.
        # This test locks the *current* behavior so #9 (fail-loud) is a
        # deliberate, visible change.
        cfg = tmp_path / "config.toml"
        cfg.write_text("this is not [valid toml", encoding="utf-8")
        rules = load_rules(cfg)
        # Should still have default rules (no crash)
        assert any(r.name == "coding" for r in rules)

    def test_malformed_rule_value_ignored(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        # `needles` should be list[str]; an int value should be skipped
        cfg.write_text(
            '[categories]\n'
            '"bogus" = 42\n'
            '"good"  = ["legit-needle"]\n',
            encoding="utf-8",
        )
        rules = load_rules(cfg)
        names = [r.name for r in rules]
        assert "good" in names
        assert "bogus" not in names


class TestLoadSettings:
    def test_defaults_when_no_config(self, tmp_path: Path):
        nonexistent = tmp_path / "config.toml"
        s = load_settings(nonexistent)
        assert s["default_bucket"] == "coding"
        assert s["monthly_quota_usd"] == 3500.0

    def test_user_quota_override(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            'monthly_quota_usd = 1500\n'
            'default_bucket = "writing"\n',
            encoding="utf-8",
        )
        s = load_settings(cfg)
        assert s["default_bucket"] == "writing"
        assert s["monthly_quota_usd"] == 1500.0


class TestColors:
    def test_known_bucket_returns_mapped_color(self):
        assert color_for("coding") == BUCKET_COLORS["coding"]
        assert color_for("investment") == BUCKET_COLORS["investment"]

    def test_unknown_bucket_returns_stable_palette_color(self):
        # Same input must produce same output across calls (deterministic)
        assert color_for("custom_bucket") == color_for("custom_bucket")

    def test_unknown_bucket_color_stable_across_processes(self):
        # Issue #82-Bug8: built-in hash() is PYTHONHASHSEED-salted, so
        # subprocess invocations would shuffle the palette. crc32 must not.
        import subprocess
        import sys

        script = (
            "from ccstory.categorizer import color_for; "
            "print(color_for('client: acme'))"
        )
        runs = {
            subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, check=True
            ).stdout.strip()
            for _ in range(3)
        }
        assert len(runs) == 1

    def test_uses_base_ansi_palette_for_unknown_buckets(self):
        # Issue #14: bright_*/256-color shades ignore the user's terminal
        # theme. Verify the fallback palette is constrained to base ANSI.
        base_ansi = {"cyan", "green", "magenta", "yellow", "blue", "red",
                     "dim", "bold"}
        for bucket in ("client-a", "client-b", "client-c", "client-d",
                       "client-e", "client-f", "client-g"):
            assert color_for(bucket) in base_ansi


class TestColorsFor:
    def test_resolves_real_collision_from_bug_report(self):
        # Custom [projects] aliases from a real recap card: color_for()
        # independently hashed 輸出/投資 to the same "green" and 學習/其他 to
        # the same "blue" (crc32(x) % 6 collision — none of these are
        # BUCKET_COLORS keys, so both fell into the 6-slot unknown palette).
        buckets = ["輸出", "投資", "學習", "其他", "職涯"]
        assert len({color_for(b) for b in buckets}) < len(buckets)  # bug, pre-fix
        colors = colors_for(buckets)
        assert len(set(colors.values())) == len(buckets)

    def test_known_buckets_keep_their_mapped_color(self):
        colors = colors_for(["coding", "investment", "custom-x"])
        assert colors["coding"] == BUCKET_COLORS["coding"]
        assert colors["investment"] == BUCKET_COLORS["investment"]

    def test_unknown_bucket_avoids_a_known_buckets_color(self):
        # "writing" is magenta; force an unknown bucket whose own crc32 slot
        # is also magenta and confirm it gets bumped to a free color instead.
        import zlib
        palette = ["cyan", "green", "magenta", "yellow", "blue", "red"]
        collider = next(
            f"custom-{i}" for i in range(200)
            if palette[zlib.crc32(f"custom-{i}".encode()) % len(palette)] == "magenta"
        )
        colors = colors_for(["writing", collider])
        assert colors["writing"] == "magenta"
        assert colors[collider] != "magenta"

    def test_deterministic_for_the_same_bucket_list(self):
        buckets = ["輸出", "投資", "學習", "其他", "職涯"]
        assert colors_for(buckets) == colors_for(list(buckets))

    def test_degrades_to_repeats_only_past_palette_size(self):
        # More unique unknown buckets than the 6-slot palette can hold —
        # every bucket still gets a valid color, repeats are unavoidable.
        buckets = [f"custom-{i}" for i in range(9)]
        colors = colors_for(buckets)
        assert set(colors) == set(buckets)
        base_ansi = {"cyan", "green", "magenta", "yellow", "blue", "red"}
        assert all(c in base_ansi for c in colors.values())

    def test_empty_and_duplicate_input(self):
        assert colors_for([]) == {}
        buckets = ["輸出", "輸出", "投資"]
        colors = colors_for(buckets)
        assert set(colors) == {"輸出", "投資"}
