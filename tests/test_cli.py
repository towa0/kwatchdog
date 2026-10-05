import pytest

from kwatchdog.cli import main, parse_value
from kwatchdog.core.storage import Store


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHDOG_HOME", str(tmp_path))
    monkeypatch.delenv("WATCHDOG_CONFIG", raising=False)
    return tmp_path


def test_parse_value():
    assert parse_value("8080") == 8080
    assert parse_value("[200, 301]") == [200, 301]
    assert parse_value("true") is True
    assert parse_value("https://x.y/z?a=1") == "https://x.y/z?a=1"
    assert parse_value("") == ""


def test_init_add_validate(home, capsys):
    assert main(["init"]) == 0
    assert (home / "config.yaml").exists() and (home / "plugins").is_dir()
    assert main(["add", "svc"]) == 0
    assert main(["add", "svc", "api", "port", "host=localhost", "port=8080", "interval=15s"]) == 0
    assert main(["add", "svc", "bad", "port", "host=localhost", "port=nope"]) == 1
    assert main(["add", "svc", "api", "port", "host=x", "port=1"]) == 1  # duplicate
    assert main(["validate"]) == 0
    out = capsys.readouterr().out
    assert "added svc/api" in out and "0 error(s)" in out
    text = (home / "config.yaml").read_text()
    assert "port: 8080" in text and "name: bad" not in text


def test_ping_and_list(home, capsys):
    main(["init"])
    assert main(["ping", "nightly"]) == 0
    assert Store(home / "watchdog.db").heartbeat_last("nightly") is not None
    assert main(["list"]) == 0
    assert "SLEEPING (not running)" in capsys.readouterr().out


def test_check_exit_codes(home, capsys):
    main(["init", "--force"])
    (home / "f.txt").write_text("x")
    main(["add", "t", "f", "file", f"path={(home / 'f.txt').as_posix()}"])
    assert main(["check", "t"]) == 0
    main(["add", "t", "missing", "file", f"path={(home / 'nope').as_posix()}"])
    assert main(["check", "t"]) == 2
    assert "ALERT" in capsys.readouterr().out
