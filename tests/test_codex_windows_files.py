"""Native Windows contracts: these tests must run, not skip, in the Windows lane."""

import ctypes
import os
import subprocess
from contextlib import closing
from ctypes import wintypes
from types import SimpleNamespace

import pytest

from qdrant_memory import codex_memory as codex
from qdrant_memory import windows_files as native
from qdrant_memory.models import Scope

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires native Windows filesystem APIs")


def _junction(link, target):
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
    )


def _point():
    scope = Scope("fixture", "codex")
    return scope, SimpleNamespace(
        id="fixture", payload={"text": "Approved fact.", **scope.as_dict()}
    )


def _assert_private(security):
    from win32security import (
        ConvertSidToStringSid,
        GetTokenInformation,
        OpenProcessToken,
        TokenUser,
    )

    with closing(
        OpenProcessToken(native.win32api.GetCurrentProcess(), native.win32con.TOKEN_QUERY)
    ) as token:
        owner = GetTokenInformation(token, TokenUser)[0]
    assert ConvertSidToStringSid(security.GetSecurityDescriptorOwner()) == ConvertSidToStringSid(
        owner
    )
    assert security.GetSecurityDescriptorControl()[0] & 0x1000  # SE_DACL_PROTECTED
    acl = security.GetSecurityDescriptorDacl()
    assert acl is not None and acl.GetAceCount() == 1
    (kind, _), mask, trustee = acl.GetAce(0)
    assert kind == 0 and mask == 0x1F01FF  # ACCESS_ALLOWED_ACE_TYPE, FILE_ALL_ACCESS
    assert ConvertSidToStringSid(trustee) == ConvertSidToStringSid(owner)


def test_native_structure_layout():
    """Catch pointer truncation or incorrect NT ABI layouts on each Python version."""
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(native._UnicodeString) == (16 if pointer_size == 8 else 8)
    assert ctypes.sizeof(native._ObjectAttributes) == (48 if pointer_size == 8 else 24)
    assert ctypes.sizeof(native._IoStatusBlock) == pointer_size * 2


def test_open_is_relative_after_drive_root(tmp_path, monkeypatch):
    """No component after the drive root is opened with a full pathname."""
    source = tmp_path / "memory.txt"
    source.write_text("approved", encoding="utf-8")
    original, calls = native._open, []

    def record(name, parent=None, **kwargs):
        calls.append((name, parent is not None))
        return original(name, parent, **kwargs)

    monkeypatch.setattr(native, "_open", record)
    monkeypatch.setattr(os, "open", lambda *a, **kw: pytest.fail("full-path os.open fallback"))
    assert codex._read(source) == "approved"
    assert calls[0] == ("\\??\\" + source.anchor, False)
    assert all(relative and "\\" not in name for name, relative in calls[1:])


def test_unicode_paths_and_contents_use_utf16_and_utf8(tmp_path):
    """Supplementary Unicode path characters must not truncate NT names."""
    root = tmp_path / "\u732b\U0001f9e0"
    (root / "memories").mkdir(parents=True)
    (root / "memories/MEMORY.md").write_text(
        "# \u504f\u597d\n\u4f7f\u7528\u56de\u5f52\u6d4b\u8bd5\u3002\n",
        encoding="utf-8",
        newline="\n",
    )
    records, report = codex.read_source(root)
    assert report["planned"] == 1
    assert records[0]["memory"] == "# \u504f\u597d\n\u4f7f\u7528\u56de\u5f52\u6d4b\u8bd5\u3002"


def test_home_case_aliases_share_source_and_record_identity(tmp_path):
    """Case aliases do not create duplicate migrations or retarget filesystem reads."""
    from pathlib import Path

    root = tmp_path / "NativeHome"
    (root / "memories").mkdir(parents=True)
    (root / "memories/MEMORY.md").write_text("# Preference\nApproved.\n")
    first, first_report = codex.read_source(root)
    alias = Path(str(root).swapcase())
    second, second_report = codex.read_source(alias)
    assert first_report["source_id"] == second_report["source_id"]
    assert first[0]["id"] == second[0]["id"]


@pytest.mark.parametrize("when", ["before_anchor", "after_anchor"])
def test_native_import_cannot_follow_replaced_parent(tmp_path, monkeypatch, when):
    """A junction substitution cannot import the attacker's alternate fact."""
    parent, outside = tmp_path / "native/memories", tmp_path / "outside"
    parent.mkdir(parents=True)
    outside.mkdir()
    source = parent / "MEMORY.md"
    source.write_text("# Preference\nApproved fact.\n", encoding="utf-8")
    (outside / source.name).write_text("# Preference\nUnapproved fact.\n", encoding="utf-8")
    monkeypatch.setattr(
        codex,
        "inspect",
        lambda *_: {
            "source_id": "fixture",
            "stores": [{"path": source, "version": "v1"}],
        },
    )
    original = native.open_directory

    def substitute(path):
        handle = original(path) if when == "after_anchor" else None
        parent.rename(tmp_path / "original")
        _junction(parent, outside)
        return handle if handle is not None else original(path)

    monkeypatch.setattr(native, "open_directory", substitute)
    if when == "before_anchor":
        with pytest.raises((ValueError, OSError)):
            codex.read_source(tmp_path / "native")
    else:
        records, _ = codex.read_source(tmp_path / "native")
        assert len(records) == 1 and records[0]["memory"].endswith("Approved fact.")


def test_junction_is_refused_by_native_backend(tmp_path):
    """The backend itself rejects junctions without relying on path prechecks."""
    outside, link = tmp_path / "outside", tmp_path / "junction"
    outside.mkdir()
    (outside / "memory").write_text("unapproved")
    _junction(link, outside)
    with pytest.raises(ValueError, match="reparse"):
        native.open_read(link / "memory")


def test_store_precheck_resolution_cannot_redirect_import(tmp_path, monkeypatch):
    """Store discovery cannot resolve a switched junction into an authorized home."""
    home = tmp_path / "native"
    source, outside = home / "memories", tmp_path / "outside"
    source.mkdir(parents=True)
    outside.mkdir()
    (source / "MEMORY.md").write_text("# Preference\nApproved.\n")
    (outside / "MEMORY.md").write_text("# Preference\nUnapproved.\n")
    original = codex.checked_absolute_path

    def substitute(value):
        result = original(value)
        if value == source:
            source.rename(tmp_path / "original")
            _junction(source, outside)
            return value.resolve()
        return result

    monkeypatch.setattr(codex, "checked_absolute_path", substitute)
    with pytest.raises(ValueError, match="reparse"):
        codex.read_source(home)


def test_held_directory_refuses_write_handles(tmp_path):
    """A checked directory cannot be opened for in-place reparse mutation."""
    path = str(tmp_path)
    with closing(native.open_directory(tmp_path)):
        with pytest.raises(native.pywintypes.error) as error:
            native.win32file.CreateFile(
                path,
                native.win32con.GENERIC_WRITE,
                7,
                None,
                native.win32con.OPEN_EXISTING,
                native.win32con.FILE_FLAG_BACKUP_SEMANTICS
                | 0x200000,  # FILE_FLAG_OPEN_REPARSE_POINT (not exported by win32con)
                None,
            )
        assert error.value.winerror == 32  # ERROR_SHARING_VIOLATION


@pytest.mark.parametrize(
    "name",
    [
        "..",
        ".",
        "",
        "a\\b",
        "a/b",
        "a:stream",
        "a\x00b",
        "a.",
        "a ",
        "CON",
        "con.txt",
        "NUL",
        "COM1",
        "COM¹",
        "LPT².txt",
        "a?",
        "a*",
    ],
)
def test_namespace_and_alias_components_never_reach_native_open(monkeypatch, name):
    """NT namespaces, streams and device aliases cannot enter relative opens."""
    monkeypatch.setattr(native, "_nt_create_file", lambda: pytest.fail("unsafe native name"))
    with pytest.raises(ValueError, match="component"):
        native._open(name, parent=1)


@pytest.mark.parametrize("path", [r"\\server\share\file", r"\\?\C:\file", r"C:file"])
def test_non_local_drive_namespaces_fail_closed(path):
    """Unsupported namespace forms never fall back to a full-path open."""
    from pathlib import Path

    path = Path(path)
    with pytest.raises(ValueError, match="local drive"):
        native.open_directory(path)


@pytest.mark.skipif(
    os.environ.get("QDRANT_WINDOWS_SMB_FIXTURE") != "1",
    reason="loopback SMB fixture requires disposable Windows CI service setup",
)
def test_mapped_remote_drive_is_refused(tmp_path):
    """A real loopback SMB mapping cannot bypass the local-volume requirement."""
    import string
    import uuid
    from pathlib import Path

    import win32net
    import win32netcon
    import win32wnet

    used = {p[:2].upper() for p in native.win32api.GetLogicalDriveStrings().split("\x00") if p}
    drive = next(c + ":" for c in reversed(string.ascii_uppercase) if c + ":" not in used)
    share = "codex-test-" + uuid.uuid4().hex
    win32net.NetShareAdd(
        None,
        2,
        {
            "netname": share,
            "type": win32netcon.STYPE_DISKTREE,
            "remark": "synthetic fixture",
            "permissions": 0,
            "max_uses": -1,
            "current_uses": 0,
            "path": str(tmp_path),
            "passwd": None,
        },
    )
    try:
        win32wnet.WNetAddConnection2(
            win32netcon.RESOURCETYPE_DISK, drive, "\\\\localhost\\" + share
        )
        try:
            path = Path(drive + "\\")
            assert native.win32file.GetDriveType(str(path)) == native.win32con.DRIVE_REMOTE
            with pytest.raises(ValueError, match="local drive"):
                native.open_directory(path)
        finally:
            win32wnet.WNetCancelConnection2(drive, 0, True)
    finally:
        win32net.NetShareDel(None, share)


def test_descriptor_conversion_failure_closes_handle(tmp_path, monkeypatch):
    """An unsuccessful CRT transfer leaves no unowned native handle."""
    path = tmp_path / "memory"
    path.write_text("approved")
    with closing(native.open_directory(tmp_path)) as parent:
        handle = native._open(path.name, parent)
    raw = int(handle)

    def fail(*_):
        raise OSError("injected conversion failure")

    monkeypatch.setattr(native.msvcrt, "open_osfhandle", fail)
    with pytest.raises(OSError, match="injected"):
        native._descriptor(handle, os.O_RDONLY)
    with pytest.raises(native.pywintypes.error) as error:
        native.win32api.GetHandleInformation(raw)
    assert error.value.winerror == 6  # ERROR_INVALID_HANDLE


def test_repeated_success_and_failure_do_not_leak_handles(tmp_path):
    """Repeated reads and ancestor-open failures release all native handles."""
    source = tmp_path / "memory"
    source.write_text("approved")
    count = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessHandleCount
    count.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    count.restype = wintypes.BOOL

    def handles():
        value = wintypes.DWORD()
        assert count(int(native.win32api.GetCurrentProcess()), ctypes.byref(value))
        return value.value

    # Initialize DLL wrappers and CRT bookkeeping before taking the baseline.
    os.close(native.open_read(source))
    before = handles()
    for _ in range(30):
        descriptor = native.open_read(source)
        assert not os.get_inheritable(descriptor)
        os.close(descriptor)
        with pytest.raises(OSError):
            native.open_read(tmp_path / "missing/leaf")
    assert handles() == before


def test_handoff_has_private_acl_from_creation_and_round_trips(tmp_path, monkeypatch):
    """Every created object is private before any data is written to it."""
    scope, point = _point()
    original, created = native._open, []

    def inspect_creation(name, parent=None, **kwargs):
        handle = original(name, parent, **kwargs)
        if kwargs.get("create"):
            security = native.win32security.GetSecurityInfo(handle, 1, 5)
            _assert_private(security)
            created.append(name)
        return handle

    monkeypatch.setattr(native, "_open", inspect_creation)
    target = tmp_path / "handoff"
    codex.write_handoff(target, "fixture", [point], scope)
    assert set(created) == {"handoff", "handoff.json", "handoff.md", "manifest.json"}
    assert codex.verify_handoff(target) == {
        "ok": True,
        "count": 1,
        "status": "HANDOFF_PENDING",
        "native_verified": False,
    }
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    with pytest.raises(FileExistsError):
        codex.write_handoff(target, "fixture", [point], scope)
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before


def test_handoff_creation_remains_anchored_after_parent_replacement(tmp_path, monkeypatch):
    """Replacing the output parent's pathname cannot redirect new files."""
    parent, outside = tmp_path / "parent", tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    original = native.open_directory

    def substitute(path):
        handle = original(path)
        parent.rename(tmp_path / "original")
        _junction(parent, outside)
        return handle

    monkeypatch.setattr(native, "open_directory", substitute)
    scope, point = _point()
    codex.write_handoff(parent / "handoff", "fixture", [point], scope)
    monkeypatch.setattr(native, "open_directory", original)
    assert not list(outside.iterdir())
    assert codex.verify_handoff(tmp_path / "original/handoff")["count"] == 1


def test_acl_capability_failure_does_not_create_directory(tmp_path, monkeypatch):
    """Unsupported filesystem permissions fail before publishing a directory."""

    def fail(_):
        raise ValueError("unsupported ACLs")

    monkeypatch.setattr(native, "_require_private_volume", fail)
    scope, point = _point()
    with pytest.raises(ValueError, match="unsupported ACLs"):
        codex.write_handoff(tmp_path / "handoff", "fixture", [point], scope)
    assert not (tmp_path / "handoff").exists()


def test_precheck_resolution_cannot_redirect_handoff(tmp_path, monkeypatch):
    """A junction introduced during precheck resolution cannot change the target."""
    parent, outside = tmp_path / "parent", tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    target = parent / "handoff"
    original = codex.checked_absolute_path

    def substitute(value):
        original(value)
        parent.rename(tmp_path / "original")
        _junction(parent, outside)
        return value.resolve()

    monkeypatch.setattr(codex, "checked_absolute_path", substitute)
    scope, point = _point()
    with pytest.raises((OSError, ValueError)):
        codex.write_handoff(target, "fixture", [point], scope)
    assert not list(outside.iterdir())
