"""Quote media: keeps vendor domains out of everything a client can see.

For each stone's image / video when a quote is saved:
  1. Direct file (image, MP4 or WebM, checked by content-type AND the file's own bytes):
     downloaded here, cleaned (images re-encoded with no metadata; videos remuxed with
     metadata stripped when ffmpeg is available) and uploaded to the "quote-media"
     storage bucket under a random UUID name. The quote then points at
     https://quote.alldiamondeverything.com/media/<uuid>.<ext>, which Netlify proxies.
  2. Anything else that still loads (an interactive 360 viewer page, a YouTube link,
     a file too large to copy): stored in the media_links table under a random token.
     The quote points at https://video.alldiamondeverything.com/v/<token>; the viewer
     looks the token up server-side. The token is random, never derived from the URL.
  3. Broken links (unreachable, 404): saved without that item.

Nothing here ever blocks saving a quote: every failure becomes a short note instead.
File names and tokens carry nothing identifying (no stock ID, cert number or vendor).
"""
import io
import ipaddress
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps

BUCKET = "quote-media"
MEDIA_BASE = "https://quote.alldiamondeverything.com/media/"
VIEWER_LINK_BASE = "https://video.alldiamondeverything.com/v/"

MAX_VIDEO_BYTES = 50 * 1024 * 1024      # 50 MB per video
MAX_IMAGE_BYTES = 20 * 1024 * 1024      # 20 MB per source image
MAX_IMAGE_SIDE = 2400                   # re-encoded images are scaled down to fit this
TIMEOUT = (10, 30)                      # connect, read (seconds)
TOKEN_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
TOKEN_LEN = 12                          # 32^12 ≈ 1.2e18 possibilities
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/pjpeg", "image/png", "image/webp", "image/gif"}
VIDEO_TYPES = {"video/mp4": "mp4", "video/webm": "webm"}
LOOSE_TYPES = {"application/octet-stream", "binary/octet-stream", ""}   # sniff the bytes instead

ALLOW_PRIVATE_HOSTS = False   # tests only

_cache = {}                    # (url, kind) -> result, so re-quoting a stone doesn't re-upload
_cache_lock = threading.Lock()


class Broken(Exception):
    """The link doesn't work at all: save without it."""


class NotAFile(Exception):
    """The link works but isn't a file we can copy: use an opaque viewer link."""


class TooSlow(Exception):
    """Timed out: the link may still work in a browser, so treat like a viewer page."""


class TooLarge(Exception):
    """Over the size limit: use an opaque viewer link (video) or leave out (image)."""


# ── Small helpers ─────────────────────────────────────────────────────────────
def _new_token():
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LEN))


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _public_host(url):
    """Refuse links to this server's own network (localhost, private ranges)."""
    if ALLOW_PRIVATE_HOSTS:
        return True
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise Broken("host not found")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _sniff(head):
    """Returns 'image', 'mp4', 'webm' or None from the first bytes of a file."""
    if head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n" or head[:6] in (b"GIF87a", b"GIF89a") \
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    return None


def _download(url, want):
    """Downloads a direct file. Returns (bytes, kind) where kind is 'image', 'mp4' or 'webm'.
    Raises Broken, NotAFile, TooSlow or TooLarge."""
    if not _public_host(url):
        raise Broken("not a public address")
    try:
        r = requests.get(url, stream=True, timeout=TIMEOUT, allow_redirects=True,
                         headers={"User-Agent": UA, "Accept": "*/*"})
    except requests.Timeout:
        raise TooSlow()
    except requests.RequestException:
        raise Broken("could not connect")
    with r:
        if r.status_code in (404, 410):
            raise Broken(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            raise NotAFile(f"HTTP {r.status_code}")      # may still load in a browser
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in IMAGE_TYPES and ctype not in VIDEO_TYPES and ctype not in LOOSE_TYPES:
            raise NotAFile(ctype or "unknown type")
        limit = MAX_IMAGE_BYTES if want == "image" else MAX_VIDEO_BYTES
        try:
            size = int(r.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        if size > limit:
            raise TooLarge()
        buf = io.BytesIO()
        try:
            for chunk in r.iter_content(256 * 1024):
                buf.write(chunk)
                if buf.tell() > limit:
                    raise TooLarge()
        except requests.RequestException:
            raise TooSlow()
    data = buf.getvalue()
    kind = _sniff(data[:16])
    if kind is None:
        raise NotAFile("not an image or video file")
    return data, kind


def _clean_image(data):
    """Re-encodes the image from its pixels only: EXIF, XMP, ICC, comments are all dropped."""
    with Image.open(io.BytesIO(data)) as im:
        im.seek(0)
        im = ImageOps.exif_transpose(im)          # keep the right way up once EXIF is gone
        im.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        clean = Image.new("RGBA" if has_alpha else "RGB", im.size)
        clean.paste(im.convert("RGBA" if has_alpha else "RGB"))
    out = io.BytesIO()
    if has_alpha:
        clean.save(out, "PNG", optimize=True)
        return out.getvalue(), "png", "image/png"
    clean.save(out, "JPEG", quality=90, optimize=True, progressive=True)
    return out.getvalue(), "jpg", "image/jpeg"


def _clean_video(data, kind):
    """Strips container metadata with ffmpeg (no re-encode). Returns (bytes, stripped?)."""
    exe = _ffmpeg()
    if not exe:
        return data, False
    with tempfile.TemporaryDirectory() as d:
        src, dst = f"{d}/in.{kind}", f"{d}/out.{kind}"
        with open(src, "wb") as f:
            f.write(data)
        cmd = [exe, "-v", "error", "-y", "-i", src, "-map", "0", "-map_metadata", "-1",
               "-map_chapters", "-1", "-c", "copy",
               "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact"]
        if kind == "mp4":
            cmd += ["-movflags", "+faststart"]
        cmd.append(dst)
        try:
            subprocess.run(cmd, check=True, timeout=120, capture_output=True)
            with open(dst, "rb") as f:
                out = f.read()
            return (out, True) if out else (data, False)
        except Exception:
            return data, False


# ── Supabase ──────────────────────────────────────────────────────────────────
def _upload(sb_url, key, data, ext, ctype):
    name = f"{uuid.uuid4().hex}.{ext}"
    r = requests.post(f"{sb_url}/storage/v1/object/{BUCKET}/{name}",
                      headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": ctype,
                               "Cache-Control": "max-age=31536000", "x-upsert": "false"},
                      data=data, timeout=(10, 120))
    if r.status_code not in (200, 201):
        raise RuntimeError("upload failed")
    return MEDIA_BASE + name


def _viewer_link(sb_url, key, url):
    """Stores the vendor URL server-side under a random token. Reuses an existing token."""
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    try:
        r = requests.get(f"{sb_url}/rest/v1/media_links", params={
            "vendor_url": f"eq.{url}", "select": "token", "limit": "1"}, headers=h, timeout=10)
        if r.status_code == 200 and r.json():
            return VIEWER_LINK_BASE + r.json()[0]["token"]
    except Exception:
        pass
    for _ in range(3):
        token = _new_token()
        r = requests.post(f"{sb_url}/rest/v1/media_links",
                          headers={**h, "Content-Type": "application/json", "Prefer": "return=minimal"},
                          json={"token": token, "vendor_url": url}, timeout=10)
        if r.status_code in (200, 201, 204):
            return VIEWER_LINK_BASE + token
        if r.status_code != 409:
            break
    raise RuntimeError("could not store viewer link")


# ── Public API ────────────────────────────────────────────────────────────────
def to_embed(url):
    """YouTube watch / short links → embeddable player links (as before)."""
    try:
        from urllib.parse import parse_qs
        u = urlparse(url)
        if u.hostname in ("www.youtube.com", "youtube.com") and u.path == "/watch":
            vid = parse_qs(u.query).get("v", [""])[0]
            if vid:
                return f"https://www.youtube.com/embed/{vid}?autoplay=0"
        elif u.hostname in ("www.youtube.com", "youtube.com") and u.path.startswith("/shorts/"):
            return f"https://www.youtube.com/embed/{u.path[len('/shorts/'):]}?autoplay=0"
        elif u.hostname == "youtu.be" and u.path[1:]:
            return f"https://www.youtube.com/embed/{u.path[1:]}?autoplay=0"
    except Exception:
        pass
    return url


def process(sb_url, key, url, want):
    """want: 'video' or 'image'. Returns dict(url=<our URL or ''>, how=..., note=<str or ''>).
    how: 'hosted' (copied to /media/), 'viewer' (opaque /v/ link), 'dropped' (nothing saved)."""
    url = (url or "").strip()
    if not url:
        return {"url": "", "how": "dropped", "note": ""}
    if not url.lower().startswith(("http://", "https://")):
        return {"url": "", "how": "dropped", "note": f"{want} link isn't a web address — left out"}
    ck = (url, want)
    with _cache_lock:
        if ck in _cache:
            return _cache[ck]
    res = _process(sb_url, key, url, want)
    if res["how"] != "dropped":
        with _cache_lock:
            _cache[ck] = res
    return res


def _process(sb_url, key, url, want):
    fallback_reason = ""
    try:
        data, kind = _download(url, want)
        if kind == "image":
            clean, ext, ctype = _clean_image(data)
            return {"url": _upload(sb_url, key, clean, ext, ctype), "how": "hosted", "note": ""}
        if want == "image":
            raise NotAFile("expected an image")
        clean, stripped = _clean_video(data, kind)
        note = "" if stripped else "video copied as-is (metadata tool unavailable)"
        return {"url": _upload(sb_url, key, clean, kind, f"video/{kind}"), "how": "hosted", "note": note}
    except Broken as e:
        return {"url": "", "how": "dropped", "note": f"{want} link doesn't work ({e}) — left out"}
    except TooLarge:
        fallback_reason = "is too large to copy"
    except TooSlow:
        fallback_reason = "took too long to download"
    except NotAFile as e:
        fallback_reason = "is a viewer page" if str(e) == "text/html" else f"isn't a file ({e})"
    except Exception:
        fallback_reason = "couldn't be copied"
    # Step 2: interactive viewer pages (and videos we couldn't copy) get an opaque link.
    if want == "image":
        return {"url": "", "how": "dropped", "note": f"image {fallback_reason} — left out"}
    try:
        return {"url": _viewer_link(sb_url, key, to_embed(url)), "how": "viewer",
                "note": f"video {fallback_reason} — using private viewer link"}
    except Exception:
        return {"url": "", "how": "dropped", "note": "video couldn't be saved — left out"}


def process_stones(sb_url, key, items, workers=4):
    """items: list of (video_url, image_url). Returns list of (video_res, image_res), same order.
    Each distinct link is processed once, several at a time."""
    jobs = list(dict.fromkeys(j for v, i in items for j in ((v, "video"), (i, "image"))))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        done = dict(zip(jobs, ex.map(lambda j: process(sb_url, key, j[0], j[1]), jobs)))
    return [(done[(v, "video")], done[(i, "image")]) for v, i in items]


def notes_for(label, pair):
    """Short human notes for one stone, e.g. 'Diamond 2: video too large to copy — …'."""
    return [f"{label}: {r['note']}" for r in pair if r.get("note")]
