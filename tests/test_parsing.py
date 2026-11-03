from tikstractormf._parsing import parse_item


def _video_item():
    return {
        "id": "1234567890",
        "desc": "hello",
        "createTime": 1700000000,
        "author": {"uniqueId": "user1", "id": "999"},
        "video": {
            "playAddr": "https://v16-webapp-prime.us.tiktok.com/video/tos/.../v.mp4?a=1",
            "cover": "https://p16-sign.tiktokcdn.com/obj/cover.jpg",
            "width": 1080,
            "height": 1920,
            "duration": 10,
        },
    }


def _photo_item():
    return {
        "id": "9876543210",
        "desc": "photos",
        "createTime": 1700000001,
        "author": {"uniqueId": "user2"},
        "imagePost": {
            "images": [
                {"imageURL": "https://p16-sign.tiktokcdn.com/obj/p1.jpg"},
                {"imageURL": "https://p16-sign.tiktokcdn.com/obj/p2.jpg"},
            ]
        },
    }


def test_parse_video_item():
    post = parse_item(_video_item())
    assert post is not None
    assert post.id == "1234567890"
    assert post.username == "user1"
    assert len(post.media) == 1
    assert post.media[0].type == "video"
    assert post.media[0].url.startswith("https://v16-webapp-prime.us.tiktok.com")


def _video_item_bitrate():
    return {
        "id": "1234567890",
        "desc": "hello",
        "createTime": 1700000000,
        "author": {"uniqueId": "user1", "id": "999"},
        "video": {
            "playAddr": "https://v16-webapp-prime.us.tiktok.com/video/tos/.../play.mp4?a=1",
            "downloadAddr": "https://v16-webapp-prime.us.tiktok.com/video/tos/.../download.mp4?a=1",
            "cover": "https://p16-sign.tiktokcdn.com/obj/cover.jpg",
            "bitrateInfo": [
                {
                    "PlayAddr": {
                        "UrlList": [
                            "https://v16-webapp-prime.us.tiktok.com/video/tos/.../b1.mp4?a=1",
                            "https://v19-webapp-prime.us.tiktok.com/video/tos/.../b1b.mp4?a=1",
                        ],
                        "Width": 720,
                        "Height": 1280,
                    }
                },
                {
                    "PlayAddr": {
                        "UrlList": [
                            "https://v16-webapp-prime.us.tiktok.com/video/tos/.../b2.mp4?a=1",
                        ],
                        "Width": 1080,
                        "Height": 1920,
                    }
                },
            ],
        },
    }


def test_parse_video_bitrate_candidates():
    post = parse_item(_video_item_bitrate())
    assert post is not None
    m = post.media[0]
    assert m.url == "https://v16-webapp-prime.us.tiktok.com/video/tos/.../download.mp4?a=1"
    assert "https://v16-webapp-prime.us.tiktok.com/video/tos/.../b2.mp4?a=1" in m.fallback_urls
    assert "https://v16-webapp-prime.us.tiktok.com/video/tos/.../play.mp4?a=1" in m.fallback_urls


def test_parse_photo_item():
    post = parse_item(_photo_item())
    assert post is not None
    assert post.id == "9876543210"
    assert len(post.media) == 2
    assert all(m.type == "image" for m in post.media)


def _photo_item_nested():
    return {
        "id": "9876543210",
        "desc": "photos",
        "createTime": 1700000001,
        "author": {"uniqueId": "user2"},
        "imagePost": {
            "images": [
                {"imageURL": {"urlList": [
                    "https://p16-sign.tiktokcdn.com/obj/p1.jpg?x=1",
                    "https://p16-sign.tiktokcdn.com/obj/p1b.jpg?x=1",
                ]}},
            ]
        },
    }


def test_parse_photo_nested_url_list():
    post = parse_item(_photo_item_nested())
    assert post is not None
    assert len(post.media) == 1
    assert post.media[0].url == "https://p16-sign.tiktokcdn.com/obj/p1.jpg?x=1"
    assert "https://p16-sign.tiktokcdn.com/obj/p1b.jpg?x=1" in post.media[0].fallback_urls


def test_parse_invalid():
    assert parse_item({}) is None
    assert parse_item({"id": "x"}) is None
