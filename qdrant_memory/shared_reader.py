"""Read-only access to explicitly authorized shared spaces.

The reader is additive: it never opens storage, never embeds, never writes and is not
imported by the Native Provider. Each credential is bound to exactly one space by the
Owner; the reader only validates and merges what the Owner already authorized.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from .shared_protocol import MARKER, VISIBILITIES, validate_loopback_url

MAX_LIMIT = 100
MAX_ROWS_PER_SPACE = 256
MAX_TEXT_BYTES = 65_536  # matches the Owner's max_text_bytes writer limit


@dataclass(frozen=True)
class SharedCredential:
    """One grant: a loopback URL, a high-entropy token and the space it unlocks."""

    url: str
    token: str = field(repr=False)
    space_id: str
    visibility: str = "shared"

    def __post_init__(self):
        """Reject incomplete or non-loopback credentials at construction."""
        validate_loopback_url(self.url)
        if not isinstance(self.space_id, str) or not self.space_id.strip():
            raise ValueError("Shared space must be nonempty")
        if self.visibility not in VISIBILITIES:
            raise ValueError("Unsupported shared visibility")
        if not isinstance(self.token, str) or len(self.token) < 32:
            raise ValueError("A high-entropy shared credential is required")

    def __repr__(self):
        """Never print the token, even in diagnostics."""
        return (
            f"SharedCredential(url={self.url!r}, token='<redacted>', "
            f"space_id={self.space_id!r}, visibility={self.visibility!r})"
        )


@dataclass(frozen=True)
class SharedHit:
    """A record with the provenance the model context must keep."""

    memory_id: str
    text: str = field(repr=False)
    score: float
    space_id: str
    visibility: str
    created_by: str | None = None
    updated_by: str | None = None
    version: str | None = None

    def __repr__(self):
        """Never print memory text, even in diagnostics."""
        return (
            f"SharedHit(memory_id={self.memory_id!r}, text='<redacted>', "
            f"score={self.score!r}, space_id={self.space_id!r}, "
            f"visibility={self.visibility!r}, version={self.version!r})"
        )


def _default_call():
    from .shared_client import call

    return call


def _validate_limit(value: object, label: str) -> int:
    """Accept a plain integer in range; reject booleans and other types."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} is out of range")
    if not 1 <= value <= MAX_LIMIT:
        raise ValueError(f"{label} is out of range")
    return value


class SharedReader:
    """Merge authorized spaces without widening what any credential can see."""

    def __init__(
        self,
        credentials: Iterable[SharedCredential],
        *,
        call: Callable[[str, str, str, dict], dict] | None = None,
        allow_private: bool = False,
        limit: int = 8,
    ):
        """Bind one credential per space and refuse duplicate or private grants."""
        if not isinstance(allow_private, bool):
            raise ValueError("allow_private must be a boolean")
        items = tuple(credentials)
        if not items:
            raise ValueError("At least one shared credential is required")
        spaces = [item.space_id for item in items]
        if len(set(spaces)) != len(spaces):
            raise ValueError("Each shared space must have exactly one credential")
        if not allow_private and any(item.visibility == "private" for item in items):
            raise ValueError("Private spaces require explicit opt-in")
        self._limit = _validate_limit(limit, "Shared result limit")
        self._credentials = {item.space_id: item for item in items}
        self._call = call or _default_call()
        self._allow_private = allow_private

    @property
    def spaces(self) -> tuple[str, ...]:
        """Spaces this reader may query."""
        return tuple(sorted(self._credentials))

    def search(
        self,
        query: str,
        *,
        spaces: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[SharedHit]:
        """Query only allowed spaces and re-validate every returned record."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Shared query must be nonempty")
        requested = self._requested(spaces)
        cap = self._limit if limit is None else _validate_limit(limit, "Shared result limit")

        best: dict[tuple[str, str], SharedHit] = {}
        for space in requested:
            credential = self._credentials[space]
            if credential.visibility == "private" and not self._allow_private:
                continue
            for hit in self._fetch(credential, query):
                key = (hit.space_id, hit.memory_id)
                current = best.get(key)
                if current is None or hit.score > current.score:
                    best[key] = hit
        hits = sorted(best.values(), key=lambda item: item.score, reverse=True)
        return hits[:cap]

    def _requested(self, spaces: Sequence[str] | None) -> tuple[str, ...]:
        """Reject malformed, duplicated or unauthorized space selections."""
        if spaces is None:
            return self.spaces
        if isinstance(spaces, (str, bytes)) or not isinstance(spaces, Sequence):
            raise ValueError("Shared spaces must be a sequence of names")
        requested = tuple(spaces)
        if not requested:
            raise ValueError("At least one shared space is required")
        for space in requested:
            if not isinstance(space, str):
                raise ValueError("Shared space names must be strings")
        if len(set(requested)) != len(requested):
            raise ValueError("Shared spaces must be unique")
        for space in requested:
            if space not in self._credentials:
                raise ValueError("Shared space is not authorized for this reader")
        return requested

    def _fetch(self, credential: SharedCredential, query: str) -> list[SharedHit]:
        """Call one granted space and keep only records it may carry."""
        response = self._call(
            credential.url,
            credential.token,
            "qdrant_memory_search",
            {"query": query},
        )
        if not isinstance(response, dict) or "error" in response:
            raise ValueError("Shared search failed")
        rows = response.get("memories")
        if not isinstance(rows, list):
            raise ValueError("Shared search response is malformed")
        hits: list[SharedHit] = []
        for row in rows[:MAX_ROWS_PER_SPACE]:
            hit = self._validated(row, credential)
            if hit is not None:
                hits.append(hit)
        return hits

    @staticmethod
    def _validated(row: object, credential: SharedCredential) -> SharedHit | None:
        """Drop anything the Owner did not mark for this exact space and visibility."""
        if not isinstance(row, dict):
            return None
        if row.get("shared_protocol") != MARKER:
            return None
        if row.get("space_id") != credential.space_id:
            return None
        if row.get("visibility") != credential.visibility:
            return None
        text = row.get("text")
        memory_id = row.get("id")
        score = row.get("score")
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            byte_size = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            # An unbalanced surrogate cannot come from a real record.
            return None
        if byte_size > MAX_TEXT_BYTES:
            return None
        if not isinstance(memory_id, str) or not memory_id:
            return None
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return None
        try:
            value = float(score)
        except (OverflowError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        created_by = row.get("created_by")
        updated_by = row.get("updated_by")
        version = row.get("version")
        return SharedHit(
            memory_id=memory_id,
            text=text,
            score=value,
            space_id=credential.space_id,
            visibility=credential.visibility,
            created_by=created_by if isinstance(created_by, str) else None,
            updated_by=updated_by if isinstance(updated_by, str) else None,
            version=version if isinstance(version, str) and version else None,
        )
