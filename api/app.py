"""
FastAPI app.

    uvicorn api.app:app                         # live Hyperliquid feed, every coin in ANALYTIX_COINS
    ANALYTIX_MODE=replay uvicorn api.app:app    # replay data/sampleSession.jsonl

If frontend/dist exists (npm run build), the dashboard is served at / too.
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes import router as rest_router
from api.runtime import Runtime
from api.stream import router as ws_router
from config import ALERTS_FILE, FRONTEND_DIST, MODE, REPLAY_FILE, REPLAY_LOOP, REPLAY_SPEED

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(mode: str = MODE, replay_file: Path = REPLAY_FILE, speed: float = REPLAY_SPEED,
               loop: bool = REPLAY_LOOP, serve_frontend: bool = True, alerts_file: Path = ALERTS_FILE) -> FastAPI:
    runtime = Runtime(mode, replay_file=replay_file, speed=speed, loop=loop, alerts_file=alerts_file)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.start()
        yield
        await runtime.stop()

    app = FastAPI(title="Analytix", description="Why did this price move?", lifespan=lifespan)
    app.state.runtime = runtime
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
    app.include_router(rest_router)
    app.include_router(ws_router)
    if serve_frontend and FRONTEND_DIST.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return app


app = create_app()
