"""Parsing helpers for TikTok API responses."""

from __future__ import annotations

import json as _json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from tikstractormf.models import Media, Post


def _guess_ext(url: str, format_hint: str | None = None) -> str:
    # format_hint comes from the JSON (video.format, mime_type, etc.)
    if format_hint:
        fh = format_hint.lower().strip(".")
        if fh in {"jpg", "jpeg", "png", "webp", "gif", "avif", "heic", "bmp",
                  "mp4", "webm", "mov", "m4v"}:
            return "jpeg" if fh == "jpeg" else fh
        if "mp4" in fh:
            return "mp4"
        if "webp" in fh:
            return "webp"
        if "png" in fh:
            return "png"
    path = urlparse(url).path.lower()
    last_segment = path.rsplit("/", 1)[-1]
    suffix = last_segment.rsplit(".", 1)[-1] if "." in last_segment else ""
    if suffix in {"jpg", "jpeg", "png", "webp", "gif", "avif", "heic", "bmp"}:
        return suffix
    video = {"mp4", "webm", "mov", "m4v"}
    if suffix in video:
        return suffix
    # inspect query string for mime_type / format
    from urllib.parse import parse_qs
    qs = parse_qs(urlparse(url).query)
    mime = (qs.get("mime_type") or qs.get("mime") or [""])[0].lower()
    fmt = (qs.get("format") or qs.get("file_format") or [""])[0].lower()
    if "video" in mime or fmt in {"mp4", "webm", "mov", "m4v"}:
        if "webm" in mime or fmt == "webm":
            return "webm"
        if "mov" in mime or fmt == "mov":
            return "mov"
        return "mp4"
    if "webp" in mime or fmt == "webp":
        return "webp"
    if "png" in mime or fmt == "png":
        return "png"
    low = url.lower()
    if ".mp4" in low or ".webm" in low or ".mov" in low:
        return "mp4" if ".mp4" in low else ("webm" if ".webm" in low else "mov")
    # Heuristic for extensionless CDN URLs (e.g. profile pics): look for a
    # known image token anywhere in the URL before falling back to jpg.
    if "webp" in low:
        return "webp"
    if "png" in low:
        return "png"
    if "jpeg" in low or "jpg" in low:
        return "jpg"
    return "jpg"


def parse_item(item: dict, fallback_username: str | None = None,
               extract_audio: bool = False) -> Post | None:
    """Parse one TikTok item (from item_list) into a Post."""
    try:
        vid = str(item.get("id") or "")
        if not vid:
            return None
        author = item.get("author") or {}
        username = author.get("uniqueId") or fallback_username or "unknown"
        desc = item.get("desc")
        create_time = item.get("createTime")
        datetime_iso = None
        if create_time:
            try:
                datetime_iso = datetime.fromtimestamp(int(create_time), tz=timezone.utc).isoformat()
            except Exception:
                pass
        permalink = f"https://www.tiktok.com/@{username}/video/{vid}"

        medias: list[Media] = []
        video = item.get("video") or {}
        # Prefer best quality from bitrateInfo
        play_addr = None
        format_hint = video.get("format")
        bitrate_info = video.get("bitrateInfo")
        bitrate_candidates: list[str] = []
        if isinstance(bitrate_info, list) and bitrate_info:
            # pick highest width*height
            best_score = -1
            best_url_list: list[str] = []
            for bi in bitrate_info:
                pa = bi.get("PlayAddr") or {}
                url_list = pa.get("UrlList") or []
                if not url_list:
                    continue
                for u in url_list:
                    if u and u not in bitrate_candidates:
                        bitrate_candidates.append(u)
                w = int(pa.get("Width") or 0)
                h = int(pa.get("Height") or 0)
                score = w * h
                if score > best_score:
                    best_score = score
                    play_addr = url_list[0]
                    best_url_list = list(url_list)
                    format_hint = pa.get("DataSize") and format_hint
            if not play_addr and bitrate_info:
                first = bitrate_info[0].get("PlayAddr", {}).get("UrlList") or []
                if first:
                    play_addr = first[0]
                    best_url_list = list(first)
            if best_url_list:
                for u in best_url_list:
                    if u and u not in bitrate_candidates:
                        bitrate_candidates.append(u)
        if not play_addr:
            play_addr = video.get("playAddr") or video.get("downloadAddr")
        cover = video.get("cover") or video.get("originCover") or video.get("dynamicCover")
        download_addr = video.get("downloadAddr")
        # Prefer downloadAddr unless it is a watermarked "logo" variant, in
        # which case playAddr is usually the clean source.
        if download_addr and "logo" in download_addr.lower() and play_addr:
            primary = play_addr
        else:
            primary = download_addr or play_addr

        # Collect all download candidates, preferring downloadAddr, then playAddr,
        # then every UrlList entry from bitrateInfo. Dedup while preserving order.
        all_candidates: list[str] = []
        for cand in (
            download_addr,
            play_addr,
            video.get("playAddr"),
            *bitrate_candidates,
        ):
            if cand and cand not in all_candidates:
                all_candidates.append(cand)

        fallback_urls: list[str] = []
        if primary:
            for cand in all_candidates:
                if cand and cand != primary and cand not in fallback_urls:
                    fallback_urls.append(cand)
            ext = _guess_ext(primary, format_hint or video.get("format"))
            medias.append(
                Media(
                    id=vid, post_id=vid, index=1, type="video", url=primary,
                    fallback_urls=fallback_urls, ext=ext,
                )
            )
        # photo posts
        image_post = item.get("imagePost") or {}
        images = image_post.get("images") or []
        if images and not play_addr:
            for idx, img in enumerate(images):
                iu = None
                img_fallbacks: list[str] = []
                if isinstance(img, dict):
                    raw = img.get("imageURL")
                    ul: list[str] = []
                    if isinstance(raw, dict):
                        ul = raw.get("urlList") or raw.get("UrlList") or []
                        if ul:
                            iu = ul[0]
                            img_fallbacks = [u for u in ul[1:] if u]
                    elif isinstance(raw, str):
                        iu = raw
                    iu = iu or img.get("displayImage") or img.get("url")
                    if not iu and isinstance(img.get("image"), dict):
                        iu2 = img["image"].get("url") or img["image"].get("imageURL")
                        if isinstance(iu2, dict):
                            ul2 = iu2.get("urlList") or iu2.get("UrlList") or []
                            if ul2:
                                iu = ul2[0]
                                img_fallbacks = [u for u in ul2[1:] if u]
                        elif isinstance(iu2, str):
                            iu = iu2
                    # displayImage may also carry a urlList
                    di = img.get("displayImage")
                    if isinstance(di, dict):
                        dul = di.get("urlList") or di.get("UrlList") or []
                        if dul and not iu:
                            iu = dul[0]
                            img_fallbacks = [u for u in dul[1:] if u]
                        elif dul:
                            for u in dul:
                                if u and u != iu and u not in img_fallbacks:
                                    img_fallbacks.append(u)
                if not iu:
                    continue
                ext = _guess_ext(iu)
                mid = vid if len(images) == 1 else f"{vid}_{idx+1}"
                medias.append(
                    Media(
                        id=mid, post_id=vid, index=idx + 1, type="image", url=iu,
                        fallback_urls=img_fallbacks, ext=ext,
                    )
                )
        # Audio track attached to a photo carousel (gallery-dl extracts it when
        # audio=True). Only add when the item has no playable video.
        music = item.get("music") or {}
        music_url = music.get("playUrl")
        if extract_audio and images and not play_addr and music_url:
            ext = _guess_ext(music_url)
            medias.append(
                Media(
                    id=f"{vid}_audio",
                    post_id=vid,
                    index=len(images) + 1,
                    type="audio",
                    url=music_url,
                    ext=ext if ext in {"mp3", "m4a", "aac", "wav", "ogg"} else "mp3",
                )
            )
        # cover as image if no media? skip
        if not medias and cover:
            ext = _guess_ext(cover)
            medias.append(Media(id=vid, post_id=vid, index=1, type="image", url=cover, ext=ext))

        if not medias:
            return None
        return Post(
            id=vid,
            permalink=permalink,
            username=username,
            caption=desc,
            datetime_iso=datetime_iso,
            media=medias,
        )
    except Exception:
        return None


def _pick_best_image(candidates):
    if not candidates:
        return None
    try:
        c = max(candidates, key=lambda x: int(x.get("width", 0)))
        return c.get("url")
    except Exception:
        return candidates[0].get("url") if candidates else None


def _unescape_json_string(value: str) -> str:
    """Decode the escape sequences found in a JSON string literal."""
    try:
        result = _json.loads('"' + value + '"')
        return str(result)
    except Exception:
        return (
            value.replace(r"\u0026", "&")
            .replace(r"\u002F", "/")
            .replace(r"\u002f", "/")
            .replace(r"\/", "/")
        )


def parse_profile_html(html: str):
    """Return the profile pic URL (or None)."""
    pic = None
    m = re.search(r'"avatarLarger"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    if m:
        pic = _unescape_json_string(m.group(1))
    if not pic:
        m = re.search(r'"avatarMedium"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
        if m:
            pic = _unescape_json_string(m.group(1))
    return pic
