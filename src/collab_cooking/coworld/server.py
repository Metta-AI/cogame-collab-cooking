"""The game server.

`/bin/collab-cooking` execs `python -m collab_cooking.coworld.server`.

Routes, and only these:

* ``GET /healthz`` -> ``{"ok": true}``
* ``GET /client/player``, ``GET /client/global`` -> real static pages,
  registered before any catch-all, neither of which opens a player socket (the
  certification runner probes both **before** starting the player pods, and a
  404 or a socket side effect fails the episode).
* ``WS /player?slot=N&token=T`` -> seat N, token checked against
  ``config.tokens[N]``, else close 1008.
* ``WS /global`` -> the spectator stream, and the runner's ping target. It
  keeps answering for the shutdown grace after the artifacts are written.

``GET /client/replay``, ``WS /replay``, ``create_replay_app()`` and
``COGAME_REPLAY_SERVER`` are **deleted**: replays are the static wasm bundle,
never a pod.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import tempfile
import time
import zlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketDisconnect

from collab_cooking.coworld.live_episode import EpisodeConfig, LiveMettaGridEpisode
from collab_cooking.coworld.llm import LlmPlanner
from collab_cooking.missions.kitchen import mission_for_config

PROCESS_START = time.monotonic()

CLIENT_DIR = Path(__file__).parent / "clients"
GAME_NAME = "collab_cooking"
GLOBAL_PROTOCOL = "collab-cooking.global.v1"


def read_uri(uri: str) -> bytes:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path)).read_bytes()
    if parsed.scheme in {"http", "https"}:
        with urlopen(uri, timeout=30) as response:  # noqa: S310 - Coworld supplies artifact URIs.
            return response.read()
    raise ValueError(f"Unsupported URI scheme for {uri!r}")


def write_uri(uri: str, data: bytes, *, method: str | None = None) -> None:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return
    if parsed.scheme in {"http", "https"}:
        request = Request(uri, data=data, method=method or "PUT")  # noqa: S310
        request.add_header("Content-Type", "application/json")
        with urlopen(request, timeout=30) as response:  # noqa: S310
            response.read()
        return
    raise ValueError(f"Unsupported URI scheme for {uri!r}")


def output_path_for(uri: str, suffix: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    path = Path(tempfile.mkdtemp(prefix="collab-cooking-coworld-")) / suffix
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_json_uri(uri: str) -> dict[str, Any]:
    data = read_uri(uri)
    if uri.endswith(".json.gz"):
        data = gzip.decompress(data)
    elif uri.endswith(".json.z"):
        data = zlib.decompress(data)
    return json.loads(data)


class CoworldGame:
    def __init__(
        self,
        raw_config: dict[str, Any],
        *,
        results_path: Path,
        replay_path: Path | None,
        request_shutdown: Callable[[], None],
        process_start: float | None = None,
    ) -> None:
        self.raw_config = raw_config
        self.tokens = list(raw_config["tokens"])
        self.config = EpisodeConfig.from_mapping(raw_config)
        self.config.num_agents = len(self.tokens)
        self.results_path = results_path
        self.replay_path = replay_path
        env = mission_for_config(self.config)
        planner = LlmPlanner(
            model=self.config.model,
            max_output_tokens=self.config.max_output_tokens,
            timeout_seconds=self.config.plan_timeout_seconds,
            max_requests_per_minute=self.config.llm_max_requests_per_minute,
            max_workers=self.config.num_agents,
        )
        self.episode = LiveMettaGridEpisode.from_env(
            env,
            config=self.config,
            tokens=self.tokens,
            player_names=[
                str(entry.get("name", f"seat-{index}"))
                for index, entry in enumerate(raw_config.get("players") or [])
            ],
            planner=planner,
            process_start=process_start if process_start is not None else PROCESS_START,
            disconnect_exception_types=(RuntimeError, WebSocketDisconnect),
            request_shutdown=request_shutdown,
        )
        self.episode.configure_artifacts(
            self._write_artifacts,
            generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def _write_artifacts(self, results: bytes, replay: bytes) -> None:
        self.results_path.write_bytes(results)
        if self.replay_path is not None:
            self.replay_path.write_bytes(replay)

    def global_message(self) -> dict[str, Any]:
        return {"protocol": GLOBAL_PROTOCOL, **self.episode.snapshot()}

    def handle_global_message(self, message: dict[str, Any]) -> None:
        if str(message.get("type", "")) != "control":
            return
        command = str(message.get("command", "")).lower()
        if command in {"play", "resume", "start"}:
            self.episode.paused = False
        elif command in {"pause", "stop"}:
            self.episode.paused = True
        elif command == "speed":
            speed = float(message.get("speed", 1))
            if speed > 0:
                self.episode.step_seconds = self.config.step_seconds / speed


def create_app(config: dict[str, Any], request_shutdown: Callable[[], None]) -> FastAPI:
    results_uri = os.environ["COGAME_RESULTS_URI"]
    replay_uri = os.environ.get("COGAME_SAVE_REPLAY_URI")
    results_path = output_path_for(results_uri, "results.json")
    replay_path = output_path_for(replay_uri, "replay.json") if replay_uri else None

    def finish_episode() -> None:
        if urlparse(results_uri).scheme != "file" and results_path.exists():
            write_uri(results_uri, results_path.read_bytes(), method=os.environ.get("COGAME_RESULTS_METHOD"))
        if replay_uri and replay_path and urlparse(replay_uri).scheme != "file" and replay_path.exists():
            write_uri(replay_uri, replay_path.read_bytes(), method=os.environ.get("COGAME_SAVE_REPLAY_METHOD"))
        request_shutdown()

    game = CoworldGame(
        config,
        results_path=results_path,
        replay_path=replay_path,
        request_shutdown=finish_episode,
    )
    app = FastAPI()

    @app.on_event("startup")
    async def _start_episode() -> None:
        game.episode.play_task = asyncio.create_task(game.episode.run())

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/client/global")
    def global_client() -> HTMLResponse:
        return HTMLResponse((CLIENT_DIR / "global.html").read_text(encoding="utf-8"))

    @app.get("/client/player")
    def player_client() -> HTMLResponse:
        return HTMLResponse((CLIENT_DIR / "player.html").read_text(encoding="utf-8"))

    @app.websocket("/global")
    async def global_viewer(websocket: WebSocket) -> None:
        await websocket.accept()
        sender = asyncio.create_task(_send_global_snapshots(websocket))
        receiver = asyncio.create_task(_drain_global_messages(websocket))
        _done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _send_global_snapshots(websocket: WebSocket) -> None:
        try:
            while not game.episode.done:
                await websocket.send_json(game.global_message())
                await asyncio.sleep(max(0.05, game.episode.step_seconds))
            await websocket.send_json({"type": "final", **game.global_message()})
            # Keep the socket answering through the shutdown grace: the
            # certification runner pings /global after the player pods start.
            while not game.episode.exited:
                await asyncio.sleep(0.25)
        except Exception:  # noqa: BLE001 - a dead spectator never fails an episode
            return

    async def _drain_global_messages(websocket: WebSocket) -> None:
        try:
            async for message in websocket.iter_json():
                game.handle_global_message(message)
        except Exception:  # noqa: BLE001
            return

    @app.websocket("/player")
    async def player(websocket: WebSocket) -> None:
        try:
            slot = int(websocket.query_params.get("slot", "-1"))
        except ValueError:
            await websocket.close(code=1008)
            return
        token = websocket.query_params.get("token", "")
        if slot < 0 or slot >= len(game.tokens) or game.tokens[slot] != token:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        connection_id = await game.episode.connect_player(slot, websocket)
        try:
            async for message in websocket.iter_json():
                if game.episode.done:
                    break
                await game.episode.handle_player_message(connection_id, message)
        except Exception:  # noqa: BLE001 - a dead player socket is not a game failure
            pass
        finally:
            game.episode.disconnect_player(connection_id)

    return app


def load_app_from_env(request_shutdown: Callable[[], None]) -> FastAPI:
    config = load_json_uri(os.environ["COGAME_CONFIG_URI"])
    return create_app(config, request_shutdown)


def main() -> None:
    server: uvicorn.Server

    def request_shutdown() -> None:
        server.should_exit = True

    host = os.environ.get("COGAME_HOST", "0.0.0.0")  # noqa: S104 - container-local bind
    port = int(os.environ.get("COGAME_PORT", "8080"))
    server = uvicorn.Server(uvicorn.Config(load_app_from_env(request_shutdown), host=host, port=port))
    server.run()


if __name__ == "__main__":
    main()
