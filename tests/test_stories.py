import httpx
import respx

from tikstractormf.api import TiktokAPI


def _story_item():
    return {
        "id": "7703815281314942216",
        "desc": "story",
        "createTime": 1700000000,
        "author": {"uniqueId": "example", "id": "123456789"},
        "video": {
            "playAddr": "https://v16-webapp-prime.us.tiktok.com/video/tos/x/s.mp4?a=1",
            "downloadAddr": "https://v16-webapp-prime.us.tiktok.com/video/tos/x/sd.mp4?a=1",
            "cover": "https://p16-sign.tiktokcdn.com/obj/cover.jpg",
        },
        "music": {"playUrl": "https://v16-webapp-prime.us.tiktok.com/audio/a.mp3"},
    }


def test_parse_story_item():
    from tikstractormf._parsing import parse_story_item

    post = parse_story_item(_story_item())
    assert post is not None
    assert post.id == "7703815281314942216"
    assert post.media[0].type == "video"
    assert post.media[0].story is True


def test_iter_stories_paginates():
    api = TiktokAPI(cookies=None)
    api._extract_author_id = lambda username: "123456789"  # type: ignore[assignment]

    with respx.mock:
        respx.get(url__regex=r".*story/item_list.*").mock(
            side_effect=[
                httpx.Response(200, json={
                    "itemList": [_story_item()],
                    "HasMoreAfter": True,
                    "MaxCursor": 12345,
                }),
                httpx.Response(200, json={
                    "itemList": [],
                    "HasMoreAfter": False,
                    "MaxCursor": 12345,
                }),
            ]
        )
        stories = api.get_stories("example", limit=1, concurrency=1)

    assert len(stories) == 1
    assert stories[0].id == "7703815281314942216"
