"""Exact native sampling modes preserve stored records and actual decoder attribution."""

import asyncio
import json

import pytest
from pydantic import ValidationError

from collab_cooking.coworld import native


def sampled(temperature=0.7):
    return {
        "policy_revision": "checkpoint-sha",
        "tokenizer_revision": "tokenizer-sha",
        "chat_template": "exact-template",
        "sampling": "full_softmax",
        "temperature": temperature,
        "enable_thinking": False,
        "max_new_tokens": 32,
        "max_sequence_length": 40,
        "sampling_seed": 7,
        "eos_token_ids": [2],
        "prompt_token_ids": [1],
        "completion_token_ids": [3, 2],
        "behavior_log_probs": [-0.5, -0.25],
        "response": "ordinary action",
        "stop_reason": "eos",
    }


def test_stored_unit_shape_and_distinct_scaled_draws():
    old = sampled()
    del old["temperature"]
    old["sampling"] = "full_softmax_temperature_one"
    assert native.SamplingEvidence.model_validate(old).model_dump() == old
    scaled = sampled()
    assert native.TemperedSamplingEvidence.model_validate(scaled).model_dump() == scaled
    with pytest.raises(ValidationError):
        native.SamplingEvidence.model_validate(scaled)
    with pytest.raises(ValidationError):
        native.TemperedSamplingEvidence.model_validate(old)


@pytest.mark.parametrize("temperature", [None, 0.0, -0.1, 2.1, float("nan"), float("inf"), True, "0.7"])
def test_scaled_temperature_is_required_positive_finite_and_strict(temperature):
    evidence = sampled(temperature)
    if temperature is None:
        del evidence["temperature"]
    with pytest.raises(ValidationError):
        native.TemperedSamplingEvidence.model_validate(evidence)


@pytest.mark.parametrize("probabilities", [None, [-0.5], [0.1, -0.25], [float("nan"), -0.25]])
def test_scaled_evidence_requires_actual_aligned_draw_probabilities(probabilities):
    with pytest.raises(ValidationError):
        native.TemperedSamplingEvidence.model_validate(sampled() | {"behavior_log_probs": probabilities})


@pytest.mark.asyncio
@pytest.mark.parametrize("served_temperature", [0.7, 1.0])
async def test_actual_native_http_retains_scaled_draws_and_rejects_decoder_mismatch(monkeypatch, served_temperature):
    captured = []
    handlers = set()
    wire = json.dumps(
        {
            "model": "checkpoint:owned",
            "content": [{"type": "text", "text": "ordinary action"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "sampling_evidence": sampled(served_temperature),
        }
    ).encode()

    async def serve(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = int(
                next(
                    line.split(b":", 1)[1]
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
            )
            captured.append(json.loads(await reader.readexactly(length)))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                + f"Content-Length: {len(wire)}\r\n\r\n".encode()
                + wire
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    monkeypatch.setenv("COWORLD_LLM_ENDPOINT", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
    request = native.Request(
        model="checkpoint:owned",
        max_tokens=32,
        temperature=0.7,
        top_p=1.0,
        system="exact instructions",
        messages=[native.Message(role="user", content="private-view")],
    )
    attempt = native.Attempt(slot=0, stage="plan", request=request)
    try:
        if served_temperature == 0.7:
            response = await native.complete(
                request, native.LearnerScope(slot=0, model=request.model), stage="plan", timeout=2.0, attempt=attempt
            )
            assert response.text == "ordinary action"
        else:
            with pytest.raises(native.ResponseRejected, match="decoder"):
                await native.complete(
                    request,
                    native.LearnerScope(slot=0, model=request.model),
                    stage="plan",
                    timeout=2.0,
                    attempt=attempt,
                )
        assert captured == [request.model_dump(exclude_none=True)]
        assert attempt.raw_response.encode() == wire
        assert attempt.sampling_evidence.model_dump() == sampled(served_temperature)
        assert attempt.response_complete is True
        assert attempt.response_reader_joined is True
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers)
