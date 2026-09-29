"""Owned child cwd cleanup, after the caller has reaped its process and pipes.

Windows can briefly refuse removal after process termination. Retry only this
newly created directory; never scan temp storage or defer deletion to a finalizer.
This is lifecycle cleanup, not protection against a hostile same-user process.
"""
from contextlib import contextmanager
from pathlib import Path
import shutil
import stat
import tempfile
import time


class ChildCleanupError(RuntimeError):
    def __init__(self):
        super().__init__("Child temporary directory cleanup failed; local data may remain in temporary storage.")


def _owned_state(path, parent, identity):
    try:
        current = path.lstat()
    except FileNotFoundError:
        return None
    if (path.parent != parent or not stat.S_ISDIR(current.st_mode)
            or getattr(current, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or (current.st_dev, current.st_ino) != identity):
        raise ChildCleanupError()
    try:
        resolved = path.resolve()
    except RuntimeError:
        raise ChildCleanupError() from None
    if resolved != path:
        raise ChildCleanupError()
    return current


def _remove_owned(path, parent, identity):
    deadline = time.monotonic() + 2.0
    while True:
        try:
            if _owned_state(path, parent, identity) is None:
                return
            shutil.rmtree(path)
            if _owned_state(path, parent, identity) is None:
                return
        except OSError:
            pass  # A sharing violation may outlive the reaped Windows Job.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChildCleanupError() from None
        time.sleep(min(0.05, remaining))


@contextmanager
def temporary_child_directory(prefix):
    parent = Path(tempfile.gettempdir()).resolve()
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        created = path.lstat()
    except OSError:
        raise ChildCleanupError() from None
    identity = (created.st_dev, created.st_ino)
    try:
        yield str(path)
    finally:
        _remove_owned(path, parent, identity)
