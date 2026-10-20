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
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketDisconnect

from collab_cooking.coworld import lifecycle
from collab_cooking.coworld.artifact_io import ARTIFACT_SECONDS, read_uri, write_uri
from collab_cooking.coworld.live_episode import EpisodeConfig, LiveMettaGridEpisode
from collab_cooking.coworld.plan_protocol import MAX_FRAME_BYTES
from collab_cooking.coworld.prompts import PromptSelection
from collab_cooking.missions.kitchen import mission_for_config

PROCESS_START = time.monotonic()

CLIENT_DIR = Path(__file__).parent / "clients"
GAME_NAME = "collab_cooking"
GLOBAL_PROTOCOL = "collab-cooking.global.v1"


def output_path_for(uri: str, suffix: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    path = Path(tempfile.mkdtemp(prefix="collab-cooking-coworld-")) / suffix
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def load_json_uri(uri: str) -> dict[str, Any]:
    data = await read_uri(uri, deadline=asyncio.get_running_loop().time() + ARTIFACT_SECONDS)
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
        self.episode = LiveMettaGridEpisode.from_env(
            env,
            config=self.config,
            prompt_selection=PromptSelection.model_validate(raw_config),
            tokens=self.tokens,
            player_names=[
                str(entry.get("name", f"seat-{index}")) for index, entry in enumerate(raw_config.get("players") or [])
            ],
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

    async def publish_artifacts() -> None:
        deadline = asyncio.get_running_loop().time() + ARTIFACT_SECONDS
        if urlparse(results_uri).scheme != "file" and results_path.exists():
            await write_uri(
                results_uri,
                results_path.read_bytes(),
                method=os.environ.get("COGAME_RESULTS_METHOD"),
                deadline=deadline,
            )
        if replay_uri and replay_path and urlparse(replay_uri).scheme != "file" and replay_path.exists():
            await write_uri(
                replay_uri,
                replay_path.read_bytes(),
                method=os.environ.get("COGAME_SAVE_REPLAY_METHOD"),
                deadline=deadline,
            )

    game = CoworldGame(
        config,
        results_path=results_path,
        replay_path=replay_path,
        request_shutdown=request_shutdown,
    )
    game.episode.artifact_publisher = publish_artifacts
    app = FastAPI()

    app.state.game = game

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/client/global")
    def global_client() -> HTMLResponse:
        return HTMLResponse((CLIENT_DIR / "global.html").read_text(encoding="utf-8"))

    @app.get("/client/player")
    def player_client() -> HTMLResponse:
        return HTMLResponse((CLIENT_DIR / "player.html").read_text(encoding="utf-8"))

    async def received_packets(websocket: WebSocket) -> AsyncIterator[dict[str, Any]]:
        while True:
            packet = await websocket.receive()
            if packet["type"] == "websocket.disconnect":
                return
            raw = packet["text"]

            async def decode(raw=raw) -> dict[str, Any]:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("WebSocket packet requires an object")
                return value

            decoding = lifecycle.owned_task(decode())
            await asyncio.wait({decoding})
            if decoding.exception() is not None:
                raise ValueError("invalid WebSocket JSON packet") from None
            yield decoding.result()

    @app.websocket("/global")
    async def global_viewer(websocket: WebSocket) -> None:
        await websocket.accept()
        sender = lifecycle.owned_task(_send_global_snapshots(websocket))
        receiver = lifecycle.owned_task(_drain_global_messages(websocket))
        try:
            done, _ = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled():
                    error = task.exception()
                    if error is not None:
                        raise RuntimeError("spectator socket owner failed") from None
        finally:
            if not await lifecycle.settle({sender, receiver}, lifecycle.cleanup_deadline(), cancel=True):
                raise lifecycle.OwnershipUnsettled("spectator socket owner did not join")

    async def _send_global_snapshots(websocket: WebSocket) -> None:
        while not game.episode.closing:
            await websocket.send_json(game.global_message())
            await asyncio.sleep(max(0.05, game.episode.step_seconds))
        while not game.episode.done and not game.episode.exited:
            await asyncio.sleep(0.05)
        if game.episode.done:
            await websocket.send_json({"type": "final", **game.global_message()})
        while not game.episode.exited:
            await asyncio.sleep(0.25)

    async def _drain_global_messages(websocket: WebSocket) -> None:
        async for message in received_packets(websocket):
            game.handle_global_message(message)

    @app.websocket("/player")
    async def player(websocket: WebSocket) -> None:
        raw_slot = websocket.query_params.get("slot", "")
        token = websocket.query_params.get("token", "")
        if raw_slot not in {str(slot) for slot in range(len(game.tokens))}:
            await websocket.close(code=1008)
            return
        slot = int(raw_slot)
        if game.tokens[slot] != token or game.episode.closing:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        receiver = asyncio.current_task()
        assert receiver is not None
        game.episode.player_receivers.add(receiver)
        connection_id = await game.episode.connect_player(slot, websocket)
        try:
            async for message in received_packets(websocket):
                await game.episode.handle_player_message(connection_id, message)
        finally:
            game.episode.disconnect_player(connection_id)

    return app


async def load_app_from_env(request_shutdown: Callable[[], None]) -> FastAPI:
    config = await load_json_uri(os.environ["COGAME_CONFIG_URI"])
    return create_app(config, request_shutdown)


async def serve_owned(server: uvicorn.Server, episode: LiveMettaGridEpisode) -> None:
    """Join engine and player evidence before shutting down the HTTP/socket owners."""
    if not server.config.loaded:
        server.config.load()
    server.lifespan = server.config.lifespan_class(server.config)
    loop_task = None
    starting = lifecycle.owned_task(server.startup())
    try:
        await starting
        if not server.should_exit:
            episode.play_task = lifecycle.owned_task(episode.run())
            loop_task = lifecycle.owned_task(server.main_loop())
            done, _ = await asyncio.wait({episode.play_task, loop_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    finally:
        deadline = lifecycle.cleanup_deadline()
        # The sockets remain available while STOP progress and exact receipts drain.
        engine_closing = lifecycle.owned_task(episode.close_owner(deadline))
        engine_joined = await lifecycle.settle({engine_closing}, deadline, cancel=False)
        engine_error = None
        if engine_joined:
            engine_error = engine_closing.exception()
            engine_joined = engine_error is None and engine_closing.result()
        server.should_exit = True
        episode.exited = True
        main_joined = loop_task is None or await lifecycle.settle({loop_task}, deadline, cancel=True)
        startup_joined = await lifecycle.settle({starting}, deadline, cancel=True)
        shutting_down = lifecycle.owned_task(server.shutdown() if server.started else server.lifespan.shutdown())
        http_joined = await lifecycle.settle({shutting_down}, deadline, cancel=False)
        if http_joined and not shutting_down.cancelled():
            shutting_down.result()
        if engine_error is not None:
            raise engine_error
        if not startup_joined or not engine_joined or not main_joined or not http_joined:
            raise lifecycle.OwnershipUnsettled("game process left engine or socket owners unresolved")


async def main_async() -> None:
    server: uvicorn.Server

    def request_shutdown() -> None:
        server.should_exit = True

    host = os.environ.get("COGAME_HOST", "0.0.0.0")  # noqa: S104 - container-local bind
    port = int(os.environ.get("COGAME_PORT", "8080"))
    app = await load_app_from_env(request_shutdown)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            ws_max_size=MAX_FRAME_BYTES,
            ws_max_queue=4,
            timeout_graceful_shutdown=int(lifecycle.CLEANUP_SECONDS),
        )
    )
    await serve_owned(server, app.state.game.episode)


def main() -> None:
    lifecycle.main_owned(main_async())


if __name__ == "__main__":
    main()
