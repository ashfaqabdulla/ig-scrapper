"""Run with:  pytest -q tests/test_api.py
Skips the real-network tests unless RUN_NETWORK=1 is set."""
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
NETWORK = os.environ.get("RUN_NETWORK") == "1"


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_root_lists_endpoints():
    r = client.get("/")
    assert r.status_code == 200
    assert "POST /collect" in r.json()["endpoints"]


def test_collect_rejects_empty_users():
    r = client.post("/collect", json={"users": []})
    assert r.status_code == 422


def test_scrape_rejects_empty_reels():
    r = client.post("/scrape", json={"reels": []})
    assert r.status_code == 422


def test_job_404():
    r = client.get("/jobs/doesnotexist")
    assert r.status_code == 404


def test_download_path_traversal_blocked():
    r = client.get("/jobs/abc/download/..%2Fetc%2Fpasswd")
    # url-encoded slash is rejected before route matching → 404 or 400
    assert r.status_code in (400, 404)


@pytest.mark.skipif(not NETWORK, reason="set RUN_NETWORK=1 to hit Instagram")
def test_collect_single_user_smoke():
    """Uses a public account; expects at least one reel or a defined failure status."""
    r = client.post("/collect", json={
        "users": ["instagram"], "max_per_user": 1, "min_per_user": 1,
        "concurrency": 1, "user_timeout": 90,
    })
    assert r.status_code == 200
    jid = r.json()["id"]

    import time
    for _ in range(60):                # wait up to 2 minutes
        s = client.get(f"/jobs/{jid}").json()
        if s["state"] in ("done", "failed", "cancelled"):
            break
        time.sleep(2)
    s = client.get(f"/jobs/{jid}/result").json()
    assert s["state"] in ("done", "failed")
    if s["state"] == "done":
        counts = s["result"]["status_counts"]
        assert any(k in counts for k in ("ok", "private", "not_available",
                                          "no_reels", "no_tiles", "timeout"))
