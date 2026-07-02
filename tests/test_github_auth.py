"""Unit tests for github_auth (detailed design/09).

Mocks httpx to verify installation token acquisition, caching, and re-issuance.
RS256 keys are generated inside tests.
"""

from __future__ import annotations

import time

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
    build_token_provider,
)

_ENV_KEYS = [
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY",
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
    assert isinstance(p, AnonymousProvider)
    assert p.get_token() is None


def test_provider_static(clean_env):
    clean_env.setenv("GITHUB_TOKEN", "ghp_xxx")
    p = build_token_provider(Settings())
    assert isinstance(p, StaticTokenProvider)
    assert p.get_token() == "ghp_xxx"


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
    clean_env.setenv("GITHUB_APP_ID", "123")  # installation/key 欠け
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
    # キャッシュ有効中は再発行しない
    assert prov.get_token() == "ghs_tok"
    assert calls["n"] == 1
    # expiry 5 分前を過ぎたら再発行
    prov._expires_at = time.time() + 100
    prov.get_token()
    assert calls["n"] == 2


@pytest.mark.parametrize(
    "code,fragment",
    [(401, "JWT が拒否"), (404, "Installation が見つかり"), (403, "権限不足")],
)
def test_refresh_error_messages(monkeypatch, rsa_pem, code, fragment):
    _mock_post(monkeypatch, lambda req: httpx.Response(code, json={"message": "x"}))
    prov = AppTokenProvider("1", rsa_pem, "2")
    with pytest.raises(RuntimeError) as ei:
        prov.get_token()
    assert fragment in str(ei.value)
