import httpx
import respx

from tikstractormf.api import TiktokAPI


def test_fetch_post_falls_back_on_403():
    api = TiktokAPI(cookies=None)
    api._extract_sec_uid = lambda username: "sec123"  # type: ignore[assignment]

    item = {
        "id": "1234567890",
        "desc": "hello",
        "createTime": 1700000000,
        "author": {"uniqueId": "user1"},
        "video": {
            "downloadAddr": "https://v16-webapp-prime.us.tiktok.com/video/tos/x/d.mp4?a=1",
        },
    }

    with respx.mock:
        respx.get(url__regex=r".*creator/item_list.*").mock(
            return_value=httpx.Response(200, json={"itemList": [item], "hasMorePrevious": False})
        )
        respx.get(url__regex=r".*tiktok\.com/@user1/video/1234567890.*").mock(
            return_value=httpx.Response(403)
        )
        posts = api.get_posts("user1", limit=1, concurrency=1)

    assert len(posts) == 1
    assert posts[0].id == "1234567890"
    assert "d.mp4" in posts[0].media[0].url


def test_fetch_post_from_page_direct_fallback():
    api = TiktokAPI(cookies=None)
    item = {
        "id": "999",
        "author": {"uniqueId": "u"},
        "video": {"playAddr": "https://v16-webapp-prime.us.tiktok.com/video/tos/y/v.mp4?x=1"},
    }
    api._item_by_id = {"999": item}
    with respx.mock:
        respx.get("https://www.tiktok.com/@u/video/999").mock(return_value=httpx.Response(403))
        post = api._fetch_post_from_page("u", "999")
    assert post is not None
    assert post.id == "999"


def test_fetch_post_falls_back_on_empty_script():
    api = TiktokAPI(cookies=None)
    item = {
        "id": "999",
        "author": {"uniqueId": "u"},
        "video": {"playAddr": "https://v16-webapp-prime.us.tiktok.com/video/tos/y/v.mp4?x=1"},
    }
    api._item_by_id = {"999": item}
    with respx.mock:
        respx.get("https://www.tiktok.com/@u/video/999").mock(
            return_value=httpx.Response(200, text="<html>no script</html>")
        )
        post = api._fetch_post_from_page("u", "999")
    assert post is not None
    assert post.id == "999"


def test_fetch_post_falls_back_on_invalid_json():
    api = TiktokAPI(cookies=None)
    item = {
        "id": "999",
        "author": {"uniqueId": "u"},
        "video": {"playAddr": "https://v16-webapp-prime.us.tiktok.com/video/tos/y/v.mp4?x=1"},
    }
    api._item_by_id = {"999": item}
    bad_html = (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        '{not json}</script>'
    )
    with respx.mock:
        respx.get("https://www.tiktok.com/@u/video/999").mock(
            return_value=httpx.Response(200, text=bad_html)
        )
        post = api._fetch_post_from_page("u", "999")
    assert post is not None
    assert post.id == "999"


def test_fetch_post_falls_back_on_missing_itemstruct():
    api = TiktokAPI(cookies=None)
    item = {
        "id": "999",
        "author": {"uniqueId": "u"},
        "video": {"playAddr": "https://v16-webapp-prime.us.tiktok.com/video/tos/y/v.mp4?x=1"},
    }
    api._item_by_id = {"999": item}
    html = (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        '{"__DEFAULT_SCOPE__":{"webapp.video-detail":{"itemInfo":{}}}}'
        '</script>'
    )
    with respx.mock:
        respx.get("https://www.tiktok.com/@u/video/999").mock(
            return_value=httpx.Response(200, text=html)
        )
        post = api._fetch_post_from_page("u", "999")
    assert post is not None
    assert post.id == "999"


def test_fetch_post_no_item_returns_none():
    api = TiktokAPI(cookies=None)
    with respx.mock:
        respx.get("https://www.tiktok.com/@u/video/999").mock(return_value=httpx.Response(403))
        post = api._fetch_post_from_page("u", "999")
    assert post is None


def _item(post_id: str, create_time: int) -> dict:
    return {
        "id": post_id,
        "desc": "d",
        "createTime": create_time,
        "author": {"uniqueId": "user1"},
        "video": {"playAddr": f"https://v16-webapp-prime.us.tiktok.com/video/tos/x/{post_id}.mp4?a=1"},
    }


def test_iter_posts_paginates_two_pages():
    api = TiktokAPI(cookies=None)
    api._extract_sec_uid = lambda username: "sec123"  # type: ignore[assignment]

    with respx.mock:
        respx.get(url__regex=r".*creator/item_list.*").mock(
            side_effect=[
                httpx.Response(200, json={
                    "itemList": [_item("1", 1700000000)],
                    "hasMorePrevious": True,
                }),
                httpx.Response(200, json={
                    "itemList": [_item("2", 1699999999)],
                    "hasMorePrevious": False,
                }),
            ]
        )
        respx.get(url__regex=r".*tiktok\.com/@user1/video/.*").mock(
            return_value=httpx.Response(403)
        )
        posts = api.get_posts("user1", limit=2, concurrency=1)

    assert [p.id for p in posts] == ["1", "2"]
    assert api.last_report.pages_fetched == 2


def test_iter_posts_has_more_field():
    api = TiktokAPI(cookies=None)
    api._extract_sec_uid = lambda username: "sec123"  # type: ignore[assignment]

    with respx.mock:
        respx.get(url__regex=r".*creator/item_list.*").mock(
            side_effect=[
                httpx.Response(200, json={
                    "itemList": [_item("1", 1700000000)],
                    "hasMore": True,
                }),
                httpx.Response(200, json={
                    "itemList": [_item("2", 1699999999)],
                    "hasMore": False,
                }),
            ]
        )
        respx.get(url__regex=r".*tiktok\.com/@user1/video/.*").mock(
            return_value=httpx.Response(403)
        )
        posts = api.get_posts("user1", limit=2, concurrency=1)

    assert [p.id for p in posts] == ["1", "2"]
    assert api.last_report.pages_fetched == 2


def test_iter_posts_api_error_on_invalid_json():
    api = TiktokAPI(cookies=None)
    api._extract_sec_uid = lambda username: "sec123"  # type: ignore[assignment]

    with respx.mock:
        respx.get(url__regex=r".*creator/item_list.*").mock(
            return_value=httpx.Response(200, text="not json")
        )
        posts = api.get_posts("user1", limit=1, concurrency=1)

    assert posts == []
    assert "invalid json" in api.last_report.api_error


def test_iter_posts_no_secuid_used_fallback_false():
    api = TiktokAPI(cookies=None)
    api._extract_sec_uid = lambda username: None  # type: ignore[assignment]
    posts = api.get_posts("user1")
    assert posts == []
    assert api.last_report.used_fallback is False


def test_solve_challenge_returns_false_without_challenge():
    api = TiktokAPI(cookies=None)
    assert api._solve_challenge("<html>no challenge</html>") is False


def test_capture_rotated_cookies_noop_on_none():
    api = TiktokAPI(cookies=None)
    # Should not raise
    api._capture_rotated_cookies(None)


def test_verbose_false_suppresses_progress(capsys):
    api = TiktokAPI(cookies=None, verbose=False)
    api._extract_sec_uid = lambda username: "sec123"  # type: ignore[assignment]
    with respx.mock:
        respx.get(url__regex=r".*creator/item_list.*").mock(
            return_value=httpx.Response(200, json={
                "itemList": [_item("1", 1700000000)],
                "hasMorePrevious": False,
            })
        )
        respx.get(url__regex=r".*tiktok\.com/@user1/video/.*").mock(
            return_value=httpx.Response(403)
        )
        api.get_posts("user1", limit=1, concurrency=1)
    captured = capsys.readouterr()
    assert "listing posts" not in captured.err
    assert "fetching posts" not in captured.err


def test_verbose_true_shows_progress(capsys):
    api = TiktokAPI(cookies=None, verbose=True)
    api._extract_sec_uid = lambda username: "sec123"  # type: ignore[assignment]
    with respx.mock:
        respx.get(url__regex=r".*creator/item_list.*").mock(
            return_value=httpx.Response(200, json={
                "itemList": [_item("1", 1700000000)],
                "hasMorePrevious": False,
            })
        )
        respx.get(url__regex=r".*tiktok\.com/@user1/video/.*").mock(
            return_value=httpx.Response(403)
        )
        api.get_posts("user1", limit=1, concurrency=1)
    captured = capsys.readouterr()
    assert "listing posts" in captured.err
