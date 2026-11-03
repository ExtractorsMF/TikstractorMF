"""Research helper: capture TikTok profile network/DOM with Playwright."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from tikstractormf._backend import chromium_launch_args, to_playwright_cookies
from tikstractormf.auth import load_from_browser, load_netscape_cookies

SENSITIVE_KEYS = {"cookie", "authorization", "x-csrf-token", "msToken", "tt_chain_token"}


def sanitize_headers(h: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in h.items():
        if k.lower() in SENSITIVE_KEYS:
            out[k] = "[REDACTED]"
        else:
            out[k] = v
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", type=Path, default=None)
    ap.add_argument("--cookies-from-browser", default=None)
    ap.add_argument("--url", default="https://www.tiktok.com/@example")
    ap.add_argument("--scrolls", type=int, default=3)
    args = ap.parse_args()

    if args.cookies_from_browser:
        cookies = load_from_browser(args.cookies_from_browser, domain="tiktok.com")
    elif args.cookies:
        cookies = load_netscape_cookies(args.cookies)
    else:
        cookies = None

    pw_cookies = to_playwright_cookies(cookies) if cookies else []

    captured: list[dict[str, Any]] = []
    universal_data = None

    def on_response(resp):
        nonlocal universal_data
        try:
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            u = resp.url.lower()
            if "item_list" not in u and "itemlist" not in u:
                return
            try:
                j = resp.json()
            except Exception:
                return
            u_raw = resp.url
            item_samples = []
            all_authors = []
            if isinstance(j, dict):
                il = j.get("itemList") or j.get("item_list") or []
                for it in il[:5]:
                    item_samples.append({
                        "id": it.get("id"),
                        "author": (it.get("author") or {}).get("uniqueId"),
                        "originalItem": it.get("originalItem"),
                        "collected": it.get("collected"),
                        "secret": it.get("secret"),
                        "isAd": it.get("isAd"),
                        "forFriend": it.get("forFriend"),
                        "desc": (it.get("desc") or "")[:60],
                    })
                all_authors = [(it.get("author") or {}).get("uniqueId") for it in il]
            captured.append({
                "url": u_raw,
                "status": resp.status,
                "headers": sanitize_headers(dict(resp.headers)),
                "json_keys": list(j.keys()) if isinstance(j, dict) else type(j).__name__,
                "item_samples": item_samples,
                "all_authors": all_authors,
            })
            # universal data
            if "__UNIVERSAL_DATA" in resp.url:
                try:
                    universal_data = j
                except Exception:
                    pass
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=chromium_launch_args())
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        )
        if pw_cookies:
            try:
                ctx.add_cookies(pw_cookies)  # type: ignore[arg-type]
            except Exception:
                pass
        page = ctx.new_page()
        page.on("response", on_response)
        page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)
        for _ in range(args.scrolls):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(3000)
        browser.close()

    # Sanitize: keep only path, not full URL with tokens
    sanitized = []
    for r in captured:
        u = urlparse(r.get("url", ""))
        sanitized.append({
            "path": u.path,
            "status": r.get("status"),
            "json_keys": r.get("json_keys"),
            "item_samples": r.get("item_samples"),
            "all_authors": r.get("all_authors"),
        })

    evidence = {
        "target": args.url,
        "captured_requests": sanitized,
        "universal_data_keys": (
            list(universal_data.keys()) if isinstance(universal_data, dict) else None
        ),
        "timestamp": time.time(),
    }

    out_dir = Path("docs/research")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tiktok_evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Wrote {out_dir / 'tiktok_evidence.json'}")
    print(f"Captured {len(captured)} network requests")


if __name__ == "__main__":
    main()
