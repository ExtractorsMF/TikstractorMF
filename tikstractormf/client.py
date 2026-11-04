"""Main facade — import tikstractormf / Tiktokscraper.

Usage:
    from tikstractormf import Tiktokscraper
    scraper = Tiktokscraper(cookies="cookies.txt")
    posts = scraper.get_posts("user", limit=50)
    scraper.download(posts[0].media[0], dest="./dl")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from tikstractormf.api import ScrapeReport, TiktokAPI
from tikstractormf.archive import (
    DownloadLedger,
    ledger_path_for,
    migrate_legacy_ledger,
)
from tikstractormf.auth import load_cookies_dict, load_netscape_cookies
from tikstractormf.downloader import (
    DownloadOutcome,
    download_media,
    download_profile_pic,
)
from tikstractormf.models import Media, Post, Profile


def _coerce_cookies(cookies: str | Path | dict[str, str] | httpx.Cookies | Any) -> Any:
    """Normalise every accepted ``cookies`` input into a CookieJar.

    A plain dict was previously stored as-is: ``get_csrf_token`` still worked
    through its fallback, but the Playwright path received a mapping where it
    expected Cookie objects and produced an *empty* cookie list, so the DOM
    scraper ran with no session at all. Normalising here means one shape for
    every consumer.
    """
    if isinstance(cookies, dict):
        return load_cookies_dict(cookies, domain=".tiktok.com")
    return cookies


class Tiktokscraper:
    def __init__(
        self,
        cookies: str | Path | httpx.Cookies | Any,
        *,
        impersonate: str | None = "chrome",
        timeout: float = 15.0,
        rate_limit: bool = True,
        cooldown_ms: int = 8000,
        cooldown_after_100_ms: int = 180000,
        rps: float = 2.0,
        archive: bool = False,
        diagnostics_dir: str | Path | None = None,
        with_audio: bool = False,
        verbose: bool = False,
    ):
        if isinstance(cookies, (str, Path)):
            cookies = load_netscape_cookies(cookies)
        cookies = _coerce_cookies(cookies)
        self.cookies = cookies
        # Chrome impersonation by default (TikTok WAF blocks plain httpx).
        # Fall back to no impersonation if curl_cffi is not installed.
        if impersonate == "chrome":
            try:
                import curl_cffi  # noqa: F401
            except ImportError:
                import sys
                print(
                    "tikstractormf: curl_cffi not installed; "
                    "disabling Chrome impersonation. "
                    "Install with: pip install 'tikstractormf[antibot]'",
                    file=sys.stderr,
                )
                impersonate = None
        self.impersonate = impersonate
        # Download ledger, keyed by (post_id, index) so a corrected extension or
        # a different filename template never re-downloads an existing archive.
        self.archive = archive
        self.diagnostics_dir = diagnostics_dir
        self.with_audio = with_audio
        self.verbose = verbose
        self._consecutive_403: list[int] = [0]
        self._ledgers: dict[str, DownloadLedger] = {}
        self._download_client: Any = None
        from tikstractormf.rate_limit import BatchCooldownLimiter, RateLimitConfig

        self.rate_limit = rate_limit
        self.download_limiter = BatchCooldownLimiter(
            cooldown_ms=cooldown_ms if rate_limit else 0,
            batch_size=50,
            batch_cooldown_ms=cooldown_after_100_ms if rate_limit else 0,
            enabled=rate_limit,
        )
        cfg = RateLimitConfig(
            cooldown_ms=cooldown_ms,
            cooldown_after_100_ms=cooldown_after_100_ms,
            rps=rps,
            enabled=rate_limit,
        )
        self.api = TiktokAPI(
            cookies, impersonate=impersonate, timeout=timeout, rate_limit_config=cfg,
            extract_audio=with_audio,
            verbose=verbose,
        )

    # --- API delegada ---
    def get_profile(self, username: str) -> Profile:
        return self.api.get_profile(username)

    def iter_posts(
        self, username: str, *, limit: int | None = None, exclude_reposts: bool = True,
        concurrency: int = 4,
    ):
        return self.api.iter_posts(
            username, limit=limit, exclude_reposts=exclude_reposts,
            concurrency=concurrency,
        )

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

    def iter_stories(self, username: str, *, limit: int | None = None, concurrency: int = 4):
        return self.api.iter_stories(username, limit=limit, concurrency=concurrency)

    def get_stories(
        self, username: str, *, limit: int | None = None, concurrency: int = 4
    ) -> list[Post]:
        return list(self.iter_stories(username, limit=limit, concurrency=concurrency))

    @property
    def last_report(self) -> ScrapeReport:
        """Counters for the most recent get_posts() call."""
        return self.api.last_report

    def _get_download_client(self) -> Any:
        """Cliente de descarga reutilizable (lazy).

        Reuse the API client's cookie jar when possible: TikTok refreshes
        ``msToken`` on the item_list response and the CDN signature is bound
        to that fresh value (gallery-dl relies on the same session).
        """
        if self._download_client is None:
            from tikstractormf.downloader import CDN_HEADERS, _build_download_client

            # Prefer the CookieJar (with rotated msToken captured from item_list)
            # over the empty httpx cookie jar. gallery-dl keeps one live session.
            cookies = self.cookies
            self._download_client = _build_download_client(
                impersonate=self.impersonate, headers=CDN_HEADERS, cookies=cookies
            )
        return self._download_client

    # --- download ---
    def download(
        self,
        media: Media,
        dest: str | Path,
        *,
        overwrite: bool = False,
        filename_template: str | None = None,
        username: str | None = None,
        date_iso: str | None = None,
        adopt_existing: bool = True,
        diagnostics_dir: str | Path | None = None,
        skip_on_403: bool = False,
    ) -> DownloadOutcome:
        diag = diagnostics_dir if diagnostics_dir is not None else self.diagnostics_dir
        # Use the CookieJar (with rotated msToken captured from item_list).
        cookies = self.cookies
        return download_media(
            media,
            dest,
            client=self._get_download_client(),
            overwrite=overwrite,
            impersonate=self.impersonate,
            limiter=self.download_limiter,
            rate_limit=self.rate_limit,
            filename_template=filename_template,
            username=username,
            date_iso=date_iso,
            ledger=self._ledger_for(dest),
            adopt_existing=adopt_existing,
            cookies=cookies,
            diagnostics_dir=diag,
            skip_on_403=skip_on_403,
            consecutive_403=self._consecutive_403,
        )

    def _ledger_for(self, dest: str | Path) -> DownloadLedger | None:
        """Ledger covering ``dest``, or None when archiving is disabled.

        One ledger per destination root — not per photos/ videos/ subdirectory —
        created and loaded on first use. Archiving is opt-in, so without it no
        ledger object is built and no directory is written.
        """
        if not self.archive:
            return None
        key = str(Path(dest).expanduser().resolve())
        ledger = self._ledgers.get(key)
        if ledger is None:
            # Carry over a pre-1.1.1 ledger before reading, so renaming the
            # bookkeeping directory does not re-download the whole library.
            migrate_legacy_ledger(key)
            ledger = DownloadLedger(ledger_path_for(key))
            ledger.load()
            self._ledgers[key] = ledger
        return ledger

    def download_many(
        self,
        medias: list[Media],
        dest: str | Path,
        *,
        overwrite: bool = False,
        filename_template: str | None = None,
        username: str | None = None,
        date_iso: str | None = None,
        concurrency: int = 1,
        download_concurrency: int | None = None,
        diagnostics_dir: str | Path | None = None,
    ) -> list[DownloadOutcome]:
        if download_concurrency is not None:
            concurrency = download_concurrency
        if concurrency is None or concurrency < 1:
            concurrency = 1

        def _one(m: Media) -> DownloadOutcome:
            return self.download(
                m,
                dest,
                overwrite=overwrite,
                filename_template=filename_template,
                username=username,
                date_iso=date_iso,
                diagnostics_dir=diagnostics_dir,
            )

        if concurrency <= 1 or len(medias) <= 1:
            out: list[DownloadOutcome] = []
            for m in medias:
                out.append(_one(m))
            return out

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = [ex.submit(_one, m) for m in medias]
            return [f.result() for f in futures]

    def download_profile_pic(
        self, username: str, dest: str | Path, *, filename_template: str | None = None,
        date_iso: str | None = None, overwrite: bool = False,
        diagnostics_dir: str | Path | None = None,
        skip_on_403: bool = False,
    ) -> DownloadOutcome:
        profile = self.get_profile(username)
        if not profile.profile_pic_url:
            raise ValueError(f"No profile_pic_url for {username}")
        diag = diagnostics_dir if diagnostics_dir is not None else self.diagnostics_dir
        cookies = self.cookies
        return download_profile_pic(
            profile.profile_pic_url,
            username,
            dest,
            client=self._get_download_client(),
            impersonate=self.impersonate,
            limiter=self.download_limiter,
            rate_limit=self.rate_limit,
            filename_template=filename_template,
            date_iso=date_iso,
            overwrite=overwrite,
            ledger=self._ledger_for(dest),
            cookies=cookies,
            diagnostics_dir=diag,
            skip_on_403=skip_on_403,
            consecutive_403=self._consecutive_403,
        )

    def close(self) -> None:
        if self._download_client is not None:
            try:
                self._download_client.close()
            except Exception:
                pass
            self._download_client = None
        self.api.close()

    def __enter__(self) -> Tiktokscraper:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
