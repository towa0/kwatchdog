import textwrap

import pytest

from kwatchdog.core import secrets
from kwatchdog.core.config import ConfigError, add_project, load_config, raw_watcher, upsert_watcher
from kwatchdog.core.models import Status, parse_duration
from kwatchdog.core.plugin import registries


@pytest.fixture(scope="module")
def regs():
    return registries()


def load(text, regs, tmp_path=None):
    return load_config(None, *regs, text=textwrap.dedent(text))


def test_durations():
    assert parse_duration("90") == 90
    assert parse_duration("5m") == 300
    assert parse_duration("1.5h") == 5400
    assert parse_duration(2) == 2
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_valid_config(regs):
    cfg = load("""
        alerts:
          default: {min_failures: 2, cooldown: 5m}
          critical: {severity: ALERT, min_failures: 1}
        channels:
          bell: {type: bell}
        projects:
          web:
            alerts: {cooldown: 1m}
            watchers:
              - {name: site, type: http, url: "https://example.com", interval: 30s}
              - {name: api, type: port, host: localhost, port: 8080, alerts: critical}
    """, regs)
    assert cfg.errors == []
    site, api = cfg.watchers()
    assert site.key == "web/site" and site.interval == 30
    assert site.rule.cooldown == 60 and site.rule.min_failures == 2  # project override on default
    assert api.rule.severity == Status.ALERT and api.rule.min_failures == 1
    assert site.config.url == "https://example.com"
    assert site.runnable


def test_validation_errors_are_collected_not_raised(regs):
    cfg = load("""
        projects:
          p:
            watchers:
              - {name: a, type: nope}
              - {name: b, type: port, host: x}
              - {name: c, type: port, host: x, port: 1, colour: red}
              - {name: d, type: port, host: x, port: 1, interval: soon}
              - {name: ok, type: port, host: x, port: 1}
    """, regs)
    errs = "\n".join(cfg.errors)
    assert "unknown watcher type 'nope'" in errs
    assert "p/b: port: Field required" in errs
    assert "colour" in errs
    assert "p/d" in errs
    specs = {w.name: w for w in cfg.watchers()}
    assert specs["ok"].runnable and not specs["a"].runnable and specs["a"].error


def test_yaml_syntax_error_raises(regs):
    with pytest.raises(ConfigError):
        load("projects: [unclosed", regs)


def test_env_secrets_expand_and_redact(regs, monkeypatch):
    monkeypatch.setenv("KW_TEST_TOKEN", "supersecret123")
    secrets.clear_secrets()
    cfg = load("""
        channels:
          tg: {type: telegram, token: "${KW_TEST_TOKEN}", chat_id: "42"}
    """, regs)
    assert cfg.errors == []
    assert cfg.channels["tg"].config.token == "supersecret123"
    assert secrets.redact("url /botsupersecret123/send") == "url /bot***/send"


def test_missing_env_var_is_error(regs, monkeypatch):
    monkeypatch.delenv("KW_NOPE", raising=False)
    cfg = load("""
        channels:
          tg: {type: telegram, token: "${KW_NOPE}", chat_id: "1"}
    """, regs)
    assert any("KW_NOPE" in e for e in cfg.errors)


def test_literal_secret_warning(regs):
    cfg = load("""
        channels:
          tg: {type: telegram, token: "123:abc", chat_id: "1"}
    """, regs)
    assert any("use ${ENV_VAR}" in w for w in cfg.warnings)


def test_unknown_channel_reference(regs):
    cfg = load("""
        alerts: {default: {channels: [pager]}}
        projects: {p: {watchers: [{name: a, type: port, host: x, port: 1}]}}
    """, regs)
    assert any("unknown channel 'pager'" in e for e in cfg.errors)


def test_dotenv_loaded(tmp_path, regs, monkeypatch):
    monkeypatch.delenv("KW_DOTENV_VAR", raising=False)
    (tmp_path / ".env").write_text("KW_DOTENV_VAR=fromdotenv\n")
    p = tmp_path / "config.yaml"
    p.write_text('channels: {n: {type: ntfy, topic: "${KW_DOTENV_VAR}"}}\n')
    cfg = load_config(p, *regs)
    assert cfg.channels["n"].config.topic == "fromdotenv"


def test_roundtrip_edit_keeps_comments(tmp_path, regs):
    p = tmp_path / "config.yaml"
    p.write_text("# my precious comment\nprojects:\n  a:\n    watchers: []  # inline\n")
    add_project(p, "b", "second")
    upsert_watcher(p, "a", {"name": "w", "type": "port", "host": "h", "port": 1})
    upsert_watcher(p, "a", {"name": "w2", "type": "port", "host": "h", "port": 2}, original_name="w")
    text = p.read_text()
    assert "# my precious comment" in text
    cfg = load_config(p, *regs)
    assert cfg.errors == []
    assert [w.key for w in cfg.watchers()] == ["a/w2"]
    assert raw_watcher(p, "a", "w2")["port"] == 2
    with pytest.raises(ConfigError):
        add_project(p, "b")
