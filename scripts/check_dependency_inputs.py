#!/usr/bin/env python3
"""Verify the dependency inputs are exact, hash-pinned and locally sourced.

The organization Snyk token must never reach a process that runs code from a
contributor-controlled tree. Dependency scanning with that token therefore runs
only from ``main``. Pull requests do not run it, so this check keeps the
dependency inputs reviewable without credentials:

* ``requirements.txt`` entries are exact ``name==version`` pins, optionally with
  an environment marker. Ranges, URLs, VCS references and option lines fail.
* ``requirements-local.txt`` entries carry at least one ``--hash=sha256:`` value
  and no index, find-links, editable or remote-source option.
* A name present in both files keeps the same version, and every unmarked base
  pin appears in the hashed snapshot.

Usage: ``python3 scripts/check_dependency_inputs.py [repo root]``.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[(?P<extras>[A-Za-z0-9._,-]+)\])?"
    r"==(?P<version>[A-Za-z0-9._+!-]+)$"
)
HASH = re.compile(r"^--hash=sha256:([0-9a-f]{64})$")
COMMENT = re.compile(r"\s+#.*$")
WINDOWS = re.compile(
    r"(sys_platform\s*==\s*['\"]win32['\"]|platform_system\s*==\s*['\"]Windows['\"])",
    re.IGNORECASE,
)
FORBIDDEN = (
    "://",
    "git+",
    "file:",
    " @ ",
    "#egg",
    "-e ",
    "--index-url",
    "--extra-index-url",
    "--find-links",
    "-r ",
    "-c ",
)


class Failure(Exception):
    """A dependency input violates the policy."""


@dataclass
class Pin:
    """One base requirement with every pinned version and marker."""

    versions: set[str] = field(default_factory=set)
    markers: list[str] = field(default_factory=list)
    unmarked: bool = False


def logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash continuations and keep the first physical line number."""
    entries: list[tuple[int, str]] = []
    buffer: list[str] = []
    first = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        continued = line.endswith("\\")
        part = line[:-1] if continued else line
        if not buffer:
            first = number
        buffer.append(part)
        if not continued:
            entries.append((first, " ".join(buffer).strip()))
            buffer = []
    if buffer:
        entries.append((first, " ".join(buffer).strip()))
    return entries


def content(line: str) -> str:
    """Drop a trailing comment and surrounding whitespace."""
    return COMMENT.sub("", line).strip()


def split_marker(line: str) -> tuple[str, str]:
    """Split a requirement from its environment marker."""
    if ";" not in line:
        return line.strip(), ""
    requirement, marker = line.split(";", 1)
    return requirement.strip(), marker.strip()


def parse_pins(path: Path) -> dict[str, Pin]:
    """Return each requirement with its pinned versions and environment markers."""
    pins: dict[str, Pin] = {}
    for number, raw in logical_lines(path.read_text(encoding="utf-8")):
        line = content(raw)
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            raise Failure(f"{path.name}:{number}: option lines are not allowed: {line}")
        requirement, marker = split_marker(line)
        for token in FORBIDDEN:
            if token in requirement:
                raise Failure(
                    f"{path.name}:{number}: source or option is not allowed: {requirement}"
                )
        match = REQUIREMENT.match(requirement)
        if not match:
            raise Failure(
                f"{path.name}:{number}: expected an exact name==version pin, got: {requirement}"
            )
        name = match.group("name").lower()
        pin = pins.setdefault(name, Pin())
        if not marker:
            if pin.unmarked:
                raise Failure(f"{path.name}:{number}: duplicate unpinned requirement: {name}")
            pin.unmarked = True
        pin.versions.add(match.group("version"))
        pin.markers.append(marker)
    if not pins:
        raise Failure(f"{path.name}: no requirements found")
    return pins


def parse_hashed(path: Path) -> dict[str, str]:
    """Return requirement name to version, requiring a sha256 hash on each entry."""
    hashed: dict[str, str] = {}
    for number, raw in logical_lines(path.read_text(encoding="utf-8")):
        line = content(raw)
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            raise Failure(f"{path.name}:{number}: option lines are not allowed: {line}")
        parts = line.split()
        requirement, options = parts[0], parts[1:]
        for token in FORBIDDEN:
            if token in line:
                raise Failure(f"{path.name}:{number}: source or option is not allowed: {line}")
        match = REQUIREMENT.match(requirement)
        if not match:
            raise Failure(
                f"{path.name}:{number}: expected an exact name==version pin, got: {requirement}"
            )
        hashes = [HASH.match(option) for option in options]
        if not options or not all(hashes):
            raise Failure(f"{path.name}:{number}: every entry needs --hash=sha256:<64 hex> values")
        name = match.group("name").lower()
        if name in hashed:
            raise Failure(f"{path.name}:{number}: duplicate requirement: {name}")
        hashed[name] = match.group("version")
    if not hashed:
        raise Failure(f"{path.name}: no requirements found")
    return hashed


def check(root: Path) -> None:
    """Run every policy check against the repository root."""
    base = parse_pins(root / "requirements.txt")
    local = parse_hashed(root / "requirements-local.txt")

    for name, pin in base.items():
        if name in local:
            if local[name] not in pin.versions:
                raise Failure(
                    f"requirements-local.txt: {name}=={local[name]} does not match "
                    f"requirements.txt ({', '.join(sorted(pin.versions))})"
                )
        elif not windows_only(pin.markers):
            raise Failure(f"requirements-local.txt is missing the hashed entry for {name}")


def windows_only(markers: list[str]) -> bool:
    """Return whether every marker positively restricts the requirement to Windows."""
    return bool(markers) and all(
        "!=" not in marker and WINDOWS.search(marker) for marker in markers
    )


def main() -> int:
    """Validate the dependency inputs and report the first violation."""
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1])
    try:
        check(root)
    except Failure as error:
        print(f"dependency input check failed: {error}", file=sys.stderr)
        return 1
    print("dependency inputs are exact, hash-pinned and locally sourced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
