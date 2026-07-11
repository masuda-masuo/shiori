"""Unit tests for github_auth (detailed design/09).

Mocks httpx to verify installation token acquisition, caching, and re-issuance.
RS256 keys are generated inside the test.
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shiori import github_auth
from shiori.config import Settings
from shiori.github_auth import (
    AnonymousProvider,
    AppTokenProvider,
    McpTokenProvider,
    StaticTokenProvider,
    TokenCommandProvider,
    build_token_provider,
)

_ENV_KEYS = [
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_TOKEN_COMMAND",
    "GITHUB_APP_PRIVATE_KEY_PATH",
]


@pytest.fixture
def rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_provider_anonymous(clean_env):
    p = build_token_provider(Settings())
    assert isinstance(p, McpTokenProvider)
    assert p.get_token() is None


def test_provider_static(clean_env):
    clean_env.setenv("GITHUB_TOKEN", "ghp_xxx")
    p = build_token_provider(Settings())
    assert isinstance(p, StaticTokenProvider)
    assert p.get_token() == "ghp_xxx"


def test_provider_static_ghp_does_not_warn(clean_env, caplog):
    """通常の PAT (ghp_) では警告を出さない。"""
    clean_env.setenv("GITHUB_TOKEN", "ghp_xxx")
    with caplog.at_level("WARNING"):
        build_token_provider(Settings())
    assert not any("ghs_" in r.message for r in caplog.records)


def test_provider_static_ghs_token_warns(clean_env, caplog):
    """GITHUB_TOKEN が ghs_ (1時間で失効するインストールトークン) の場合は起動時に警告する(issue #187)。"""
    clean_env.setenv("GITHUB_TOKEN", "ghs_shortlived123")
    with caplog.at_level("WARNING"):
        p = build_token_provider(Settings())
    assert isinstance(p, StaticTokenProvider)
    assert any("ghs_" in r.message for r in caplog.records)
    assert any("expires" in r.message for r in caplog.records)


def test_provider_app(clean_env, rsa_pem):
    clean_env.setenv("GITHUB_APP_ID", "123")
    clean_env.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    clean_env.setenv("GITHUB_APP_PRIVATE_KEY", rsa_pem)
    p = build_token_provider(Settings())
    assert isinstance(p, AppTokenProvider)


def test_provider_app_preferred_over_pat(clean_env, rsa_pem):
    clean_env.setenv("GITHUB_TOKEN", "ghp_xxx")
    clean_env.setenv("GITHUB_APP_ID", "123")
    clean_env.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    clean_env.setenv("GITHUB_APP_PRIVATE_KEY", rsa_pem)
    p = build_token_provider(Settings())
    assert isinstance(p, AppTokenProvider)


def test_provider_app_incomplete_raises(clean_env):
    clean_env.setenv("GITHUB_APP_ID", "123")  # missing installation/key
    with pytest.raises(ValueError):
        build_token_provider(Settings())


def test_app_jwt_claims(rsa_pem):
    key = serialization.load_pem_private_key(rsa_pem.encode(), password=None)
    prov = AppTokenProvider("123456", rsa_pem, "789")
    token = prov._app_jwt()
    decoded = jwt.decode(token, key.public_key(), algorithms=["RS256"])
    now = int(time.time())
    assert decoded["iss"] == "123456"
    assert abs(decoded["iat"] - (now - 60)) < 5
    assert abs(decoded["exp"] - (now + 540)) < 5


def _mock_post(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def fake_post(url, **kw):
        allowed = {k: v for k, v in kw.items() if k in ("headers", "timeout", "json")}
        with httpx.Client(transport=transport) as c:
            return c.post(url, **allowed)

    monkeypatch.setattr(github_auth.httpx, "post", fake_post)


def test_refresh_and_cache(monkeypatch, rsa_pem):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path == "/app/installations/789/access_tokens"
        assert request.headers["Authorization"].startswith("Bearer ")
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        return httpx.Response(201, json={"token": "ghs_tok", "expires_at": exp})

    _mock_post(monkeypatch, handler)
    prov = AppTokenProvider("123456", rsa_pem, "789")

    assert prov.get_token() == "ghs_tok"
    assert calls["n"] == 1
    # do not re-issue while cache is valid
    assert prov.get_token() == "ghs_tok"
    assert calls["n"] == 1
    # re-issue once past 5 minutes before expiry
    prov._expires_at = time.time() + 100
    prov.get_token()
    assert calls["n"] == 2


@pytest.mark.parametrize(
    "code,fragment",
    [(401, "JWT was rejected"), (404, "Installation not found"), (403, "Insufficient permissions")],
)
def test_refresh_error_messages(monkeypatch, rsa_pem, code, fragment):
    _mock_post(monkeypatch, lambda req: httpx.Response(code, json={"message": "x"}))
    prov = AppTokenProvider("1", rsa_pem, "2")
    with pytest.raises(RuntimeError) as ei:
        prov.get_token()
    assert fragment in str(ei.value)



def test_provider_token_command(clean_env):
    clean_env.setenv("GITHUB_TOKEN_COMMAND", "echo ghs_token123")
    p = build_token_provider(Settings())
    assert isinstance(p, TokenCommandProvider)
    assert p.get_token() == "ghs_token123"


def test_provider_token_command_preferred_over_pat(clean_env):
    clean_env.setenv("GITHUB_TOKEN_COMMAND", "echo ghs_token456")
    clean_env.setenv("GITHUB_TOKEN", "ghp_xxx")
    p = build_token_provider(Settings())
    assert isinstance(p, TokenCommandProvider)
    assert p.get_token() == "ghs_token456"


def test_provider_command_after_app(clean_env, rsa_pem):
    clean_env.setenv("GITHUB_APP_ID", "123")
    clean_env.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    clean_env.setenv("GITHUB_APP_PRIVATE_KEY", rsa_pem)
    clean_env.setenv("GITHUB_TOKEN_COMMAND", "echo ghs_token789")
    p = build_token_provider(Settings())
    assert isinstance(p, AppTokenProvider)


def test_token_command_provider_cache(clean_env):
    p = TokenCommandProvider("echo ghs_cached")
    assert p.get_token() == "ghs_cached"
    # second call should use cache (not re-run command)
    assert p.get_token() == "ghs_cached"


def test_token_command_provider_empty_output(clean_env):
    p = TokenCommandProvider("echo")
    with pytest.raises(RuntimeError):
        p.get_token()


def test_token_command_provider_fallback(clean_env, monkeypatch):
    from unittest.mock import MagicMock
    import shiori.github_auth as ga
    p = TokenCommandProvider("nonexistent_cmd_xyz")
    # prime cache
    p._token = "ghs_old"
    p._fetched_at = time.time()
    # make the actual subprocess fail
    mock_run = MagicMock(side_effect=FileNotFoundError("no such command"))
    monkeypatch.setattr(ga.subprocess, "run", mock_run)
    # fallback to cached token
    assert p.get_token() == "ghs_old"


#
# McpTokenProvider tests
#

def test_mcp_provider_returns_none_when_no_binary(clean_env, tmp_path):
    p = McpTokenProvider(str(tmp_path))
    assert p.get_token() is None


def test_mcp_provider_resolve_env_exe(clean_env, tmp_path, monkeypatch):
    fake_bin = tmp_path / "mcp-token"
    fake_bin.write_text("#!/bin/sh\necho ghs_fake")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("MCP_TOKEN_EXE", str(fake_bin))
    p = McpTokenProvider(str(tmp_path))
    assert p._resolve_binary() == str(fake_bin)


def test_mcp_provider_resolve_path(clean_env, tmp_path, monkeypatch):
    """When MCP_TOKEN_EXE is not set and mcp-token is on PATH, use PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_bin = bindir / "mcp-token"
    fake_bin.write_text("#!/bin/sh\necho ghs_path")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir), prepend=os.pathsep)
    p = McpTokenProvider(str(tmp_path))
    resolved = p._resolve_binary()
    assert resolved == str(fake_bin)


def test_mcp_provider_resolve_cache(clean_env, tmp_path):
    cached = tmp_path / "mcp-token"
    cached.write_text("#!/bin/sh\necho ghs_cached")
    cached.chmod(0o755)
    p = McpTokenProvider(str(tmp_path))
    resolved = p._resolve_binary()
    assert resolved == str(cached)


def test_mcp_provider_refresh_calls_binary(clean_env, tmp_path, monkeypatch):
    fake_bin = tmp_path / "mcp-token"
    fake_bin.write_text("#!/bin/sh\necho ghs_token_from_binary")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)
    p = McpTokenProvider(str(tmp_path))
    token = p.get_token()
    assert token == "ghs_token_from_binary"


def test_mcp_provider_cache_expiry(clean_env, tmp_path, monkeypatch):
    call_count = [0]

    class TrackingProvider(McpTokenProvider):
        def _refresh(self) -> None:
            call_count[0] += 1
            self._token = "ghs_tok"
            self._fetched_at = time.time()

    p = TrackingProvider(str(tmp_path))
    assert p.get_token() == "ghs_tok"
    assert call_count[0] == 1
    # cached
    assert p.get_token() == "ghs_tok"
    assert call_count[0] == 1
    # force expiry
    p._fetched_at = 0.0
    assert p.get_token() == "ghs_tok"
    assert call_count[0] == 2


def test_mcp_provider_download_fallback(clean_env, tmp_path, monkeypatch):
    p = McpTokenProvider(str(tmp_path))
    # mock _download to fail
    monkeypatch.setattr(p, "_download", MagicMock(side_effect=RuntimeError("network error")))
    resolved = p._resolve_binary()
    assert resolved is None
    # get_token returns None after resolution failure
    assert p.get_token() is None


def test_mcp_provider_refresh_failure_reuses_cached(clean_env, tmp_path, monkeypatch):
    fake_bin = tmp_path / "mcp-token"
    fake_bin.write_text("#!/bin/sh\necho ghs_initial")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)
    p = McpTokenProvider(str(tmp_path))
    assert p.get_token() == "ghs_initial"
    # make binary fail next time
    p._fetched_at = 0.0
    fake_bin.write_text("#!/bin/sh\nexit 1")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(p, "_binary", str(fake_bin))  # keep resolved
    # should reuse cached token within HARD_EXPIRY
    assert p.get_token() == "ghs_initial"


def test_mcp_provider_detect_arch(monkeypatch):
    from shiori.github_auth import _detect_arch

    def fake_uname(machine):
        class Fake:
            def __init__(self, m):
                self.machine = m
        return Fake(machine)

    monkeypatch.setattr(os, "uname", lambda: fake_uname("x86_64"))
    assert _detect_arch() == "amd64"

    monkeypatch.setattr(os, "uname", lambda: fake_uname("aarch64"))
    assert _detect_arch() == "arm64"

    monkeypatch.setattr(os, "uname", lambda: fake_uname("arm64"))
    assert _detect_arch() == "arm64"


#
# Provider name attribute (issue #188): shiori_status reports this via
# build_token_provider(settings).name so it can surface the *actual* provider
# selected, not just what the raw config implies.
#


def test_provider_names(clean_env, rsa_pem):
    assert AnonymousProvider().name == "anonymous"
    assert StaticTokenProvider("ghp_xxx").name == "static"
    assert TokenCommandProvider("echo x").name == "token_command"
    assert AppTokenProvider("1", rsa_pem, "2").name == "app"
    assert McpTokenProvider("/tmp").name == "mcp_token"


def test_build_token_provider_name_matches_selected_class(clean_env):
    """build_token_provider's return value exposes .name matching the class actually
    selected (mcp_token here, since no App/TokenCommand/PAT env is set)."""
    p = build_token_provider(Settings())
    assert isinstance(p, McpTokenProvider)
    assert p.name == "mcp_token"


#
# McpTokenProvider silent fallback-to-anonymous tracking (issue #188)
#
# McpTokenProvider is meant for native execution where the host process can
# reach the OS keystore mcp-token reads from. Inside a container the binary
# still resolves but minting fails, and get_token() silently returns None
# (anonymous) with only a warning log line. shiori_status can't inspect the
# McpTokenProvider instance responsible (a fresh, short-lived one is built per
# call), so the outcome is tracked at module level instead.
#


def test_no_fallback_reason_before_any_use(clean_env, monkeypatch):
    """Module state starts clean (no McpTokenProvider has run yet in this test)."""
    monkeypatch.setattr(github_auth, "_mcp_token_last_fallback_reason", None)
    assert github_auth.get_mcp_token_fallback_reason() is None


def test_fallback_reason_recorded_when_binary_unresolved(clean_env, tmp_path, monkeypatch):
    """get_token() returning None (no binary found) is recorded at module level."""
    monkeypatch.setattr(github_auth, "_mcp_token_last_fallback_reason", None)
    p = McpTokenProvider(str(tmp_path))
    assert p.get_token() is None
    assert github_auth.get_mcp_token_fallback_reason() is not None


def test_fallback_reason_cleared_after_successful_token(clean_env, tmp_path, monkeypatch):
    """A later successful get_token() clears the module-level fallback state
    (issue #188 asks for the *current* effective provider, not a permanent scar)."""
    monkeypatch.setattr(github_auth, "_mcp_token_last_fallback_reason", "stale reason from before")
    fake_bin = tmp_path / "mcp-token"
    fake_bin.write_text("#!/bin/sh\necho ghs_recovered")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)
    p = McpTokenProvider(str(tmp_path))
    assert p.get_token() == "ghs_recovered"
    assert github_auth.get_mcp_token_fallback_reason() is None


def test_fallback_reason_survives_across_separate_provider_instances(clean_env, tmp_path, monkeypatch):
    """The whole point of module-level (not instance) state: a *different*
    McpTokenProvider instance built later (as shiori_status does on every call)
    still sees a fallback observed by an earlier, now-discarded instance."""
    monkeypatch.setattr(github_auth, "_mcp_token_last_fallback_reason", None)
    p1 = McpTokenProvider(str(tmp_path))
    assert p1.get_token() is None  # no binary -> falls back, recorded at module level
    del p1
    assert github_auth.get_mcp_token_fallback_reason() is not None
