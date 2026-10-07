"""Exclusive profile/destination writer ownership across provider reloads."""

import os
from pathlib import Path

import portalocker

from .models import digest


class WriterLease:
    """Hold an OS lock until every owned service and worker has finished."""

    @classmethod
    def for_config(cls, home, cfg):
        """Exclude writers to the same physical destination across mode/URL aliases."""
        from .config import backend_destination

        q = cfg["qdrant"]
        destination = backend_destination(q)
        if q["mode"] == "embedded":
            # Keep the writer lock identity stable across plugin upgrades.
            destination = (destination[0], destination[-1])
        return cls(home, [destination, q["collection"]])

    def __init__(self, home, namespace):
        """Acquire without waiting so a replacement cannot overlap an old writer."""
        directory = Path(home) / "qdrant-memory"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            directory / (digest(namespace) + ".writer.lock"), os.O_CREAT | os.O_RDWR, 0o600
        )
        self.file = os.fdopen(descriptor, "a")
        try:
            portalocker.lock(self.file, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.LockException as exc:
            self.file.close()
            raise RuntimeError("Another writer still owns this profile destination") from exc

    def close(self):
        """Release ownership only after the owner's other cleanup callbacks."""
        self.file.close()
