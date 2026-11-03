"""TikTok client — Playwright-based scraping of public profiles."""

from __future__ import annotations

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from tikstractormf._backend import (
    CurlClient,
    is_transient_error,
)
from tikstractormf._parsing import parse_item, parse_profile_html
from tikstractormf.models import Media, Post, Profile
from tikstractormf.rate_limit import (
    ApiRateLimiter,
    RateLimitConfig,
    backoff_sleep,
    parse_retry_after,
)


def _warn(message: str) -> None:
    print(f"tikstractormf: {message}", file=sys.stderr, flush=True)


@dataclass
class ScrapeReport:
    posts: int = 0
    media: int = 0
    unparsed: int = 0
    reposts_skipped: int = 0
    no_media: int = 0
    pages_fetched: int = 0
    truncated: bool = False
    stop_reason: str = ""
    used_fallback: bool = False
    fallback_reason: str = ""
    api_error: str = ""
    limit: int | None = None
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    filtered: int = 0
    profile_pic: bool = False


def _finish_report(report: ScrapeReport, posts: list[Any]) -> None:
    report.posts = len(posts)
    report.media = sum(len(getattr(p, "media", ())) for p in posts)
    report.no_media = sum(1 for p in posts if not getattr(p, "media", ()))


def _decoy_headers() -> dict[str, str]:
    """Port of gallery-dl's generate_headers(): random header names/values to
    break the WAF fingerprint during rehydration requests."""
    import random

    def _rand(n: int) -> str:
        return "".join(random.choices("bcdfghjklmnpqrstvwxz", k=n))

    return {
        _rand(random.randint(8, 24)): _rand(random.randint(16, 32))
        for _ in range(random.randint(3, 8))
    }


class TiktokAPI:
    def __init__(
        self,
        cookies: httpx.Cookies | Any | str | Path,
        *,
        impersonate: str | None = None,
        timeout: float = 15.0,
        rate_limit_config: RateLimitConfig | None = None,
        extract_audio: bool = False,
    ):
        if isinstance(cookies, (str, Path)):
            from tikstractormf.auth import load_netscape_cookies
            cookies = load_netscape_cookies(cookies)
        self.cookies = cookies
        self.impersonate = impersonate
        self.timeout = timeout
        self._client: Any = None
        self._owns_client = False
        self.rate_limit_config = rate_limit_config or RateLimitConfig()
        self.extract_audio = extract_audio
        self._api_limiter = ApiRateLimiter(
            rps=self.rate_limit_config.rps, enabled=self.rate_limit_config.enabled
        )
        self._profile_cache: dict[str, str] = {}
        self._sec_uid_cache: dict[str, str] = {}
        self.last_report = ScrapeReport()
        # Reused across creator/item_list pages, like gallery-dl's pagination.
        import random as _random
        self._device_id = str(_random.randint(
            7_250_000_000_000_000_000, 7_325_099_899_999_994_577))

    def _solve_challenge(self, html: str) -> bool:
        """Port of gallery-dl's _solve_challenge. Returns True on success."""
        if 'id="cs"' not in html:
            return False
        try:
            import base64
            import hashlib
            import json as _json
            import time as _time

            def _extr(s: str, start: str, end: str) -> str:
                i = s.find(start)
                if i < 0:
                    return ""
                i += len(start)
                j = s.find(end, i)
                if j < 0:
                    return ""
                return s[i:j]

            cs = _extr(_extr(html, 'id="cs"', ">"), 'class="', '"')
            if not cs:
                return False
            c = _json.loads(base64.b64decode((cs + "==").encode()).decode())
            expected = base64.b64decode((c["v"]["c"] + "==").encode())
            base = hashlib.sha256(base64.b64decode((c["v"]["a"] + "==").encode()))
            idx = 0
            for i in range(1_000_000):
                test = base.copy()
                test.update(str(i).encode())
                if test.digest() == expected:
                    idx = i
                    break
            else:
                return False

            wci = _extr(_extr(html, 'id="wci"', ">"), 'class="', '"')
            rci = _extr(_extr(html, 'id="rci"', ">"), 'class="', '"')
            rs = _extr(_extr(html, 'id="rs"', ">"), 'class="', '"')

            domain = ".tiktok.com"
            expires = int(_time.time()) + 5
            # gallery-dl: b64rencode = binascii.b2a_base64 (standard, with padding)
            c["d"] = base64.b64encode(str(idx).encode()).decode()
            v = base64.b64encode(_json.dumps(c).encode()).decode()

            self._set_challenge_cookie(wci, v, domain, expires)
            if rs and rci:
                self._set_challenge_cookie(rci, rs, domain, expires)
            return True
        except Exception:
            return False

    def _capture_rotated_cookies(self, resp: Any) -> None:
        """TikTok rotates msToken/tt_chain_token on item_list responses; the CDN
        signature is bound to that fresh value. Capture them into self.cookies so
        downloads use the same session as gallery-dl."""
        try:
            rc = getattr(resp, "cookies", None)
            if not rc:
                return
            items = list(rc.items()) if hasattr(rc, "items") else list(rc)
            for name, value in items:
                self._set_challenge_cookie(
                    str(name), str(value), ".tiktok.com", None
                )
        except Exception:
            pass

    def _set_challenge_cookie(
        self, name: str, value: str, domain: str, expires: int | None
    ) -> None:
        cookies = self.cookies
        if hasattr(cookies, "set_cookie"):
            import http.cookiejar

            ck = http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path="/",
                path_specified=True,
                secure=True,
                expires=expires,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
            try:
                cookies.set_cookie(ck)
            except Exception:
                pass
        elif hasattr(cookies, "set"):
            try:
                cookies.set(name, value, domain=domain)
            except Exception:
                pass

    def _get_client(self) -> Any:
        if self._client:
            return self._client
        self._owns_client = True
        # Send ALL cookies as a raw Cookie header, like gallery-dl/urllib3.
        # httpx filters cookies by domain and may drop the second msToken,
        # which breaks TikTok's CDN signature.
        cookie_parts = [
            f"{getattr(c, 'name', '')}={getattr(c, 'value', '')}"
            for c in (self.cookies or ())
        ]
        cookie_header = "; ".join(cookie_parts)
        headers = {"Cookie": cookie_header} if cookie_header else {}
        if self.impersonate:
            self._client = CurlClient(
                impersonate=self.impersonate,
                cookies=None,
                headers=headers,
                connect_timeout=5.0,
                total_timeout=max(30.0, self.timeout * 2),
            )
            return self._client
        site_timeout = httpx.Timeout(connect=5.0, read=self.timeout, write=10.0, pool=5.0)
        self._client = httpx.Client(
            cookies=None,
            headers=headers,
            timeout=site_timeout,
            follow_redirects=True,
            http2=True,
        )
        return self._client

    def _request_with_rate_limit(self, method: str, url: str, *,
                                 rate_limit: bool = True, **kwargs) -> Any:
        client = self._get_client()
        if rate_limit and self.rate_limit_config.enabled:
            self._api_limiter.wait()
        max_retries = self.rate_limit_config.max_retries
        base = self.rate_limit_config.backoff_base
        last_exc: BaseException | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = client.request(method, url, **kwargs)
            except Exception as exc:
                if attempt < max_retries and is_transient_error(exc):
                    last_exc = exc
                    backoff_sleep(attempt, base=base)
                    continue
                raise
            from tikstractormf._backend import is_retryable_status
            if is_retryable_status(resp.status_code):
                if resp.status_code == 429:
                    self._api_limiter.penalize()
                if attempt < max_retries:
                    retry_after = parse_retry_after(dict(resp.headers))
                    if retry_after is not None:
                        time.sleep(retry_after)
                    else:
                        backoff_sleep(attempt, base=base)
                    continue
                resp.raise_for_status()
                return resp
            self._capture_rotated_cookies(resp)
            resp.raise_for_status()
            self._api_limiter.relax()
            return resp
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("request failed without an exception")

    def _profile_html(self, username: str) -> str | None:
        if not username or not re.fullmatch(r"[A-Za-z0-9._]+", username):
            return None
        cached = self._profile_cache.get(username)
        if cached is not None:
            return cached
        url = f"https://www.tiktok.com/@{username}"
        headers = {"Referer": "https://www.tiktok.com/"}
        headers.update(_decoy_headers())
        try:
            resp = self._request_with_rate_limit("GET", url, headers=headers)
        except Exception:
            return None
        html = str(resp.text)
        # If TikTok served a JS challenge instead of rehydration data, solve it
        # and retry once (gallery-dl behaviour).
        if "__UNIVERSAL_DATA_FOR_REHYDRATION__" not in html and 'id="cs"' in html:
            if self._solve_challenge(html):
                try:
                    headers = {"Referer": "https://www.tiktok.com/"}
                    headers.update(_decoy_headers())
                    resp = self._request_with_rate_limit(
                        "GET", url, headers=headers, rate_limit=False
                    )
                    html = str(resp.text)
                except Exception:
                    pass
        self._profile_cache[username] = html
        return html

    def get_profile(self, username: str) -> Profile:
        username = username.lstrip("@").split("/")[0]
        try:
            html = self._profile_html(username)
            pic = None
            if html is not None:
                pic = parse_profile_html(html)
            profile_pic_media = None
            if pic:
                from tikstractormf._parsing import _guess_ext
                from tikstractormf.models import sanitize_filename
                stem = f"{sanitize_filename(username)}_profile"
                profile_pic_media = Media(
                    id=stem, post_id=stem, index=1, type="image", url=pic, ext=_guess_ext(pic)
                )
            return Profile(
                username=username,
                profile_pic_url=pic,
                profile_pic_media=profile_pic_media,
            )
        except Exception as e:
            raise RuntimeError(f"get_profile failed for @{username}: {e}") from e

    def _fetch_post_from_page(
        self, username: str, post_id: str, item: dict[str, Any] | None = None
    ) -> Post | None:
        import json as _json
        import re as _re

        if item is None:
            item = getattr(self, "_item_by_id", {}).get(post_id)

        url = f"https://www.tiktok.com/@{username}/video/{post_id}"
        try:
            req_headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Referer": f"https://www.tiktok.com/@{username}/video/{post_id}",
            }
            req_headers.update(_decoy_headers())
            resp = self._request_with_rate_limit(
                "GET",
                url,
                rate_limit=False,
                headers=req_headers,
            )
            # JS challenge handling: if served a challenge, solve and retry once.
            text_body = getattr(resp, "text", "")
            if (
                resp.status_code == 200
                and "__UNIVERSAL_DATA_FOR_REHYDRATION__" not in text_body
                and 'id="cs"' in text_body
            ):
                if self._solve_challenge(resp.text):
                    try:
                        req_headers = {
                            "Accept": (
                                "text/html,application/xhtml+xml,application/xml;"
                                "q=0.9,*/*;q=0.8"
                            ),
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/131.0.0.0 Safari/537.36"
                            ),
                            "Referer": f"https://www.tiktok.com/@{username}/video/{post_id}",
                        }
                        req_headers.update(_decoy_headers())
                        resp = self._request_with_rate_limit(
                            "GET", url, rate_limit=False, headers=req_headers,
                        )
                    except Exception:
                        pass
        except Exception:
            # Rehydrated URL unavailable (e.g. 403): fall back to the
            # downloadAddr/playAddr captured in the item_list payload.
            if item:
                return parse_item(
                        item, fallback_username=username,
                        extract_audio=self.extract_audio,
                    )
            return None
        m = _re.search(
            r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
            resp.text,
            _re.S,
        )
        if not m:
            if item:
                return parse_item(
                        item, fallback_username=username,
                        extract_audio=self.extract_audio,
                    )
            return None
        try:
            data = _json.loads(m.group(1))
        except Exception:
            if item:
                return parse_item(
                        item, fallback_username=username,
                        extract_audio=self.extract_audio,
                    )
            return None
        scope = data.get("__DEFAULT_SCOPE__", {})
        vd = scope.get("webapp.video-detail", {})
        ii = vd.get("itemInfo", {}) or {}
        item_struct = ii.get("itemStruct")
        if not item_struct:
            if item:
                return parse_item(
                        item, fallback_username=username,
                        extract_audio=self.extract_audio,
                    )
            return None
        post = parse_item(
                        item_struct, fallback_username=username,
                        extract_audio=self.extract_audio,
                    )
        if not post and item:
            return parse_item(
                        item, fallback_username=username,
                        extract_audio=self.extract_audio,
                    )
        return post

    def _extract_sec_uid(self, username: str) -> str | None:
        if username in self._sec_uid_cache:
            return self._sec_uid_cache[username]
        html = self._profile_html(username)
        if not html:
            return None
        import json as _json
        import re as _re
        m = _re.search(
            r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
            html,
            _re.S,
        )
        if m:
            try:
                data = _json.loads(m.group(1))
                scope = data.get("__DEFAULT_SCOPE__", {})
                ud = scope.get("webapp.user-detail", {})
                ui = ud.get("userInfo", {})
                u = ui.get("user", {})
                sec = u.get("secUid")
                if sec:
                    self._sec_uid_cache[username] = str(sec)
                    return self._sec_uid_cache[username]
            except Exception:
                pass
        m = _re.search(r"MS4wLjABAAAA[\\w-]{32,64}", html)
        if m:
            self._sec_uid_cache[username] = m.group(0)
            return self._sec_uid_cache[username]
        self._sec_uid_cache[username] = None  # type: ignore[assignment]
        return None

    def _build_item_list_url(self, secuid: str, cursor: int) -> str:
        import random as _random
        from urllib.parse import urlencode

        params = {
            "aid": "1988",
            "app_language": "en",
            "app_name": "tiktok_web",
            "browser_language": "en-US",
            "browser_name": "Mozilla",
            "browser_online": "true",
            "browser_platform": "Win32",
            "browser_version": "5.0 (Windows)",
            "channel": "tiktok_web",
            "cookie_enabled": "true",
            "device_id": self._device_id,
            "device_platform": "web_pc",
            "focus_state": "true",
            "from_page": "user",
            "history_len": "2",
            "is_fullscreen": "false",
            "is_page_visible": "true",
            "language": "en",
            "os": "windows",
            "priority_region": "",
            "referer": "",
            "region": "US",
            "screen_height": "1080",
            "screen_width": "1920",
            "tz_name": "UTC",
            "verifyFp": "verify_" + "".join(
                _random.choices("0123456789abcdef", k=7)
            ),
            "webcast_language": "en",
            "secUid": secuid,
            "type": "1",
            "count": "15",
            "cursor": str(cursor),
        }
        return "https://www.tiktok.com/api/creator/item_list/?" + urlencode(params)

    def iter_posts(
        self, username: str, *, limit: int | None = None, exclude_reposts: bool = True,
        concurrency: int = 4,
    ):
        username = username.lstrip("@").split("/")[0]
        report = ScrapeReport(limit=limit)
        self.last_report = report

        secuid = self._extract_sec_uid(username)
        if not secuid:
            _warn(f"could not resolve secUid for @{username}.")
            report.used_fallback = False
            report.stop_reason = "secUid not resolved"
            report.api_error = report.api_error or "could not resolve secUid"
            _finish_report(report, [])
            return []

        # Streamed walk: for each item_list page, collect candidate ids, then
        # rehydrate (fetch post pages) immediately before asking for the next
        # page. This keeps the signed CDN URLs fresh instead of letting them
        # expire while a large profile is being listed.
        candidate_ids: list[str] = []
        item_by_id: dict[str, dict] = {}
        self._item_by_id = item_by_id
        seen_page_items: set[str] = set()
        seen_cursors: set[int] = set()
        cursor = int(__import__("time").time()) * 1000
        pages = 0
        posts: list[Post] = []
        seen_ids: set[str] = set()
        done_total = 0

        def _fetch_one(pid: str) -> tuple[str, Post | None]:
            post = self._fetch_post_from_page(username, pid, item=item_by_id.get(pid))
            if not post:
                post = parse_item(
                        item_by_id.get(pid, {}), fallback_username=username,
                        extract_audio=self.extract_audio,
                    )
            return pid, post

        while True:
            if limit is not None and len(candidate_ids) >= limit:
                break
            if cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            pages += 1
            url = self._build_item_list_url(secuid, cursor)
            try:
                resp = self._request_with_rate_limit(
                    "GET",
                    url,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0.0.0 Safari/537.36"
                        ),
                        "Referer": f"https://www.tiktok.com/@{username}",
                    },
                )
            except Exception as exc:
                report.api_error = str(exc)
                break
            try:
                data = resp.json()
            except Exception as exc:
                report.api_error = f"invalid json: {exc}"
                break

            items = data.get("itemList") or data.get("item_list") or []
            if not items:
                break

            page_ids: list[str] = []
            for it in items:
                author = it.get("author") or {}
                if exclude_reposts:
                    author_username = author.get("uniqueId")
                    is_repost = False
                    if (
                        author_username
                        and str(author_username).lower() != username.lower()
                    ):
                        is_repost = True
                    if it.get("originalItem") is True:
                        is_repost = True
                    if is_repost:
                        report.reposts_skipped += 1
                        continue
                post_id = str(it.get("id") or "")
                if not post_id or post_id in seen_page_items:
                    continue
                seen_page_items.add(post_id)
                item_by_id[post_id] = it
                candidate_ids.append(post_id)
                page_ids.append(post_id)
                if limit is not None and len(candidate_ids) >= limit:
                    break

            report.pages_fetched = pages
            _warn(f"listing posts… page {pages} ({len(candidate_ids)} found)")

            # Rehydrate this page immediately (parallel), before next page.
            if page_ids:
                total_page = len(page_ids)
                workers = max(1, min(concurrency, total_page))
                done_page = 0
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futures = {ex.submit(_fetch_one, pid): pid for pid in page_ids}
                    for fut in as_completed(futures):
                        pid, post = fut.result()
                        done_page += 1
                        done_total += 1
                        if done_page == total_page or done_page % 5 == 0:
                            _warn(f"fetching posts… {done_total}/{len(candidate_ids)}")
                        if post and post.id not in seen_ids:
                            seen_ids.add(post.id)
                            posts.append(post)
                            yield post
                        elif not post:
                            report.unparsed += 1

            if limit is not None and len(candidate_ids) >= limit:
                break

            has_more = data.get("hasMore")
            if has_more is None:
                has_more = data.get("hasMorePrevious")
            if has_more is None:
                has_more = data.get("has_more")
            if not has_more:
                break
            try:
                new_cursor = int(items[-1].get("createTime", 0)) * 1000
            except (TypeError, ValueError):
                break
            if not new_cursor or new_cursor == cursor:
                break
            cursor = new_cursor

        if limit is not None:
            candidate_ids = candidate_ids[:limit]
            posts = posts[:limit]

        report.used_fallback = False
        report.truncated = False
        if not posts:
            _warn(f"no posts found for @{username}. The account may be empty or private.")
        _finish_report(report, posts)
        return

    def get_posts(
        self, username: str, *, limit: int | None = None, exclude_reposts: bool = True,
        concurrency: int = 4,
    ) -> list[Post]:
        return list(
            self.iter_posts(
                username, limit=limit, exclude_reposts=exclude_reposts,
                concurrency=concurrency,
            )
        )

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
