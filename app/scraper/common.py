import re


def walk(o):
    if isinstance(o, dict):
        if "code" in o:
            yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def normalize_user(line: str):
    s = line.strip().strip(",;")
    if not s or s.startswith("#"):
        return None
    m = re.search(r"instagram\.com/([^/?#\s]+)", s)
    if m:
        s = m.group(1)
    return s.lstrip("@").strip().lower()


def parse_reel_url(url: str):
    url = url.split("?")[0].split("#")[0]
    m = re.search(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        return None, None
    um = re.search(r"instagram\.com/([^/?#]+)/(?:reel|reels|p|tv)/", url)
    return m.group(1), (um.group(1) if um else None)
