"""Compose image and build arg contract: app and ingest services must share
the same image expression and pass TORCH_INDEX_URL build arg.

Static check over docker-compose.yml, following the parser style of
test_compose_env_forwarding.py (no PyYAML -- not a declared dependency).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"

EXPECTED_IMAGE = "${SHIORI_APP_IMAGE:-ghcr.io/masuda-masuo/shiori/app}"
EXPECTED_TORCH_INDEX_ARG = "${SHIORI_TORCH_INDEX_URL:-}"


def _parse_compose_image_and_build(text: str) -> dict[str, dict]:
    """Parse docker-compose.yml extracting image and build args per service.

    Line-based parser (no PyYAML). Understands:
    - 0-space top-level sections (services:)
    - 2-space service names (app:, ingest:, db:)
    - 4-space service properties (image:, build:)
    - 6-space build properties (args:, context:, dockerfile:)
    - 8-space build arg entries (mapping or list form)
    Fails loudly on any unparseable line within monitored sections.
    """
    services: dict[str, dict] = {}
    service: str | None = None
    section: str | None = None
    sub_section: str | None = None
    top_level: str | None = None

    for line_no, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())

        if indent == 0:
            if stripped.endswith(":"):
                top_level = stripped[:-1]
            else:
                top_level = stripped
            service = None
            section = None
            sub_section = None
            continue

        if top_level != "services":
            continue

        if indent == 2:
            if stripped.endswith(":") and not stripped.startswith("-"):
                service = stripped[:-1]
                services[service] = {"image": None, "build_args": {}}
                section = None
                sub_section = None
                continue
            raise AssertionError(
                f"unparseable service line {line_no}: {stripped!r}"
            )

        if service is None:
            continue

        if indent == 4:
            sub_section = None
            if stripped.startswith("image:"):
                _, _, val = stripped.partition(":")
                services[service]["image"] = val.strip()
                section = None
                continue
            if stripped == "build:":
                section = "build"
                continue
            if stripped.endswith(":") and not stripped.startswith("-"):
                section = stripped[:-1]
                continue
            section = None
            continue

        if indent == 6:
            if section == "build":
                if stripped == "args:":
                    sub_section = "args"
                    continue
                if stripped.endswith(":") and not stripped.startswith("-"):
                    sub_section = stripped[:-1]
                    continue
                if ":" in stripped:
                    # e.g. context: ., dockerfile: docker/app/Dockerfile
                    continue
                raise AssertionError(
                    f"unparseable build section line {line_no}: {stripped!r}"
                )
            continue

        if indent >= 8:
            if section == "build" and sub_section == "args":
                if stripped.startswith("- "):
                    item = stripped[2:].strip()
                    if "=" not in item:
                        raise AssertionError(
                            f"unparseable build arg list entry line {line_no}: {stripped!r}"
                        )
                    name, _, value = item.partition("=")
                elif ":" in stripped:
                    name, _, value = stripped.partition(":")
                else:
                    raise AssertionError(
                        f"unparseable build arg entry line {line_no}: {stripped!r}"
                    )
                services[service]["build_args"][name.strip()] = value.strip()
            continue

    return services


class TestComposeImageContract:
    def test_app_and_ingest_share_identical_image_expression(self):
        """app and ingest services must have identical image expressions."""
        services = _parse_compose_image_and_build(COMPOSE.read_text(encoding="utf-8"))
        assert "app" in services, "app service missing from docker-compose.yml"
        assert "ingest" in services, "ingest service missing from docker-compose.yml"

        app_image = services["app"]["image"]
        ingest_image = services["ingest"]["image"]

        assert app_image is not None, "app service missing image property"
        assert ingest_image is not None, "ingest service missing image property"
        assert (
            app_image == ingest_image == EXPECTED_IMAGE
        ), f"app ({app_image!r}) and ingest ({ingest_image!r}) must match {EXPECTED_IMAGE!r}"

    def test_app_and_ingest_pass_torch_index_url_build_arg(self):
        """both app and ingest build blocks must pass TORCH_INDEX_URL: ${SHIORI_TORCH_INDEX_URL:-}."""
        services = _parse_compose_image_and_build(COMPOSE.read_text(encoding="utf-8"))
        for service_name in ("app", "ingest"):
            assert service_name in services, f"{service_name} missing from docker-compose.yml"
            build_args = services[service_name]["build_args"]
            assert "TORCH_INDEX_URL" in build_args, (
                f"{service_name} build block missing TORCH_INDEX_URL build arg"
            )
            assert build_args["TORCH_INDEX_URL"] == EXPECTED_TORCH_INDEX_ARG, (
                f"{service_name} build block TORCH_INDEX_URL arg must be {EXPECTED_TORCH_INDEX_ARG!r}, "
                f"got {build_args['TORCH_INDEX_URL']!r}"
            )
