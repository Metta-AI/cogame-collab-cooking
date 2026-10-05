"""A registered native container shares one absolute stop publication budget."""

import asyncio
from uuid import uuid4

import pytest

from collab_cooking.coworld import lifecycle
from collab_cooking.coworld.native_container import NativeContainer
from collab_cooking.coworld.plan_protocol import NativeProfile, NativeRegistration, PlayerConfiguration, Stop
from tests.harness import build_episode


@pytest.mark.asyncio
async def test_expired_stop_budget_does_not_publish_a_joined_ack():
    episode = build_episode({"layout": "open-kitchen", "max_steps": 900})
    sent = []

    async def send(raw):
        sent.append(raw)

    configuration = PlayerConfiguration(slot=0, alias="Cog-A", layout="open-kitchen", policy_env=episode.policy_env)
    container = NativeContainer(
        configuration,
        NativeRegistration(slot=0, prompt="strategy", profile=NativeProfile()),
        "http://127.0.0.1:8080",
        send,
    )
    container.deadline = asyncio.get_running_loop().time() - 1

    with pytest.raises(lifecycle.OwnershipUnsettled, match="remaining owner budget"):
        await container.receive(Stop(request_id=uuid4()).model_dump_json())
    assert sent == []


@pytest.mark.asyncio
async def test_stalled_socket_lock_is_bounded_before_any_publication():
    episode = build_episode({"layout": "open-kitchen", "max_steps": 900})
    sent = []

    async def send(raw):
        sent.append(raw)

    configuration = PlayerConfiguration(slot=0, alias="Cog-A", layout="open-kitchen", policy_env=episode.policy_env)
    container = NativeContainer(
        configuration,
        NativeRegistration(slot=0, prompt="strategy", profile=NativeProfile()),
        "http://127.0.0.1:8080",
        send,
    )
    await container.send_lock.acquire()
    container.deadline = asyncio.get_running_loop().time() + 0.02
    try:
        with pytest.raises(TimeoutError):
            await container.send(Stop(request_id=uuid4()))
        assert sent == []
    finally:
        container.send_lock.release()


@pytest.mark.asyncio
async def test_failed_native_owner_cannot_be_credited_as_a_successful_joined_stop():
    episode = build_episode({"layout": "open-kitchen", "max_steps": 900})
    sent = []

    async def send(raw):
        sent.append(raw)

    container = NativeContainer(
        PlayerConfiguration(slot=0, alias="Cog-A", layout="open-kitchen", policy_env=episode.policy_env),
        NativeRegistration(slot=0, prompt="strategy", profile=NativeProfile()),
        "http://127.0.0.1:8080",
        send,
    )

    async def failed_owner():
        raise RuntimeError("owned inference invariant failed")

    container.worker.task = asyncio.create_task(failed_owner())
    await asyncio.wait({container.worker.task})
    with pytest.raises(RuntimeError, match="owned inference invariant failed"):
        await container.receive(Stop(request_id=uuid4()).model_dump_json())
    assert sent == []
