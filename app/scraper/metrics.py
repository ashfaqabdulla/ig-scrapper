"""Refactor of ig_multi(v2).py as an importable coroutine."""
import asyncio
import csv
import json
import random
import time
from collections import defaultdict
from pathlib import Path

from playwright.async_api import async_playwright

from .browser import launch
from .common import walk, parse_reel_url

FIELDS = ("play_count", "like_count", "comment_count")
MAX_SCROLLS = 150


async def _capture(ctx, url, on_obj, max_scrolls=0, done=lambda: False):
    tasks, seen = [], set()

    def merge(data):
        for o in walk(data):
            seen.add(o["code"])
            on_obj(o)

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

    page = await ctx.new_page()
    page.on("response", lambda r: tasks.append(asyncio.create_task(on_response(r))))
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        try:
            await page.wait_for_selector('a[href*="/reel/"], a[href*="/p/"]', timeout=25000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)
        await page.keyboard.press("Escape")

        try:
            for txt in await page.eval_on_selector_all(
                    'script[type="application/json"]', "els => els.map(e => e.textContent)"):
                try:
                    merge(json.loads(txt))
                except Exception:
                    pass
        except Exception:
            pass

        stale, last = 0, -1
        for _ in range(max_scrolls):
            if done():
                break
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(2000)
            await asyncio.gather(*list(tasks), return_exceptions=True)
            stale = stale + 1 if len(seen) == last else 0
            last = len(seen)
            if stale >= 6:
                break
        await asyncio.gather(*list(tasks), return_exceptions=True)
    finally:
        await page.close()


async def _find_owner(ctx, code):
    owner = {}

    def on_obj(o):
        if o.get("code") == code:
            u = (o.get("user") or o.get("owner") or {}).get("username")
            if u:
                owner["u"] = u

    await _capture(ctx, f"https://www.instagram.com/reel/{code}/", on_obj)
    return owner.get("u")


async def _scrape_user(ctx, user, codes):
    want, got = set(codes), {}

    def on_obj(o):
        c = o.get("code")
        if c in want:
            rec = got.setdefault(c, {})
            for k in FIELDS:
                if o.get(k) is not None:
                    rec[k] = o[k]

    def done():
        return all(got.get(c) for c in want)

    for path in ("reels/", ""):
        for _attempt in (1, 2):
            if done():
                break
            await _capture(ctx, f"https://www.instagram.com/{user}/{path}",
                           on_obj, MAX_SCROLLS, done=done)
        if done():
            break
    return got, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def run_metrics(reel_urls, out_dir: Path, opts: dict, log):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    concurrency = int(opts.get("concurrency", 1))

    items, seen_codes = [], set()
    for line in reel_urls:
        line = (line or "").strip()
        if not line or line.startswith("#"):
            continue
        url = line.split("?")[0].split("#")[0]
        code, user = parse_reel_url(url)
        if not code:
            log(f"skipped (no shortcode): {line}")
        elif code in seen_codes:
            log(f"skipped (duplicate): {code}")
        else:
            seen_codes.add(code)
            items.append({"url": url, "code": code, "user": user})

    if not items:
        raise ValueError("No valid reel URLs")

    sem = asyncio.Semaphore(concurrency)
    stats = {"done": 0, "streak": 0, "t0": time.time(), "abort": False}

    async with async_playwright() as p:
        browser, ctx = await launch(p)
        try:
            async def guarded(fn, *args):
                async with sem:
                    if stats["abort"]:
                        return None
                    await asyncio.sleep(random.uniform(2, 2 + concurrency))
                    try:
                        return await fn(*args)
                    except Exception as e:
                        log(f"error: {e}")
                        return None

            need = [i for i in items if not i["user"]]
            owners = await asyncio.gather(*[guarded(_find_owner, ctx, i["code"]) for i in need])
            for i, u in zip(need, owners):
                i["user"] = u

            groups = defaultdict(list)
            for i in items:
                if i["user"]:
                    groups[i["user"]].append(i["code"])

            async def work(user, codes):
                async with sem:
                    if stats["abort"]:
                        return None
                    await asyncio.sleep(random.uniform(2, 2 + concurrency))
                    try:
                        res = await _scrape_user(ctx, user, codes)
                    except Exception as e:
                        log(f"error {user}: {e}")
                        res = None
                    got = res[0] if res else {}
                    stats["done"] += 1
                    stats["streak"] = 0 if got else stats["streak"] + 1
                    el = int(time.time() - stats["t0"])
                    log(f"[{stats['done']}/{len(groups)}] {user}: "
                        f"{len(got)}/{len(codes)} reels ({el}s)")
                    if stats["streak"] >= 10:
                        stats["abort"] = True
                        log("!! 10 accounts in a row captured nothing, aborting")
                    return res

            results = await asyncio.gather(*[work(u, c) for u, c in groups.items()])
            by_user = {}
            for u, res in zip(groups.keys(), results):
                by_user[u] = res if res else (
                    {}, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        finally:
            await browser.close()

    rows = []
    for i in items:
        got, at = by_user.get(i["user"], ({}, None))
        rec = got.get(i["code"], {})
        status = ("ok" if rec.get("play_count") is not None
                  else "likes only (no plays)" if rec.get("like_count") is not None
                  else "owner unknown" if not i["user"] else "not found")
        rows.append({"url": i["url"], "username": i["user"], "shortcode": i["code"],
                     "plays": rec.get("play_count"), "likes": rec.get("like_count"),
                     "comments": rec.get("comment_count"), "status": status,
                     "fetched_at": at})

    counts = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1

    stamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"multi_{stamp}.csv"
    json_path = out_dir / f"multi_{stamp}.json"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)

    return {
        "reels_total": len(rows),
        "status_counts": dict(counts),
        "csv": str(csv_path),
        "json": str(json_path),
        "rows": rows,
    }
