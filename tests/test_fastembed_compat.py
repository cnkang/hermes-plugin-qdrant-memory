"""Require an auditable temporary Pillow compatibility build, not ignored metadata."""

import base64
import csv
import hashlib
import importlib.util
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_fastembed_compat.py"


def test_untrusted_source_wheel_is_rejected_before_building(tmp_path):
    """A local source path must not bypass the pinned upstream checksum."""
    source = tmp_path / "untrusted.whl"
    source.write_bytes(b"not the pinned official FastEmbed wheel")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source-wheel",
            str(source),
            "--output-dir",
            str(tmp_path / "wheels"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "checksum mismatch" in result.stderr
    assert not list((tmp_path / "wheels").glob("*.whl"))


@pytest.fixture
def compat():
    """Load the stdlib-only builder without executing its CLI."""
    spec = importlib.util.spec_from_file_location("fastembed_compat", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_fixture(pillow_cap="<13.0"):
    """Provide inference bytes and three distinct upstream dependency markers."""
    output = io.BytesIO()
    metadata = "Metadata-Version: 2.4\nName: fastembed\nVersion: 0.9.0\n"
    metadata += "Requires-Dist: huggingface-hub (>=0.20,<2.0)\n"
    for marker in (
        'python_version < "3.13"',
        'python_version == "3.13"',
        'python_version >= "3.14"',
    ):
        metadata += f"Requires-Dist: pillow (>=10.3.0,{pillow_cap}); {marker}\n"
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("fastembed/text/model.py", b"# unchanged inference bytes\n")
        archive.writestr("fastembed-0.9.0.dist-info/METADATA", metadata)
        archive.writestr("fastembed-0.9.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        archive.writestr("fastembed-0.9.0.dist-info/RECORD", "old record\n")
        archive.writestr("fastembed-0.9.0.dist-info/LICENSE", "upstream license\n")
    return output.getvalue()


def test_metadata_patch_preserves_code_license_other_dependencies_and_valid_record(
    compat, monkeypatch
):
    """Do not silently alter inference or hide unrelated dependency incompatibilities."""
    source = source_fixture()
    monkeypatch.setattr(compat, "SOURCE_SHA256", hashlib.sha256(source).hexdigest())
    wheel = compat.build_wheel(source)
    assert wheel == compat.build_wheel(source)
    info = f"fastembed-{compat.COMPAT_VERSION}.dist-info/"
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        assert archive.read("fastembed/text/model.py") == b"# unchanged inference bytes\n"
        assert archive.read(info + "LICENSE") == b"upstream license\n"
        metadata = archive.read(info + "METADATA").decode()
        assert "Version: 0.9.0+qm.pillow13\n" in metadata
        assert metadata.count("pillow (>=10.3.0,<14.0)") == 3
        assert "Requires-Dist: huggingface-hub (>=0.20,<2.0)" in metadata
        records = list(csv.reader(io.StringIO(archive.read(info + "RECORD").decode())))
        assert {row[0] for row in records} == set(archive.namelist())
        for name, digest, size in records:
            if name == info + "RECORD":
                assert digest == size == ""
                continue
            data = archive.read(name)
            expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            assert digest == "sha256=" + expected
            assert int(size) == len(data)


def test_changed_upstream_dependency_shape_requires_review(compat, monkeypatch):
    """A future wheel must not receive this patch just because its version matches."""
    source = source_fixture("<15.0")
    monkeypatch.setattr(compat, "SOURCE_SHA256", hashlib.sha256(source).hexdigest())
    with pytest.raises(ValueError, match="Unexpected upstream Pillow"):
        compat.build_wheel(source)


@pytest.mark.parametrize("location", ["comment", "other-package", "hash-suffix"])
def test_lock_hash_must_belong_to_the_fastembed_requirement(
    compat, monkeypatch, tmp_path, location
):
    """An unrelated or merely prefixed hash must not validate the CPU lock entry."""
    source = source_fixture()
    monkeypatch.setattr(compat, "SOURCE_SHA256", hashlib.sha256(source).hexdigest())
    digest = hashlib.sha256(compat.build_wheel(source)).hexdigest()
    source_path = tmp_path / "source.whl"
    source_path.write_bytes(source)
    lock = tmp_path / "requirements.txt"
    expected = f"--hash=sha256:{digest}"
    misplaced = {
        "comment": f"# {expected}\nfastembed==0.9.0 --hash=sha256:wrong\n",
        "other-package": f"other==1 {expected}\nfastembed==0.9.0 --hash=sha256:wrong\n",
        "hash-suffix": f"fastembed==0.9.0 {expected}bad\n",
    }
    lock.write_text(misplaced[location])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--source-wheel",
            str(source_path),
            "--output-dir",
            str(tmp_path / "output"),
            "--check-lock",
            str(lock),
        ],
    )
    assert compat.main() == 1
    assert not list((tmp_path / "output").glob("*.whl"))


def test_lock_hash_accepts_only_the_complete_fastembed_continuation(compat):
    """Accept the generated pip lock syntax without relying on unrelated comments."""
    digest = "a" * 64
    text = "# ignored\nother==1 --hash=sha256:other\nfastembed==0.9.0 \\\n"
    text += f"    --hash=sha256:{digest}\n    # via the local extra\n"
    compat.require_locked_hash(text, digest)
