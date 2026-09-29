"""One serial, deduplicated text-preparation queue per open vault root.

Requests do not wait for extraction. The owner window retains the writer lease
until the queue is idle, then closes it before releasing the lease.
"""
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from threading import Condition, Event, RLock, Thread
from typing import Callable
from weakref import WeakValueDictionary

from .errors import VaultError
from .text_cache import DocumentTextCache


@dataclass
class _Job:
    source: Path
    authorize: Callable[[], None]
    done: Event = field(default_factory=Event)
    error: Exception | None = None


class TextPreparationQueue:
    def __init__(self, paths, log):
        self.paths, self.log = paths, log
        self._condition = Condition(RLock())
        self._jobs: dict[Path, _Job] = {}
        self._waiting = deque()
        self._worker = None
        self.closed = False

    @property
    def busy(self) -> bool:
        with self._condition:
            return self._worker is not None

    def is_pending(self, source: Path) -> bool:
        with self._condition:
            return Path(source).absolute() in self._jobs

    def request(self, source: Path, authorize: Callable[[], None]) -> _Job:
        authorize()
        source = Path(source).absolute()
        with self._condition:
            if self.closed or self.log.read_only:
                raise VaultError("Text preparation is unavailable in this read-only or closing window.")
            if source in self._jobs:
                return self._jobs[source]
            job = _Job(source, authorize)
            self._jobs[source] = job
            self._waiting.append(job)
            if self._worker is None:
                self._worker = Thread(target=self._run, name="vault-text-preparation", daemon=True)
                try:
                    self._worker.start()
                except Exception:
                    self._worker = None
                    self._waiting.remove(job)
                    del self._jobs[source]
                    raise
            return job

    def _run(self):
        cache = DocumentTextCache(self.paths, self.log)
        while True:
            with self._condition:
                if not self._waiting:
                    self._worker = None
                    self._condition.notify_all()
                    return
                job = self._waiting.popleft()
            try:
                cache.read_snapshot(job.source, authorize=job.authorize)
            except Exception as exc:  # terminal failure, never a silent empty cache
                job.error = exc
            finally:
                with self._condition:
                    del self._jobs[job.source]
                    job.done.set()
                    self._condition.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self._worker is None, timeout=timeout)

    def close_if_idle(self) -> bool:
        with self._condition:
            if self._worker is not None:
                return False
            self.closed = True
            return True


_queues = WeakValueDictionary()
_registry_lock = RLock()


def text_preparation_queue(paths, log) -> TextPreparationQueue:
    # A read-only pane can exist before or after the writer window. It must
    # neither own the writer's queue nor seed its future ReceiptLog binding.
    if log.read_only:
        return TextPreparationQueue(paths, log)
    root = paths.root.absolute()
    with _registry_lock:
        queue = _queues.get(root)
        if queue is None or queue.closed:
            queue = TextPreparationQueue(paths, log)
            _queues[root] = queue
        return queue
