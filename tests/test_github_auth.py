"""Unit tests for github_auth (detailed design/09).

Token supply is pull-based: a host-side minter issues short-lived tokens and the
provider pulls them via a mint socket, a host command, or a static PAT. The
in-container GitHub App provider was retired (#243 under EPIC #237), so there is
no JWT/installation-token machinery to exercise here anymore.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from shiori import github_auth
from shiori.config import Settings
from shiori.github_auth import (
    AnonymousProvider,
    StaticTokenProvider,
    TokenCommandProvider,
    TokenSocketProvider,
    build_token_provider,
)

_ENV_KEYS = [
    "GITHUB_TOKEN",
    "GITHUB_TOKEN_COMMAND",
    "GITHUB_TOKEN_SOCKET",
]


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
# Bounded retry for transient mint-socket failures (issue #413)
#


def _patch_sleep(monkeypatch):
    """Intercept time.sleep so the suite never actually sleeps; returns a
    recorder list of the sleep arguments."""
    sleeps = []

    def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(github_auth.time, "sleep", _sleep)
    return sleeps


def test_token_socket_retry_transient_oserror(clean_env, monkeypatch):
    """AC1: attempt 1 raises OSError, attempt 2 succeeds -> token returned,
    even with no cached token."""
    attempts = {"n": 0}
    sleeps = _patch_sleep(monkeypatch)

    class FlakySocket:
        def __init__(self, family, sock_type):
            self._timeout = None
            self._data = b"ghs_retried\n"

        def settimeout(self, timeout):
            self._timeout = timeout

        def connect(self, path):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError(f"connection refused: {path}")

        def recv(self, bufsize):
            chunk = self._data[:bufsize]
            self._data = self._data[bufsize:]
            return chunk

        def close(self):
            pass

    monkeypatch.setattr(github_auth.socket, "socket", FlakySocket)
    p = TokenSocketProvider("/run/shiori/mint.sock")
    assert p.get_token() == "ghs_retried"
    assert attempts["n"] == 2
    assert sleeps == [0.5]


def test_token_socket_retry_transient_empty_response(clean_env, monkeypatch):
    """AC2: attempt 1 returns 0 bytes, attempt 2 returns a token -> success."""
    attempts = {"n": 0}
    sleeps = _patch_sleep(monkeypatch)

    class FlakySocket:
        def __init__(self, family, sock_type):
            self._timeout = None
            attempts["n"] += 1
            self._data = b"" if attempts["n"] == 1 else b"ghs_second\n"

        def settimeout(self, timeout):
            self._timeout = timeout

        def connect(self, path):
            pass

        def recv(self, bufsize):
            chunk = self._data[:bufsize]
            self._data = self._data[bufsize:]
            return chunk

        def close(self):
            pass

    monkeypatch.setattr(github_auth.socket, "socket", FlakySocket)
    p = TokenSocketProvider("/run/shiori/mint.sock")
    assert p.get_token() == "ghs_second"
    assert attempts["n"] == 2
    assert sleeps == [0.5]


def test_token_socket_retry_exhausted_raises_without_cache(clean_env, monkeypatch, caplog):
    """AC3a/AC4: all 3 attempts fail with no cached token -> RuntimeError with
    the existing message; exactly 3 attempts; sleeps are 0.5 then 1.0 and none
    after the last attempt; each failed attempt logs a warning carrying the
    attempt number."""
    attempts = {"n": 0}
    sleeps = _patch_sleep(monkeypatch)

    class AlwaysFailSocket:
        def __init__(self, family, sock_type):
            self._timeout = None

        def settimeout(self, timeout):
            self._timeout = timeout

        def connect(self, path):
            attempts["n"] += 1
            raise OSError(f"connection refused: {path}")

        def recv(self, bufsize):
            raise OSError("read error")

        def close(self):
            pass

    monkeypatch.setattr(github_auth.socket, "socket", AlwaysFailSocket)
    p = TokenSocketProvider("/run/shiori/mint.sock")
    with caplog.at_level("WARNING"), pytest.raises(RuntimeError) as exc_info:
        p.get_token()
    assert str(exc_info.value) == (
        "token socket /run/shiori/mint.sock failed and no cached token available"
    )
    assert attempts["n"] == 3
    assert sleeps == [0.5, 1.0]
    attempts_logged = [r.message for r in caplog.records if "attempt" in r.message]
    assert len(attempts_logged) == 3
    assert any("attempt 1/3" in m for m in attempts_logged)
    assert any("attempt 2/3" in m for m in attempts_logged)
    assert any("attempt 3/3" in m for m in attempts_logged)


def test_token_socket_retry_exhausted_falls_back_to_cache(clean_env, monkeypatch, caplog):
    """AC3b: all 3 attempts fail but a cached token is inside HARD_EXPIRY ->
    the cached token is returned."""
    attempts = {"n": 0}
    sleeps = _patch_sleep(monkeypatch)

    class AlwaysFailSocket:
        def __init__(self, family, sock_type):
            self._timeout = None

        def settimeout(self, timeout):
            self._timeout = timeout

        def connect(self, path):
            attempts["n"] += 1
            raise OSError(f"connection refused: {path}")

        def recv(self, bufsize):
            raise OSError("read error")

        def close(self):
            pass

    monkeypatch.setattr(github_auth.socket, "socket", AlwaysFailSocket)
    p = TokenSocketProvider("/run/shiori/mint.sock")
    # prime cache: stale enough that get_token() re-fetches, but still inside
    # HARD_EXPIRY so the fallback can reuse it
    p._token = "ghs_old"
    p._fetched_at = time.time() - (TokenSocketProvider.HARD_EXPIRY - 60)
    with caplog.at_level("WARNING"):
        assert p.get_token() == "ghs_old"
    assert attempts["n"] == 3
    assert sleeps == [0.5, 1.0]
    assert any("reusing cached token" in r.message for r in caplog.records)


def test_token_socket_retry_real_unix_socket(clean_env, tmp_path):
    """AC5: a real AF_UNIX socket bound in a tmp dir with a tiny in-test server
    thread; the first connection is closed with no data, the second serves a
    token -> success through the real connect/read path (no mocked socket)."""
    import socket as _socket
    import threading

    sock_path = str(tmp_path / "mint.sock")
    server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    connections = {"n": 0}

    def serve():
        for _ in range(2):
            conn, _ = server.accept()
            connections["n"] += 1
            if connections["n"] == 1:
                # empty response: the mint-side keyring race (mcp-launcher #45/#46)
                conn.close()
            else:
                conn.sendall(b"ghs_real_socket\n")
                conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        p = TokenSocketProvider(sock_path)
        assert p.get_token() == "ghs_real_socket"
        assert connections["n"] == 2
    finally:
        server.close()
        thread.join(timeout=5)


#
# Provider name attribute (issue #188): shiori_status reports this via
# build_token_provider(settings).name so it can surface the *actual* provider
# selected, not just what the raw config implies.
#


def test_provider_names(clean_env):
    assert AnonymousProvider().name == "anonymous"
    assert StaticTokenProvider("ghp_xxx").name == "static"
    assert TokenCommandProvider("echo x").name == "token_command"
    assert TokenSocketProvider("/sock").name == "token_socket"


def test_build_token_provider_name_matches_selected_class(clean_env):
    """build_token_provider's return value exposes .name matching the class actually
    selected (anonymous here, since no TokenSocket/TokenCommand/PAT env is set)."""
    p = build_token_provider(Settings())
    assert isinstance(p, AnonymousProvider)
    assert p.name == "anonymous"
