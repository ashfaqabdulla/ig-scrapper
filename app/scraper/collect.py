"""Refactor of ig_collect2.py as an importable coroutine."""
import asyncio
import csv
import json
import random
import re
import time
from collections import Counter
from pathlib import Path

from playwright.async_api import async_playwright

from .browser import launch
from .common import walk, normalize_user

BAD_NAMES = {"reel", "reels", "p", "tv", "explore", "accounts"}
PERMANENT = {"private", "not_available", "no_reels"}
TRANSIENT = {"no_tiles", "timeout", "error"}


async def _scrape(ctx, user, path, pool):
    codes, tasks = {}, []

    def merge(data):
        for o in walk(data):
            c = o.get("code")
            if not c:
                continue
            if not (o.get("product_type") == "clips" or o.get("play_count") is not None):
                continue
            owner = (o.get("user") or o.get("owner") or {}).get("username")
            if owner and owner.lower() != user:
                continue
            codes.setdefault(c, None)

    async def on_response(resp):
        try:
            if "instagram.com" not in resp.url or not (
                    "/graphql" in resp.url or "/api/v1/" in resp.url):
                return
            body = (await resp.text()).replace("for (;;);", "", 1).strip()
            if body and body[0] in "{[":
                merge(json.loads(body))
        except Exception:
            pass

    async def dom_codes(page):
        try:
            hrefs = await page.eval_on_selector_all(
                'a[href*="/reel/"]', "els => els.map(e => e.getAttribute('href'))")
        except Exception:
            return
        for h in hrefs:
            m = re.search(r"^/(?:([^/]+)/)?reel/([A-Za-z0-9_-]+)", h or "")
            if not m:
                continue
            if m.group(1) and m.group(1).lower() != user:
                continue
            codes.setdefault(m.group(2), None)

    page = await ctx.new_page()
    page.on("response", lambda r: tasks.append(asyncio.create_task(on_response(r))))
    try:
        await page.goto(f"https://www.instagram.com/{user}/{path}",
                        wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_selector('a[href*="/reel/"], a[href*="/p/"]', timeout=20000)
            tiles = True
        except Exception:
            tiles = False
        await page.wait_for_timeout(1500)
        await page.keyboard.press("Escape")

        if not tiles:
            try:
                body = (await page.inner_text("body")).lower()
            except Exception:
                body = ""
            if "this account is private" in body:
                return [], "private"
            if "isn't available" in body or "isn\u2019t available" in body:
                return [], "not_available"
            return [], "no_tiles"

        try:
            for txt in await page.eval_on_selector_all(
                    'script[type="application/json"]', "els => els.map(e => e.textContent)"):
                try:
                    merge(json.loads(txt))
                except Exception:
                    pass
        except Exception:
            pass
        await dom_codes(page)

        stale, last = 0, -1
        for _ in range(20):
            if len(codes) >= pool:
                break
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(1800)
            await asyncio.gather(*list(tasks), return_exceptions=True)
            await dom_codes(page)
            stale = stale + 1 if len(codes) == last else 0
            last = len(codes)
            if stale >= 3:
                break
        await asyncio.gather(*list(tasks), return_exceptions=True)

        if not codes:
            return [], "no_reels"
        return list(codes), "ok"
    finally:
        await page.close()


async def _collect_user(ctx, user, pool):
    last = "no_tiles"
    for _attempt in (1, 2):
        for path in ("reels/", ""):
            codes, reason = await _scrape(ctx, user, path, pool)
            if codes:
                return codes, "ok"
            last = reason
            if reason in ("private", "not_available"):
                return [], reason
        if last == "no_reels":
            return [], "no_reels"
        await asyncio.sleep(random.uniform(5, 10))
    return [], last


async def run_collect(users, out_dir: Path, opts: dict, log):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    max_per = int(opts.get("max_per_user", 10))
    min_per = min(int(opts.get("min_per_user", 1)), max_per)
    concurrency = int(opts.get("concurrency", 3))
    user_timeout = int(opts.get("user_timeout", 150))
    pool = max_per

    seen, clean_users, invalid = set(), [], []
    for u in users:
        n = normalize_user(u)
        if n is None:
            continue
        if n in BAD_NAMES or not re.fullmatch(r"[a-z0-9._]{1,30}", n):
            invalid.append(n)
        elif n not in seen:
            seen.add(n)
            clean_users.append(n)

    if not clean_users:
        raise ValueError("No valid usernames")

    progress = {}
    sem = asyncio.Semaphore(concurrency)
    state = {"done": 0, "streak": 0, "abort": False}

    async with async_playwright() as p:
        browser, ctx = await launch(p)
        try:
            async def work(user):
                async with sem:
                    if state["abort"]:
                        return
                    await asyncio.sleep(random.uniform(1, 3 + concurrency))
                    try:
                        codes, status = await asyncio.wait_for(
                            _collect_user(ctx, user, pool), user_timeout)
                    except asyncio.TimeoutError:
                        codes, status = [], "timeout"
                    except Exception as e:
                        log(f"{user}: error {e}")
                        codes, status = [], "error"
                    rec = {"user": user, "status": status, "codes": codes,
                           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                    progress[user] = rec
                    state["done"] += 1
                    state["streak"] = state["streak"] + 1 if status in TRANSIENT else 0
                    log(f"[{state['done']}/{len(clean_users)}] {user}: {status}"
                        + (f" ({len(codes)} reels)" if codes else ""))
                    if state["streak"] >= 8 and not state["abort"]:
                        state["abort"] = True
                        log("!! 8 block-like failures in a row, aborting early")

            await asyncio.gather(*[work(u) for u in clean_users])
        finally:
            await browser.close()

    reels_file = out_dir / "reels.txt"
    picked = {}
    with open(reels_file, "w", encoding="utf-8") as out:
        for user in clean_users:
            rec = progress.get(user)
            if not rec or rec["status"] != "ok":
                continue
            pool_codes = rec["codes"][:pool]
            rng = random.Random(user)
            k = rng.randint(min_per, max_per)
            chosen = rng.sample(pool_codes, min(k, len(pool_codes)))
            picked[user] = len(chosen)
            for c in chosen:
                out.write(f"https://www.instagram.com/{user}/reel/{c}/\n")

    report_file = out_dir / "collect_report.csv"
    with open(report_file, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["username", "status", "reels_found", "reels_picked"])
        for user in clean_users:
            rec = progress.get(user, {})
            w.writerow([user, rec.get("status", "not_run"),
                        len(rec.get("codes", [])), picked.get(user, 0)])

    counts = Counter(progress[u]["status"] if u in progress else "not_run" for u in clean_users)
    total_reels = sum(picked.values())

    (out_dir / "collect.json").write_text(json.dumps({
        "users": clean_users, "invalid": invalid, "progress": progress,
        "status_counts": dict(counts), "picked": picked, "total_reels": total_reels,
    }, indent=2), encoding="utf-8")

    return {
        "users_total": len(clean_users),
        "invalid": invalid,
        "status_counts": dict(counts),
        "reels_picked": total_reels,
        "reels_file": str(reels_file),
        "report_file": str(report_file),
    }
