"""Exclusive profile/destination writer ownership across provider reloads."""

import os
from pathlib import Path

from .models import digest


class WriterBusyError(RuntimeError):
    """Report exclusive writer ownership without disclosing destination details."""


class WriterLease:
    """Hold an OS lock until every owned service and worker has finished."""

    @classmethod
    def for_config(cls, home, cfg, *, destination_policy=None):
        """Exclude writers to the same physical destination across mode/URL aliases."""
        from .config import backend_destination

        q = cfg["qdrant"]
        destination = backend_destination(q)
        if q["mode"] == "embedded":
            # Keep the writer lock identity stable across plugin upgrades.
            destination = (destination[0], destination[-1])
        from .shared_security import (
            admit_claim,
            destination_key,
            private_entry,
            registry_path,
            validate_destination,
        )

        # Every cooperating writer takes a shared account lock, so enrollment
        # cannot race a private writer that started before a claim existed.
        # Protected owners require the exclusive form for their entire lifetime.
        if os.name == "nt":
            if destination_policy is not None:
                validate_destination(home, cfg, destination_policy)
            return cls(home, [destination, q["collection"]])
        import portalocker

        if destination_policy is not None:
            validate_destination(home, cfg, destination_policy)
        try:
            root = registry_path()
        except KeyError:
            if destination_policy is not None:
                raise PermissionError(
                    "Protected mode requires a resolvable local account"
                ) from None
            return cls(home, [destination, q["collection"]])
        from .private_paths import checked_absolute_path

        checked_absolute_path(root)
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except PermissionError:
            if destination_policy is not None or root.exists():
                raise
            # An account with a read-only home cannot enroll a protected claim.
            # Preserve its legacy private workflow until an operator provisions
            # the registry offline, with all writers stopped.
            return cls(home, [destination, q["collection"]])
        private_entry(root, directory=True)
        lock_path = root / (destination_key(cfg) + ".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        account_lock = os.fdopen(descriptor, "a")
        lease = None
        try:
            private_entry(lock_path)
            flags = portalocker.LOCK_EX if destination_policy is not None else portalocker.LOCK_SH
            portalocker.lock(account_lock, flags | portalocker.LOCK_NB)
            admit_claim(home, cfg, destination_policy)
            lease = cls(home, [destination, q["collection"]])
            lease.account_lock = account_lock
            return lease
        except portalocker.LockException as exc:
            account_lock.close()
            raise WriterBusyError("Another local writer owns this destination") from exc
        except BaseException:
            account_lock.close()
            if lease is not None:
                lease.close()
            raise

    def __init__(self, home, namespace):
        """Acquire without waiting so a replacement cannot overlap an old writer."""
        # Imported lazily so provider discovery and registration stays free of
        # runtime dependencies before Hermes has prepared them.
        import portalocker

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
            raise WriterBusyError("Another writer still owns this profile destination") from exc

    def close(self):
        """Release ownership only after the owner's other cleanup callbacks."""
        self.file.close()
        if hasattr(self, "account_lock"):
            self.account_lock.close()
