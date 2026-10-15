"""Reject symlink/reparse substitutions in newly managed private destinations."""

import stat
from pathlib import Path


def checked_absolute_path(value):
    """Require an absolute path with no existing symlink or reparse ancestors.

    Callers may select a canonical path explicitly. Resolving first would hide
    a substituted intermediate directory and authorize writes outside that path.
    Existing advanced profile loading retains its separate path policy.
    """
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Choose a canonical absolute private directory")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Managed paths cannot contain symlinks")
        try:
            attributes = getattr(part.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            continue
        if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("Managed paths cannot contain reparse points")
    return path.resolve()
