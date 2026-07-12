import os
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

def register_dashboard(mcp):
    from .config import load_settings
    settings = load_settings()
    dashboard_dist = os.path.join(os.path.dirname(__file__), "dashboard_dist")

    @mcp.custom_route("/api/repos", methods=["GET"])
    async def api_repos(request: Request):
        return JSONResponse({"repos": settings.repos})

    @mcp.custom_route("/api/report", methods=["GET"])
    async def api_report(request: Request):
        from .mcp_server import report
        from starlette.concurrency import run_in_threadpool
        
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
        except Exception as e:
            return JSONResponse({"detail": str(e)}, status_code=500)

    # fallback route if dashboard is not built
    async def index_fallback(request: Request):
        return HTMLResponse(
            "<h1>Dashboard not built</h1><p>Run <code>npm install && npm run build</code> in the <code>dashboard/</code> directory.</p>",
            status_code=404
        )

    if os.path.exists(dashboard_dist):
        # WORKAROUND: FastMCP currently lacks a public method to mount static files or custom starlette apps.
        # We append directly to the private `_custom_starlette_routes` list so the routes are included
        # when the underlying Starlette app is built.
        mcp._custom_starlette_routes.append(
            Mount("/", app=StaticFiles(directory=dashboard_dist, html=True), name="dashboard")
        )
    else:
        # WORKAROUND: See above
        mcp._custom_starlette_routes.append(
            Route("/", index_fallback, methods=["GET"])
        )