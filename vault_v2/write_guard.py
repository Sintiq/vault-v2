"""One cooperating writer per canonical root within this process.

Lock order is root guard -> AgentAPI._proposal_lock -> store.mutation_lock.
Model calls, phone/network requests and backup encryption stay outside this
guard. The runtime lease excludes another cooperating writable window.
"""
from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path
from threading import Lock, RLock, local

from .errors import InvalidJournal, MayHaveApplied, VaultBusy, VaultReadOnly, VaultWriteBlocked

_registry_lock = Lock()
_guards = {}


def root_guard(root: Path):
    key = os.path.normcase(str(Path(root).resolve()))
    with _registry_lock:
        if key not in _guards:
            _guards[key] = RootGuard()
        return _guards[key]


class RootGuard:
    def __init__(self):
        self.lock = RLock()
        self._local = local()
        self.busy = False
        self.status = ""
        self.blocked = False

    @contextmanager
    def nonblocking(self):
        before = getattr(self._local, "nonblocking", False)
        self._local.nonblocking = True
        try:
            yield
        finally:
            self._local.nonblocking = before

    @contextmanager
    def prefer_nonblocking(self):
        """UI-thread lifetime preference; overlapping windows may close in any order."""
        self._local.preferences = getattr(self._local, "preferences", 0) + 1
        try:
            yield
        finally:
            self._local.preferences -= 1

    def _blocking(self):
        return not (getattr(self._local, "nonblocking", False) or getattr(self._local, "preferences", 0))

    @contextmanager
    def read(self):
        if not self.lock.acquire(blocking=self._blocking()):
            raise VaultBusy()
        try:
            yield
        finally:
            self.lock.release()

    @contextmanager
    def write(self, log, label="", *, wait=False):
        if log.read_only:
            raise VaultReadOnly()
        acquired = self.lock.acquire(blocking=wait or self._blocking())
        if not acquired:
            raise VaultBusy()
        outer = not getattr(self._local, "depth", 0)
        entered = False
        try:
            if self.blocked:
                raise VaultWriteBlocked()
            if outer:
                try:
                    log.verify()
                except (ValueError, TypeError, AttributeError, OSError, RecursionError):
                    raise InvalidJournal() from None
                self._local.effects = 0
                self.busy, self.status = True, label or "writing vault"
            self._local.depth = getattr(self._local, "depth", 0) + 1
            entered = True
            before = self._local.effects
            try:
                yield
            except BaseException as exc:
                if self._local.effects > before:
                    self.blocked = True
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    raise MayHaveApplied() from exc
                raise
        finally:
            if entered:
                self._local.depth -= 1
            if outer:
                self.busy, self.status = False, ""
            self.lock.release()

    def effect(self):
        if not getattr(self._local, "depth", 0):
            raise RuntimeError("effect requires the root write guard")
        self._local.effects += 1


def guarded(method):
    """For write-only public methods; never wrap model/network work."""
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.log.write(method.__name__):
            return method(self, *args, **kwargs)
    return call
