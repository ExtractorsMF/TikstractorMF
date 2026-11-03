"""Downloader — port of background.js:787-904 with post_id naming.

User rules:
  - media.id = post_id if post has a single media
  - media.id = f"{post_id}_{i}" for carousels (1-index)
  - filename = f"{media.id}.{ext}" (no datetime)
  - profile pic = f"{username}_profile.{ext}"
  - URL validated with isValidMediaUrl (CDN allowlist)
  - filename sanitized
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from tikstractormf._backend import (
    CurlClient,
    StreamClient,
    is_transient_error,
)
from tikstractormf.archive import DownloadLedger, find_existing_for_post
from tikstractormf.models import Media, sanitize_filename
from tikstractormf.rate_limit import BatchCooldownLimiter

# Allowlist same as background.js:112-116
# Hostname suffixes Meta serves media from.
#
# Matched as a *domain suffix*, never as a substring. Two reasons:
#   * the CDN rotates regional hosts (instagram.fpei1-1.fna.fbcdn.net,
#     scontent-xx.cdninstagram.com), so pinning literal hosts would go stale,
#     while a suffix keeps working for a POP we have never seen;
#   * a substring match accepts any host that merely embeds the name, which is
#     what let https://instagram.evil.test/x.jpg and
#     https://scontent.evil.com/v/t51.1/a.jpg through.
#
# "instagram" and "threads" are deliberately absent: those are brand names, not
# hostnames.
_CDN_HOST_SUFFIXES = (
    ".tiktok.com",
    ".tiktokv.com",
    ".tiktokcdn.com",
    ".tiktokcdn-us.com",
    ".tiktokcdn-eu.com",
    ".muscdn.com",
    ".byteoversea.com",
    ".ibytedtos.com",
)

# Global limiter por defecto (port background.js:16-17). Se puede inyectar otro por llamada.
_default_limiter = BatchCooldownLimiter(
    cooldown_ms=8000, batch_size=50, batch_cooldown_ms=180000, jitter=0.2, enabled=True
)

# Adaptive pacing: counter of consecutive 403s so a WAF block triggers a long
# pause instead of hammering the CDN. Defaults to a process-global counter;
# callers (e.g. Tiktokscraper) may pass their own per-instance counter.
_DEFAULT_CONSECUTIVE_403 = [0]


# Directories that already represent one media type. Landing in one of them is
# taken as "the caller decided where things go", so no further split happens.
CONTENT_DIRS = frozenset({"photos", "videos", "profile", "posts"})

# Browser-like headers: video endpoints (/v/t65.*) reject python-httpx UA with 403.
CDN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tiktok.com/",
    "Origin": "https://www.tiktok.com",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "video",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}


def _split_subfolder(media: Media, dest: Path) -> str | None:
    """Subdirectory for this media, or None to write straight into ``dest``.

    Behaviour (unchanged, previously inlined):

    ==================  ==========  ==========================
    media                dest.name   result
    ==================  ==========  ==========================
    avatar               any but     ``profile``
                        ``profile``
    any                  one of      written into dest
                        CONTENT_DIRS
    video                anything    ``videos``
                        else
    image                anything    ``photos``
                        else
    ==================  ==========  ==========================

    Note the folder comes from ``media.type`` (what the API declares), while the
    file extension comes from the URL. The two are independent: a ``.webm`` video
    is ``type="video"`` and therefore lands in ``videos/``.
    """
    if media.id.endswith("_profile"):
        return None if dest.name == "profile" else "profile"
    if getattr(media, "story", False):
        return None if dest.name == "stories" else "stories"
    if dest.name in CONTENT_DIRS:
        return None
    if media.type == "video":
        return "videos"
    if media.type == "audio":
        return "audio"
    return "photos"


def is_valid_media_url(url: str) -> bool:
    """Port background.js:96-144 isValidMediaUrl, with a domain-suffix host check."""
    if not url or not isinstance(url, str):
        return False
    try:
        u = urlparse(url)
        if u.scheme != "https":
            return False
        # A fully-qualified name may arrive with a trailing dot.
        host = u.netloc.lower().rstrip(".")
        # s[1:] allows the bare registrable domain (cdninstagram.com); otherwise
        # the dot must match exactly one label boundary, so cdninstagram.com.evil.io
        # and notcdninstagram.com are both rejected.
        if not any(host == s[1:] or host.endswith(s) for s in _CDN_HOST_SUFFIXES):
            return False
        path = u.path.lower()
        has_ext = bool(re.search(r"\.(jpg|jpeg|png|webp|gif|mp4|webm|mov|avi)(\?|$)", path))
        has_media_path = (
            "/v/t51." in path or "/image/" in path or "/video/" in path or "/media/" in path
            or "/obj/" in path or "/tos/" in path
        )
        has_qs = bool(u.query)
        return has_ext or has_media_path or has_qs
    except Exception:
        return False


# Retry policy for transient network errors (DNS resolution flaps, timeouts,
# CDN hiccups). Extra attempts give the resolver/network time to recover.
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_BACKOFF_S = (2.0, 5.0)

# Generous CDN timeouts: slow-yet-active transfers stay alive (read timeout is
# per-chunk, not total) and DNS/TLS setup gets breathing room on bad networks.
_CDN_TIMEOUT = httpx.Timeout(connect=15.0, read=45.0, write=45.0, pool=30.0)

# curl_cffi takes (connect, total): the second value bounds the whole transfer,
# so it must be well above the per-chunk httpx read budget or big videos on slow
# links get cut mid-download.
_CDN_CURL_TIMEOUT = (15.0, 900.0)


def _build_download_client(
    *, impersonate: str | None, headers: dict[str, str], cookies: Any = None
) -> StreamClient:
    """Create the CDN client: Chrome impersonation when asked, else httpx."""
    # Send all cookies as a raw Cookie header (gallery-dl style).
    cookie_parts = [f"{getattr(c, 'name', '')}={getattr(c, 'value', '')}"
                    for c in (cookies or ())]
    cookie_str = "; ".join(p for p in cookie_parts if "=" in p)
    merged = dict(headers)
    if cookie_str:
        merged["Cookie"] = cookie_str
    if impersonate:
        return CurlClient(
            impersonate=impersonate,
            cookies=None,
            headers=merged,
            connect_timeout=_CDN_CURL_TIMEOUT[0],
            total_timeout=_CDN_CURL_TIMEOUT[1],
        )
    return httpx.Client(
        cookies=None, follow_redirects=True, timeout=_CDN_TIMEOUT, http2=True, headers=merged
    )


class DownloadStatus(str, Enum):
    """Whether a media call actually fetched bytes.

    download_media used to return a bare Path from every branch, so a file the
    ledger already knew about was indistinguishable from a fresh download: the
    CLI announced both as "downloaded" and any summary built on it would have
    been fiction.
    """

    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"  # the ledger already had this (post_id, index)
    ADOPTED = "adopted"  # found on disk under another name
    BLOCKED = "blocked"  # skipped because the CDN returned 403 (WAF/rate limit)


@dataclass(frozen=True)
class DownloadOutcome:
    """Result of one media download: where it ended up, and how it got there."""

    path: Path
    status: DownloadStatus


def _status_code(exc: BaseException) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    curl = None
    try:
        from tikstractormf._backend import _curl_exc

        curl = _curl_exc()
    except Exception:
        curl = None
    if curl is not None and isinstance(exc, curl.HTTPError):
        resp = getattr(exc, "response", None)
        sc = getattr(resp, "status_code", None)
        return int(sc) if sc is not None else None
    sc = getattr(exc, "status_code", None)
    return int(sc) if sc is not None else None


def _is_forbidden(exc: BaseException) -> bool:
    return _status_code(exc) == 403


def _redact_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not headers:
        return out
    sensitive = ("cookie", "set-cookie", "authorization", "x-csrf-token")
    for k, v in headers.items():
        lk = str(k).lower()
        if lk in sensitive or "token" in lk:
            out[str(k)] = "[REDACTED]"
        else:
            out[str(k)] = str(v)
    return out


def _save_403_diagnostic(
    path: Path,
    *,
    media: Media,
    url: str,
    request_headers: dict[str, str],
    response: Any = None,
    exc: BaseException | None = None,
) -> None:
    try:
        body_snippet = ""
        resp_headers: dict[str, str] = {}
        status_code: int | None = None
        if response is not None:
            status_code = getattr(response, "status_code", None)
            try:
                resp_headers = _redact_headers(dict(getattr(response, "headers", {}) or {}))
            except Exception:
                resp_headers = {}
            try:
                text = getattr(response, "text", None)
                if text is None and hasattr(response, "content"):
                    text = response.content.decode("utf-8", errors="replace")
                if text is not None:
                    body_snippet = str(text)[:1024]
            except Exception:
                body_snippet = ""
        if status_code is None and exc is not None:
            status_code = _status_code(exc)
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "media_id": media.id,
            "post_id": media.post_id,
            "url": url,
            "status_code": status_code,
            "request_headers": _redact_headers(request_headers),
            "response_headers": resp_headers,
            "response_body_snippet": body_snippet,
            "exception": repr(exc) if exc is not None else None,
        }
        path.mkdir(parents=True, exist_ok=True)
        filename = f"403_{media.id}_{int(time.time() * 1000)}.json"
        text_out = json.dumps(payload, ensure_ascii=False, indent=2)
        (path / filename).write_text(text_out, encoding="utf-8")
    except Exception:
        pass


def _referer_for(media: Media, username: str | None) -> str:
    user = (username or "").lstrip("@").split("/")[0]
    if media.id.endswith("_profile") or media.post_id.endswith("_profile"):
        if user:
            return f"https://www.tiktok.com/@{user}"
        return "https://www.tiktok.com/"
    if user and media.post_id and not media.post_id.endswith("_profile"):
        return f"https://www.tiktok.com/@{user}/video/{media.post_id}"
    return "https://www.tiktok.com/"


def download_media(
    media: Media,
    dest: str | Path,
    *,
    client: StreamClient | None = None,
    impersonate: str | None = None,
    overwrite: bool = False,
    limiter: BatchCooldownLimiter | None = None,
    rate_limit: bool = True,
    filename_template: str | None = None,
    username: str | None = None,
    date_iso: str | None = None,
    ledger: DownloadLedger | None = None,
    adopt_existing: bool = True,
    headers: dict[str, str] | None = None,
    cookies: Any = None,
    diagnostics_dir: str | Path | None = None,
    skip_on_403: bool = False,
    consecutive_403: list[int] | None = None,
) -> DownloadOutcome:
    """Download a Media to dest.

    Returns where it landed plus whether bytes were actually fetched: a media
    the ledger already knew about returns SKIPPED, not DOWNLOADED.

    Applies anti rate-limit if ``rate_limit=True``. When ``filename_template`` is
    given it generates the filename (gallery-dl style).

    Skipping happens at three levels, cheapest first:

    1. ``(post_id, index)`` already in ``ledger`` — immune to the filename
       changing (a fixed extension guess, a different template, a date that
       rolled over);
    2. the exact destination file exists — records it in the ledger;
    3. with ``adopt_existing``, a file for the same ``post_id`` under a different
       name is adopted — this is what saves a historical archive from being
       re-downloaded when the naming scheme is corrected.

    Levels 2 and 3 make the ledger self-seeding: the first run after upgrading
    behaves exactly as before (filename dedup) while filling the ledger.
    ``overwrite`` bypasses all three.
    """
    if not is_valid_media_url(media.url):
        raise ValueError(f"invalid download URL: {media.url}")

    root = Path(dest).expanduser().resolve()
    # Split photos/videos/profile only when dest is not already one of them.
    # The ledger key is derived from `root`, BEFORE the split, so one account
    # keeps a single ledger instead of one per media type.
    subfolder = _split_subfolder(media, root)
    dest = root / subfolder if subfolder else root
    dest.mkdir(parents=True, exist_ok=True)

    if filename_template:
        # For profile, ignore date template and use simple username
        if media.id.endswith("_profile") and "{date" in filename_template:
            filename = media.filename(
                template="{username}_profile.{extension}", username=username, date_iso=date_iso
            )
        else:
            filename = media.filename(
                template=filename_template, username=username, date_iso=date_iso
            )
    else:
        filename = media.filename()
    filename = sanitize_filename(filename)
    out = dest / filename

    if not overwrite:
        # 1. the ledger knows about it under any name
        if ledger is not None and ledger.has(media.post_id, media.index):
            return DownloadOutcome(out, DownloadStatus.SKIPPED)
        # 2. the exact file is already there -> adopt it into the ledger
        if out.exists():
            if ledger is not None:
                ledger.record(media.post_id, media.index, out)
            return DownloadOutcome(out, DownloadStatus.ADOPTED)
        # 3. the same post under a *different* name (extension fix, template
        #    change, date rollover). The ledger starts empty on the first run
        #    after such a change, so without this the whole archive re-downloads.
        if adopt_existing and ledger is not None:
            adopted = find_existing_for_post(dest, media.post_id, media.index)
            if adopted is not None:
                ledger.record(media.post_id, media.index, adopted)
                return DownloadOutcome(adopted, DownloadStatus.ADOPTED)

    # Anti rate-limit: wait cooldown before downloading (port background.js:789-792)
    use_limiter = limiter if limiter is not None else _default_limiter
    # allow disabling globally
    should_wait = rate_limit and use_limiter.enabled
    if should_wait:
        use_limiter.wait()

    close_client = False
    if client is None:
        client = _build_download_client(
            impersonate=impersonate, headers=CDN_HEADERS, cookies=cookies
        )
        close_client = True

    # Retry loop for transient network errors; writes go to a .part temp file
    # so a failed attempt never leaves a truncated media at the final path.
    # 403 from Akamai is also retried (short backoff) instead of aborting.
    import sys
    import time as _time

    tmp = out.with_name(out.name + ".part")
    last_exc: BaseException | None = None
    urls_to_try = [media.url] + [
        u for u in getattr(media, "fallback_urls", []) or []
        if u and u != media.url
    ]
    saved_403_for_media = False
    counter = consecutive_403 if consecutive_403 is not None else _DEFAULT_CONSECUTIVE_403
    try:
        for url_idx, download_url in enumerate(urls_to_try):
            for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
                # Build per-request headers: CDN defaults + specific Referer +
                # caller overrides. Cookies are sent both by the client jar and
                # as a raw Cookie header (gallery-dl style) when available.
                req_headers = dict(CDN_HEADERS)
                if attempt == 1:
                    req_headers["Referer"] = _referer_for(media, username)
                else:
                    req_headers["Referer"] = "https://www.tiktok.com/"
                if headers:
                    req_headers.update(headers)
                cookie_str = None
                if cookies is not None:
                    try:
                        parts = [
                            f"{getattr(c, 'name', '')}={getattr(c, 'value', '')}"
                            for c in (cookies or ())
                        ]
                        cookie_str = "; ".join(p for p in parts if "=" in p)
                    except Exception:
                        cookie_str = None
                if cookie_str:
                    req_headers["Cookie"] = cookie_str
                try:
                    with client.stream("GET", download_url, headers=req_headers) as r:
                        if r.status_code >= 400:
                            try:
                                r.read()
                            except Exception:
                                pass
                        r.raise_for_status()
                        with tmp.open("wb") as f:
                            for chunk in r.iter_bytes(chunk_size=8192):
                                f.write(chunk)
                    tmp.replace(out)
                    # Reset the consecutive-403 counter on success.
                    counter[0] = 0
                    # El cooldown de lote se cuenta solo tras una descarga exitosa.
                    if hasattr(use_limiter, "increment"):
                        use_limiter.increment()
                    if ledger is not None:
                        ledger.record(media.post_id, media.index, out)
                    return DownloadOutcome(out, DownloadStatus.DOWNLOADED)
                except Exception as e:
                    last_exc = e
                    tmp.unlink(missing_ok=True)
                    retryable = is_transient_error(e) or _is_forbidden(e)
                    # Persist the first 403 for diagnosis.
                    if _is_forbidden(e) and not saved_403_for_media and diagnostics_dir is not None:
                        saved_403_for_media = True
                        resp_obj = getattr(e, "response", None)
                        _save_403_diagnostic(
                            Path(diagnostics_dir),
                            media=media,
                            url=download_url,
                            request_headers=req_headers,
                            response=resp_obj,
                            exc=e,
                        )
                    if _is_forbidden(e):
                        counter[0] += 1
                        if skip_on_403:
                            tmp.unlink(missing_ok=True)
                            return DownloadOutcome(out, DownloadStatus.BLOCKED)
                        if counter[0] >= 3:
                            print(
                                "  3+ consecutive 403s; pausing 60s...",
                                file=sys.stderr, flush=True,
                            )
                            _time.sleep(60.0)
                    if attempt < _DOWNLOAD_ATTEMPTS and retryable:
                        if _is_forbidden(e):
                            wait_s = random.uniform(15.0, 30.0)
                        else:
                            wait_s = _DOWNLOAD_BACKOFF_S[
                                min(attempt - 1, len(_DOWNLOAD_BACKOFF_S) - 1)
                            ]
                        print(
                            f"  {media.id} retrying ({attempt}/{_DOWNLOAD_ATTEMPTS - 1}) after "
                            f"{wait_s:.0f}s: {e}",
                            file=sys.stderr,
                            flush=True,
                        )
                        _time.sleep(wait_s)
                        continue
                    break
            if url_idx < len(urls_to_try) - 1:
                print(
                    f"  {media.id} trying fallback URL...",
                    file=sys.stderr,
                    flush=True,
                )
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("download loop exited without result") from last_exc
    finally:
        if close_client:
            client.close()


def download_profile_pic(
    url: str,
    username: str,
    dest: str | Path,
    *,
    client: StreamClient | None = None,
    impersonate: str | None = None,
    limiter: BatchCooldownLimiter | None = None,
    rate_limit: bool = True,
    filename_template: str | None = None,
    ledger: DownloadLedger | None = None,
    adopt_existing: bool = True,
    headers: dict[str, str] | None = None,
    cookies: Any = None,
    diagnostics_dir: str | Path | None = None,
    skip_on_403: bool = False,
    consecutive_403: list[int] | None = None,
) -> DownloadOutcome:
    """Download profile pic with id f"{username}_profile"."""
    from tikstractormf._parsing import _guess_ext
    from tikstractormf.models import Media

    ext = _guess_ext(url)
    media = Media(
        id=f"{sanitize_filename(username)}_profile",
        post_id=f"{sanitize_filename(username)}_profile",
        index=1,
        type="image",
        url=url,
        ext=ext,
    )
    if filename_template:
        return download_media(
            media, dest, client=client, impersonate=impersonate, limiter=limiter,
            rate_limit=rate_limit, filename_template=filename_template, username=username,
            ledger=ledger, adopt_existing=adopt_existing, headers=headers,
            cookies=cookies, diagnostics_dir=diagnostics_dir,
            skip_on_403=skip_on_403, consecutive_403=consecutive_403,
        )
    return download_media(
        media, dest, client=client, impersonate=impersonate, limiter=limiter,
        rate_limit=rate_limit, username=username, ledger=ledger,
        adopt_existing=adopt_existing, headers=headers, cookies=cookies,
        diagnostics_dir=diagnostics_dir, skip_on_403=skip_on_403,
        consecutive_403=consecutive_403,
    )
