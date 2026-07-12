from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .mcp_server import report

app = FastAPI(title="Shiori Dashboard API")

# Allow CORS for local development with Vite dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/report")
def get_report(
    template: str,
    repo: str | None = None,
    path: str | None = None,
    kind: str | None = None,
    public_only: bool = False,
    max_results: int = 500,
    prog_lang: str | None = None,
    max_chars: int = 50000,
):
    try:
        return report(
            template=template,
            repo=repo,
            path=path,
            kind=kind,
            public_only=public_only,
            max_results=max_results,
            prog_lang=prog_lang,
            max_chars=max_chars,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

dashboard_dist = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "dashboard", "dist")

@app.get("/")
def index():
    index_path = os.path.join(dashboard_dist, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse(
        "<h1>Dashboard not built</h1><p>Run <code>npm install && npm run build</code> in the <code>dashboard/</code> directory.</p>",
        status_code=404
    )

if os.path.exists(dashboard_dist):
    app.mount("/", StaticFiles(directory=dashboard_dist), name="dashboard")

def run(port: int = 8000) -> None:
    import uvicorn
    uvicorn.run("shiori.dashboard_api:app", host="0.0.0.0", port=port, reload=False)
