"""
test_config.py — Tests for config load/save/defaults.
"""
import json
import pytest
import config


class TestConfigLoad:
    def test_returns_defaults_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "cfg.json"))
        cfg = config.load()
        assert cfg["lab_id"] == ""
        assert cfg["api_key"] == ""
        assert cfg["poll_interval"] == 60

    def test_all_default_keys_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "cfg.json"))
        cfg = config.load()
        for key in config.DEFAULTS:
            assert key in cfg

    def test_saved_values_override_defaults(self, tmp_path, monkeypatch):
        cfg_path = str(tmp_path / "cfg.json")
        monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
        with open(cfg_path, "w") as f:
            json.dump({"lab_id": "mylab", "poll_interval": 30}, f)
        cfg = config.load()
        assert cfg["lab_id"] == "mylab"
        assert cfg["poll_interval"] == 30

    def test_missing_keys_filled_from_defaults(self, tmp_path, monkeypatch):
        cfg_path = str(tmp_path / "cfg.json")
        monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
        with open(cfg_path, "w") as f:
            json.dump({"lab_id": "mylab"}, f)
        cfg = config.load()
        assert cfg["poll_interval"] == 60    # default filled in

    def test_corrupt_file_returns_defaults(self, tmp_path, monkeypatch):
        cfg_path = str(tmp_path / "cfg.json")
        monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
        with open(cfg_path, "w") as f:
            f.write("THIS IS NOT JSON {{{{")
        cfg = config.load()
        assert cfg["lab_id"] == ""   # fell back to defaults


class TestConfigSave:
    def test_save_returns_true_on_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "cfg.json"))
        assert config.save({"lab_id": "x"}) is True

    def test_saved_file_is_valid_json(self, tmp_path, monkeypatch):
        cfg_path = str(tmp_path / "cfg.json")
        monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
        config.save({"lab_id": "x"})
        with open(cfg_path) as f:
            data = json.load(f)
        assert data["lab_id"] == "x"

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "cfg.json"))
        payload = {
            "lab_id": "lab123",
            "api_key": "secret",
            "studio_name": "Test Studio",
            "poll_interval": 30,
        }
        config.save(payload)
        loaded = config.load()
        assert loaded["lab_id"] == "lab123"
        assert loaded["api_key"] == "secret"
        assert loaded["studio_name"] == "Test Studio"
        assert loaded["poll_interval"] == 30

    def test_save_includes_all_defaults(self, tmp_path, monkeypatch):
        """Saving a partial dict still writes all default keys."""
        monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "cfg.json"))
        config.save({"lab_id": "only_this"})
        with open(str(tmp_path / "cfg.json")) as f:
            data = json.load(f)
        for key in config.DEFAULTS:
            assert key in data

    def test_extra_keys_preserved_after_save(self, tmp_path, monkeypatch):
        """Keys not in DEFAULTS are still written and survive a load."""
        monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "cfg.json"))
        config.save({"lab_id": "x", "custom_key": "custom_value"})
        loaded = config.load()
        assert loaded["custom_key"] == "custom_value"

    def test_a_partial_save_does_not_reset_other_fields_to_defaults(self, tmp_path, monkeypatch):
        """Regression test: found by code review, not by a caller hitting it.
        save() used to merge onto DEFAULTS instead of the currently-saved
        config — safe only as long as every caller always submits every
        field. The Settings page's normal Save button doesn't know about
        station_role/station_name/joined_primary_url, so a plain settings
        save was silently resetting a configured multi-station role back to
        "solo" every time. Any partial save, for any field, must not touch
        fields it didn't mention."""
        monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "cfg.json"))
        config.save({"lab_id": "LAB1", "api_key": "KEY1", "station_role": "primary", "station_name": "Front Desk"})

        # A normal Settings-page save only ever sends the settings-form
        # fields — never station_role/station_name.
        config.save({"lab_id": "LAB1", "api_key": "KEY1", "studio_name": "My Studio"})

        cfg = config.load()
        assert cfg["station_role"] == "primary"
        assert cfg["station_name"] == "Front Desk"
        assert cfg["studio_name"] == "My Studio"


class TestPerEnvironmentCredentials:
    """Production and staging each keep their own saved Lab ID/API Key.
    lab_id/api_key (used everywhere else — poller, job-seeding, etc.) always
    mirror whichever environment is currently active."""

    def test_legacy_lab_id_migrates_to_production_on_load(self, tmp_path, monkeypatch):
        """A studio that saved credentials before this feature existed had
        an implicitly-production lab_id/api_key — load() must not make it
        look like they lost their Lab ID the first time Settings opens."""
        cfg_path = str(tmp_path / "cfg.json")
        monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
        with open(cfg_path, "w") as f:
            json.dump({"lab_id": "OLD_LAB", "api_key": "OLD_KEY"}, f)

        cfg = config.load()
        assert cfg["production_lab_id"] == "OLD_LAB"
        assert cfg["production_api_key"] == "OLD_KEY"

    def test_migration_does_not_override_an_already_saved_production_lab_id(self, tmp_path, monkeypatch):
        cfg_path = str(tmp_path / "cfg.json")
        monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
        with open(cfg_path, "w") as f:
            json.dump({"lab_id": "CURRENT", "production_lab_id": "ALREADY_SET"}, f)

        cfg = config.load()
        assert cfg["production_lab_id"] == "ALREADY_SET"

    def test_saving_staging_credentials_does_not_overwrite_production(self, tmp_path, monkeypatch):
        """The actual bug this fixes: before per-environment storage, saving
        staging's Lab ID overwrote production's in the same lab_id field."""
        monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "cfg.json"))
        config.save({
            "api_environment": "production",
            "lab_id": "PROD_LAB", "api_key": "PROD_KEY",
            "production_lab_id": "PROD_LAB", "production_api_key": "PROD_KEY",
            "staging_lab_id": "", "staging_api_key": "",
        })
        config.save({
            "api_environment": "staging",
            "lab_id": "STAGE_LAB", "api_key": "STAGE_KEY",
            "production_lab_id": "PROD_LAB", "production_api_key": "PROD_KEY",
            "staging_lab_id": "STAGE_LAB", "staging_api_key": "STAGE_KEY",
        })

        cfg = config.load()
        assert cfg["production_lab_id"] == "PROD_LAB"
        assert cfg["production_api_key"] == "PROD_KEY"
        assert cfg["staging_lab_id"] == "STAGE_LAB"
        assert cfg["staging_api_key"] == "STAGE_KEY"
        assert cfg["lab_id"] == "STAGE_LAB"  # active credentials match the active environment
