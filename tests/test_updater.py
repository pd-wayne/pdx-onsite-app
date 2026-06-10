"""
test_updater.py — Tests for version parsing and update logic.
"""
import pytest
from updater import parse_version, APP_VERSION, PRESERVE


class TestParseVersion:
    def test_basic_semver(self):
        assert parse_version("2.0.0") == (2, 0, 0)

    def test_v_prefix(self):
        assert parse_version("v1.5.3") == (1, 5, 3)

    def test_v_prefix_uppercase(self):
        assert parse_version("V2.0.0") == (2, 0, 0)

    def test_single_zeros(self):
        assert parse_version("0.0.0") == (0, 0, 0)

    def test_large_version(self):
        assert parse_version("10.20.30") == (10, 20, 30)

    def test_invalid_string_returns_zero(self):
        assert parse_version("not-a-version") == (0, 0, 0)

    def test_empty_string_returns_zero(self):
        assert parse_version("") == (0, 0, 0)

    def test_whitespace_trimmed(self):
        assert parse_version("  2.0.0  ") == (2, 0, 0)


class TestVersionComparisons:
    def test_newer_minor(self):
        assert parse_version("2.1.0") > parse_version("2.0.0")

    def test_newer_patch(self):
        assert parse_version("2.0.1") > parse_version("2.0.0")

    def test_newer_major(self):
        assert parse_version("3.0.0") > parse_version("2.9.9")

    def test_older_version(self):
        assert parse_version("1.9.9") < parse_version("2.0.0")

    def test_equal_versions(self):
        assert parse_version("2.0.0") == parse_version("2.0.0")

    def test_v_prefix_equal_to_plain(self):
        assert parse_version("v2.0.0") == parse_version("2.0.0")

    def test_update_check_logic(self):
        """Simulates the update check: remote > current means update available."""
        current = parse_version(APP_VERSION)
        remote_newer = tuple(x + (1 if i == 1 else 0) for i, x in enumerate(current))
        assert remote_newer > current

    def test_no_update_when_same(self):
        assert not (parse_version(APP_VERSION) > parse_version(APP_VERSION))

    def test_no_update_when_older_remote(self):
        current = parse_version(APP_VERSION)
        older = (max(0, current[0] - 1), 0, 0)
        assert not (older > current)


class TestAppVersion:
    def test_app_version_parseable(self):
        assert parse_version(APP_VERSION) >= (2, 0, 0)

    def test_app_version_is_string(self):
        assert isinstance(APP_VERSION, str)

    def test_app_version_format(self):
        parts = APP_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)


class TestPreserveSet:
    def test_db_file_preserved(self):
        assert "pdx_onsite.db" in PRESERVE

    def test_config_file_preserved(self):
        assert "pdx_onsite_config.json" in PRESERVE

    def test_log_file_preserved(self):
        assert "pdx_onsite.log" in PRESERVE

    def test_sumatra_removed(self):
        """SumatraPDF.exe should not be in the preserve list (v2.0 removed it)."""
        assert "SumatraPDF.exe" not in PRESERVE

    def test_preserve_is_set(self):
        assert isinstance(PRESERVE, (set, frozenset))
