from tikstractormf.downloader import is_valid_media_url


def test_tiktok_cdn_hosts():
    urls = [
        "https://v16-webapp-prime.us.tiktok.com/video/tos/x/v.mp4?a=1&b=2",
        "https://v19-webapp-prime.us.tiktok.com/video/tos/y/v.mp4?x=1",
        "https://p16-sign.tiktokcdn.com/obj/abc123",
    ]
    for u in urls:
        assert is_valid_media_url(u), u


def test_invalid_hosts():
    assert not is_valid_media_url("https://evil.com/video.mp4")
    assert not is_valid_media_url("http://v16-webapp-prime.us.tiktok.com/v.mp4")


def test_profile_pic_url_without_extension():
    assert is_valid_media_url("https://p16-sign.tiktokcdn.com/obj/abc123~tplv")
