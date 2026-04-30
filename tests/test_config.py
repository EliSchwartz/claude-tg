import pytest
from pathlib import Path

from claude_tg.config import Config, ConfigError, load_config


def write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


def test_loads_minimal_valid_config(tmp_path):
    p = write_config(tmp_path, """
        telegram_bot_token = "bot:TOKEN"
        telegram_supergroup_id = -1001234
        allowed_user_ids = [42]
    """)
    cfg = load_config(p)
    assert isinstance(cfg, Config)
    assert cfg.telegram_bot_token == "bot:TOKEN"
    assert cfg.telegram_supergroup_id == -1001234
    assert cfg.allowed_user_ids == [42]
    # defaults
    assert cfg.reply_timeout_sec == 0
    assert cfg.on_telegram_failure == "deny"
    assert cfg.heartbeat_interval_sec == 30
    assert cfg.idle_threshold_sec == 120


def test_missing_required_field_raises(tmp_path):
    p = write_config(tmp_path, 'telegram_bot_token = "x"\n')
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "telegram_supergroup_id" in str(e.value)


def test_invalid_on_telegram_failure_raises(tmp_path):
    p = write_config(tmp_path, """
        telegram_bot_token = "x"
        telegram_supergroup_id = -1
        allowed_user_ids = [1]
        on_telegram_failure = "bogus"
    """)
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "on_telegram_failure" in str(e.value)


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(tmp_path / "nope.toml")
    assert "not found" in str(e.value).lower()
