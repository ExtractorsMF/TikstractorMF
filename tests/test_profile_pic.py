import httpx
import respx

from tikstractormf.archive import DownloadLedger, ledger_path_for
from tikstractormf.downloader import (
    DownloadStatus,
    download_profile_pic,
)


def test_profile_pic_versions_do_not_overwrite(tmp_path):
    url1 = "https://p16-sign.tiktokcdn.com/obj/aaa~tplv"
    url2 = "https://p16-sign.tiktokcdn.com/obj/bbb~tplv"
    with respx.mock:
        respx.get(url1).mock(return_value=httpx.Response(200, content=b"img1"))
        respx.get(url2).mock(return_value=httpx.Response(200, content=b"img2"))
        o1 = download_profile_pic(url1, "user", tmp_path)
        o2 = download_profile_pic(url2, "user", tmp_path)
    assert o1.path != o2.path
    assert o1.path.exists() and o2.path.exists()
    assert o1.path.read_bytes() == b"img1"
    assert o2.path.read_bytes() == b"img2"


def test_profile_pic_same_version_skipped_with_ledger(tmp_path):
    url = "https://p16-sign.tiktokcdn.com/obj/aaa~tplv"
    ledger = DownloadLedger(ledger_path_for(tmp_path))
    ledger.load()
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=b"img"))
        o1 = download_profile_pic(url, "user", tmp_path, ledger=ledger)
        respx.get(url).mock(return_value=httpx.Response(200, content=b"img"))
        o2 = download_profile_pic(url, "user", tmp_path, ledger=ledger)
    assert o1.status is not DownloadStatus.SKIPPED
    assert o2.status is DownloadStatus.SKIPPED
