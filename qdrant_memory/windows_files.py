"""Handle-relative, no-reparse Codex I/O on local Windows drives.

pywin32 owns handles and security descriptors. Only the missing NtCreateFile
binding and the handle-based volume capability query use ctypes. There is no
full-path fallback after opening the drive root.
"""

import ctypes
import os
import re
from contextlib import closing
from ctypes import wintypes
from functools import cache

if os.name == "nt":
    import msvcrt

    import pywintypes
    import win32api
    import win32con
    import win32file
    import win32security


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


@cache
def _nt_create_file():
    function = ctypes.WinDLL("ntdll").NtCreateFile
    function.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    ]
    function.restype = wintypes.LONG
    return function


@cache
def _status_to_error():
    function = ctypes.WinDLL("ntdll").RtlNtStatusToDosError
    function.argtypes, function.restype = [wintypes.LONG], wintypes.ULONG
    return function


def _component(name):
    """Permit one literal component, never an NT namespace or stream name."""
    if (
        not name
        or name in {".", ".."}
        or name != name.rstrip(" .")
        or any(c in name for c in '\\/:\x00<>"|?*')
        or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", name, re.I)
    ):
        raise ValueError("Unsupported Windows path component")
    return name


def _open(name, parent=None, *, directory=False, create=False, security=None):
    """Open exactly one component; inspect the resulting handle before use."""
    if parent is not None:
        _component(name)
    length = len(name.encode("utf-16-le"))
    if length > 65532:
        raise ValueError("Windows path component is too long")
    buffer = ctypes.create_unicode_buffer(name)
    text = _UnicodeString(length, length + 2, ctypes.cast(buffer, wintypes.LPWSTR))
    descriptor = ctypes.create_string_buffer(bytes(security)) if security is not None else None
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        int(parent) if parent is not None else None,
        ctypes.pointer(text),
        0x40,
        ctypes.cast(descriptor, ctypes.c_void_p),
        None,
    )
    result, status = wintypes.HANDLE(), _IoStatusBlock()
    # SYNCHRONIZE | FILE_READ_ATTRIBUTES, plus traversal or actual file I/O.
    access = 0x100080 | (0x20 if directory else (0x2 if create else 0x1))
    if create:
        access |= 0x20000  # READ_CONTROL for checking the new object's private ACL.
    options = 0x200000 | 0x20 | (0x1 if directory else 0x40)
    code = _nt_create_file()(
        ctypes.byref(result),
        access,
        ctypes.byref(attributes),
        ctypes.byref(status),
        None,
        0x80,
        5 if directory else 7,  # Allow rename, deny directory write/reparse mutation.
        2 if create else 1,
        options,
        None,
        0,
    )
    if code != 0:
        raise ctypes.WinError(_status_to_error()(code))
    handle = pywintypes.HANDLE(result.value)
    try:
        flags = win32file.GetFileInformationByHandle(handle)[0]
        if flags & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Managed paths cannot contain reparse points")
        if bool(flags & win32con.FILE_ATTRIBUTE_DIRECTORY) != directory:
            raise ValueError("Unexpected Windows file type")
    except BaseException:
        handle.Close()
        raise
    return handle


def open_directory(path):
    """Walk from a local drive root through handle-relative directory opens."""
    if not re.fullmatch(r"[A-Za-z]:", path.drive) or path.anchor != path.drive + "\\":
        raise ValueError("Secure Windows Codex I/O requires an absolute local drive path")
    handle = _open("\\??\\" + path.anchor, directory=True)
    try:
        for part in path.parts[1:]:
            child = _open(part, handle, directory=True)
            handle.Close()
            handle = child
    except BaseException:
        handle.Close()
        raise
    return handle


def _descriptor(handle, flags):
    """Transfer ownership to the CRT only after conversion succeeds."""
    try:
        descriptor = msvcrt.open_osfhandle(int(handle), flags | os.O_BINARY)
    except BaseException:
        handle.Close()
        raise
    handle.Detach()
    return descriptor


def open_read(path):
    """Return one no-reparse file descriptor for the common bounded reader."""
    with closing(open_directory(path.parent)) as parent:
        return _descriptor(_open(path.name, parent), os.O_RDONLY)


def _private_security():
    with closing(
        win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    ) as token:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    owner = win32security.ConvertSidToStringSid(sid)
    return win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"O:{owner}D:P(A;OICI;FA;;;{owner})",
        win32security.SDDL_REVISION_1,
    )


@cache
def _volume_information():
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetVolumeInformationByHandleW
    function.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    function.restype = wintypes.BOOL
    return function


def _require_private_volume(parent):
    """Refuse filesystems that cannot enforce the creation-time private DACL."""
    flags = wintypes.DWORD()
    if not _volume_information()(int(parent), None, 0, None, None, ctypes.byref(flags), None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    if not flags.value & 0x8:  # FILE_PERSISTENT_ACLS
        raise ValueError("Private handoff creation requires persistent Windows ACLs")


def publish_handoff(target, files):
    """Create a new directory and files with owner-only ACLs at creation time."""
    security = _private_security()
    with closing(open_directory(target.parent)) as parent:
        _require_private_volume(parent)
        directory = _open(target.name, parent, directory=True, create=True, security=security)
    with closing(directory):
        for name, data in files.items():
            descriptor = _descriptor(
                _open(name, directory, create=True, security=security), os.O_WRONLY
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
