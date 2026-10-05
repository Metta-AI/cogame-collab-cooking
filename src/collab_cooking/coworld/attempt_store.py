"""Private received attempts remain on an owned spool, indexed by call identity."""

import tempfile
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from uuid import UUID

from .native import Attempt


class AttemptStore(MutableMapping[UUID, Attempt]):
    """Keep only offsets in memory; each lookup validates one actual attempt."""

    def __init__(self):
        self.stream = tempfile.NamedTemporaryFile(mode="w+b", prefix="collab-private-attempt-", delete=False)
        self.path = Path(self.stream.name)
        self.offsets: dict[UUID, tuple[int, int]] = {}

    def __getitem__(self, identity: UUID) -> Attempt:
        offset, size = self.offsets[identity]
        if self.stream.closed:
            with self.path.open("rb") as reader:
                reader.seek(offset)
                data = reader.read(size)
        else:
            self.stream.seek(offset)
            data = self.stream.read(size)
        return Attempt.model_validate_json(data)

    def __setitem__(self, identity: UUID, attempt: Attempt) -> None:
        if str(identity) != attempt.attempt_id:
            raise ValueError("attempt spool identity differs from received call")
        data = attempt.model_dump_json().encode("utf-8")
        self.stream.seek(0, 2)
        offset = self.stream.tell()
        self.stream.write(data)
        self.stream.flush()
        self.offsets[identity] = (offset, len(data))

    def __delitem__(self, identity: UUID) -> None:
        del self.offsets[identity]

    def __iter__(self) -> Iterator[UUID]:
        return iter(self.offsets)

    def __len__(self) -> int:
        return len(self.offsets)

    def close_joined(self) -> None:
        self.stream.close()
