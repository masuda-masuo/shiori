"""Deployment env parity: settings documented in .env.example that are read
by code must be forwardable to the compose service whose code reads them.

Regression guard for #372 (SHIORI_LOG_LEVEL) and #376
(SHIORI_BULK_PENDING_THRESHOLD): both shipped documented-and-read but were
never listed in a service ``environment:`` block, so in a deployed
``docker compose`` run they silently kept their built-in defaults forever.
Compose's ``.env`` file only performs ``${VAR}`` interpolation -- it never
injects anything into containers.

Static check over the three artifacts that already live in the repo
(``.env.example``, ``docker-compose.yml`` / ``docker-compose.gpu.yml``, and
the source reads), so it runs without docker compose or a database. No
PyYAML dependency (not declared in pyproject.toml): the compose files are
parsed with a conservative line-based parser that understands exactly the
layout these files use and fails loudly on anything unparseable.

ONNX trap (issue #353): ``SHIORI_ONNX_MODEL_PATH`` must NOT be forwarded via
``${SHIORI_ONNX_MODEL_PATH:-}`` -- the empty string is an explicit
off-switch in ``embedding._resolve_onnx_path()``, so default-to-empty would
hard-disable ONNX in every run. The only delivery form that keeps the
three-way contract (unset -> default candidates, path -> that path,
GPU overlay ``""`` -> off) is ``env_file: .env``: a key absent from the
deployer's ``.env`` is simply not injected. ``SHIORI_FETCH_CONCURRENCY``
used to have the same problem in reverse (a bare ``int(os.environ.get(...))``
read raised on the empty string, forcing compose to carry a copy of the
default in ``${SHIORI_FETCH_CONCURRENCY:-4}``). Its read is now defensive,
so the plain ``${VAR:-}`` form is safe -- and the general-rule test below
pins that compose may only use the empty-default form for settings whose
code read is defensive.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
COMPOSE = REPO_ROOT / "docker-compose.yml"
COMPOSE_GPU = REPO_ROOT / "docker-compose.gpu.yml"
SRC_DIR = REPO_ROOT / "src"

#: Which service must be able to deliver each setting, and where the reading
#: code runs (module -> service):
#:   app    -- `python -m shiori serve` (mcp_server.py and its imports)
#:   ingest -- `python -m shiori ingest` (ingest entry points)
#: Both services run the same image, so `config.py`'s Settings is built in
#: both; the table below is the *used* value's service, not mere construction.
SERVICE_SCOPES: dict[str, set[str]] = {
    "app": {
        # config.py (_repos_from_env); used by mcp_server.py/pipeline.py/tools
        "SHIORI_REPOS",
        # config.py (_dev_repos_from_env); used by tools/status.py, sync_issues
        "SHIORI_DEV_REPOS",
        # config.py (_dev_repos_from_env backward-compat fallback)
        "SHIORI_INDEX_CODE",
        # config.py; used by tools/read.py:180 (app-side bot allowlist)
        "SHIORI_INDEX_BOT_LOGINS",
        # config.py; used by walk_utils.py via sync_code (MCP ingest path)
        "SHIORI_CODE_EXTENSIONS",
        # config.py; used by walk_utils.py via sync_code (MCP ingest path)
        "SHIORI_CODE_EXCLUDE_GLOBS",
        # __main__.py: log_level_from_env() -- serve entry runs main()
        "SHIORI_LOG_LEVEL",
        # config.py; mcp_server.py:55 gates rebuild=True on it
        "SHIORI_ALLOW_REBUILD",
        # config.py; pipeline.py:139 _ensure_phase1 debounce, tools/status.py
        "SHIORI_SYNC_INTERVAL_SECONDS",
        # embedding.py: _resolve_onnx_path -- Embedder built by
        # pipeline._get_embedder on the MCP ingest path
        "SHIORI_ONNX_MODEL_PATH",
    },
    "ingest": {
        # config.py; used by ingest.py _validate_repos / _order_repos_dev_first
        "SHIORI_REPOS",
        # config.py; used by ingest.py _order_repos_dev_first
        "SHIORI_DEV_REPOS",
        # config.py (_dev_repos_from_env backward-compat fallback)
        "SHIORI_INDEX_CODE",
        # config.py; used by sync_utils.py (bot-comment exclusion)
        "SHIORI_INDEX_BOT_LOGINS",
        # config.py; used by walk_utils.py during code indexing
        "SHIORI_CODE_EXTENSIONS",
        # config.py; used by walk_utils.py during code indexing
        "SHIORI_CODE_EXCLUDE_GLOBS",
        # __main__.py: log_level_from_env() -- ingest entry runs main()
        "SHIORI_LOG_LEVEL",
        # config.py + ingest.py:102 -- the volume-based bulk-path trigger.
        # The MCP copy in pipeline.py deliberately does NOT read it (#376).
        "SHIORI_BULK_PENDING_THRESHOLD",
        # config.py; ingest.py run_index/run_ingest -- the index-run
        # working-time budget (issue #377). Empty/unset = unbounded, so the
        # plain ${VAR:-} form is safe here.
        "SHIORI_INGEST_TIME_BUDGET",
        # config.py; ingest.py:446,853 -- fetch-phase worker count
        "SHIORI_FETCH_CONCURRENCY",
        # config.py; ingest.py _should_skip_repo -- circuit breaker. The
        # backoff cap is chosen per lane from settings.dev_repos (#371);
        # the breaker only runs on the ingest CLI path.
        "SHIORI_CB_THRESHOLD",
        "SHIORI_CB_BASE_BACKOFF",
        "SHIORI_CB_MAX_BACKOFF",
        "SHIORI_CB_REF_MAX_BACKOFF",
        # config.py; ingest.py:338 _resolve_backfill_since (ref repos only)
        "SHIORI_REF_BACKFILL_SINCE",
        # embedding.py: _resolve_onnx_path -- Embedder used by the index phase
        "SHIORI_ONNX_MODEL_PATH",
    },
}

#: Settings whose empty-string value is meaningful, so the naive
#: ``${VAR:-}`` (expand-to-empty-when-unset) form is forbidden for them.
EMPTY_STRING_MEANINGFUL = {
    # embedding.py treats "" as an explicit off-switch distinct from unset;
    # ${VAR:-} would hard-disable ONNX in every run (#353).
    "SHIORI_ONNX_MODEL_PATH",
}


def _documented_settings() -> set[str]:
    """SHIORI_* keys documented in .env.example (commented or not)."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(re.findall(r"^\s*#?\s*(SHIORI_[A-Z0-9_]+)=", text, re.MULTILINE))


def _settings_read_by_code() -> set[str]:
    """SHIORI_* keys read via os.environ.get/os.getenv anywhere in src/.

    Also matches the defensive numeric read form of #397:
    ``_int_from_env("SHIORI_X", ...)`` / ``_float_from_env("SHIORI_X", ...)``
    take the variable *name* and perform the ``os.environ.get`` internally,
    so the literal scan alone can no longer see those reads.
    """
    found: set[str] = set()
    for path in SRC_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        found.update(
            re.findall(r'os\.(?:environ\.get|getenv)\("(SHIORI_[A-Z0-9_]+)"', text)
        )
        found.update(
            re.findall(
                r'_(?:int|float)_from_env\(\s*"(SHIORI_[A-Z0-9_]+)"', text
            )
        )
    return found


def _parse_compose(text: str) -> dict[str, dict]:
    """Parse services -> {"env": {name: value}, "env_file": [paths]}.

    Conservative line-based parser (no PyYAML -- not a declared
    dependency). Understands the layout these compose files actually use:
    2-space service names, 4-space keys, 6-space entries. An unparseable
    line inside an ``environment:``/``env_file:`` section raises, so a
    reformat can never silently weaken this test.
    """
    services: dict[str, dict] = {}
    service: str | None = None
    section: str | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 2 and stripped.endswith(":") and not stripped.startswith("-"):
            service = stripped[:-1]
            services.setdefault(service, {"env": {}, "env_file": []})
            section = None
            continue
        if service is None:
            continue
        if indent == 4 and stripped == "environment:":
            section = "environment"
            continue
        if indent == 4 and stripped == "env_file:":
            section = "env_file"
            continue
        if indent == 4 and stripped.endswith(":") and not stripped.startswith("-"):
            section = None
            continue
        if section == "environment" and indent >= 6:
            if stripped.startswith("- "):  # list form: - NAME=value
                item = stripped[2:].strip()
                if "=" not in item:
                    raise AssertionError(
                        f"unparseable environment list entry: {stripped!r}"
                    )
                name, _, value = item.partition("=")
            else:  # mapping form: NAME: value
                if ":" not in stripped:
                    raise AssertionError(
                        f"unparseable environment entry: {stripped!r}"
                    )
                name, _, value = stripped.partition(":")
            services[service]["env"][name.strip()] = value.strip()
        elif section == "env_file" and indent >= 6:
            # Short form ``- .env`` and long form ``- path: .env`` /
            # ``required: false`` (the long form is what lets a fresh clone
            # without a .env still run docker compose at all).
            if stripped.startswith("- path:"):
                services[service]["env_file"].append(
                    stripped.split(":", 1)[1].strip()
                )
            elif stripped.startswith("- "):
                services[service]["env_file"].append(stripped[2:].strip())
            elif stripped.startswith("required:"):
                pass  # attribute of the preceding long-form entry
            else:
                raise AssertionError(f"unparseable env_file entry: {stripped!r}")
    return services


def _effective_env(service_env: dict, documented: set[str]) -> set[str]:
    """Keys the container can receive: environment: block plus, when
    ``env_file: .env`` is declared, every documented key (the deployer's
    .env is a copy of .env.example per docs/guides/setup.md, and env_file
    injects keys verbatim -- absent keys are simply not injected)."""
    env = set(service_env["env"])
    if any(p == ".env" for p in service_env["env_file"]):
        env |= documented
    return env


def _compose_env(services: dict[str, dict], service: str) -> dict[str, str]:
    return services[service]["env"]


#: An env read converted directly with no defensive parse: the value can be
#: "" (compose's ``${VAR:-}`` form expands an unset host variable to the
#: empty string) and ``int("")`` / ``float("")`` raise.
_BARE_NUMERIC_ENV_READ_RE = re.compile(
    r"(?:int|float)\s*\(\s*os\.(?:environ\.get|getenv)\("
)


def _bare_numeric_env_reads() -> list[tuple[str, int, str]]:
    """(path, line, env var) for every env read wrapped directly in
    ``int()``/``float()`` anywhere in src/."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in _BARE_NUMERIC_ENV_READ_RE.finditer(text):
            rest = text[match.end() : match.end() + 200]
            name_match = re.match(r'\s*"([^"]+)"', rest)
            name = name_match.group(1) if name_match else "?"
            lineno = text.count("\n", 0, match.start()) + 1
            found.append((str(path.relative_to(REPO_ROOT)), lineno, name))
    return found


def _compose_empty_default_forwarded() -> set[str]:
    """Env vars docker-compose.yml forwards with the plain ``${VAR:-}`` form
    (an unset host variable becomes the empty string in the container)."""
    compose = _parse_compose(COMPOSE.read_text(encoding="utf-8"))
    forwarded: set[str] = set()
    for service_env in compose.values():
        for value in service_env["env"].values():
            entry = re.fullmatch(r"\$\{(?P<var>[A-Z_][A-Z0-9_]*):-\}", value)
            if entry is not None:
                forwarded.add(entry.group("var"))
    return forwarded


# ===================================================================
# Documented <-> read <-> forwarded consistency (the defect class)
# ===================================================================


class TestDocumentedSettings:
    def test_every_documented_setting_is_read_by_code(self):
        """Outcome 2: a documented setting that no code reads must not stay
        in .env.example (SHIORI_EMBED_BATCH_SIZE was removed for this)."""
        dead = _documented_settings() - _settings_read_by_code()
        assert dead == set(), (
            f"documented in .env.example but read by no code: {sorted(dead)}"
        )

    def test_scope_table_covers_exactly_the_documented_and_read_settings(self):
        """The SERVICE_SCOPES table must not drift from reality: it covers
        exactly the settings that are both documented and read."""
        expected = _documented_settings() & _settings_read_by_code()
        tabled = set(SERVICE_SCOPES["app"]) | set(SERVICE_SCOPES["ingest"])
        assert tabled == expected, (
            f"SERVICE_SCOPES drifted: missing {sorted(expected - tabled)}, "
            f"extra {sorted(tabled - expected)}"
        )


class TestForwarding:
    def test_documented_and_read_settings_reach_the_reading_service(self):
        """Core: every setting documented in .env.example and read by code
        is deliverable to every service whose code reads it. This fails for
        the #372/#376 class: a documented+read setting listed in no
        environment: block and with no env_file: .env coverage."""
        compose = _parse_compose(COMPOSE.read_text(encoding="utf-8"))
        documented = _documented_settings()
        missing: list[str] = []
        for service, scopes in SERVICE_SCOPES.items():
            effective = _effective_env(compose[service], documented)
            for name in sorted(scopes - effective):
                missing.append(f"{name} -> {service}")
        assert missing == [], (
            "settings documented in .env.example and read by code are not "
            "forwarded to the service that reads them: "
            + ", ".join(missing)
        )

    def test_scoped_settings_are_listed_in_the_environment_block(self):
        """Issue #396: ``env_file: .env`` must not stand in for an
        ``environment:`` entry.

        The two are different delivery paths and only one of them carries a
        value from the *host process* environment:

        - ``env_file: .env`` injects the keys a deployer happens to have
          written into their .env file.
        - ``environment: FOO: ${FOO:-}`` forwards whatever the process that
          runs compose has in its environment -- a systemd unit's
          ``Environment=``, a one-off ``SHIORI_GPU=1 ./scripts/ingest.sh``,
          a CI export.

        ``_effective_env()`` deliberately treats every documented setting as
        reachable once a service declares ``env_file: .env`` (that is how
        SHIORI_ONNX_MODEL_PATH is delivered, issue #353).  The side effect is
        that deleting an ``environment:`` line leaves every other assertion
        in this file green while the host-process path silently dies -- the
        #372/#376 defect class, reached through the guard rather than around
        it.  Measured: removing SHIORI_CB_REF_MAX_BACKOFF from compose left
        the whole suite passing.

        So require the entry explicitly.  The only exemption is the class of
        settings for which the ``${VAR:-}`` form is itself the bug.
        """
        compose = _parse_compose(COMPOSE.read_text(encoding="utf-8"))
        missing: list[str] = []
        for service, scopes in SERVICE_SCOPES.items():
            listed = set(compose[service]["env"])
            for name in sorted(scopes - EMPTY_STRING_MEANINGFUL - listed):
                missing.append(f"{name} -> {service}")
        assert missing == [], (
            "settings in SERVICE_SCOPES are not listed in the service's "
            "environment: block, so they cannot arrive from the host "
            "process environment (only from a deployer's .env): "
            + ", ".join(missing)
        )

    def test_empty_string_meaningful_settings_stay_out_of_environment(self):
        """The converse of the rule above, so the exemption cannot rot.

        A setting is exempt from the environment: requirement precisely
        because ``${VAR:-}`` would break it.  If one ever appears in an
        environment: block, the exemption stopped being an exemption and
        became a hole.
        """
        compose = _parse_compose(COMPOSE.read_text(encoding="utf-8"))
        offenders = [
            f"{name} -> {service}"
            for service in SERVICE_SCOPES
            for name in sorted(EMPTY_STRING_MEANINGFUL)
            if name in compose[service]["env"]
        ]
        assert offenders == [], (
            "settings whose empty string is meaningful must not be forwarded "
            "through environment: at all: " + ", ".join(offenders)
        )


# ===================================================================
# The ONNX trap (issue #353) and friends
# ===================================================================


class TestEmptyStringSemantics:
    def test_onnx_path_never_forwarded_with_default_to_empty_form(self):
        """${SHIORI_ONNX_MODEL_PATH:-} in an environment: block expands an
        unset host variable to "", which embedding._resolve_onnx_path()
        treats as an explicit off-switch -- it would hard-disable ONNX in
        every run. With env_file: .env in place, no environment: entry is
        needed (or correct) for it; delivery is covered by
        TestForwarding."""
        compose = _parse_compose(COMPOSE.read_text(encoding="utf-8"))
        for service in ("app", "ingest"):
            assert "SHIORI_ONNX_MODEL_PATH" not in _compose_env(
                compose, service
            ), (
                f"{service}: SHIORI_ONNX_MODEL_PATH must be delivered via "
                f"env_file: .env, not an environment: entry"
            )

    def test_gpu_overlay_keeps_the_explicit_onnx_off_switch(self):
        """docker-compose.gpu.yml must still set the literal "" for ingest
        (the off-switch that keeps GPU runs on SentenceTransformer/CUDA),
        and must win over env_file via environment: precedence."""
        gpu = _parse_compose(COMPOSE_GPU.read_text(encoding="utf-8"))
        raw = gpu["ingest"]["env"].get("SHIORI_ONNX_MODEL_PATH")
        assert raw is not None, (
            "gpu overlay lost the SHIORI_ONNX_MODEL_PATH off-switch"
        )
        assert raw.strip().strip('"').strip("'") == "", (
            f"gpu overlay off-switch must be the literal empty string, "
            f"got {raw!r}"
        )

    def test_no_compose_empty_default_for_bare_numeric_env_reads(self):
        """General rule, of which the old SHIORI_FETCH_CONCURRENCY pin tests
        were a special case: compose may only use the plain ``${VAR:-}``
        form for settings whose code read is defensive.

        The empty-default form expands an unset host variable to "", and a
        bare ``int(os.environ.get(...))`` / ``float(os.environ.get(...))``
        read raises on that -- crashing Settings construction on every host
        that does not set the variable. No setting in src/ may read an
        environment variable in a way that raises on the empty string.
        """
        bare = _bare_numeric_env_reads()
        empty_default_forwarded = _compose_empty_default_forwarded()
        offenders = [
            (path, lineno, name)
            for path, lineno, name in bare
            if name in empty_default_forwarded
        ]
        assert offenders == [], (
            "settings read with a bare int()/float() wrap are forwarded by "
            "docker-compose.yml with the empty-default ${VAR:-} form (unset "
            "host variable -> '' in the container -> the conversion "
            "raises). Make the code read defensive instead:\n"
            + "\n".join(f"{path}:{lineno}: {name}" for path, lineno, name in offenders)
        )

    def test_single_source_defaults_are_not_duplicated_in_compose(self):
        """A literal default in compose makes config.py's default dead.

        compose sets the variable on *every* deployed run, so a non-empty
        ``${VAR:-<value>}`` entry means the constant in config.py never
        applies -- changing it there silently does nothing. That is the
        defect #386 removed for SHIORI_FETCH_CONCURRENCY; this keeps it
        from drifting back now that the code read is defensive.
        """
        single_source = {
            "SHIORI_FETCH_CONCURRENCY",
            # Circuit-breaker settings: their reads became defensive with
            # the per-lane cap change (#371), so compose may carry only the
            # plain ${VAR:-} form and the defaults live in config.py only.
            "SHIORI_CB_THRESHOLD",
            "SHIORI_CB_BASE_BACKOFF",
            "SHIORI_CB_MAX_BACKOFF",
            "SHIORI_CB_REF_MAX_BACKOFF",
            # Read became defensive in #397; the old ${VAR:-0} compose
            # entry (the last known gap of this rule) is gone and the
            # default (0) lives in config.py only.
            "SHIORI_SYNC_INTERVAL_SECONDS",
        }
        # Settings knowingly still carrying a literal default in compose.
        # Empty since #397 (SHIORI_SYNC_INTERVAL_SECONDS was the last);
        # the mechanism stays so a future gap is listed, not silently
        # exempted.
        known_gaps: set[str] = set()
        assert not (single_source & known_gaps)

        compose = _parse_compose(COMPOSE.read_text(encoding="utf-8"))
        for service in ("app", "ingest"):
            env = _compose_env(compose, service)
            for name in sorted(single_source):
                value = env.get(name)
                if value is None:
                    continue
                assert value == "${" + name + ":-}", (
                    f"{service}: {name} must be forwarded as the plain "
                    f"${{{name}:-}} form so its default lives only in "
                    f"config.py, got {value!r}"
                )


def test_no_bare_numeric_env_reads_anywhere_in_src():
    """No env read in src/ may be wrapped directly in int()/float() (#397).

    Stronger than the compose-conjunction rule above: since #397 converted
    the last ten bare reads, every numeric setting parses defensively, and
    any new bare read would re-arm the ``int("")`` crash the moment someone
    forwards the variable with the ``${VAR:-}`` form. Keep the invariant
    absolute instead of waiting for the conjunction.
    """
    assert _bare_numeric_env_reads() == []


def test_env_file_is_the_onnx_delivery_mechanism():
    """Sanity: both services declare env_file: .env (the only mechanism
    that can deliver SHIORI_ONNX_MODEL_PATH without breaking its empty
    string semantics)."""
    compose = _parse_compose(COMPOSE.read_text(encoding="utf-8"))
    for service in ("app", "ingest"):
        assert ".env" in compose[service]["env_file"], (
            f"{service} must declare env_file: .env (ONNX delivery + "
            f"structural catch-all for documented settings)"
        )
