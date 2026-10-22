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

import ast
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
    """Join backslash continuations and keep the first physical line number.

    A comment line never continues, even when its text ends with a backslash.
    pip treats such a line as a comment; joining it would swallow the next
    requirement and hide it from every policy check.
    """
    entries: list[tuple[int, str]] = []
    buffer: list[str] = []
    first = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not buffer and line.strip().startswith("#"):
            entries.append((number, line.strip()))
            continue
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


def normalize(name: str) -> str:
    """Normalize a package name as PEP 503 defines it."""
    return re.sub(r"[-_.]+", "-", name).lower()


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
        name = normalize(match.group("name"))
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
        name = normalize(match.group("name"))
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
    """Prove each marker false on all non-Windows environments.

    Parse the Python-compatible equality/boolean subset of PEP 508 with the
    standard-library AST. Only actual variable-to-literal Windows equalities
    supply proof; quoted text and all other comparisons remain unknown. An AND
    needs one false term, while an OR needs every term false. Unsupported syntax
    fails closed and requires a hashed entry. No marker text is executed, and the
    credential-free CI check needs no third-party bootstrap dependency.
    """

    def excludes_non_windows(node: ast.AST) -> bool:
        if isinstance(node, ast.BoolOp):
            proofs = [excludes_non_windows(value) for value in node.values]
            return any(proofs) if isinstance(node.op, ast.And) else all(proofs)
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            return False
        if not isinstance(node.ops[0], ast.Eq):
            return False
        left, right = node.left, node.comparators[0]
        if isinstance(right, ast.Name):
            left, right = right, left
        return (
            isinstance(left, ast.Name)
            and isinstance(right, ast.Constant)
            and (left.id, right.value)
            in (("sys_platform", "win32"), ("platform_system", "Windows"))
        )

    if not markers:
        return False
    for marker in markers:
        try:
            expression = ast.parse(marker, mode="eval")
        except (SyntaxError, ValueError, RecursionError):
            return False
        if not excludes_non_windows(expression.body):
            return False
    return True


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
