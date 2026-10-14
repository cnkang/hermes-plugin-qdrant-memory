#!/usr/bin/env python3
"""Reconcile the tested Hermes host's dependency pins with this plugin's specifiers.

The dependency manager resolves this plugin inside the host workspace. Every
pin the host declares for a package this plugin also requires must satisfy the
plugin's specifier, or the workspace lock fails. This script checks that
invariant against a host checkout and can emit the host pins for a
co-resolution build.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


def load_pyproject(directory: Path) -> dict:
    """Read one pyproject.toml as a dictionary."""
    path = directory / "pyproject.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SystemExit(f"no pyproject.toml under {directory}")
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"invalid pyproject.toml under {directory}: {exc}")


def applicable(requirement: Requirement) -> bool:
    """True when the requirement's environment marker matches this interpreter."""
    if requirement.marker is None:
        return True
    return requirement.marker.evaluate()


def collect_host_pins(host: Path) -> dict[str, list[tuple[Requirement, str]]]:
    """Every dependency pin the host declares, keyed by canonical package name."""
    document = load_pyproject(host)
    project = document.get("project", {})
    pins: dict[str, list[tuple[Requirement, str]]] = {}

    def add(specs: object, origin: str) -> None:
        if not isinstance(specs, list):
            return
        for spec in specs:
            if not isinstance(spec, str):
                continue
            requirement = Requirement(spec)
            if requirement.url or canonicalize_name(requirement.name) == "hermes-agent":
                continue
            if not applicable(requirement):
                continue
            pins.setdefault(canonicalize_name(requirement.name), []).append((requirement, origin))

    add(project.get("dependencies"), "project.dependencies")
    for name, specs in sorted((project.get("optional-dependencies") or {}).items()):
        add(specs, f"optional-dependencies.{name}")
    for name, specs in sorted((document.get("dependency-groups") or {}).items()):
        add(specs, f"dependency-groups.{name}")
    return pins


def collect_plugin_specifiers(plugin: Path) -> dict[str, list[tuple[SpecifierSet, str]]]:
    """The plugin's dependency specifiers from base dependencies and extras."""
    document = load_pyproject(plugin)
    project = document.get("project", {})
    required: dict[str, list[tuple[SpecifierSet, str]]] = {}

    def add(specs: object, origin: str) -> None:
        if not isinstance(specs, list):
            return
        for spec in specs:
            if not isinstance(spec, str):
                continue
            requirement = Requirement(spec)
            if requirement.url:
                continue
            name = canonicalize_name(requirement.name)
            required.setdefault(name, []).append((requirement.specifier, origin))

    add(project.get("dependencies"), "project.dependencies")
    for name, specs in sorted((project.get("optional-dependencies") or {}).items()):
        add(specs, f"optional-dependencies.{name}")
    return required


def exact_pins(specifier_set: SpecifierSet) -> list[Version]:
    """The exact versions a specifier set pins, when it pins them."""
    versions = []
    for specifier in specifier_set:
        if specifier.operator in ("==", "===") and "*" not in specifier.version:
            try:
                versions.append(Version(specifier.version))
            except InvalidVersion:
                continue
    return versions


def candidate_versions(*sets: SpecifierSet) -> list[Version]:
    """Version literals that appear in the given specifier sets."""
    versions = []
    for specifier_set in sets:
        for specifier in specifier_set:
            if "*" in specifier.version:
                continue
            try:
                versions.append(Version(specifier.version))
            except InvalidVersion:
                continue
    return versions


def compatible(plugin_spec: SpecifierSet, host_spec: SpecifierSet) -> tuple[bool, str]:
    """Whether some version satisfies both sets, with a reason when not."""
    exact = exact_pins(host_spec)
    if exact:
        missing = [str(version) for version in exact if version not in plugin_spec]
        if missing:
            return False, f"host pins {', '.join(missing)} outside the plugin's range"
        return True, ""
    candidates = candidate_versions(plugin_spec, host_spec)
    if not candidates:
        return True, "no comparable version literal; skipped"
    if any(version in plugin_spec and version in host_spec for version in candidates):
        return True, ""
    return False, "no version satisfies both specifiers"


def plugin_requirement(plugin: Path) -> str:
    """The plugin requirement line for a co-resolution build."""
    document = load_pyproject(plugin)
    name = document["project"]["name"]
    extras = sorted(document.get("project", {}).get("optional-dependencies") or {})
    suffix = f"[{','.join(extras)}]" if extras else ""
    return f"{name}{suffix} @ file://{plugin.resolve()}"


def main() -> int:
    """Run the reconciliation, optionally emitting pins for a co-resolution build."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True, help="Hermes host checkout to inspect")
    parser.add_argument(
        "--plugin",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Plugin checkout (default: this repository)",
    )
    parser.add_argument(
        "--emit-requirements",
        type=Path,
        help="Write the host pins for shared packages to this file",
    )
    parser.add_argument(
        "--print-plugin-requirement",
        action="store_true",
        help="Print the plugin requirement line for a co-resolution build",
    )
    args = parser.parse_args()

    if args.print_plugin_requirement:
        print(plugin_requirement(args.plugin))
        return 0

    host_pins = collect_host_pins(args.host)
    required = collect_plugin_specifiers(args.plugin)
    failures = []
    shared: list[Requirement] = []
    for name, specifiers in sorted(required.items()):
        for requirement, host_origin in host_pins.get(name, []):
            for specifier, origin in specifiers:
                ok, reason = compatible(specifier, requirement.specifier)
                if not ok:
                    failures.append(
                        f"{name}: host '{requirement}' ({host_origin}) conflicts with "
                        f"plugin '{specifier}' ({origin}): {reason}"
                    )
                    break
            else:
                shared.append(requirement)
    if failures:
        for line in failures:
            print(f"host pin conflict: {line}", file=sys.stderr)
        return 1
    if args.emit_requirements:
        seen = set()
        lines = []
        for requirement in shared:
            text = str(requirement)
            if text not in seen:
                seen.add(text)
                lines.append(text)
        args.emit_requirements.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"wrote {len(lines)} host pin(s) to {args.emit_requirements}")
    print("host pin compatibility: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
