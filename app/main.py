import asyncio
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .jobs import JobManager, JobState
from .scraper.collect import run_collect
from .scraper.metrics import run_metrics

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="IG Reel Leaderboard API", version="1.0.0")
jm = JobManager(DATA_DIR)


# ---------- schemas ----------

class CollectRequest(BaseModel):
    users: list[str] = Field(..., min_length=1)
    min_per_user: int = 1
    max_per_user: int = 10
    concurrency: int = 3
    user_timeout: int = 150


class ScrapeRequest(BaseModel):
    reels: list[str] = Field(..., min_length=1)
    concurrency: int = 1


class PipelineRequest(BaseModel):
    users: list[str] = Field(..., min_length=1)
    min_per_user: int = 1
    max_per_user: int = 10
    collect_concurrency: int = 3
    scrape_concurrency: int = 1
    user_timeout: int = 150


# ---------- helpers ----------

def _job_dir(job_id: str) -> Path:
    return DATA_DIR / "jobs" / job_id


def _start(job, coro_factory):
    """Run coro_factory() in the background and wire state transitions."""
    async def runner():
        job.state = JobState.running
        job.started_at = time.time()
        try:
            job.result = await coro_factory()
            job.state = JobState.done
        except asyncio.CancelledError:
            job.state = JobState.cancelled
            jm.log(job, "cancelled")
            raise
        except Exception as e:
            job.error = str(e)
            job.state = JobState.failed
            jm.log(job, f"FAILED: {e}")
        finally:
            job.finished_at = time.time()

    task = asyncio.create_task(runner())
    job._task = task
    return task


# ---------- health & info ----------

@app.get("/health")
async def health():
    return {"status": "ok", "version": app.version}


@app.get("/")
async def root():
    return {
        "service": "ig-reel-api",
        "endpoints": [
            "POST /collect", "POST /scrape", "POST /pipeline",
            "GET /jobs", "GET /jobs/{id}", "GET /jobs/{id}/logs",
            "GET /jobs/{id}/result", "GET /jobs/{id}/download/{filename}",
            "DELETE /jobs/{id}", "GET /health",
        ],
    }


# ---------- collect ----------

@app.post("/collect")
async def start_collect(req: CollectRequest):
    job = jm.create("collect", req.model_dump())
    out_dir = _job_dir(job.id)

    async def work():
        return await run_collect(req.users, out_dir, req.model_dump(),
                                 lambda m: jm.log(job, m))

    _start(job, work)
    return jm.to_dict(job, include_logs=False)


# ---------- scrape ----------

@app.post("/scrape")
async def start_scrape(req: ScrapeRequest):
    job = jm.create("scrape", req.model_dump())
    out_dir = _job_dir(job.id)

    async def work():
        return await run_metrics(req.reels, out_dir, req.model_dump(),
                                 lambda m: jm.log(job, m))

    _start(job, work)
    return jm.to_dict(job, include_logs=False)


# ---------- pipeline (collect then scrape) ----------

@app.post("/pipeline")
async def start_pipeline(req: PipelineRequest):
    job = jm.create("pipeline", req.model_dump())
    out_dir = _job_dir(job.id)

    async def work():
        jm.log(job, "stage 1/2: collect")
        collect_result = await run_collect(
            req.users, out_dir, {
                "min_per_user": req.min_per_user,
                "max_per_user": req.max_per_user,
                "concurrency": req.collect_concurrency,
                "user_timeout": req.user_timeout,
            }, lambda m: jm.log(job, m))

        reels_file = Path(collect_result["reels_file"])
        reel_urls = reels_file.read_text(encoding="utf-8").splitlines()
        jm.log(job, f"stage 2/2: scrape {len(reel_urls)} reels")

        scrape_result = await run_metrics(
            reel_urls, out_dir, {"concurrency": req.scrape_concurrency},
            lambda m: jm.log(job, m))

        return {"collect": collect_result, "scrape": scrape_result}

    _start(job, work)
    return jm.to_dict(job, include_logs=False)


# ---------- job inspection ----------

@app.get("/jobs")
async def list_jobs():
    return [jm.to_dict(j, include_logs=False)
            for j in sorted(jm.jobs.values(), key=lambda x: x.created_at, reverse=True)]


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, logs: bool = False):
    job = jm.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return jm.to_dict(job, include_logs=logs)


@app.get("/jobs/{job_id}/logs")
async def get_job_logs(job_id: str):
    job = jm.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {"id": job.id, "state": job.state.value, "logs": job.logs}


@app.get("/jobs/{job_id}/result")
async def get_job_result(job_id: str):
    job = jm.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job.state not in (JobState.done, JobState.failed, JobState.cancelled):
        raise HTTPException(409, f"job is {job.state.value}, not finished")
    return {"id": job.id, "state": job.state.value,
            "error": job.error, "result": job.result}


@app.get("/jobs/{job_id}/download/{filename}")
async def download(job_id: str, filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "invalid filename")
    path = _job_dir(job_id) / filename
    if not path.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(path)


@app.delete("/jobs/{job_id}")
async def cancel_job(job_id: str):
    job = jm.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    ok = jm.cancel(job)
    return {"id": job.id, "cancelled": ok, "state": job.state.value}
