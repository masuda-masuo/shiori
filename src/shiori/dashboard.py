import os

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles


def register_dashboard(mcp):
    from .config import load_settings
    settings = load_settings()
    dashboard_dist = os.path.join(os.path.dirname(__file__), "dashboard_dist")

    @mcp.custom_route("/api/repos", methods=["GET"])
    async def api_repos(request: Request):
        return JSONResponse({"repos": settings.repos})

    @mcp.custom_route("/api/search", methods=["GET"])
    async def api_search(request: Request):
        from starlette.concurrency import run_in_threadpool

        from . import search
        from .mcp_server import _conn, _get_embedder, _resolve_repo_filter, settings

        query = request.query_params.get("query")
        if not query:
            return JSONResponse({"detail": "query is required"}, status_code=400)

        search_type = request.query_params.get("type", "semantic")
        source_type = request.query_params.get("source_type")
        repo = request.query_params.get("repo")
        path_prefix = request.query_params.get("path_prefix")
        prog_lang = request.query_params.get("prog_lang")
        kind = request.query_params.get("kind")

        limit_val = request.query_params.get("limit")
        limit = int(limit_val) if limit_val else None

        try:
            resolved_repo = _resolve_repo_filter(repo) if repo else None
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)

        filters = {
            "source_type": source_type if source_type else None,
            "repo": resolved_repo,
            "path_prefix": path_prefix if path_prefix else None,
            "prog_lang": prog_lang if prog_lang else None,
            "kind": kind if kind else None,
        }

        try:
            if search_type == "keyword":
                def run():
                    with _conn() as conn:
                        return search.keyword_search(
                            settings, conn, query, filters=filters, top_k=limit
                        )
            else:
                def run():
                    with _conn() as conn:
                        return search.semantic_search(
                            settings, conn, _get_embedder(), query, filters=filters, top_k=limit
                        )

            results = await run_in_threadpool(run)
            return JSONResponse({"results": results})
        except Exception as e:  # noqa: BLE001 - API boundary: report as HTTP 500
            return JSONResponse({"detail": str(e)}, status_code=500)

    @mcp.custom_route("/api/read_file", methods=["GET"])
    async def api_read_file(request: Request):
        from starlette.concurrency import run_in_threadpool

        from .mcp_server import read_file

        path = request.query_params.get("path")
        repo = request.query_params.get("repo")
        start_line_val = request.query_params.get("start_line")
        end_line_val = request.query_params.get("end_line")

        if not path:
            return JSONResponse({"detail": "path is required"}, status_code=400)

        start_line = int(start_line_val) if start_line_val else None
        end_line = int(end_line_val) if end_line_val else None

        try:
            result = await run_in_threadpool(
                read_file,
                path=path,
                start_line=start_line,
                end_line=end_line,
                repo=repo,
            )
            return JSONResponse(result)
        except Exception as e:  # noqa: BLE001 - API boundary: report as HTTP 500
            return JSONResponse({"detail": str(e)}, status_code=500)

    @mcp.custom_route("/api/issue", methods=["GET"])
    async def api_issue(request: Request):
        from starlette.concurrency import run_in_threadpool

        from .mcp_server import read_issue

        number_val = request.query_params.get("number")
        repo = request.query_params.get("repo")
        exclude_noise_bots_val = request.query_params.get("exclude_noise_bots")
        exclude_noise_bots = exclude_noise_bots_val.lower() == "true" if exclude_noise_bots_val else False

        if not number_val:
            return JSONResponse({"detail": "number is required"}, status_code=400)

        number = int(number_val)

        try:
            result = await run_in_threadpool(
                read_issue,
                number=number,
                repo=repo,
                exclude_noise_bots=exclude_noise_bots,
            )
            return JSONResponse(result)
        except Exception as e:  # noqa: BLE001 - API boundary: report as HTTP 500
            return JSONResponse({"detail": str(e)}, status_code=500)

    @mcp.custom_route("/api/report", methods=["GET"])
    async def api_report(request: Request):
        from starlette.concurrency import run_in_threadpool

        from .mcp_server import report

        template = request.query_params.get("template")
        repo = request.query_params.get("repo")
        path = request.query_params.get("path")
        kind = request.query_params.get("kind")

        public_only_val = request.query_params.get("public_only")
        public_only = public_only_val.lower() == "true" if public_only_val else True

        max_results_val = request.query_params.get("max_results")
        max_results = int(max_results_val) if max_results_val else 500

        prog_lang = request.query_params.get("prog_lang")

        max_chars_val = request.query_params.get("max_chars")
        max_chars = int(max_chars_val) if max_chars_val else 50000

        if not template:
            return JSONResponse({"detail": "template is required"}, status_code=400)

        try:
            result = await run_in_threadpool(
                report,
                template=template,
                repo=repo,
                path=path,
                kind=kind,
                public_only=public_only,
                max_results=max_results,
                prog_lang=prog_lang,
                max_chars=max_chars,
            )
            return JSONResponse(result)
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001 - API boundary: report as HTTP 500
            return JSONResponse({"detail": str(e)}, status_code=500)

    # fallback route if dashboard is not built
    async def index_fallback(request: Request):
        return HTMLResponse(
            "<h1>Dashboard not built</h1><p>Run <code>npm install && npm run build</code> in the <code>dashboard/</code> directory.</p>",
            status_code=404
        )

    if os.path.exists(dashboard_dist):
        # WORKAROUND: as of mcp 2.0.0 there is still no public API to mount a
        # Starlette sub-app / StaticFiles -- `custom_route` covers plain routes
        # only. We deliberately keep appending the Mount to the private
        # `_custom_starlette_routes` list (verified present in 2.0.0) so it is
        # included when the underlying Starlette app is built.
        mcp._custom_starlette_routes.append(
            Mount("/", app=StaticFiles(directory=dashboard_dist, html=True), name="dashboard")
        )
    else:
        # WORKAROUND: See above (custom_route covers plain routes only)
        mcp._custom_starlette_routes.append(
            Route("/", index_fallback, methods=["GET"])
        )
