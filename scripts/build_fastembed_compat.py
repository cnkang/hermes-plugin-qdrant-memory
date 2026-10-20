"""Build the temporary, reproducible CI wheel until FastEmbed releases Pillow 13 support.

Upstream tracking: https://github.com/qdrant/fastembed/pull/830
Remove this builder, the local-version allowance and CI find-links together after
an official release passes the same CPU model and dependency-security gates.
Only distribution metadata changes; every inference/model file stays byte-identical.
"""

import argparse
import base64
import csv
import hashlib
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

BASE_VERSION = "0.9.0"
COMPAT_VERSION = "0.9.0+qm.pillow13"
SOURCE_URL = (
    "https://files.pythonhosted.org/packages/84/bc/21791fa8b16c6f5f8e2717f8defab377e74c1"
    "ccc8687180b7224907e7641/fastembed-0.9.0-py3-none-any.whl"
)
SOURCE_SHA256 = "273d408edec8c0f161711d8f6e44e4a5b559d18e8edf6bf805415d55dc772846"
MAX_SOURCE_BYTES = 5 * 1024 * 1024
WHEEL_NAME = f"fastembed-{COMPAT_VERSION}-py3-none-any.whl"


class HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the credential-free upstream download on verified HTTPS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Reject insecure redirects before opening the next connection."""
        if urlsplit(newurl).scheme != "https":
            raise ValueError("Wheel download requires HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_wheel(source):
    """Verify official bytes, patch only metadata, and rebuild RECORD deterministically."""
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise ValueError("Official FastEmbed wheel checksum mismatch")
    old = f"fastembed-{BASE_VERSION}.dist-info/"
    new = f"fastembed-{COMPAT_VERSION}.dist-info/"
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        files = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    metadata = files[old + "METADATA"].decode("utf-8")
    pillow = [line for line in metadata.splitlines() if line.startswith("Requires-Dist: pillow ")]
    if len(pillow) != 3 or any("<13.0" not in line for line in pillow):
        raise ValueError("Unexpected upstream Pillow dependency metadata")
    if metadata.count(f"\nVersion: {BASE_VERSION}\n") != 1:
        raise ValueError("Unexpected upstream package version")
    metadata = metadata.replace(f"\nVersion: {BASE_VERSION}\n", f"\nVersion: {COMPAT_VERSION}\n")
    for line in pillow:
        metadata = metadata.replace(line, line.replace("<13.0", "<14.0"))
    files[old + "METADATA"] = metadata.encode("utf-8")
    # A local version identifies the compatibility build; it is not an upstream release.
    files = {
        name.replace(old, new, 1): data for name, data in files.items() if name != old + "RECORD"
    }
    files[new + "qdrant_memory_compat.json"] = (
        json.dumps(
            {
                "upstream_url": SOURCE_URL,
                "upstream_sha256": SOURCE_SHA256,
                "change": "Pillow upper bound <13.0 -> <14.0; inference files unchanged",
                "tracking": "https://github.com/qdrant/fastembed/pull/830",
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + digest, len(data)])
    writer.writerow([new + "RECORD", "", ""])
    files[new + "RECORD"] = record.getvalue().encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            # Fixed timestamps, permissions and no compression make hashes OS-independent.
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output.getvalue()


def main():
    """Build a hash-checked wheel before resolving either CI dependency snapshot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-wheel", type=Path)
    parser.add_argument("--check-lock", type=Path)
    args = parser.parse_args()
    try:
        if args.source_wheel:
            if args.source_wheel.stat().st_size > MAX_SOURCE_BYTES:
                raise ValueError("Source wheel exceeds size limit")
            source = args.source_wheel.read_bytes()
        else:
            opener = urllib.request.build_opener(HTTPSOnlyRedirect())
            with opener.open(SOURCE_URL, timeout=30) as response:
                source = response.read(MAX_SOURCE_BYTES + 1)
        if len(source) > MAX_SOURCE_BYTES:
            raise ValueError("Source wheel exceeds size limit")
        wheel = build_wheel(source)
        digest = hashlib.sha256(wheel).hexdigest()
        if args.check_lock and f"--hash=sha256:{digest}" not in args.check_lock.read_text():
            raise ValueError("Compatibility wheel hash missing from CPU security snapshot")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / WHEEL_NAME).write_bytes(wheel)
        print(f"{WHEEL_NAME} sha256:{digest}")
        return 0
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Compatibility build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
