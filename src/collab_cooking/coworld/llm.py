"""Game-side LLM transport: one parallel batch per plan turn.

A port of `cogame-factorio`'s `players/llm_player.py`, moved server-side. The
transport ladder is kept as it is there:

1. ``AWS_ENDPOINT_URL_BEDROCK_RUNTIME`` / ``AWS_BEARER_TOKEN_BEDROCK`` present
   -> the minimal Bedrock ``InvokeModel`` HTTP client;
2. else ``ANTHROPIC_API_KEY``;
3. else read ``ANTHROPIC_API_KEY_URI`` (the ``secret://`` URI the platform
   mounts) and use that;
4. else **disabled** -- zero network calls for the whole episode.

The LLM lives in the game container, not the player container, because "all
seats' calls go out as ONE parallel batch per turn" is satisfiable only by the
party that owns the turn boundary, and because retry-once-then-fall-back must
be enforced by that same party or a hung player pod becomes a silently passing
seat.

Nothing here blocks the tick loop: `LlmPlanner.start_turn` submits the whole
batch to a thread pool and returns; the loop polls `PlanBatch.poll()` and
delivers whatever landed.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from collab_cooking.coworld.plans import (
    ERROR_RUNES,
    RETRY_HINT,
    ParsedPlan,
    PlanError,
    parse_plan,
    truncate_runes,
)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def _log(message: str) -> None:
    print(f"collab-cooking llm: {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True, slots=True)
class PlanRequest:
    slot: int
    alias: str
    system: str
    user: str
    legal_stations: list[str]
    ally_aliases: list[str]


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    slot: int
    plan: ParsedPlan | None
    cause: str = ""
    detail: str = ""
    requests: int = 0

    @property
    def ok(self) -> bool:
        return self.plan is not None

    @property
    def src(self) -> str:
        return "llm" if self.ok else f"fallback:{self.cause}"


class RateBudget:
    """A rolling requests-per-minute budget shared by every seat.

    Retries draw from it, and a request that would exceed it is simply not
    made -- the seat plays its fallback for that turn with `rate_budget`.
    """

    def __init__(self, max_per_minute: int) -> None:
        self._max = max(1, int(max_per_minute))
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def take(self, now: float | None = None) -> bool:
        stamp = time.monotonic() if now is None else now
        with self._lock:
            while self._events and stamp - self._events[0] >= 60.0:
                self._events.popleft()
            if len(self._events) >= self._max:
                return False
            self._events.append(stamp)
            return True

    @property
    def spent(self) -> int:
        with self._lock:
            return len(self._events)


def read_secret_uri(uri: str) -> str:
    """Read a `secret://`, `file://` or bare-path credential mount."""
    parsed = urlparse(uri)
    if parsed.scheme in ("", "file"):
        path = Path(unquote(parsed.path or uri))
        return path.read_text(encoding="utf-8").strip()
    if parsed.scheme == "secret":
        # The platform mounts secret:// URIs into the filesystem; the mount
        # point is named by the URI's own path.
        for candidate in (
            Path("/run/secrets") / parsed.netloc / parsed.path.lstrip("/"),
            Path("/var/run/secrets") / parsed.netloc / parsed.path.lstrip("/"),
            Path(parsed.path),
        ):
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(f"no readable credential at {uri!r}")


class _Transport:
    """One `messages.create`-shaped call site over urllib, no SDK required."""

    def __init__(self, kind: str, model: str, timeout: float, api_key: str = "") -> None:
        self.kind = kind
        self.model = model
        self.timeout = timeout
        self.api_key = api_key
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
        endpoint = (
            os.environ.get("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "").strip()
            or f"https://bedrock-runtime.{region}.amazonaws.com"
        )
        self.endpoint = endpoint.rstrip("/")
        self.bedrock_token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        if self.kind == "bedrock":
            url = f"{self.endpoint}/model/{self.model}/invoke"
            # `output_config.effort` is never sent: Haiku 4.5 rejects it.
            body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            headers = {"content-type": "application/json", "accept": "application/json"}
            if self.bedrock_token:
                headers["authorization"] = f"Bearer {self.bedrock_token}"
        else:
            url = ANTHROPIC_MESSAGES_URL
            body = {
                "model": self.model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            headers = {
                "content-type": "application/json",
                "accept": "application/json",
                "anthropic-version": ANTHROPIC_VERSION,
                "x-api-key": self.api_key,
            }
        request = urllib.request.Request(  # noqa: S310 - fixed https endpoints
            url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # noqa: BLE001
                detail = ""
            raise RuntimeError(f"http {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
        blocks = payload.get("content") or []
        return "".join(
            block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text"
        )


def build_transport(model: str, timeout: float) -> _Transport | None:
    """The ladder. Returns None when there are no credentials at all."""
    if os.environ.get("AWS_ENDPOINT_URL_BEDROCK_RUNTIME") or os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        return _Transport("bedrock", BEDROCK_MODEL, timeout)
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return _Transport("anthropic", model, timeout, api_key=key)
    uri = (os.environ.get("ANTHROPIC_API_KEY_URI") or "").strip()
    if uri:
        try:
            key = read_secret_uri(uri)
        except Exception as exc:  # noqa: BLE001
            _log(f"ANTHROPIC_API_KEY_URI unreadable ({exc!r}); disabling the LLM")
            return None
        if key:
            return _Transport("anthropic", model, timeout, api_key=key)
    return None


@dataclass
class PlanBatch:
    """One turn's worth of calls, all issued at the same instant."""

    turn: int
    deadline: float
    futures: dict[int, Future] = field(default_factory=dict)
    delivered: set[int] = field(default_factory=set)

    def poll(self) -> list[PlanOutcome]:
        """Outcomes that have landed since the last poll. Never blocks."""
        landed: list[PlanOutcome] = []
        expired = time.monotonic() >= self.deadline
        for slot, future in self.futures.items():
            if slot in self.delivered:
                continue
            if future.done():
                self.delivered.add(slot)
                try:
                    landed.append(future.result())
                except Exception as exc:  # noqa: BLE001 - never escapes the batch
                    _log(f"slot {slot}: batch worker raised {exc!r}")
                    landed.append(
                        PlanOutcome(slot=slot, plan=None, cause="transport", detail=truncate_runes(repr(exc), ERROR_RUNES))
                    )
            elif expired:
                self.delivered.add(slot)
                future.cancel()
                landed.append(PlanOutcome(slot=slot, plan=None, cause="timeout"))
        return landed

    @property
    def finished(self) -> bool:
        return len(self.delivered) >= len(self.futures)


class LlmPlanner:
    """Issues one parallel batch per plan turn and never blocks the tick loop."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        max_output_tokens: int = 900,
        timeout_seconds: float = 12.0,
        max_requests_per_minute: int = 26,
        max_workers: int = 4,
        transport: Any | None = None,
    ) -> None:
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds
        self.budget = RateBudget(max_requests_per_minute)
        self._transport = transport if transport is not None else build_transport(model, timeout_seconds)
        self._pool = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="cc-plan")
        self._requests = 0
        self._requests_lock = threading.Lock()
        if self._transport is None:
            _log("no credentials (ANTHROPIC_API_KEY / _URI / Bedrock): disabled, zero network calls")

    @property
    def disabled(self) -> bool:
        return self._transport is None

    @property
    def requests(self) -> int:
        with self._requests_lock:
            return self._requests

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def start_turn(self, turn: int, requests: list[PlanRequest]) -> PlanBatch:
        """Submit every prompt seat's call at once. Returns immediately."""
        batch = PlanBatch(turn=turn, deadline=time.monotonic() + self.timeout_seconds)
        for request in requests:
            batch.futures[request.slot] = self._pool.submit(self._one_seat, request)
        return batch

    # -- worker -------------------------------------------------------------
    def _one_seat(self, request: PlanRequest) -> PlanOutcome:
        """Call, parse, retry once, else fall back. Never raises."""
        if self._transport is None:
            return PlanOutcome(slot=request.slot, plan=None, cause="disabled")
        used = 0
        cause = "transport"
        detail = ""
        user = request.user
        for attempt in (0, 1):
            if not self.budget.take():
                return PlanOutcome(
                    slot=request.slot, plan=None, cause="rate_budget", detail="26 req/min", requests=used
                )
            used += 1
            with self._requests_lock:
                self._requests += 1
            try:
                text = self._transport.complete(request.system, user, self.max_output_tokens)
            except Exception as exc:  # noqa: BLE001 - classified, never escapes
                message = f"{exc}"
                cause = "timeout" if "timed out" in message.lower() or "timeout" in message.lower() else "transport"
                detail = truncate_runes(message, ERROR_RUNES)
                _log(f"slot {request.slot} attempt {attempt}: {cause} {detail}")
            else:
                try:
                    plan = parse_plan(text, request.legal_stations, request.ally_aliases)
                except PlanError as exc:
                    cause, detail = exc.cause, exc.detail
                    _log(f"slot {request.slot} attempt {attempt}: {cause} {detail}")
                else:
                    return PlanOutcome(slot=request.slot, plan=plan, requests=used)
            if attempt == 0:
                user = f"{request.user}\n\n{RETRY_HINT}"
        return PlanOutcome(slot=request.slot, plan=None, cause=cause, detail=detail, requests=used)


# ---------------------------------------------------------------------------
# The prompt. Constant system block per episode; a deterministic, bounded user
# message per seat per turn, rendered from that seat's OWN remembered world
# model (the same `ObsParser` + `EntityMap` the scripted brain builds), so a
# prompt seat can never see further than a scripted one.
# ---------------------------------------------------------------------------

SYSTEM_TEMPLATE = """You are {alias}, one of 4 cogs running a kitchen together for {max_steps} ticks.
Orders arrive on the board every 18 ticks and expire 50 ticks later. A dish is a chain:
fetch veg or meat -> chop it (3 uses) -> pot it (soup: chopped veg + chopped meat, 10 ticks,
burns 14 ticks after it is ready) or fry it (chopped veg, 8 ticks, burns after 11) or plate
chopped veg as a salad -> carry it to the pass and serve it against a live ticket -> the plate
comes back dirty and needs 3 uses at the sink.
YOU CAN CARRY EXACTLY ONE THING. Counters (the walls) hold one item each: put your item down
and a team-mate can pick it up. That is often faster than walking round.
Your score and everyone else's is the same number: dishes the team serves. Nothing else scores.
Kitchen: {layout} - {layout_line}
You give one standing order; a controller walks you there tick by tick until your next order,
about {plan_interval} ticks from now.
Reply with a single JSON object and NOTHING else. Your reply MUST begin with the character {{.
Schema:
{{"station":"<one of LEGAL STATIONS>","recipe":"salad|soup|fries|any","zone":"left|right|pass|any",
 "handoff":"<ally alias or none>","yield_to":"<ally alias or none>",
 "say":"<=120 chars","note":"<=200 chars"}}
"say" is heard by your team-mates next turn and shown to spectators. "note" is private and is
handed back only to you."""

USER_CAP = 2000


def build_system_prompt(
    alias: str, max_steps: int, layout: str, layout_line: str, plan_interval: int, standing_orders: str
) -> str:
    """The constant system block, with the seat's PLAYER_PROMPT appended."""
    body = SYSTEM_TEMPLATE.format(
        alias=alias,
        max_steps=max_steps,
        layout=layout,
        layout_line=layout_line,
        plan_interval=plan_interval,
    )
    if standing_orders:
        body = f"{body}\nSTANDING ORDERS: {standing_orders}"
    return body


def _fmt_pos(pos: tuple[int, int]) -> str:
    return f"({pos[0]},{pos[1]})"


def build_user_message(view: dict[str, Any]) -> str:
    """The per-turn observation. Deterministic, every list bounded, <= 2000 chars."""
    lines: list[str] = []
    lines.append(
        f"TURN {view['turn']}/{view['turns']}  TICK {view['tick']}/{view['ticks']}  "
        f"KITCHEN {view['layout']}  DISHES {view['dishes']}  TICKETS LIVE {view['live']}"
    )
    lines.append(
        f"YOU {view['alias']} at {_fmt_pos(view['position'])} carrying "
        f"{view['carrying'] or 'nothing'}  served {view['delivered']}"
    )
    lines.append("TEAM (last seen)")
    for mate in view["team"][:3]:
        if mate["seen"] is None:
            lines.append(f"  {mate['alias']} not seen yet")
        elif mate["pos"] is None:
            lines.append(f"  {mate['alias']} not seen since tick {mate['seen']}")
        else:
            lines.append(
                f"  {mate['alias']} {_fmt_pos(mate['pos'])} {mate['age']} ticks ago "
                f"carrying {mate['carrying'] or 'nothing'}"
            )
    board = view["board"]
    if board["age"] is None:
        lines.append("BOARD: not seen yet - go and look at it")
    else:
        lines.append(
            f"BOARD (seen {board['age']} ticks ago): soup {board['soup']}, "
            f"salad {board['salad']}, fries {board['fries']}"
        )
    lines.append("STATIONS YOU KNOW")
    for station in view["stations"][:9]:
        note = f": {station['note']}" if station["note"] else ""
        lines.append(f"  {station['name']} {_fmt_pos(station['pos'])} {station['age']} ticks ago{note}")
    held = view["counters"][:6]
    if held:
        lines.append(
            "COUNTERS HOLDING SOMETHING (<=6): "
            + ", ".join(f"{_fmt_pos(pos)} {item}" for pos, item in held)
        )
    lines.append(f"BLOCKED LAST TURN: {view['blocked']} times" + (f", mostly at {_fmt_pos(view['blocked_at'])}" if view["blocked_at"] else ""))
    lines.append("LEGAL STATIONS: " + ", ".join(view["legal_stations"]))
    lines.append(f"LAST ORDER: {view['last_order']}")
    radio = view["radio"][:3]
    if radio:
        lines.append("TEAM RADIO (<=3 lines)")
        for who, what in radio:
            lines.append(f"  {who}: {what}")
    if view["note"]:
        lines.append(f"YOUR NOTE: {view['note']}")
    text = "\n".join(lines)
    return text if len(text) <= USER_CAP else text[:USER_CAP]
