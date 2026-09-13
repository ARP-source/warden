"""Entry point for the Target service. Run with: python run_target.py"""
from __future__ import annotations

import uvicorn

from warden.config import get_config

if __name__ == "__main__":
    cfg = get_config()
    uvicorn.run("target.app:app", host=cfg.target.host, port=cfg.target.port,
                log_level="warning")
