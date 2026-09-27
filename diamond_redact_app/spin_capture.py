"""360 capture: turns a stone's 360 frames (from its own API fields) into our own numbered frames.

The frames are downloaded here, re-encoded (no metadata, max FRAME_WIDTH px wide) and
uploaded to the "quote-media" bucket as <random uuid>/000.jpg, 001.jpg, … so the quote
and share pages can play them with our own spinner (spin360.js) from
https://quote.alldiamondeverything.com/media/<uuid>/NNN.jpg. Nothing a client loads
then comes from a vendor.

Where the frames come from — ONLY the certificate's 360 API fields (v360 / product_videos:
url, frame_count, top_index):
  - the stone's own API fields, passed in as `hint` (Live Search / direct lookup), or
  - for a stone without them whose viewer link is /diamond/<certificate id>/video/<w>/<h>,
    the same API fields fetched from the search API by that certificate ID (CERT_LOOKUP).
Frames are <url>/<n>.webp (or .jpg / .png), n = 0 … frame_count-1, all inside that one
folder. Frame 0 and the last frame are checked before anything is used.
Viewer pages are NEVER read or parsed, and no video file is ever taken from one. When the
API fields are missing or fail a check, nothing is captured: the stone keeps its /v/ link.

Safety rules: at least MIN_FRAMES frames; every frame from the stone's own v360 folder;
thinned evenly to MAX_FRAMES, always including top_index; the spinner starts on top_index
(if the top frame can't be downloaded, the capture fails).

Every step records a short diagnostic (hosts masked) so a failed capture says exactly why.
Nothing identifying goes into file names: only the random folder and 000.jpg, 001.jpg, …
"""
import io
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps

import quote_media as qm

# ── Capture code version ──────────────────────────────────────────────────────
# Bump when capture rules change. Everything captured records it (stone spin.v,
# media_links.spin_version); pages and Netlify functions only show captures from
# version >= TRUSTED_VERSION. v1 = page scraping (8-frame minimum), v2/v3 = certificate
# fields with page analysis as a fallback. v4 = the certificate's API fields ONLY.
# netlify/functions/lib/spin.js must carry the same CAPTURE_VERSION and TRUSTED_VERSION.
CAPTURE_VERSION = 4
TRUSTED_VERSION = 4          # only captures made by the current (API-fields-only) code are shown


def _commit():
    import os, subprocess
    c = os.environ.get("RENDER_GIT_COMMIT", "").strip()
    if not c:
        try:
            c = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5,
                               cwd=os.path.dirname(os.path.abspath(__file__))).stdout.strip()
        except Exception:
            c = ""
    return c[:7] or "unknown"


COMMIT = _commit()
CODE_TAG = f"capture code v{CAPTURE_VERSION} (commit {COMMIT})"

MAX_FRAMES = 120             # more are evenly thinned out: 3° a frame is smooth, and far lighter on phones
MIN_FRAMES = 24             # fewer is not a real 360 (e.g. a page's shared images)
FRAME_WIDTH = 1000
JPEG_QUALITY = 84
DOWNLOAD_WORKERS = 8
MAX_MISSING = 0.10           # up to 10% of frames may fail to download; they're skipped
MAX_COUNT = 1024             # a larger frame_count is implausible
# Shared site images that are never a stone's frames (a page's own gallery, logos, …)
_SHARED_ASSET = re.compile(r"bridal_image|banner|logo|icon|sprite|placeholder|avatar|thumbnail", re.I)

# Certificate lookup by ID for /diamond/<id>/video/<w>/<h> viewer links: set by the app to a
# function cert_id -> (hint dict or None, reason). None means no lookup is available.
CERT_LOOKUP = None


def shared_asset(url):
    try:
        return bool(_SHARED_ASSET.search(urlparse(str(url)).path))
    except Exception:
        return True


class CaptureFailed(Exception):
    """Capture didn't work. str(e) is a short user-safe reason."""


# ── Diagnostics: hosts and long IDs masked, supplier name removed ─────────────
_LONG_ID = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{16,}(?![A-Za-z0-9])")
_NAME = re.compile(r"nivoda\w*", re.I)


def mask(u):
    """'https://cdn.x.com/abc/5f3e…9a/still/{n}.jpg' -> '[host]/abc/<id>/still/{n}.jpg'."""
    try:
        p = urlparse(str(u))
        path = p.path + (("?" + p.query) if p.query else "")
        out = ("[host]" if p.netloc else "") + _LONG_ID.sub("<id>", path)
    except Exception:
        out = "?"
    out = _NAME.sub("[source]", out)
    return out if len(out) <= 90 else out[:87] + "…"


def _clean_text(t):
    return _NAME.sub("[source]", str(t))


# ── HTTP ──────────────────────────────────────────────────────────────────────
def _is_image(url):
    """True if the URL serves an image (checked from the file's own first bytes)."""
    try:
        if not qm._public_host(url):
            return False
        with requests.get(url, stream=True, timeout=(8, 15), allow_redirects=True,
                          headers={"User-Agent": qm.UA, "Accept": "image/*,*/*"}) as r:
            if r.status_code >= 400:
                return False
            head = next(r.iter_content(32), b"")
            return qm._sniff(head) == "image"
    except Exception:
        return False


# ── Frames ────────────────────────────────────────────────────────────────────
class FrameSet:
    def __init__(self, urls, pattern, source, top=0, method=None, still="", folder=""):
        self.urls, self.pattern, self.source = urls, pattern, source
        self.folder = folder                               # the stone's own v360 folder
        self.top = top if 0 <= top < len(urls) else 0      # the frame the stone is shown from first
        self.method = method or source
        self.still = still                                 # the certificate's still image, if any


# ── The certificate's 360 API fields ──────────────────────────────────────────
def video_file_from_hint(hint):
    """A direct MP4 / WebM file of THIS stone from its own API fields: a product_videos entry
    whose type is a video (not a 360 frame set) and whose URL is a video file. Nothing else
    counts — in particular never a file found inside a viewer page (that is the viewer's own
    promo clip, the same for every stone)."""
    hint = hint or {}
    c = hint.get("certificate") if isinstance(hint.get("certificate"), dict) else {}
    for pv in (c.get("product_videos"), hint.get("product_videos")):
        for v in (pv if isinstance(pv, list) else [pv] if isinstance(pv, dict) else []):
            if not isinstance(v, dict):
                continue
            t = str(v.get("type") or "").strip().lower()
            u = str(v.get("url") or "")
            if t and t not in ("360", "v360") and re.match(r"https?://", u) and re.search(r"\.(mp4|webm)(\?|$)", u, re.I):
                return u
    return ""


def _int0(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _v360_candidates(hint):
    """(where, {url, frame_count, top_index}) from the certificate's 360 fields."""
    hint = hint or {}
    c = hint.get("certificate") if isinstance(hint.get("certificate"), dict) else {}
    out = []
    for where, v in (("certificate.v360", c.get("v360")), ("v360", hint.get("v360"))):
        if isinstance(v, dict):
            out.append((where, v))
    pv = c.get("product_videos")
    for v in (pv if isinstance(pv, list) else [pv] if isinstance(pv, dict) else []):
        if isinstance(v, dict) and str(v.get("type") or "360").strip().lower() in ("360", "v360"):
            out.append(("certificate.product_videos", v))
    return [(w, v) for w, v in out if isinstance(v.get("url"), str) and v.get("url") and v.get("frame_count") is not None]


def still_from_hint(hint):
    """The certificate's still image URL, if the stone data has one."""
    c = (hint or {}).get("certificate") or {}
    u = c.get("image") if isinstance(c, dict) else None
    return u if isinstance(u, str) and re.match(r"https?://", u) else ""


def _from_v360(hint, facts, method):
    """Frames from the certificate's 360 fields: <url>/<n>.webp for n = 0 … frame_count-1."""
    cands = _v360_candidates(hint)
    if not cands:
        return None
    tried = set()
    for where, v in cands:
        url, fc, top = str(v.get("url") or "").strip(), _int0(v.get("frame_count")), _int0(v.get("top_index"))
        if not re.match(r"https?://", url) or (url, fc) in tried:
            continue
        tried.add((url, fc))
        if not fc or fc < MIN_FRAMES:
            facts.append(f"{method}: {where} has {fc or 'no'} frame(s) — under the {MIN_FRAMES}-frame minimum")
            continue
        if fc > MAX_COUNT:
            facts.append(f"{method}: {where} frame count {fc} is implausible")
            continue
        if shared_asset(url):
            facts.append(f"{method}: {where} points at a shared site image folder — skipped")
            continue
        base = url.split("?")[0].rstrip("/")
        partial = False
        for ext in ("webp", "jpg", "png"):
            first, last = f"{base}/0.{ext}", f"{base}/{fc - 1}.{ext}"
            if not _is_image(first):
                continue
            if not _is_image(last):
                partial = True
                facts.append(f"{method}: {mask(base)}/{{n}}.{ext} — frame 0 loads but frame {fc - 1} doesn't")
                continue
            top = top if top is not None and 0 <= top < fc else 0
            pattern = f"{mask(base)}/{{n}}.{ext}"
            facts.append(f"{method}: {where} → {fc} frames, top frame {top}, pattern {pattern} "
                         f"(frames 0 and {fc - 1} checked)")
            return FrameSet([f"{base}/{i}.{ext}" for i in range(fc)], pattern, f"{method} ({where})",
                            top=top, method=method, still=still_from_hint(hint), folder=base)
        if not partial:
            facts.append(f"{method}: {where} ({mask(base)}) — no frames answered as {{n}}.webp/.jpg/.png")
    return None


# /diamond/<certificate id>/video/<w>/<h>: the ID is the search API's certificate ID. The page
# itself is never read; the certificate's 360 API fields are fetched by that ID instead.
_CERT_VIEWER = re.compile(r"^/diamond/([^/]+)/video/\d+/\d+/?$", re.I)


def _from_cert_lookup(viewer_url, facts):
    try:
        m = _CERT_VIEWER.search(urlparse(str(viewer_url)).path)
    except Exception:
        m = None
    if not m:
        return None
    facts.append("viewer link carries a certificate ID: looking up its 360 API fields")
    if CERT_LOOKUP is None:
        raise CaptureFailed("certificate lookup unavailable (search API not configured)")
    try:
        hint, reason = CERT_LOOKUP(m.group(1))
    except Exception as e:
        hint, reason = None, f"certificate lookup error ({type(e).__name__})"
    facts.append(f"certificate lookup: {'found 360 fields' if hint else reason}")
    if not hint:
        raise CaptureFailed(reason if reason.startswith("certificate") else f"certificate lookup: {reason}")
    fs = _from_v360(hint, facts, "API fields (certificate lookup)")
    if fs is None:
        raise CaptureFailed("certificate lookup: its 360 fields gave no usable frames")
    return fs


# ── Public API ────────────────────────────────────────────────────────────────
def find_frames(viewer_url, hint=None):
    """Returns (FrameSet or None, facts[list of str]). Only the certificate's 360 API fields
    are used; a viewer page is never read."""
    facts = []
    try:
        fs = _from_v360(hint, facts, "API fields")
        if fs is None and not _v360_candidates(hint):
            fs = _from_cert_lookup(viewer_url, facts)
            if fs is None:
                facts.append("no 360 API fields for this stone (url / frame_count) — viewer pages are never read")
        return fs, facts
    except CaptureFailed as e:
        facts.append(str(e))
        return None, facts


def pick(n, top=0, limit=None):
    """Indices of at most `limit` (MAX_FRAMES) frames out of n, evenly spaced around the turn,
    in rotation order and always including `top`. Returns (indices, position of top)."""
    limit = limit or MAX_FRAMES
    top = top if 0 <= top < n else 0
    if n <= limit:
        return list(range(n)), top
    step = n / limit
    idx = sorted({(top + round(i * step)) % n for i in range(limit)})
    return idx, idx.index(top)


def clean_frame(data, size=None):
    """Re-encodes one frame from its pixels only (no EXIF/XMP/ICC), max FRAME_WIDTH wide.
    `size` forces every frame to the first frame's size."""
    with Image.open(io.BytesIO(data)) as im:
        im.seek(0)
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[3])
        else:
            flat = im.convert("RGB")
        if size is None:
            w, h = flat.size
            if w > FRAME_WIDTH:
                size = (FRAME_WIDTH, max(1, round(h * FRAME_WIDTH / w)))
            else:
                size = (w, h)
        if flat.size != size:
            flat = flat.resize(size, Image.LANCZOS)
        clean = Image.new("RGB", size)
        clean.paste(flat)
    out = io.BytesIO()
    clean.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue(), size


def _upload_frame(sb_url, key, path, data):
    r = requests.post(f"{sb_url}/storage/v1/object/{qm.BUCKET}/{path}",
                      headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "image/jpeg",
                               "Cache-Control": "max-age=31536000", "x-upsert": "false"},
                      data=data, timeout=(10, 60))
    if r.status_code not in (200, 201):
        raise RuntimeError(f"upload HTTP {r.status_code}")


def _remove(sb_url, key, paths):
    try:
        requests.delete(f"{sb_url}/storage/v1/object/{qm.BUCKET}",
                        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        data=json.dumps({"prefixes": paths}), timeout=20)
    except Exception:
        pass


def store_frames(sb_url, key, urls, facts, top=0):
    """Downloads, cleans and uploads the frames. Returns (folder_id, count, top position)."""
    if any(shared_asset(u) for u in urls[:1] + urls[-1:]):
        raise CaptureFailed("frames are shared site images, not the stone")
    idx, top_pos = pick(len(urls), top)
    urls = [urls[i] for i in idx]
    if len(urls) < MIN_FRAMES:
        raise CaptureFailed(f"only {len(urls)} frame(s) — under the {MIN_FRAMES}-frame minimum")

    def get(u):
        for attempt in range(2):
            try:
                data, kind = qm._download(u, "image")
                if kind == "image":
                    return data
            except (qm.Broken, qm.NotAFile, qm.TooLarge):
                return None
            except Exception:
                pass
        return None

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as ex:
        raw = list(ex.map(get, urls))
    kept = [i for i, d in enumerate(raw) if d]            # positions (in rotation order) that downloaded
    good = [raw[i] for i in kept]
    missing = len(urls) - len(good)
    facts.append(f"downloaded {len(good)}/{len(urls)} frame(s) in {time.time() - t0:.1f}s")
    if not good or missing > len(urls) * MAX_MISSING or len(good) < MIN_FRAMES:
        raise CaptureFailed(f"{missing} of {len(urls)} frames wouldn't download")
    if not raw[top_pos]:
        raise CaptureFailed("the top frame (top_index) wouldn't download")
    first, size = clean_frame(good[0])
    cleaned, pos = [first], [kept[0]]
    for i, d in zip(kept[1:], good[1:]):
        try:
            cleaned.append(clean_frame(d, size)[0])
            pos.append(i)
        except Exception:
            pass
    if top_pos not in pos:
        raise CaptureFailed("the top frame (top_index) couldn't be read as an image")
    new_top = pos.index(top_pos)                          # the spinner starts on top_index
    if len(cleaned) < max(MIN_FRAMES, len(urls) * (1 - MAX_MISSING)):
        raise CaptureFailed("too many frames couldn't be read as images")
    folder = uuid.uuid4().hex
    paths = [f"{folder}/{i:03d}.jpg" for i in range(len(cleaned))]
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as ex:
        errs = list(ex.map(lambda pd: _safe_upload(sb_url, key, *pd), zip(paths, cleaned)))
    if any(errs):
        _remove(sb_url, key, paths)
        raise CaptureFailed(f"frame upload failed ({next(e for e in errs if e)})")
    facts.append(f"stored {len(cleaned)} frame(s), {size[0]}×{size[1]}, "
                 f"{sum(len(c) for c in cleaned) // 1024} KB total")
    return folder, len(cleaned), new_top


def _safe_upload(sb_url, key, path, data):
    try:
        _upload_frame(sb_url, key, path, data)
        return None
    except Exception as e:
        return str(e)[:60]


_cache = {}                   # viewer URL -> result, so re-quoting a stone doesn't re-capture
_cache_lock = threading.Lock()


def own_folder(fs):
    """True when every frame is <the stone's v360 folder>/<n>.<ext> (nothing from elsewhere)."""
    base = str(fs.folder or "")
    return bool(base) and all(re.fullmatch(re.escape(base) + r"/\d{1,4}\.(webp|jpg|png)", u) for u in fs.urls)


def capture(sb_url, key, viewer_url, hint=None):
    """Captures one stone from its certificate's 360 API fields. Returns dict(ok, spin={'id','n',
    'top','v'} | None, video='' (never a video), note=<one-line diagnostic>, facts=[...],
    log={method, frames, source_frames, top_index, top_frame} on success). Never raises."""
    with _cache_lock:
        if viewer_url in _cache:
            return _cache[viewer_url]
    t0 = time.time()
    try:
        fs, facts = find_frames(viewer_url, hint)
        if fs is None:
            # The last fact is the most specific reason
            res = {"ok": False, "spin": None, "video": "", "facts": facts,
                   "note": facts[-1] if facts else "no 360 API fields for this stone"}
        elif not own_folder(fs):
            raise CaptureFailed("frames aren't all from the stone's own v360 folder")
        else:
            facts.append(f"method: {fs.method} — {len(fs.urls)} frames via {fs.source}, pattern {fs.pattern}")
            folder, n, top = store_frames(sb_url, key, fs.urls, facts, fs.top)
            log = {"method": fs.source, "frames": n, "source_frames": len(fs.urls),
                   "top_index": fs.top, "top_frame": top}
            res = {"ok": True, "spin": {"id": folder, "n": n, "top": top, "v": CAPTURE_VERSION}, "video": "", "facts": facts,
                   "method": fs.method, "still": fs.still, "log": log,
                   "note": f"360 captured via {fs.method}: {n} frames"
                           + (f" (thinned evenly from {len(fs.urls)})" if n < len(fs.urls) else "")
                           + f", starts on top_index {fs.top}; pattern {fs.pattern}"}
    except CaptureFailed as e:
        facts = locals().get("facts") or []
        res = {"ok": False, "spin": None, "video": "", "facts": facts + [str(e)], "note": str(e)}
    except Exception as e:
        facts = locals().get("facts") or []
        res = {"ok": False, "spin": None, "video": "", "facts": facts + [type(e).__name__],
               "note": f"capture error ({type(e).__name__})"}
    res["seconds"] = round(time.time() - t0, 1)
    res["note"] = _clean_text(res["note"])
    res["facts"] = [_clean_text(f) for f in res["facts"]]
    if res["ok"]:
        with _cache_lock:
            _cache[viewer_url] = res
    res["facts"].append(CODE_TAG)
    print("[spin-capture] " + json.dumps({"ok": res["ok"], "note": res["note"], "log": res.get("log"), "facts": res["facts"],
                                          "seconds": res["seconds"], "version": CAPTURE_VERSION,
                                          "commit": COMMIT}), flush=True)
    return res
