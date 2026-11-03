"""gallery-dl / yt-dlp style CLI — entry point: tikstractormf."""

from __future__ import annotations

import http.cookiejar
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import typer
from rich.console import Console

from tikstractormf import __version__
from tikstractormf.api import ScrapeReport
from tikstractormf.auth import load_netscape_cookies
from tikstractormf.downloader import DownloadOutcome, DownloadStatus, _is_forbidden

try:  # browser_cookie3 is an optional extra
    from browser_cookie3 import BrowserCookieError as _BrowserCookieError
except ImportError:  # pragma: no cover

    class _BrowserCookieError(Exception):  # type: ignore[no-redef]
        pass

app = typer.Typer(
    add_completion=False,
    help="tikstractormf — TikTok downloader (post_id naming, gallery-dl cookies)",
)
console = Console()
err_console = Console(stderr=True)


# stdout carries the "one line per downloaded file" contract that scrapmf counts,
# so nothing printed to it may be wrapped. Rich wraps at the terminal width
# (80 columns when piped), which silently turned a single 600-character CDN URL
# into eight lines and made an orchestrator count eight files instead of one.
# ``soft_wrap=True`` disables that for the lines we emit.


def _count(outcome: DownloadOutcome, report: ScrapeReport) -> None:
    """Tally one media outcome so the summary can tell the truth."""
    if outcome.status is DownloadStatus.DOWNLOADED:
        report.downloaded += 1
    elif outcome.status is DownloadStatus.BLOCKED:
        report.failed += 1
    else:
        report.skipped += 1


def _report_scrape(report: ScrapeReport) -> None:
    """Print what the scrape actually found, and what it could not.

    Goes to stderr: stdout is the "one line per downloaded file" contract, and
    a summary there would be counted as a file by scrapmf. Before this, a
    scrape that returned 4 posts out of a 60-post profile ended in complete
    silence and was indistinguishable from a full one.
    """
    parts = [
        f"posts: {report.posts}",
        f"media: {report.media}",
        f"downloaded: {report.downloaded}",
        f"skipped: {report.skipped}",
    ]
    # Only when it is non-zero: `filtered: 0` on every unfiltered run is noise,
    # and it only explains a number the reader cannot otherwise reconcile.
    if report.filtered:
        parts.append(f"filtered: {report.filtered}")
    parts.append(f"failed: {report.failed}")
    if report.profile_pic:
        parts.append("profile pic: 1")
    # `downloaded: 0` next to a non-zero `skipped` reads as a failed run when it
    # is the opposite: every file was already there. Say so.
    if not report.downloaded and report.skipped:
        err_console.print(
            f"note — nothing was downloaded because all {report.skipped} "
            "file(s) were already on disk.",
            soft_wrap=True,
        )
    if report.no_media:
        parts.append(f"without media: {report.no_media}")
    if report.reposts_skipped:
        parts.append(f"reposts excluded: {report.reposts_skipped}")
    if report.unparsed:
        parts.append(f"unparseable: {report.unparsed}")
    err_console.print("summary — " + " | ".join(parts), soft_wrap=True)

    if report.api_error:
        err_console.print(
            f"warning — the API did not respond as expected: {report.api_error}",
            soft_wrap=True,
        )
    if report.truncated:
        # Only when the API walk is the one that delivered the posts. With the
        # fallback, truncation is folded into the message above: printing both
        # claimed the profile was read in full and cut short at the same time,
        # and the reason quoted belonged to the API attempt that had already
        # handed over.
        err_console.print(
            f"warning — the walk stopped before exhausting the profile "
            f"({report.stop_reason or 'unknown reason'}).",
            soft_wrap=True,
        )
    if report.failed:
        err_console.print(
            f"warning — {report.failed} media item(s) could not be downloaded.",
            soft_wrap=True,
        )


def _version_callback(value: bool) -> None:
    """Print the version and exit.

    Eager and callback-style rather than a plain option in main(): the target
    argument is required, so a plain flag could not run before typer rejected
    the missing argument. A callback with is_eager runs first and exits.
    """
    if value:
        # __version__ is what semantic-release rewrites (version_variables in
        # pyproject.toml), so this can never drift from the released build.
        console.print(f"tikstractormf {__version__}", soft_wrap=True)
        raise typer.Exit()


@app.command()
def main(
    target: str = typer.Argument(..., help="URL https://www.tiktok.com/@user or @username"),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed version and exit",
    ),
    cookies: Path | None = typer.Option(
        None, "--cookies", "-c", help="Netscape cookies.txt (gallery-dl compatible)"
    ),
    cookies_from_browser: str | None = typer.Option(
        None,
        "--cookies-from-browser",
        help="brave|chrome|firefox (reads local DB, like gallery-dl)",
    ),
    dest: Path = typer.Option(Path("./dl"), "--dest", "-d", help="Output directory"),
    limit: int | None = typer.Option(None, "--limit", "-l", help="Posts limit (default all)"),
    profile_pic_only: bool = typer.Option(False, "--profile-pic-only", help="Profile pic only"),
    photos_only: bool = typer.Option(False, "--photos-only", help="Photos only"),
    videos_only: bool = typer.Option(False, "--videos-only", help="Videos only"),
    audio_only: bool = typer.Option(False, "--audio-only", help="Audio only"),
    with_audio: bool = typer.Option(False, "--with-audio", help="Extract audio from photo posts"),
    # Chrome impersonation is always on (TikTok WAF blocks plain httpx).
    # Kept as a local default so the rest of the flow is unchanged.
    impersonate: str | None = "chrome",
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing files"),
    no_rate_limit: bool = typer.Option(
        False, "--no-rate-limit", help="Disable anti rate-limit (not recommended)"
    ),
    cooldown: int = typer.Option(
        8000, "--cooldown", help="Cooldown between downloads ms (default 8000)"
    ),
    batch_cooldown: int = typer.Option(
        180000, "--batch-cooldown", help="Cooldown every 100 downloads ms (default 120000)"
    ),
    rps: float = typer.Option(2.0, "--rps", help="Requests/s for item_list API (default 2.0)"),
    concurrency: int = typer.Option(4, "--concurrency", help="Parallel post fetches (default 4)"),
    download_concurrency: int = typer.Option(
        1, "--download-concurrency", help="Parallel downloads (default 1)"
    ),
    diagnostics_dir: Path | None = typer.Option(
        None, "--diagnostics-dir", help="Directory to save 403 diagnostics"
    ),
    skip_on_403: bool = typer.Option(
        False, "--skip-on-403", help="Skip media that returns 403 instead of retrying"
    ),
    retry_failed: str | None = typer.Option(
        None, "--retry-failed", help="Comma-separated media IDs to retry"
    ),
    filename_template: str | None = typer.Option(
        None,
        "--filename-template",
        help="Template filename like gallery-dl: {date:%Y-%m-%d}_{post_id}_{num:02d}.{extension}",
    ),
    directory_template: str | None = typer.Option(
        None,
        "--directory-template",
        help="Template directory, e.g. {scrapmf_root}/{category}/{username}/{subcategory}",
    ),
    archive: bool = typer.Option(
        False,
        "--archive",
        help="Keep a ledger of downloads in <dest>/.archive so re-runs skip them",
    ),
    no_adopt_existing: bool = typer.Option(
        False,
        "--no-adopt-existing",
        help="Do not adopt files already on disk under an older name",
    ),
    get_urls: bool = typer.Option(
        False,
        "--get-urls",
        help="Print the media URLs instead of downloading (dry run)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show progress messages (listing/fetching posts)",
    ),
) -> None:
    """Download TikTok media by post_id."""
    # normalize @user — supports tiktok.com URLs or @handle
    import re as _re

    if "tiktok.com" in target:
        m = _re.search(r"/@([^/]+)", target)
        if m:
            username = m.group(1)
        else:
            username = target.strip().rstrip("/").split("/")[-1].lstrip("@")
    else:
        username = target.lstrip("@").split("/")[0]
    if not username:
        console.print("[red]Invalid target. Use @username or URL[/red]")
        raise typer.Exit(1)

    # Validate --impersonate before any work. The check otherwise only runs when the
    # first download builds the client, so a typo surfaced minutes later — or
    # never, on a profile with no media.
    if impersonate:
        from tikstractormf._backend import known_impersonate_targets

        if impersonate not in known_impersonate_targets():
            err_console.print(
                f"[red]unknown --impersonate target: {impersonate!r}[/red]\n"
                f"Valid targets include: "
                f"{', '.join(sorted(known_impersonate_targets())[:8])}"
            )
            raise typer.Exit(1)

    # Load cookies before touching the network, and report a bad file, a missing
    # browser or an unknown browser name as a plain message instead of a
    # traceback: exporting cookies as JSON is the most common mistake, and the
    # LoadError text is what explains it.
    cookies_arg: Any
    try:
        if cookies_from_browser:
            from tikstractormf.auth import load_from_browser

            cookies_arg = load_from_browser(cookies_from_browser)
        elif cookies is not None:
            if not cookies.exists():
                err_console.print(f"[red]cookies file not found: {cookies}[/red]")
                raise typer.Exit(1)
            cookies_arg = load_netscape_cookies(cookies)
        else:
            err_console.print(
                "[red]--cookies or --cookies-from-browser required "
                "(gallery-dl compatible)[/red]"
            )
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except (
        http.cookiejar.LoadError,
        OSError,
        ValueError,
        # browser_cookie3.BrowserCookieError subclasses Exception directly, so it
        # is not caught by the OSError/ValueError branches above; its message is
        # the actionable one ("Failed to find cookies for Chrome browser").
        _BrowserCookieError,
    ) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except ImportError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    _execute_scrape(
        username=username,
        cookies_arg=cookies_arg,
        dest=dest,
        limit=limit,
        profile_pic_only=profile_pic_only,
        photos_only=photos_only,
        videos_only=videos_only,
        audio_only=audio_only,
        with_audio=with_audio,
        impersonate=impersonate,
        overwrite=overwrite,
        no_rate_limit=no_rate_limit,
        cooldown=cooldown,
        batch_cooldown=batch_cooldown,
        rps=rps,
        concurrency=concurrency,
        download_concurrency=download_concurrency,
        diagnostics_dir=diagnostics_dir,
        skip_on_403=skip_on_403,
        retry_failed=retry_failed,
        filename_template=filename_template,
        directory_template=directory_template,
        archive=archive,
        no_adopt_existing=no_adopt_existing,
        get_urls=get_urls,
        verbose=verbose,
    )


def _url_expired(url: str) -> bool:
    try:
        qs = parse_qs(urlparse(url).query)
        for key in ("expire", "expires"):
            if key in qs:
                return time.time() > int(qs[key][0])
    except Exception:
        return False
    return False


def _execute_scrape(
    username: str,
    cookies_arg: Any,
    dest: Path,
    limit: int | None,
    profile_pic_only: bool,
    photos_only: bool,
    videos_only: bool,
    audio_only: bool,
    with_audio: bool,
    impersonate: str | None,
    overwrite: bool,
    no_rate_limit: bool,
    cooldown: int,
    batch_cooldown: int,
    rps: float,
    concurrency: int,
    download_concurrency: int = 3,
    diagnostics_dir: Path | None = None,
    skip_on_403: bool = False,
    retry_failed: str | None = None,
    filename_template: str | None = None,
    directory_template: str | None = None,
    archive: bool = False,
    no_adopt_existing: bool = False,
    get_urls: bool = False,
    verbose: bool = False,
) -> None:
    from tikstractormf.client import Tiktokscraper

    if audio_only:
        with_audio = True
    scraper = Tiktokscraper(
        cookies=cookies_arg,
        impersonate=impersonate,
        rate_limit=not no_rate_limit,
        cooldown_ms=cooldown,
        cooldown_after_100_ms=batch_cooldown,
        rps=rps,
        archive=archive,
        diagnostics_dir=diagnostics_dir,
        with_audio=with_audio,
        verbose=verbose,
    )
    adopt_existing = not no_adopt_existing
    # resolve directory_template -> final dest
    final_dest = dest
    if diagnostics_dir is None:
        scraper.diagnostics_dir = final_dest / ".diagnostics"
    if directory_template:
        from tikstractormf.template import render_directory

        # scrapmf passes scrapmf_root/profile; default used otherwise
        subcat = "profile" if profile_pic_only else "posts"
        rendered = render_directory(
            directory_template, username=username, category="tiktok", subcategory=subcat
        )
        # si dest es ~/Descargas y template es {scrapmf_root}/threads/... lo unimos
        # If template is already absolute, use it as-is
        if rendered.startswith("/"):
            final_dest = Path(rendered)
        else:
            # For All, rendered is "<user_root>" without '/', goes to dest/<user_root>
            final_dest = dest / rendered if rendered else dest
        # Defence in depth. render_directory already reduces {username} to a
        # single component, but a relative template must not be able to walk out
        # of --dest either way: an absolute template is a deliberate choice and
        # is honoured, a relative one may not escape. Catching it here turns a
        # silent write into an error, instead of files landing one directory up.
        if not rendered.startswith("/"):
            root_dir = dest.expanduser().resolve()
            if not final_dest.resolve().is_relative_to(root_dir):
                err_console.print(
                    f"[red]--directory-template resolves outside --dest: "
                    f"{final_dest.resolve()} (escapes {root_dir})[/red]"
                )
                raise typer.Exit(1)
        if not get_urls:
            # A dry run must leave the filesystem untouched, so it does not
            # create the directory tree it would have written into.
            final_dest.mkdir(parents=True, exist_ok=True)

    try:
        if profile_pic_only:
            if get_urls:
                # One URL per line, same shape as the per-file lines scrapmf
                # counts, so a dry run reads the same way.
                profile = scraper.get_profile(username)
                if profile.profile_pic_url:
                    console.print(profile.profile_pic_url, soft_wrap=True)
                return
            out = scraper.download_profile_pic(
                username, final_dest, filename_template=filename_template,
                skip_on_403=skip_on_403,
            )
            console.print(f"[green]profile pic -> {out}[/green]", soft_wrap=True)
            return
        is_all = not photos_only and not videos_only and not profile_pic_only
        retry_ids = set()
        if retry_failed:
            for part in retry_failed.split(","):
                part = part.strip()
                if part:
                    retry_ids.add(part)

        report = None
        failed_ids: list[str] = []

        def _download_one(post, media):
            nonlocal report
            if report is None:
                report = scraper.last_report
            refreshed = False
            while True:
                try:
                    outcome = scraper.download(
                        media,
                        final_dest,
                        overwrite=overwrite,
                        filename_template=filename_template,
                        username=username,
                        date_iso=post.datetime_iso,
                        adopt_existing=adopt_existing,
                        skip_on_403=skip_on_403,
                    )
                    _count(outcome, report)
                    if outcome.status is DownloadStatus.BLOCKED:
                        err_console.print(
                            f"[yellow]  {media.id} blocked (403): skipped[/yellow]",
                            soft_wrap=True,
                        )
                    else:
                        console.print(f"  {media.id} -> {outcome.path}", soft_wrap=True)
                    break
                except Exception as e:
                    if (not refreshed) and (_is_forbidden(e) or _url_expired(media.url)):
                        refreshed = True
                        try:
                            fresh_post = scraper.api._fetch_post_from_page(
                                username, media.post_id,
                                item=scraper.api._item_by_id.get(media.post_id),
                            )
                            fresh_media = None
                            if fresh_post is not None:
                                for m in fresh_post.media:
                                    if m.index == media.index:
                                        fresh_media = m
                                        break
                            if fresh_media is not None:
                                media.url = fresh_media.url
                                media.fallback_urls = list(fresh_media.fallback_urls)
                                continue
                        except Exception:
                            pass
                    report.failed += 1
                    failed_ids.append(media.id)
                    err_console.print(f"[yellow]  {media.id} failed: {e}[/yellow]")
                    break

        from concurrent.futures import ThreadPoolExecutor

        futures = []
        with ThreadPoolExecutor(max_workers=max(1, download_concurrency or 1)) as ex:
            for post in scraper.iter_posts(
                username, limit=limit, concurrency=concurrency
            ):
                if report is None:
                    report = scraper.last_report
                for media in post.media:
                    if retry_ids and media.id not in retry_ids:
                        continue
                    if photos_only and media.type != "image":
                        report.filtered += 1
                        continue
                    if videos_only and media.type != "video":
                        report.filtered += 1
                        continue
                    if audio_only and media.type != "audio":
                        report.filtered += 1
                        continue
                    if media.type == "audio" and not with_audio:
                        report.filtered += 1
                        continue
                    if get_urls:
                        # Dry run: print the URL and move on, leaving the pacing
                        # limiter alone (a scrape still must not hammer the API).
                        console.print(media.url, soft_wrap=True)
                        continue
                    futures.append(ex.submit(_download_one, post, media))
            for f in futures:
                f.result()

        if report is None:
            report = scraper.last_report
        if report is None:
            report = ScrapeReport(limit=limit)

        # For All (no filter), also download profile pic after posts as sibling
        if is_all and not get_urls:
            try:
                profile_dest = final_dest
                if profile_dest.name in {"photos", "videos", "profile", "posts"}:
                    profile_dest = profile_dest.parent
                outcome = scraper.download_profile_pic(
                    username, profile_dest, filename_template=filename_template,
                    skip_on_403=skip_on_403,
                )
                # Not counted through _count: the avatar is not one of the
                # posts' media, so tallying it made downloaded+skipped exceed
                # `media` and broke the identity the summary is read by.
                # Reported on its own line instead.
                report.profile_pic = outcome.path is not None
                console.print(f"  {username}_profile -> {outcome.path}", soft_wrap=True)
            except Exception as exc:
                report.profile_pic = False
                err_console.print(f"[yellow]  profile pic failed: {exc}[/yellow]")

        if failed_ids:
            err_console.print(
                "failed ids: " + ", ".join(failed_ids), soft_wrap=True
            )
        # En modo dry-run (--get-urls) no se descarga nada: imprimir el tally
        # de descargas (downloaded=0/skipped=0) seria engañoso, asi que se omite.
        if not get_urls:
            _report_scrape(report)


    finally:
        scraper.close()

if __name__ == "__main__":
    app()
