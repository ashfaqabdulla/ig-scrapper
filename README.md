# IG Reel Scraper + FastAPI Wrapper

A complete, self-contained Dockerized setup for scraping Instagram reels behind a small FastAPI server.

## Design Choices

- **One container, one port** (`8099` on host) — no conflicts with other Dockers.
- Uses **Playwright's bundled Chromium** (`--with-deps chromium`) to avoid timeout issues.
- Everything the scrape produces goes under `data/jobs/<job_id>/` so runs are isolated and downloadable.
- Long scrapes are **async jobs**: the API returns a `job_id` immediately, which you can poll at `/jobs/{id}`.

## Deploy

On the server (from the folder containing `docker-compose.yml`):

```bash
mkdir -p data
docker compose build
docker compose up -d
docker compose logs -f ig-reel-api
```

### Testing the Deployment

```bash
curl http://localhost:8099/health
BASE=http://localhost:8099 bash tests/smoke.sh
```

*(If your server is remote, replace `localhost` with the server IP and open port 8099 on the firewall.)*

## Test cases you can run against the live API

| # | Test | Command | Expected |
|---|------|---------|----------|
| 1 | Health | `curl -s localhost:8099/health` | `{"status":"ok",...}` |
| 2 | Endpoints list | `curl -s localhost:8099/` | JSON with `/collect`, `/scrape`, `/pipeline` |
| 3 | Empty users rejected | `curl -X POST localhost:8099/collect -H 'content-type: application/json' -d '{"users":[]}'` | 422 |
| 4 | Empty reels rejected | `curl -X POST localhost:8099/scrape -H 'content-type: application/json' -d '{"reels":[]}'` | 422 |
| 5 | Unknown job | `curl -s localhost:8099/jobs/nope` | 404 |
| 6 | Start collect | `curl -X POST localhost:8099/collect -H 'content-type: application/json' -d '{"users":["instagram"],"max_per_user":1,"min_per_user":1,"concurrency":1}'` | JSON with `id`, `state:"queued"` |
| 7 | Poll job | `curl -s localhost:8099/jobs/<id>` | state moves queued → running → done |
| 8 | Get logs | `curl -s localhost:8099/jobs/<id>/logs` | array of `{t,msg}` |
| 9 | Get result | `curl -s localhost:8099/jobs/<id>/result` | `result.status_counts`, `result.reels_file` |
| 10 | Download reels.txt | `curl -o reels.txt localhost:8099/jobs/<id>/download/reels.txt` | list of reel URLs |
| 11 | Start scrape | `curl -X POST localhost:8099/scrape -H 'content-type: application/json' -d '{"reels":["https://www.instagram.com/instagram/reel/ABC/"]}'` | job created |
| 12 | Full pipeline | `curl -X POST localhost:8099/pipeline -H 'content-type: application/json' -d '{"users":["instagram"],"max_per_user":2,"min_per_user":1,"collect_concurrency":1,"scrape_concurrency":1}'` | both stages run, `result.collect` + `result.scrape` |
| 13 | Cancel | `curl -X DELETE localhost:8099/jobs/<id>` | `{"cancelled":true}` or false if already finished |
| 14 | Path traversal | `curl -s localhost:8099/jobs/x/download/..%2Fpasswd` | 400 or 404 |
| 15 | pytest | `pytest -q tests/` inside the container | all non-network tests pass |

Run pytest inside the container:

```bash
docker exec -it ig-reel-api pytest -q /app/tests
RUN_NETWORK=1 docker exec -it ig-reel-api pytest -q /app/tests
```

## Things to be aware of

1. **`ig_multi` was `CONCURRENCY=1` by default.** This default is kept in `ScrapeRequest`. Raise it once you know the IP's ceiling.
2. **You will still be blocked by IP eventually** — the code already aborts after a streak of failures. If it happens, set `BD_BROWSER_WS` in `.env` / `docker-compose.yml` and restart; `browser.py` will route through Bright Data.
3. **The job registry is in memory.** If you `docker restart`, job history is lost (but the `data/jobs/<id>/` files remain on disk). 
4. **Python 3.12** is used. Playwright's wheel set is very reliable on 3.12.
5. **`shm_size: "1gb"`** is not optional for Chromium in a container — do not remove it from `docker-compose.yml`.
6. **Do not commit `.env`.** Add to `.gitignore`. Your Bright Data key is the only secret.
