import os
from playwright.async_api import async_playwright  # noqa: F401  (re-exported)


async def launch(pw):
    """Return (browser, context).

    If BD_BROWSER_WS is set we connect to Bright Data's Browser API.
    Otherwise we launch the local Chromium bundled with Playwright.
    Set BROWSER=chrome to use system Chrome instead of the bundle.
    """
    cdp = os.environ.get("BD_BROWSER_WS")
    if cdp:
        browser = await pw.chromium.connect_over_cdp(cdp)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        return browser, ctx

    headless = os.environ.get("HEADLESS", "1") == "1"
    channel = os.environ.get("BROWSER", "").strip()
    kwargs = {"headless": headless, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    if channel:
        kwargs["channel"] = channel
    browser = await pw.chromium.launch(**kwargs)
    ctx = await browser.new_context()
    return browser, ctx
