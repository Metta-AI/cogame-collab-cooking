"""Jev chooses a shift while all four seats use the ordinary action stream."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from collab_cooking.coworld.jev_player import JevSeat, external_registration
from collab_cooking.coworld.player import Seat
from tests.harness import fast_cert_config, run_episode, scripted_registration


def test_mock_sidecar_choice_reaches_mixed_episode(monkeypatch, tmp_path: Path) -> None:
    requests: list[dict] = []
    headers: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            assert self.path == "/v1/systemone"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            headers.append({name.lower(): value for name, value in self.headers.items()})
            criteria = request["questions"]["shift"]["criteria"]
            probabilities = {name: float(name == "hold") for name in criteria}
            response = json.dumps({"answers": {"shift": {"type": "choice", "probabilities": probabilities}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", f"http://127.0.0.1:{server.server_port}")
    try:
        out = run_episode(
            fast_cert_config(
                layout="open-kitchen",
                max_steps=120,
                step_seconds=0.01,
                shutdown_grace_seconds=0,
                player_connect_timeout_seconds=2,
            ),
            [external_registration(), *[scripted_registration() for _ in range(3)]],
            tmp_path,
            seat_factories=[JevSeat, Seat, Seat, Seat],
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join()

    jev = out["sockets"][0].seat
    assert out["results"]["reason"] == "complete"
    assert out["results"]["seat_kinds"] == ["external", *["scripted:brigade"] * 3]
    assert out["results"]["cross_play"]
    assert requests and jev.choice == "hold"
    assert all(request["model"] == "typesafe/jev-1.13" for request in requests)
    assert all(header["x-coworld-player-slot"] == "0" and "authorization" not in header for header in headers)
    assert all("known_entities" in json.loads(request["state"]) for request in requests)
    assert any(action == "noop" for step, action in out["sockets"][0].actions if step > 50)
    replay = json.loads(out["replay_bytes"])
    assert replay["seats"][0]["kind"] == "external"
    baseline = run_episode(
        fast_cert_config(
            layout="open-kitchen",
            max_steps=120,
            step_seconds=0,
            shutdown_grace_seconds=0,
            player_connect_timeout_seconds=2,
        ),
        [scripted_registration() for _ in range(4)],
        tmp_path / "baseline",
    )
    baseline_replay = json.loads(baseline["replay_bytes"])
    assert [tick["c"][0][3] for tick in replay["ticks"][10:]] != [
        tick["c"][0][3] for tick in baseline_replay["ticks"][10:]
    ]
    assert out["sockets"][0].final is not None
    assert all(socket.final is not None for socket in out["sockets"])
