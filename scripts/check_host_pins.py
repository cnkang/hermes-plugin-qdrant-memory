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

from packaging.requirements import InvalidRequirement, Requirement
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


def parse_requirement(spec: str, origin: str) -> Requirement:
    """Parse one dependency string, or stop with the offending entry named."""
    try:
        return Requirement(spec)
    except InvalidRequirement as exc:
        raise SystemExit(f"invalid dependency '{spec}' in {origin}: {exc}")


def reject_dynamic(project: dict, origin: str) -> None:
    """Stop when dependency metadata is dynamic, since it cannot be reconciled."""
    dynamic = project.get("dynamic") or []
    fields = ("dependencies", "optional-dependencies")
    if any(field in dynamic for field in fields):
        raise SystemExit(
            f"{origin} declares dynamic dependency metadata; this check needs static metadata"
        )


def applicable(requirement: Requirement) -> bool:
    """True when the requirement's environment marker matches this interpreter."""
    if requirement.marker is None:
        return True
    return requirement.marker.evaluate()


def group_specs(document: dict, name: str, seen: frozenset) -> list:
    """The dependency strings of one dependency group, including nested groups."""
    groups = document.get("dependency-groups") or {}
    specs = groups.get(name)
    if not isinstance(specs, list):
        return []
    collected = []
    for entry in specs:
        if isinstance(entry, str):
            collected.append(entry)
        elif isinstance(entry, dict) and "include-group" in entry:
            target = entry["include-group"]
            if target in seen:
                raise SystemExit(f"dependency group cycle in {name}: {target} repeats")
            collected.extend(group_specs(document, target, seen | {name}))
    return collected


def collect_host_pins(host: Path) -> dict:
    """Every dependency pin the host declares, keyed by canonical package name."""
    document = load_pyproject(host)
    project = document.get("project") or {}
    reject_dynamic(project, str(host))
    pins: dict = {}

    def add(specs: object, origin: str) -> None:
        if not isinstance(specs, list):
            return
        for spec in specs:
            if not isinstance(spec, str):
                continue
            requirement = parse_requirement(spec, origin)
            if requirement.url or canonicalize_name(requirement.name) == "hermes-agent":
                continue
            if not applicable(requirement):
                continue
            pins.setdefault(canonicalize_name(requirement.name), []).append((requirement, origin))

    add(project.get("dependencies"), "project.dependencies")
    for name, specs in sorted((project.get("optional-dependencies") or {}).items()):
        add(specs, f"optional-dependencies.{name}")
    for name in sorted(document.get("dependency-groups") or {}):
        add(group_specs(document, name, frozenset()), f"dependency-groups.{name}")
    return pins


def collect_plugin_specifiers(plugin: Path) -> dict:
    """The plugin's dependency specifiers from base dependencies and extras."""
    document = load_pyproject(plugin)
    project = document.get("project") or {}
    reject_dynamic(project, str(plugin))
    required: dict = {}

    def add(specs: object, origin: str) -> None:
        if not isinstance(specs, list):
            return
        for spec in specs:
            if not isinstance(spec, str):
                continue
            requirement = parse_requirement(spec, origin)
            if requirement.url:
                continue
            name = canonicalize_name(requirement.name)
            required.setdefault(name, []).append((requirement.specifier, origin))

    add(project.get("dependencies"), "project.dependencies")
    for name, specs in sorted((project.get("optional-dependencies") or {}).items()):
        add(specs, f"optional-dependencies.{name}")
    return required


def strip_arbitrary(specifier_set: SpecifierSet) -> SpecifierSet:
    """Drop arbitrary-equality specifiers, which compare raw strings."""
    return SpecifierSet(
        ",".join(str(specifier) for specifier in specifier_set if specifier.operator != "===")
    )


def version_literals(specifier_set: SpecifierSet) -> list:
    """The version literals the specifiers declare; a wildcard yields its base."""
    literals = []
    for specifier in specifier_set:
        raw = specifier.version
        if raw.endswith(".*"):
            raw = raw[:-2]
        try:
            literals.append(Version(raw))
        except InvalidVersion:
            continue
    return literals


def successor(version: Version) -> Version | None:
    """A version just above the given one; None when it cannot be constructed."""
    base = ".".join(str(part) for part in version.release)
    try:
        return Version(f"{base}.1")
    except InvalidVersion:
        return None


def probe_versions(*sets: SpecifierSet) -> list:
    """Versions to test for overlap: zero, each declared literal and its successor."""
    probes = [Version("0"), Version("0.post1")]
    for specifier_set in sets:
        for literal in version_literals(specifier_set):
            probes.append(literal)
            follow = successor(literal)
            if follow is not None:
                probes.append(follow)
    return probes


def exact_pins(specifier_set: SpecifierSet) -> list:
    """The exact versions a specifier set pins, when it pins them."""
    pins = []
    for specifier in specifier_set:
        if specifier.operator == "==" and "*" not in specifier.version:
            try:
                pins.append(Version(specifier.version))
            except InvalidVersion:
                continue
    return pins


def compatible(plugin_spec: SpecifierSet, host_spec: SpecifierSet) -> tuple:
    """Whether some version satisfies both sets, with a reason when not."""
    plugin_reasonable = strip_arbitrary(plugin_spec)
    host_reasonable = strip_arbitrary(host_spec)
    if not list(host_reasonable) or not list(plugin_reasonable):
        return True, "no comparable version constraint; skipped"
    exact = exact_pins(host_reasonable)
    if exact:
        missing = [str(version) for version in exact if version not in plugin_reasonable]
        if missing:
            return False, f"host pins {', '.join(missing)} outside the plugin's range"
        return True, ""
    probes = probe_versions(plugin_reasonable, host_reasonable)
    if any(version in plugin_reasonable and version in host_reasonable for version in probes):
        return True, ""
    return False, "no version satisfies both specifiers"


def plugin_requirement(plugin: Path) -> str:
    """The plugin requirement line for a co-resolution build."""
    document = load_pyproject(plugin)
    project = document.get("project") or {}
    name = project.get("name")
    if not isinstance(name, str) or not name:
        raise SystemExit(f"no [project].name under {plugin}")
    extras = sorted(project.get("optional-dependencies") or {})
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

    if args.print_plugin_requirement and not args.emit_requirements:
        print(plugin_requirement(args.plugin))
        return 0

    host_pins = collect_host_pins(args.host)
    required = collect_plugin_specifiers(args.plugin)
    failures: list = []
    notes: list = []
    shared: list = []
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
                if reason:
                    notes.append(f"{name}: host '{requirement}' ({host_origin}): {reason}")
            else:
                shared.append(requirement)

    for name in sorted({canonicalize_name(requirement.name) for requirement in shared}):
        versions: dict = {}
        for requirement, origin in host_pins.get(name, []):
            for specifier in requirement.specifier:
                if specifier.operator == "==" and "*" not in specifier.version:
                    versions.setdefault(specifier.version, []).append(origin)
        if len(versions) > 1:
            detail = "; ".join(
                f"{version} ({', '.join(origins)})" for version, origins in sorted(versions.items())
            )
            failures.append(f"{name}: host declares inconsistent exact pins: {detail}")

    if failures:
        for line in failures:
            print(f"host pin conflict: {line}", file=sys.stderr)
        return 1
    for line in notes:
        print(f"note: {line}", file=sys.stderr)

    messages = []
    if args.emit_requirements:
        seen = set()
        lines = []
        for requirement in shared:
            text = str(requirement)
            if text not in seen:
                seen.add(text)
                lines.append(text)
        args.emit_requirements.write_text("\n".join(lines) + "\n", encoding="utf-8")
        messages.append(f"wrote {len(lines)} host pin(s) to {args.emit_requirements}")
    messages.append(
        f"host pin compatibility: OK ({len({str(r) for r in shared})} shared pin(s) checked)"
    )
    stream = sys.stderr if args.print_plugin_requirement else sys.stdout
    for message in messages:
        print(message, file=stream)
    if args.print_plugin_requirement:
        print(plugin_requirement(args.plugin))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
