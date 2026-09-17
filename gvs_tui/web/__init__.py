"""Web GUI backend for gvs-tui (FastAPI + WebSocket)."""

from .server import create_app

__all__ = ["create_app"]
