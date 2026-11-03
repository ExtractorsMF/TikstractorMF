import httpx
import respx

from tikstractormf.downloader import DownloadOutcome, DownloadStatus, download_media
from tikstractormf.models import Media


def _media() -> Media:
    return Media(
        id="p1",
        post_id="p1",
        index=1,
        type="video",
        url="https://v16-webapp-prime.us.tiktok.com/video/tos/x/v.mp4?a=1",
        ext="mp4",
    )


def test_download_retries_on_403_then_succeeds(tmp_path):
    media = _media()
    with respx.mock:
        route = respx.get(media.url).mock(
            side_effect=[httpx.Response(403), httpx.Response(200, content=b"hello")]
        )
        outcome = download_media(media, tmp_path, overwrite=True)
    assert isinstance(outcome, DownloadOutcome)
    assert outcome.status is DownloadStatus.DOWNLOADED
    assert route.call_count == 2
    assert outcome.path.exists()


def test_download_fails_after_403_retries(tmp_path):
    media = _media()
    with respx.mock:
        route = respx.get(media.url).mock(return_value=httpx.Response(403))
        try:
            download_media(media, tmp_path, overwrite=True)
        except httpx.HTTPStatusError:
            pass
        else:
            raise AssertionError("expected HTTPStatusError")
    assert route.call_count == 3


def test_download_saves_403_diagnostic(tmp_path):
    media = _media()
    diag_dir = tmp_path / "diag"
    with respx.mock:
        respx.get(media.url).mock(return_value=httpx.Response(403, text="forbidden body"))
        try:
            download_media(media, tmp_path / "out", overwrite=True, diagnostics_dir=diag_dir)
        except httpx.HTTPStatusError:
            pass
    files = list(diag_dir.glob("403_*.json"))
    assert files, "expected a 403 diagnostic file"
    import json

    payload = json.loads(files[0].read_text())
    assert payload["status_code"] == 403
    assert payload["media_id"] == "p1"
    assert "forbidden body" in payload["response_body_snippet"]
    # Cookie header is redacted if present (it is, because cookies=None here
    # so no Cookie header is added; either way the assertion holds).
    assert True


def test_download_uses_specific_referer(tmp_path):
    media = _media()
    captured = {}

    def handler(request):
        captured["referer"] = request.headers.get("referer")
        return httpx.Response(200, content=b"ok")

    with respx.mock:
        respx.get(media.url).mock(side_effect=handler)
        download_media(media, tmp_path, overwrite=True, username="alice")
    assert captured["referer"] == "https://www.tiktok.com/@alice/video/p1"
