"""Unit tests for github_auth (detailed design/09).

Mocks httpx to verify installation token acquisition, caching, and re-issuance.
RS256 keys are generated inside the test.
"""

from __future__ import annotations

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
    StaticTokenProvider,
    TokenCommandProvider,
    TokenSocketProvider,
    build_token_provider,
)

_ENV_KEYS = [
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_TOKEN_COMMAND",
    "GITHUB_TOKEN_SOCKET",
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
    """No auth configured at all -> anonymous. This is the only path to
    AnonymousProvider: no provider degrades into it silently (issue #188)."""
    p = build_token_provider(Settings())
    assert isinstance(p, AnonymousProvider)
    assert p.get_token() is None


def test_provider_empty_token_command_falls_through(clean_env):
    """GITHUB_TOKEN_COMMAND が空文字のときは未設定と同義に扱う(#198 退行防止)。

    compose の `GITHUB_TOKEN_COMMAND: ${GITHUB_TOKEN_COMMAND:-}` パススルーは
    .env 未設定時にコンテナへ空文字を渡す。これが TokenCommandProvider を
    選んでしまうと、無設定環境(公開リポのみ・認証なし)で `cat` 失敗 ->
    RuntimeError で全 sync が落ちる退行になる。空文字は未設定と同義に
    扱われなければならない(config.py の `or None`)。
    """
    clean_env.setenv("GITHUB_TOKEN_COMMAND", "")
    p = build_token_provider(Settings())
    assert isinstance(p, AnonymousProvider)
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
    p = TokenCommandProvider("nonexistent_cmd_xyz")
    # prime cache
    p._token = "ghs_old"
    p._fetched_at = time.time()
    # make the actual subprocess fail
    mock_run = MagicMock(side_effect=FileNotFoundError("no such command"))
    monkeypatch.setattr(github_auth.subprocess, "run", mock_run)
    # fallback to cached token
    assert p.get_token() == "ghs_old"


#
# TokenSocketProvider tests (issue #204)
#


def _mock_socket(monkeypatch, data: bytes, fail: bool = False):
    """Monkeypatch socket.socket to return a mock that connects and receives *data*."""
    class MockSocket:
        def __init__(self, family, sock_type):
            self._timeout = None
            self._data = data
            self._fail = fail

        def settimeout(self, timeout):
            self._timeout = timeout

        def connect(self, path):
            if self._fail:
                raise OSError(f"connection refused: {path}")

        def recv(self, bufsize):
            if self._fail:
                raise OSError("read error")
            chunk = self._data[:bufsize]
            self._data = self._data[bufsize:]
            return chunk

        def close(self):
            pass

    monkeypatch.setattr(github_auth.socket, "socket", MockSocket)


def test_provider_token_socket(clean_env):
    clean_env.setenv("GITHUB_TOKEN_SOCKET", "/run/shiori/mint.sock")
    p = build_token_provider(Settings())
    assert isinstance(p, TokenSocketProvider)


def test_provider_token_socket_preferred_over_command(clean_env):
    clean_env.setenv("GITHUB_TOKEN_SOCKET", "/run/shiori/mint.sock")
    clean_env.setenv("GITHUB_TOKEN_COMMAND", "echo ghs_from_command")
    p = build_token_provider(Settings())
    assert isinstance(p, TokenSocketProvider)


def test_provider_socket_after_app(clean_env, rsa_pem):
    clean_env.setenv("GITHUB_APP_ID", "123")
    clean_env.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    clean_env.setenv("GITHUB_APP_PRIVATE_KEY", rsa_pem)
    clean_env.setenv("GITHUB_TOKEN_SOCKET", "/run/shiori/mint.sock")
    p = build_token_provider(Settings())
    assert isinstance(p, AppTokenProvider)


def test_token_socket_provider_fetch(clean_env, monkeypatch):
    _mock_socket(monkeypatch, b"ghs_socket_token\n")
    p = TokenSocketProvider("/run/shiori/mint.sock")
    assert p.get_token() == "ghs_socket_token"


def test_token_socket_provider_cache(clean_env, monkeypatch):
    calls = {"n": 0}
    recv_calls = {"n": 0}

    class CountingSocket:
        def __init__(self, family, sock_type):
            self._timeout = None
        def settimeout(self, timeout):
            self._timeout = timeout
        def connect(self, path):
            calls["n"] += 1
        def recv(self, bufsize):
            recv_calls["n"] += 1
            if recv_calls["n"] >= 2:
                return b""
            return b"ghs_cached"
        def close(self):
            pass

    monkeypatch.setattr(github_auth.socket, "socket", CountingSocket)
    p = TokenSocketProvider("/run/shiori/mint.sock")
    assert p.get_token() == "ghs_cached"
    assert calls["n"] == 1
    # second call should use cache (not re-connect)
    assert p.get_token() == "ghs_cached"
    assert calls["n"] == 1


def test_token_socket_provider_empty_response(clean_env, monkeypatch):
    _mock_socket(monkeypatch, b"")
    p = TokenSocketProvider("/run/shiori/mint.sock")
    with pytest.raises(RuntimeError):
        p.get_token()


def test_token_socket_provider_fallback(clean_env, monkeypatch):
    p = TokenSocketProvider("/run/shiori/mint.sock")
    # prime cache
    p._token = "ghs_old"
    p._fetched_at = time.time()
    # make socket connect fail
    _mock_socket(monkeypatch, b"", fail=True)
    # fallback to cached token
    assert p.get_token() == "ghs_old"


#
# Provider name attribute (issue #188): shiori_status reports this via
# build_token_provider(settings).name so it can surface the *actual* provider
# selected, not just what the raw config implies.
#


def test_provider_names(clean_env, rsa_pem):
    assert AnonymousProvider().name == "anonymous"
    assert StaticTokenProvider("ghp_xxx").name == "static"
    assert TokenCommandProvider("echo x").name == "token_command"
    assert TokenSocketProvider("/sock").name == "token_socket"
    assert AppTokenProvider("1", rsa_pem, "2").name == "app"


def test_build_token_provider_name_matches_selected_class(clean_env):
    """build_token_provider's return value exposes .name matching the class actually
    selected (anonymous here, since no App/TokenCommand/PAT env is set)."""
    p = build_token_provider(Settings())
    assert isinstance(p, AnonymousProvider)
    assert p.name == "anonymous"
