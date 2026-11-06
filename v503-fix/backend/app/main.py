"""
FastAPI application entry point.

Provides:
- REST API endpoints (used by the React dashboard)
- WebSocket for real-time updates
- Background orchestrator task (+ fast position monitor)
- Serves the React dashboard (frontend) as static files
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .core.config import settings
from .core.database import init_db, close_db, get_session
from .core.logger import logger, setup_logging
from .core.models import TradingState
from .core.security import APIKeyMiddleware
from .api.routes import router
from .jobs.orchestrator import MainOrchestrator


# Global orchestrator (lives across requests)
_orchestrator: MainOrchestrator | None = None
_background_tasks: list[asyncio.Task] = []


async def _ensure_trading_state() -> None:
    """Create the singleton TradingState row and auto-start if configured."""
    try:
        async with get_session() as s:
            state = await s.get(TradingState, 1)
            if state is None:
                state = TradingState(
                    id=1,
                    is_active=bool(settings.auto_start_trading),
                    started_at=datetime.utcnow() if settings.auto_start_trading else None,
                )
                s.add(state)
                logger.info(
                    f"TradingState created (is_active={settings.auto_start_trading})"
                )
            elif settings.auto_start_trading and not state.is_active:
                state.is_active = True
                state.started_at = datetime.utcnow()
                s.add(state)
                logger.info("TradingState auto-started")
    except Exception as exc:
        logger.warning(f"TradingState init failed (non-fatal): {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle - fully fault tolerant."""
    global _orchestrator

    setup_logging()
    logger.info("Memecoin AI Bot starting...")

    try:
        await init_db()
        logger.info("Database OK")
    except Exception as exc:
        logger.error(f"Database init failed: {exc}")

    await _ensure_trading_state()

    if settings.is_live_trading:
        logger.warning(
            "🔴 LIVE TRADING ENABLED — real SOL will be spent "
            f"(max {settings.pump_max_trade_sol} SOL/trade, "
            f"{settings.live_daily_cap_sol} SOL/day)"
        )
    else:
        logger.info(
            "🟡 PAPER MODE — live requirements missing: "
            + (", ".join(settings.live_requirements) or "none")
        )

    _orchestrator = None
    if not settings.is_test and settings.enable_orchestrator:
        try:
            _orchestrator = MainOrchestrator()
            await _orchestrator._init_decision_engine()
            app.state.orchestrator = _orchestrator
            logger.info("Orchestrator OK")
        except Exception as exc:
            logger.warning(f"Orchestrator init failed (non-fatal): {exc}")
            _orchestrator = None

        if _orchestrator:
            try:
                _background_tasks.append(
                    asyncio.create_task(_orchestrator.run_forever())
                )
                _background_tasks.append(
                    asyncio.create_task(_orchestrator.fast_monitor_forever(1.0))
                )
                logger.info("🚀 Orchestrator + fast monitor started")
            except Exception as exc:
                logger.warning(f"Orchestrator task failed (non-fatal): {exc}")
    elif not settings.enable_orchestrator:
        logger.info("⏸️ Orchestrator DISABLED (ENABLE_ORCHESTRATOR=false)")

    logger.info("Startup complete!")

    yield

    logger.info("Shutting down...")
    for task in _background_tasks:
        task.cancel()
    for task in _background_tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _background_tasks.clear()
    try:
        if _orchestrator:
            await _orchestrator.stop()
    except Exception:
        pass
    try:
        await close_db()
    except Exception:
        pass
    logger.info("Goodbye!")


def _inject_api_key(html: str) -> str:
    """SECURITY FIX: never embed the server API key in public HTML.

    Previously the key was written into the page for every visitor, so anyone
    who opened the URL could call trade/exit endpoints. Now the page only
    contains a tiny script that asks the operator for the key once and keeps it
    in this browser's localStorage. Open the site with #resetkey to clear it.
    """
    if not settings.api_auth_key.strip():
        return html  # auth disabled: nothing to inject
    script = (
        "<script>(function(){try{"
        "if(location.hash==='#resetkey'){localStorage.removeItem('bot_api_key');location.hash='';}"
        "var k=localStorage.getItem('bot_api_key');"
        "if(!k){k=window.prompt('API key:')||'';if(k){localStorage.setItem('bot_api_key',k.trim());}}"
        "window.__API_KEY__=(k||'').trim();"
        "}catch(e){window.__API_KEY__='';}})();</script>"
    )
    if "</head>" in html:
        return html.replace("</head>", script + "</head>", 1)
    return script + html


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title="Memecoin AI Bot",
        description="Autonomous intelligence system for memecoin analysis & trading",
        version="0.3.0",
        lifespan=lifespan,
    )

    origins = settings.cors_origins_list
    # V31.3: allow_credentials=True is only valid with explicit origins.
    # When origins == ["*"] we MUST disable credentials, otherwise
    # browsers reject the response AND any site could read it with
    # the user's cookies. This is the standard CORS security rule.
    allow_credentials = origins != ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(APIKeyMiddleware)

    app.include_router(router)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                if _orchestrator:
                    account = await _orchestrator.engine.get_account_state()
                    await websocket.send_json({
                        "type": "account_update",
                        "data": {
                            "equity": account.equity,
                            "total_pnl": account.total_pnl,
                            "open_positions": account.open_positions_count,
                            "win_rate": account.win_rate,
                            "trading_mode": settings.trading_mode,
                            "live_trading": settings.is_live_trading,
                        },
                    })
                await asyncio.sleep(5)
        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
        except Exception as exc:
            logger.debug(f"WebSocket error: {exc}")

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": "0.3.0",
            "trading_mode": settings.trading_mode,
            "live_trading": settings.is_live_trading,
        }

    # === Serve React Dashboard (frontend) ===
    frontend_paths = [
        "/app/frontend/dist",
        "../frontend/dist",
        "../../frontend/dist",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "dist"),
    ]

    frontend_dir = None
    for path in frontend_paths:
        abs_path = os.path.abspath(path)
        if os.path.isdir(abs_path):
            frontend_dir = abs_path
            break

    if frontend_dir and os.path.isdir(frontend_dir):
        logger.info(f"🎨 Serving dashboard from: {frontend_dir}")

        assets_dir = os.path.join(frontend_dir, "assets")
        if os.path.isdir(assets_dir):
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}")
        async def serve_dashboard(full_path: str):
            if (
                full_path.startswith("api/")
                or full_path.startswith("ws")
                or full_path == "health"
                or full_path.startswith("assets/")
            ):
                return JSONResponse({"detail": "Not Found"}, status_code=404)

            file_path = os.path.join(frontend_dir, full_path)
            if full_path and os.path.isfile(file_path):
                return FileResponse(file_path)

            index_path = os.path.join(frontend_dir, "index.html")
            if os.path.isfile(index_path):
                with open(index_path, "r", encoding="utf-8") as fh:
                    html = fh.read()
                return HTMLResponse(_inject_api_key(html))

            return JSONResponse({"detail": "Frontend not found"}, status_code=404)
    else:
        logger.warning("⚠️ No frontend build found. Dashboard will not be served.")
        logger.warning(f"   Searched in: {frontend_paths}")

        @app.get("/")
        async def root():
            return {
                "name": "Memecoin AI Bot",
                "version": "0.3.0",
                "status": "running",
                "trading_mode": settings.trading_mode,
                "live_trading": settings.is_live_trading,
                "endpoints": {
                    "api_docs": "/docs",
                    "health": "/health",
                    "system": "/api/system/status",
                },
                "note": "Dashboard not built. Run: cd frontend && npm run build",
            }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_env == "development",
    )
