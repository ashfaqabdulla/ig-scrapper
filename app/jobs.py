import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class JobState(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


@dataclass
class Job:
    id: str
    kind: str
    state: JobState = JobState.queued
    params: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    logs: list = field(default_factory=list)
    result: Optional[dict] = None
    error: Optional[str] = None
    _task: Any = None


class JobManager:
    def __init__(self, data_dir: Path):
        self.jobs: dict[str, Job] = {}
        self.data_dir = Path(data_dir)

    def create(self, kind: str, params: dict) -> Job:
        jid = uuid.uuid4().hex[:12]
        job = Job(id=jid, kind=kind, params=params)
        (self.data_dir / "jobs" / jid).mkdir(parents=True, exist_ok=True)
        self.jobs[jid] = job
        return job

    def get(self, jid: str) -> Optional[Job]:
        return self.jobs.get(jid)

    def log(self, job: Job, msg: str) -> None:
        job.logs.append({"t": time.time(), "msg": msg})
        if len(job.logs) > 1000:
            job.logs = job.logs[-1000:]

    def to_dict(self, job: Job, include_logs: bool = True) -> dict:
        d = {
            "id": job.id,
            "kind": job.kind,
            "state": job.state.value,
            "params": job.params,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "error": job.error,
            "log_count": len(job.logs),
            "result": job.result,
            "job_dir": str(self.data_dir / "jobs" / job.id),
        }
        if include_logs:
            d["logs"] = job.logs
        return d

    def cancel(self, job: Job) -> bool:
        if job._task and not job._task.done():
            job._task.cancel()
            job.state = JobState.cancelled
            job.finished_at = time.time()
            return True
        return False
