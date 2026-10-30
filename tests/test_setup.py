"""Setup with nothing typed: clients listed by name get a token the add-on generates and logs once,
namespaces map without a configured owner, and every place the version is written agrees."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_memory"))
import mcp
import namespaces as ns
import page
import server as srv

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {"clients": [{"name": "claude-code", "scopes": ["read", "write", "delete"]}, {"name": "codex", "scopes": ["read", "write", "delete"]}]}


def tokens_file(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "client_tokens.json").read_text(encoding="utf-8"))


def test_a_client_listed_by_name_gets_a_generated_token_that_authenticates(tmp_path):
    app = srv.App(DEFAULTS, str(tmp_path))
    assert sorted(app.issued) == ["claude-code", "codex"]
    token = app.issued["codex"]
    assert len(token) >= 40 and app.issued["claude-code"] != token
    assert app.auth.identify(f"Bearer {token}", "192.0.2.7") == ("codex", {"read", "write", "delete"}, True)
    assert app.auth.identify("Bearer not-the-token", "192.0.2.7") is None
    assert app.auth.identify(None, "192.0.2.7") is None, "a generated client never matches by address"
    stored = tokens_file(tmp_path)
    assert stored["codex"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in (tmp_path / "client_tokens.json").read_text(encoding="utf-8"), "only the hash is kept"


def test_a_generated_token_survives_a_restart_and_is_shown_only_once(tmp_path):
    first = srv.App(DEFAULTS, str(tmp_path))
    again = srv.App(DEFAULTS, str(tmp_path))
    assert again.issued == {}
    token = first.issued["claude-code"]
    assert again.auth.identify(f"Bearer {token}", "192.0.2.7")[0] == "claude-code"


def test_removing_a_client_drops_its_token_and_adding_it_back_issues_a_new_one(tmp_path):
    first = srv.App(DEFAULTS, str(tmp_path))
    old = first.issued["codex"]
    srv.App({"clients": DEFAULTS["clients"][:1]}, str(tmp_path))
    assert set(tokens_file(tmp_path)) == {"claude-code"}
    back = srv.App(DEFAULTS, str(tmp_path))
    assert list(back.issued) == ["codex"] and back.issued["codex"] != old
    assert back.auth.identify(f"Bearer {old}", "192.0.2.7") is None


def test_clients_with_an_address_or_a_hash_are_left_as_configured(tmp_path):
    digest = hashlib.sha256(b"a token made elsewhere").hexdigest()
    clients = [{"name": "desk", "ip": "192.0.2.2", "scopes": ["read"]}, {"name": "bridge", "token_sha256": digest, "scopes": ["read", "write"]}]
    app = srv.App({"clients": clients}, str(tmp_path))
    assert app.issued == {} and app.auth.clients == clients
    assert not (tmp_path / "client_tokens.json").exists(), "nothing to generate, nothing written"
    assert [r["by"] for r in app.client_rows()] == ["address", "token"]


def test_the_log_shows_each_new_token_once_with_the_commands(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(srv, "serve", lambda app, port: None)
    monkeypatch.setattr(srv.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt))
    (tmp_path / "options.json").write_text(json.dumps(DEFAULTS), encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="agent-memory"):
        srv.main(["--options", str(tmp_path / "options.json"), "--data", str(tmp_path)])
    text = "\n".join(r.getMessage() for r in caplog.records)
    for name in ("claude-code", "codex"):
        assert f"client {name!r}: new token, shown once: " in text
    assert "claude mcp add --scope user --transport http agent-memory http://<home-assistant-host>:8765/mcp --header 'Authorization: Bearer " in text
    assert 'bearer_token_env_var = "AGENT_MEMORY_TOKEN"' in text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="agent-memory"):
        srv.main(["--options", str(tmp_path / "options.json"), "--data", str(tmp_path)])
    assert "new token" not in "\n".join(r.getMessage() for r in caplog.records)


def test_the_page_says_how_to_connect_and_shows_no_token(tmp_path):
    app = srv.App(DEFAULTS, str(tmp_path))
    html = page.render(app.panel_data("ha.example"))
    assert "Connect an agent" in html and "http://ha.example:8765/mcp" in html
    assert "generated token" in html
    for token in app.issued.values():
        assert token not in html
    for digest in tokens_file(tmp_path).values():
        assert digest not in html


@pytest.mark.parametrize(
    ("header", "host"),
    [("ha.example:8123", "ha.example"), ("192.0.2.10:8123", "192.0.2.10"), ("[2001:db8::1]:8123", "2001:db8::1"), ("ha.example", "ha.example"), (None, ""), ("<script>", ""), ("a b", "")],
)
def test_the_forwarded_host_is_a_plain_name_or_nothing(header, host):
    assert srv.forwarded_host(header) == host


def test_the_mcp_url_brackets_an_ipv6_host_and_names_a_missing_one():
    assert srv.mcp_url("2001:db8::1") == "http://[2001:db8::1]:8765/mcp"
    assert srv.mcp_url("") == "http://<home-assistant-host>:8765/mcp"


def test_without_owners_a_remote_maps_under_its_own_owner_and_no_forge_is_assumed():
    assert ns.normalize_remote("git@github.com:octo/tools.git", "", "", ()) == "github/octo/tools"
    assert ns.normalize_remote("https://github.com/someone/repo", "", "", ()) == "github/someone/repo"
    assert ns.normalize_remote("ssh://forge.example/octo/tools.git", "", "", ()) is None
    assert ns.normalize_remote("ssh://forge.example/octo/tools.git", "", "", ("forge.example",)) == "gitea/octo/tools"
    assert ns.normalize_remote("ssh://forge.example/other/tools.git", "", "octo", ("forge.example",)) is None
    assert mcp.Memory(None, {}).namespace("git@github.com:octo/tools.git")["namespace"] == "github/octo/tools"


def test_every_place_the_version_is_written_agrees():
    config = re.search(r'^version: "([^"]+)"$', (ROOT / "agent_memory" / "config.yaml").read_text(encoding="utf-8"), re.M).group(1)
    pyproject = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    changelog = re.search(r"^## (\S+)$", (ROOT / "agent_memory" / "CHANGELOG.md").read_text(encoding="utf-8"), re.M).group(1)
    assert config == pyproject == changelog == mcp.VERSION
    assert srv.make_handler(None).server_version == f"agent-memory/{mcp.VERSION}"


def test_no_option_default_is_needed_to_start(tmp_path):
    app = srv.App({}, str(tmp_path))
    assert app.auth.clients == [] and app.health()["version"] == mcp.VERSION
