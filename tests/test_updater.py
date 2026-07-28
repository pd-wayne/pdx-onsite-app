"""
test_updater.py — Tests for version parsing and update logic.
"""
import threading
import time
import pytest
import updater
from updater import parse_version, APP_VERSION, PRESERVE, download_and_install


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


class _SyncThread:
    """Runs the thread's target synchronously so tests can assert immediately."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}
    def start(self):
        self._target(*self._args, **self._kwargs)


class _FakeResp:
    def __init__(self, chunks, content_length=None, ok=True):
        self._chunks = chunks
        self.ok = ok
        self.status_code = 200 if ok else 500
        self.headers = {"content-length": str(content_length)} if content_length is not None else {}
    def iter_content(self, chunk_size=65536):
        return iter(self._chunks)


class TestDownloadAndInstallIntegrityChecks:
    """A silently-truncated or corrupted OTA download must never overwrite the
    working exe — that's exactly what produces PyInstaller's "Failed to load
    Python DLL" bootloader error on next launch, by which point it's too late
    to recover automatically. These checks must reject bad downloads BEFORE
    any swap-relevant file (the .bat script) is even written."""

    def _run_and_capture(self, monkeypatch, fake_resp, tmp_path):
        import requests as _requests
        monkeypatch.setattr(_requests, "get", lambda *a, **k: fake_resp)
        monkeypatch.setattr(threading, "Thread", _SyncThread)
        monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
        import tempfile as _tempfile
        monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(tmp_path))

        results = {"progress": [], "complete": False, "error": None}
        download_and_install(
            "https://example.com/fake.exe",
            on_progress=lambda pct, msg: results["progress"].append((pct, msg)),
            on_complete=lambda: results.__setitem__("complete", True),
            on_error=lambda err: results.__setitem__("error", err),
        )
        return results

    def test_rejects_incomplete_download(self, monkeypatch, tmp_path):
        resp = _FakeResp([b"x" * 2_000_000], content_length=5_000_000)
        results = self._run_and_capture(monkeypatch, resp, tmp_path)
        assert results["error"] is not None
        assert "incomplete" in results["error"].lower()
        assert results["complete"] is False
        assert not (tmp_path / "_pdx_update.bat").exists()
        assert not (tmp_path / "PDX_Onsite_update.exe").exists()

    def test_rejects_too_small_download(self, monkeypatch, tmp_path):
        resp = _FakeResp([b"MZ" + b"x" * 100], content_length=None)
        results = self._run_and_capture(monkeypatch, resp, tmp_path)
        assert results["error"] is not None
        assert "small" in results["error"].lower()
        assert results["complete"] is False

    def test_rejects_invalid_executable_magic_bytes(self, monkeypatch, tmp_path):
        resp = _FakeResp([b"<html>not an exe</html>" + b"x" * 2_000_000], content_length=None)
        results = self._run_and_capture(monkeypatch, resp, tmp_path)
        assert results["error"] is not None
        assert "not a valid" in results["error"].lower()
        assert results["complete"] is False

    def test_accepts_valid_download_and_backs_up_current_exe(self, monkeypatch, tmp_path):
        import subprocess as _subprocess
        # DETACHED_PROCESS / CREATE_NEW_PROCESS_GROUP are Windows-only constants —
        # stub them so this test can run on any dev machine.
        monkeypatch.setattr(_subprocess, "DETACHED_PROCESS", 0x00000008, raising=False)
        monkeypatch.setattr(_subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
        monkeypatch.setattr(_subprocess, "Popen", lambda *a, **k: None)
        monkeypatch.setattr(__import__("os"), "_exit", lambda *a, **k: None)

        payload = b"MZ" + b"x" * 2_000_000
        resp = _FakeResp([payload], content_length=len(payload))
        results = self._run_and_capture(monkeypatch, resp, tmp_path)

        assert results["error"] is None
        assert results["complete"] is True
        bat_path = tmp_path / "_pdx_update.bat"
        assert bat_path.exists()
        bat_content = bat_path.read_text()
        assert ".bak" in bat_content
        assert "move /Y" in bat_content


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
