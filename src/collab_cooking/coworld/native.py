"""Native Messages and actual received evidence, owned by one granted plan request."""

from __future__ import annotations

import asyncio
import base64
import math
import os
import sys
import time
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4
from weakref import WeakKeyDictionary

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from . import lifecycle as _lifecycle
from .transport import OwnedHTTPTransport

# One received body is repeated as raw UTF-8/base64 and parsed sampling evidence.
# Keep one progress frame below the separate 16 MiB WebSocket evidence capacity.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_capacities: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()


class ResponseRejected(ValueError):
    """Observed native response cannot supply an ordinary action."""


class PrivateModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        allow_inf_nan=False,
        protected_namespaces=(),
    )


class LearnerScope(PrivateModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        protected_namespaces=(),
        validate_default=True,
    )
    endpoint: str = Field(default_factory=lambda: os.environ["COWORLD_LLM_ENDPOINT"])

    @field_validator("endpoint")
    @classmethod
    def registered_sidecar_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("native learner requires its exact local sidecar endpoint")
        return value

    purpose: Literal["learner"] = "learner"
    slot: StrictInt = Field(ge=0, le=3)
    model: str = Field(min_length=1)


class TextBlock(PrivateModel):
    type: Literal["text"] = "text"
    text: str
    cache_control: dict[str, JsonValue] | None = None


class Message(PrivateModel):
    role: Literal["user", "assistant"]
    content: str | list[TextBlock]


class Request(PrivateModel):
    model: str = Field(min_length=1)
    max_tokens: StrictInt = Field(gt=0)
    temperature: float = Field(strict=True, ge=0, le=1)
    top_p: float = Field(strict=True, gt=0, le=1)
    system: str | list[TextBlock]
    messages: list[Message] = Field(min_length=1)
    stream: Literal[False] = False


class ResponseBlock(BaseModel):
    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)
    type: str
    text: str | None = None

    @model_validator(mode="after")
    def text_requires_content(self) -> ResponseBlock:
        if self.type == "text" and self.text is None:
            raise ValueError("native text block lacks text")
        return self


class Usage(BaseModel):
    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)
    input_tokens: StrictInt = Field(ge=0)
    output_tokens: StrictInt = Field(ge=0)


class SamplingFields(PrivateModel):
    policy_revision: str = Field(min_length=1)
    tokenizer_revision: str = Field(min_length=1)
    chat_template: str = Field(min_length=1)
    enable_thinking: StrictBool
    max_new_tokens: StrictInt = Field(gt=0)
    max_sequence_length: StrictInt = Field(gt=0)
    sampling_seed: StrictInt
    eos_token_ids: list[Annotated[int, Field(strict=True, ge=0)]]
    prompt_token_ids: list[Annotated[int, Field(strict=True, ge=0)]]
    completion_token_ids: list[Annotated[int, Field(strict=True, ge=0)]]
    behavior_log_probs: list[StrictFloat] | None
    response: str
    stop_reason: Literal["eos", "length"]

    @model_validator(mode="after")
    def actual_draws(self) -> SamplingFields:
        if self.enable_thinking:
            raise ValueError("action sample cannot include unobserved reasoning")
        if self.behavior_log_probs is not None:
            if len(self.behavior_log_probs) != len(self.completion_token_ids):
                raise ValueError("draw probability count differs from token count")
            if any(value > 0 for value in self.behavior_log_probs):
                raise ValueError("draw log probabilities must be nonpositive")
        if len(self.prompt_token_ids) + len(self.completion_token_ids) > self.max_sequence_length:
            raise ValueError("sample exceeds actual context budget")
        if len(self.completion_token_ids) > self.max_new_tokens:
            raise ValueError("sample exceeds actual output budget")
        if self.stop_reason == "eos" and (
            not self.completion_token_ids or self.completion_token_ids[-1] not in self.eos_token_ids
        ):
            raise ValueError("EOS sample lacks actual EOS token")
        return self


class SamplingEvidence(SamplingFields):
    """Stored unit-temperature or explicitly captured greedy metadata, unchanged."""

    # Pydantic discriminated unions require every tag, including null, in Literal.
    sampling: Literal["full_softmax_temperature_one", None]  # noqa: PYI061

    @model_validator(mode="after")
    def original_sampling_mode(self) -> SamplingEvidence:
        if self.sampling is None:
            if self.behavior_log_probs is not None:
                raise ValueError("greedy decoding has no draw probabilities")
        elif self.behavior_log_probs is None:
            raise ValueError("sampled decoding lacks actual draw probabilities")
        return self


class TemperedSamplingEvidence(SamplingFields):
    """Actual full-softmax draws from the serving model's scaled logits."""

    sampling: Literal["full_softmax"]
    temperature: float = Field(strict=True, gt=0, le=2)

    @model_validator(mode="after")
    def actual_scaled_draws(self) -> TemperedSamplingEvidence:
        if self.behavior_log_probs is None:
            raise ValueError("tempered decoding lacks actual draw probabilities")
        return self


SampledCompletion = Annotated[SamplingEvidence | TemperedSamplingEvidence, Field(discriminator="sampling")]


class Response(BaseModel):
    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)
    model: str = Field(min_length=1)
    content: list[ResponseBlock]
    stop_reason: str
    usage: Usage
    sampling_evidence: SampledCompletion | None = None

    @property
    def text(self) -> str:
        return "".join(block.text for block in self.content if block.type == "text" and block.text is not None)


class Attempt(PrivateModel):
    attempt_id: str = Field(default_factory=lambda: str(uuid4()))
    purpose: Literal["learner"] = "learner"
    slot: StrictInt = Field(ge=0, le=3)
    inference_mode: Literal["text_action"] = "text_action"
    stage: Literal["plan"]
    protocol: Literal["anthropic_messages"] = "anthropic_messages"
    request: Request
    endpoint: str | None = Field(default=None, max_length=4096)
    model: str | None = None
    response: str | None = None
    raw_response: str | None = None
    response_body_b64: str | None = None
    response_complete: StrictBool | None = None
    response_reader_joined: StrictBool | None = None
    http_status: StrictInt | None = None
    received_header_pairs: list[tuple[str, str]] = Field(default_factory=list)
    response_headers: dict[str, str] = Field(default_factory=dict)
    platform_call_id: str | None = None
    provider_request_id: str | None = None
    model_identity: str | None = None
    tokenizer_identity: str | None = None
    chat_template_sha256: str | None = None
    sampling_evidence: SampledCompletion | None = None
    usage: Usage | None = None
    stop_reason: str | None = None
    timeout_ms: float | None = Field(default=None, gt=0)
    latency_ms: float | None = None
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def received_utf8_matches_actual_bytes(self) -> Attempt:
        if self.response_body_b64 is not None:
            body = base64.b64decode(self.response_body_b64, validate=True)
            if len(body) > MAX_RESPONSE_BYTES + 65536:
                raise ValueError("native received prefix exceeds its bounded reader capacity")
            if self.raw_response is not None and self.raw_response.encode("utf-8") != body:
                raise ValueError("native raw text differs from actual received bytes")
        elif self.raw_response is not None:
            raise ValueError("native raw response has no received byte evidence")
        return self


Progress = Callable[[Attempt], Coroutine[Any, Any, None]]


async def complete(
    request: Request,
    scope: LearnerScope,
    *,
    stage: Literal["plan"],
    timeout: float,
    attempt: Attempt,
    progress: Progress | None = None,
) -> Response:
    """One absolute request deadline and one bounded owned reader/writer drain."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("native call requires finite positive timeout")
    if (
        request.model != scope.model
        or attempt.request != request
        or attempt.stage != stage
        or attempt.purpose != scope.purpose
        or attempt.slot != scope.slot
    ):
        raise ValueError("native call identity differs from its frozen request")
    attempt.timeout_ms = timeout * 1000
    attempt.endpoint = scope.endpoint
    owner = _lifecycle.current_owner.get()
    while owner is not None:
        if owner.stopping:
            raise _lifecycle.OwnershipUnsettled("native call issued after owner stop")
        owner = owner.parent
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "accept-encoding": "identity",
    }
    headers["X-Coworld-Player-Slot"] = str(scope.slot)
    started = time.monotonic()
    deadline = asyncio.get_running_loop().time() + timeout
    client = httpx.AsyncClient(transport=OwnedHTTPTransport(), timeout=None, trust_env=False)
    response: httpx.Response | None = None

    async def receive() -> Response:
        nonlocal response
        capacity = _capacities.setdefault(
            asyncio.get_running_loop(),
            asyncio.Semaphore(int(os.environ.get("LM_MAX_CONCURRENT_CALLS", "6"))),
        )
        async with asyncio.timeout_at(deadline), capacity:
            if progress is not None:
                await progress(attempt.model_copy(deep=True))
            response = await client.send(
                client.build_request(
                    "POST",
                    scope.endpoint.rstrip("/") + "/v1/messages",
                    headers=headers,
                    content=request.model_dump_json(exclude_none=True).encode(),
                ),
                stream=True,
            )
            attempt.http_status = response.status_code
            attempt.response_complete = False
            attempt.response_reader_joined = False
            attempt.response_body_b64 = ""
            attempt.raw_response = ""
            attempt.received_header_pairs = [
                (name.decode("latin-1"), value.decode("latin-1")) for name, value in response.headers.raw
            ]
            names = [name.lower() for name, _ in attempt.received_header_pairs]
            controlled = {
                "x-softmax-llm-call-id",
                "request-id",
                "x-request-id",
                "x-coworld-checkpoint-sha256",
                "x-coworld-tokenizer-sha256",
                "x-coworld-chat-template-sha256",
            }
            if any(names.count(name) > 1 for name in controlled):
                raise ResponseRejected("duplicate native identity header")
            if (
                "request-id" in names
                and "x-request-id" in names
                and response.headers["request-id"] != response.headers["x-request-id"]
            ):
                raise ResponseRejected("conflicting native identity aliases")
            attempt.response_headers = dict(response.headers)
            attempt.platform_call_id = response.headers.get("x-softmax-llm-call-id")
            attempt.provider_request_id = response.headers.get("request-id") or response.headers.get("x-request-id")
            attempt.model_identity = response.headers.get("x-coworld-checkpoint-sha256")
            attempt.tokenizer_identity = response.headers.get("x-coworld-tokenizer-sha256")
            attempt.chat_template_sha256 = response.headers.get("x-coworld-chat-template-sha256")
            if progress is not None:
                await progress(attempt.model_copy(deep=True))
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ResponseRejected("native response encoding must be identity")
            received = bytearray()
            async for chunk in response.aiter_raw():
                received.extend(chunk)
                attempt.response_body_b64 = base64.b64encode(received).decode("ascii")
                text = received.decode("utf-8", errors="ignore")
                attempt.raw_response = text if text.encode() == received else None
                if len(received) > MAX_RESPONSE_BYTES:
                    raise ResponseRejected("native response exceeds private packet budget")
                if progress is not None:
                    await progress(attempt.model_copy(deep=True))
            attempt.response_complete = True
            attempt.raw_response = None
            attempt.raw_response = received.decode("utf-8")
            if response.status_code != 200:
                raise ResponseRejected(f"native Messages HTTP status {response.status_code}")
            result = Response.model_validate_json(attempt.raw_response)
            attempt.model = result.model
            attempt.response = result.text
            attempt.stop_reason = result.stop_reason
            attempt.usage = result.usage
            attempt.sampling_evidence = result.sampling_evidence
            if not result.text:
                raise ResponseRejected("native response has no action text")
            if result.sampling_evidence is not None:
                sample = result.sampling_evidence
                temperature = (
                    sample.temperature
                    if isinstance(sample, TemperedSamplingEvidence)
                    else 0
                    if sample.sampling is None
                    else 1
                )
                if (
                    request.temperature != temperature
                    or request.top_p != 1
                    or sample.max_new_tokens != request.max_tokens
                ):
                    raise ResponseRejected("sample does not match request decoder")
                if (
                    sample.response != result.text
                    or result.usage.input_tokens != len(sample.prompt_token_ids)
                    or result.usage.output_tokens != len(sample.completion_token_ids)
                ):
                    raise ResponseRejected("sample does not match actual native completion")
                expected_stop = "end_turn" if sample.stop_reason == "eos" else "max_tokens"
                if result.stop_reason != expected_stop:
                    raise ResponseRejected("sample stop differs from native stop")
                attempt.stop_reason = sample.stop_reason
            return result

    reader = _lifecycle.owned_task(receive())
    try:
        done, _ = await asyncio.wait({reader}, timeout=max(0, deadline - asyncio.get_running_loop().time()))
        if reader not in done:
            raise TimeoutError("native request reached its absolute deadline")
        return reader.result()
    finally:
        failure = sys.exception()
        if failure is not None:
            attempt.rejection_reason = type(failure).__name__
        drain_deadline = _lifecycle.cleanup_deadline()

        async def close_transport() -> None:
            try:
                if response is not None:
                    await response.aclose()
            finally:
                await client.aclose()

        reader_joined = await _lifecycle.settle({reader}, drain_deadline, cancel=not reader.done())
        joined = False
        if reader_joined:
            closer = _lifecycle.owned_task(close_transport())
            joined = await _lifecycle.settle({closer}, drain_deadline, cancel=False)
            if not joined:
                await _lifecycle.settle({closer}, drain_deadline, cancel=True)
        if response is not None:
            attempt.response_reader_joined = (
                reader_joined and joined and not closer.cancelled() and closer.exception() is None
            )
        attempt.latency_ms = (time.monotonic() - started) * 1000
        progress_joined = reader_joined
        if not reader_joined:
            # The active reader retains its resources. Only its actual terminal
            # state admits resource close; the episode owner still supervises it.
            def release_late_reader(task: asyncio.Task) -> None:
                if not task.cancelled():
                    error = task.exception()
                    if error is not None:
                        attempt.rejection_reason = type(error).__name__
                _lifecycle.owned_task(close_transport())

            reader.add_done_callback(release_late_reader)
        if progress is not None and reader_joined:
            writer = _lifecycle.owned_task(progress(attempt.model_copy(deep=True)))
            progress_joined = await _lifecycle.settle({writer}, drain_deadline, cancel=False)
            if not progress_joined:
                await _lifecycle.settle({writer}, drain_deadline, cancel=True)
            elif not writer.cancelled():
                writer.result()
        if not joined or not progress_joined:
            raise _lifecycle.OwnershipUnsettled("native reader or evidence writer remains unresolved")
        if not closer.cancelled():
            closer.result()
