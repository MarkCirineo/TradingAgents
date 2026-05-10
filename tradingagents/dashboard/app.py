"""FastAPI application for the TradingAgents dashboard.

Serves the single-page frontend and exposes REST API endpoints
for portfolio data, order details (with bracket legs), and SSE
for real-time updates.

Usage (standalone, for development):
    python -m tradingagents.dashboard.app

The app is designed to run in the same process as the trading
daemon in production — see Phase 7 integration.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared state — lazily initialised components accessible by all routes
# ---------------------------------------------------------------------------

_state: dict = {}


def get_trade_db():
    """Return the shared TradeDB instance (lazy init)."""
    if "trade_db" not in _state:
        from tradingagents.execution.trade_db import TradeDB
        _state["trade_db"] = TradeDB()
    return _state["trade_db"]


def get_alpaca_client():
    """Return the shared AlpacaClient instance (lazy init)."""
    if "alpaca_client" not in _state:
        try:
            from tradingagents.execution.alpaca_client import AlpacaClient
            _state["alpaca_client"] = AlpacaClient()
        except Exception as exc:
            logger.warning("AlpacaClient not available: %s", exc)
            _state["alpaca_client"] = None
    return _state["alpaca_client"]


def get_config():
    """Return the current daemon configuration."""
    if "config" not in _state:
        from tradingagents.default_config import DEFAULT_CONFIG
        _state["config"] = DEFAULT_CONFIG
    return _state["config"]


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise shared components on startup, cleanup on shutdown."""
    logger.info("Dashboard starting — initialising shared components")
    # Eagerly init so first request isn't slow
    get_trade_db()
    get_alpaca_client()
    get_config()
    logger.info("Dashboard ready")
    yield
    logger.info("Dashboard shutting down")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="TradingAgents Dashboard",
    description="Real-time monitoring & control for the autonomous trading daemon",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow browser access during development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# API routers
# ---------------------------------------------------------------------------

from tradingagents.dashboard.api.portfolio import router as portfolio_router
from tradingagents.dashboard.api.stream import router as stream_router

app.include_router(portfolio_router, prefix="/api", tags=["portfolio"])
app.include_router(stream_router, prefix="/api", tags=["stream"])

# ---------------------------------------------------------------------------
# Static files — serve the SPA
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"

# Mount static assets (CSS, JS) under /static
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/")
async def serve_spa():
    """Serve the main SPA shell for all non-API routes."""
    return FileResponse(str(_STATIC_DIR / "index.html"))


# Catch-all for SPA client-side routing (non-API, non-static paths)
@app.get("/{path:path}")
async def spa_fallback(path: str):
    """Catch-all route that serves index.html for client-side routing."""
    # Don't catch API or static requests
    if path.startswith("api/") or path.startswith("static/"):
        return None
    index_path = _STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """Run the dashboard server (development mode)."""
    import uvicorn
    from dotenv import load_dotenv
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    port = int(os.environ.get("DASHBOARD_PORT", "8050"))
    logger.info("Starting TradingAgents Dashboard on port %d", port)

    uvicorn.run(
        "tradingagents.dashboard.app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
