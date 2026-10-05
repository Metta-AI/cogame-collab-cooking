"""DNS lookup uses an owned process, never an unjoined executor thread."""

import asyncio
import ipaddress
import socket
import sys

from pydantic import BaseModel, ConfigDict, StrictInt, TypeAdapter

from . import lifecycle

MAX_DNS_BYTES = 65536
LOOKUP_SECONDS = 20.0
LOOKUP = """
import json,socket,sys
rows = list(dict.fromkeys((family, address[0]) for family, kind, proto, canon, address
                        in socket.getaddrinfo(sys.argv[1], int(sys.argv[2]),
                                              type=socket.SOCK_STREAM)))
body = json.dumps([{"family": family, "address": address} for family,address in rows])
if len(body.encode()) > 65536:
    raise ValueError("DNS lookup exceeds declared capacity")
print(body, end="")
"""


class Address(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    family: StrictInt
    address: str


ADDRESSES = TypeAdapter(list[Address], config=ConfigDict(hide_input_in_errors=True))


class OwnedResolver:
    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        if not host or len(host) > 253:
            raise ValueError("DNS hostname exceeds the runtime authority contract")
        deadline = asyncio.get_running_loop().time() + LOOKUP_SECONDS
        child: asyncio.subprocess.Process | None = None
        tasks: set[asyncio.Task] = set()
        stdout: asyncio.Task | None = None
        stderr: asyncio.Task | None = None
        exited: asyncio.Task | None = None

        async def acquire() -> None:
            nonlocal child, stdout, stderr, exited
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-c",
                LOOKUP,
                host,
                str(port),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                close_fds=True,
            )
            assert child.stdout is not None and child.stderr is not None
            stdout = lifecycle.owned_task(child.stdout.read())
            stderr = lifecycle.owned_task(child.stderr.read())
            exited = lifecycle.owned_task(child.wait())
            tasks.update({stdout, stderr, exited})

        # Creation itself owns an unobserved OS process until its Process returns.
        # Keep it shielded from the ancestor's cancellation sweep; this resolver
        # must observe acquisition, then terminate and join the actual child.
        creation = asyncio.create_task(acquire())
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.shield(creation)
            assert stdout is not None and stderr is not None and exited is not None
            if not await lifecycle.settle(tasks, deadline, cancel=False):
                raise TimeoutError("DNS owner exceeded its lookup deadline")
            if exited.result() != 0:
                raise RuntimeError("owned DNS lookup process failed")
            body = stdout.result()
            if len(body) > MAX_DNS_BYTES or len(stderr.result()) > MAX_DNS_BYTES:
                raise ValueError("owned DNS lookup exceeded receive capacity")
            rows = ADDRESSES.validate_json(body)
            answers = []
            for row in rows:
                address = ipaddress.ip_address(row.address)
                actual_family = socket.AF_INET if address.version == 4 else socket.AF_INET6
                if row.family != actual_family:
                    raise ValueError("DNS family differs from actual numeric address")
                if family in (socket.AF_UNSPEC, row.family):
                    answers.append(
                        {
                            "hostname": host,
                            "host": str(address),
                            "port": port,
                            "family": row.family,
                            "proto": socket.IPPROTO_TCP,
                            "flags": socket.AI_NUMERICHOST,
                        }
                    )
            if not answers:
                raise ValueError("owned DNS lookup produced no permitted address")
            return answers
        finally:
            cleanup = lifecycle.cleanup_deadline()
            if not await lifecycle.settle({creation}, cleanup, cancel=False):
                raise lifecycle.OwnershipUnsettled("DNS process acquisition did not settle")
            creation.result()
            assert child is not None
            if child.returncode is None:
                child.kill()
            if not await lifecycle.settle(tasks, cleanup, cancel=False):
                raise lifecycle.OwnershipUnsettled("DNS child or pipe reader did not join")

    async def close(self) -> None:
        # Every resolve owns and joins its own subprocess and pipe readers.
        return None
