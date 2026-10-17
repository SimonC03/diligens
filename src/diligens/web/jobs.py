"""Background jobs for the app: a run reports its stage, and the page polls for it.

Jobs live in memory only; what they produce is saved in the workspace. Messages shown to the
advisor are Swedish and never contain credentials.
"""

import datetime as dt
import logging
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from diligens.agent._llm import LlmError
from diligens.sources.bolagsverket import BolagsverketError
from diligens.sources.scb import ScbError

log = logging.getLogger(__name__)

JobKind = Literal["analys", "screening", "memo"]
UNEXPECTED = "Ett oväntat fel inträffade. Detaljerna finns i terminalen där appen körs."


class Job(BaseModel):
    id: str
    kind: JobKind
    title: str
    stages: list[str]
    current: int = Field(default=-1, description="index of the running stage")
    detail: str = Field(default="", description="e.g. '12 av 61 årsredovisningar'")
    status: Literal["körs", "klar", "fel"] = "körs"
    error: str = ""
    target: str = Field(default="", description="page to open when done")
    back: str = Field(default="", description="page to return to on error")
    started: dt.datetime
    finished: dt.datetime | None = None

    @property
    def seconds(self) -> float:
        end = self.finished or dt.datetime.now()
        return (end - self.started).total_seconds()


class Reporter:
    """What a running job uses to say where it is."""

    def __init__(self, runner: "JobRunner", job_id: str) -> None:
        self._runner = runner
        self._id = job_id

    def stage(self, name: str) -> None:
        def update(job: Job) -> None:
            if name in job.stages:
                job.current = job.stages.index(name)
            job.detail = ""

        self._runner._update(self._id, update)

    def detail(self, text: str) -> None:
        self._runner._update(self._id, lambda job: setattr(job, "detail", text))


class _Inline(Executor):
    """Runs work at once in the calling thread; used by tests for deterministic jobs."""

    def submit[**P, T](self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        future: Future[T] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # pragma: no cover - fn catches its own errors
            future.set_exception(exc)
        return future


class JobRunner:
    def __init__(self, inline: bool = False, workers: int = 2) -> None:
        self._executor: Executor = _Inline() if inline else ThreadPoolExecutor(workers)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def _update(self, job_id: str, change: Callable[[Job], None]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                change(job)

    def shutdown(self) -> None:
        """Stop taking work and let the app exit without waiting for a running job."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.model_copy() if job is not None else None

    def start(
        self,
        kind: JobKind,
        title: str,
        stages: list[str] | tuple[str, ...],
        work: Callable[[Reporter], str],
        back: str = "",
    ) -> Job:
        """Run `work` in the background. It returns the page to open when done."""
        job = Job(
            id=secrets.token_hex(8),
            kind=kind,
            title=title,
            stages=list(stages),
            back=back,
            started=dt.datetime.now(),
        )
        with self._lock:
            self._jobs[job.id] = job
        self._executor.submit(self._run, job.id, work)
        return job.model_copy()

    def _run(self, job_id: str, work: Callable[[Reporter], str]) -> None:
        try:
            target = work(Reporter(self, job_id))
        except (ValidationError, OSError):
            # Technical detail (a schema dump, a file path) belongs in the log, not on screen.
            log.exception("Jobb %s avbröts", job_id)
            self._update(job_id, lambda job: _fail(job, UNEXPECTED))
            return
        except (ValueError, LlmError, ScbError, BolagsverketError) as exc:
            message = str(exc) or UNEXPECTED
            self._update(job_id, lambda job: _fail(job, message))
            return
        except Exception:
            log.exception("Jobb %s avbröts", job_id)
            self._update(job_id, lambda job: _fail(job, UNEXPECTED))
            return

        def done(job: Job) -> None:
            job.status = "klar"
            job.current = len(job.stages)
            job.detail = ""
            job.target = target
            job.finished = dt.datetime.now()

        self._update(job_id, done)


def _fail(job: Job, message: str) -> None:
    job.status = "fel"
    job.error = message
    job.finished = dt.datetime.now()
