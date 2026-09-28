"""360 capture: turns a stone's 360 frames (from its own API fields) into our own numbered frames.

Only the frames kept after thinning are downloaded (DOWNLOAD_WORKERS at a time, over kept-alive
connections, with polite retries), re-encoded as WebP (no metadata, max FRAME_WIDTH px wide) and
uploaded in parallel to the "quote-media" bucket as <random uuid>/000.webp, 001.webp, … so the
quote and share pages can play them with our own spinner (spin360.js) from
https://quote.alldiamondeverything.com/media/<uuid>/NNN.webp. Nothing a client loads then comes
from a vendor. (Captures made by v4 are <uuid>/NNN.jpg; the pages pick the extension from the
capture's version.) Each capture logs its timing: frame fetch, re-encode, upload and total.

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
Nothing identifying goes into file names: only the random folder and 000.webp, 001.webp, …
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
from requests.adapters import HTTPAdapter
from PIL import Image, ImageOps

import quote_media as qm

# ── Capture code version ──────────────────────────────────────────────────────
# Bump when capture rules change. Everything captured records it (stone spin.v,
# media_links.spin_version); pages and Netlify functions only show captures from
# version >= TRUSTED_VERSION. v1 = page scraping (8-frame minimum), v2/v3 = certificate
# fields with page analysis as a fallback. v4 = the certificate's API fields ONLY (JPEG frames).
# v5 = the same capture rules, frames stored as WebP (NNN.webp) — the pages use the version
# to pick the file extension. v4 captures stay trusted: they were made by the same rules.
# netlify/functions/lib/spin.js must carry the same CAPTURE_VERSION and TRUSTED_VERSION.
CAPTURE_VERSION = 5
TRUSTED_VERSION = 4          # only captures made under the API-fields-only rules are shown
WEBP_SINCE = 5               # captures from this version on are .webp


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

MAX_FRAMES = 72              # kept frames (5° a frame); more are evenly thinned out. The app's
                             # SPIN_MAX_FRAMES setting can raise it (e.g. 120) — see set_max_frames()
MIN_FRAMES = 24             # fewer is not a real 360 (e.g. a page's shared images)
FRAME_WIDTH = 720
WEBP_QUALITY = 80
WEBP_METHOD = 2              # encoder effort 0-6: 2 is ~20% faster than the default 4, same size
FRAME_EXT, FRAME_TYPE = "webp", "image/webp"
DOWNLOAD_WORKERS = 24        # frame downloads at once, shared by every capture (polite to the source)
UPLOAD_WORKERS = 12          # frame uploads at once, shared by every capture
ENCODE_WORKERS = 2           # re-encoding releases the GIL, so a second core is used when there is one
RETRIES = 2                  # extra tries for a frame after a timeout, connection error, 429 or 5xx
RETRY_WAIT = (0.5, 1.5)      # seconds before each retry (a Retry-After up to 5 s is respected)
CACHE_CONTROL = "public, max-age=31536000, immutable"   # frame names are unique: cache them forever
MAX_MISSING = 0.10           # up to 10% of frames may fail to download; they're skipped
MAX_COUNT = 1024             # a larger frame_count is implausible
# Shared site images that are never a stone's frames (a page's own gallery, logos, …)
_SHARED_ASSET = re.compile(r"bridal_image|banner|logo|icon|sprite|placeholder|avatar|thumbnail", re.I)

# Certificate lookup by ID for /diamond/<id>/video/<w>/<h> viewer links: set by the app to a
# function cert_id -> (hint dict or None, reason). None means no lookup is available.
CERT_LOOKUP = None


def set_max_frames(value):
    """Kept-frame count from the SPIN_MAX_FRAMES setting (24-240); anything else keeps the default."""
    global MAX_FRAMES
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        return MAX_FRAMES
    if MIN_FRAMES <= v <= 240:
        MAX_FRAMES = v
    return MAX_FRAMES


# One kept-alive HTTP session and shared worker pools for every capture: no new DNS lookup or
# TLS handshake per frame, and the source never sees more than DOWNLOAD_WORKERS requests at once.
_http = requests.Session()
_http.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=DOWNLOAD_WORKERS + UPLOAD_WORKERS))
_http.mount("http://", HTTPAdapter(pool_connections=8, pool_maxsize=DOWNLOAD_WORKERS + UPLOAD_WORKERS))
_dl_pool = ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS, thread_name_prefix="frame-dl")
_up_pool = ThreadPoolExecutor(max_workers=UPLOAD_WORKERS, thread_name_prefix="frame-up")
_enc_pool = ThreadPoolExecutor(max_workers=ENCODE_WORKERS, thread_name_prefix="frame-enc")


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


_URL = re.compile(r"https?://\S+")


def mask_text(t, limit=140):
    """A storage error message made safe for the capture log: web addresses become [host]/…,
    long IDs become <id>, the supplier name is removed, control characters dropped, length capped."""
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    t = _URL.sub(lambda m: mask(m.group(0)), t)
    t = _LONG_ID.sub("<id>", t)
    t = _NAME.sub("[source]", t)
    return t if len(t) <= limit else t[:limit - 1] + "…"


def storage_error(r):
    """The storage service's own words for a failed upload, masked: 'upload HTTP 415: mime type
    image/webp is not supported'. Falls back to the status alone."""
    msg = ""
    try:
        body = r.json()
        if isinstance(body, dict):
            msg = body.get("message") or body.get("error_description") or body.get("error") or ""
            if body.get("error") and body.get("error") not in msg:
                msg = f"{body['error']}: {msg}" if msg else body["error"]
    except Exception:
        msg = getattr(r, "text", "") or ""
    msg = mask_text(msg)
    return f"upload HTTP {r.status_code}" + (f": {msg}" if msg else "")


# ── HTTP ──────────────────────────────────────────────────────────────────────
def _is_image(url):
    """True if the URL serves an image (checked from the file's own first bytes)."""
    try:
        if not qm._public_host(url):
            return False
        with _http.get(url, stream=True, timeout=(8, 15), allow_redirects=True,
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
    """Re-encodes one frame from its pixels only (no EXIF/XMP/ICC) as WebP, max FRAME_WIDTH wide.
    `size` forces every frame to the first frame's size. Returns (bytes, size)."""
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
    clean.save(out, "WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
    return out.getvalue(), size


def _retry_wait(attempt, r=None):
    wait = RETRY_WAIT[min(attempt, len(RETRY_WAIT) - 1)]
    try:
        wait = max(wait, min(5.0, float(r.headers.get("Retry-After")))) if r is not None else wait
    except (TypeError, ValueError):
        pass
    time.sleep(wait)


def _fetch(url):
    """One frame's bytes over the shared session, with polite retries. None if it can't be had.
    Same checks as quote_media._download: a real image, under the size limit."""
    for attempt in range(RETRIES + 1):
        try:
            with _http.get(url, stream=True, timeout=(8, 20), allow_redirects=True,
                           headers={"User-Agent": qm.UA, "Accept": "image/*,*/*"}) as r:
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt < RETRIES:
                        _retry_wait(attempt, r)
                        continue
                    return None
                if r.status_code >= 400:
                    return None                                  # 404 etc.: not worth retrying
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype not in qm.IMAGE_TYPES and ctype not in qm.LOOSE_TYPES:
                    return None
                buf = io.BytesIO()
                for chunk in r.iter_content(256 * 1024):
                    buf.write(chunk)
                    if buf.tell() > qm.MAX_IMAGE_BYTES:
                        return None
            data = buf.getvalue()
            return data if qm._sniff(data[:16]) == "image" else None
        except requests.RequestException:
            if attempt < RETRIES:
                _retry_wait(attempt)
                continue
            return None
    return None


def _upload_frame(sb_url, key, path, data):
    """Uploads one frame, cached forever by browsers (the name is unique). Retries on 5xx / 429 /
    connection errors; a 409 on a retry means the earlier try already stored it."""
    for attempt in range(RETRIES + 1):
        try:
            r = _http.post(f"{sb_url}/storage/v1/object/{qm.BUCKET}/{path}",
                           headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": FRAME_TYPE,
                                    "Cache-Control": CACHE_CONTROL, "x-upsert": "false"},
                           data=data, timeout=(10, 60))
        except requests.RequestException as e:
            if attempt < RETRIES:
                _retry_wait(attempt)
                continue
            raise RuntimeError(f"upload failed ({type(e).__name__})")
        if r.status_code in (200, 201) or (attempt and r.status_code == 409):
            return
        if (r.status_code == 429 or r.status_code >= 500) and attempt < RETRIES:
            _retry_wait(attempt, r)
            continue
        raise RuntimeError(storage_error(r))


def _remove(sb_url, key, paths):
    try:
        _http.delete(f"{sb_url}/storage/v1/object/{qm.BUCKET}",
                     headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                     data=json.dumps({"prefixes": paths}), timeout=20)
    except Exception:
        pass


PHASE2_STEP = 8               # the spinner loads every 8th frame (from the top frame) first


def first_view(n, top):
    """Frames the spinner loads before dragging works: the top frame, then every 8th from it."""
    return sorted({(top + k * PHASE2_STEP) % n for k in range(-(-n // PHASE2_STEP))})


def store_frames(sb_url, key, urls, facts, top=0, timing=None):
    """Downloads ONLY the frames kept after thinning (top_index always among them), cleans and
    uploads them. Returns (folder_id, count, top position). Fills `timing` (a dict) with
    fetch_s / encode_s / upload_s (seconds), bytes and first_view_bytes."""
    tm = timing if timing is not None else {}
    t0 = time.time()
    if any(shared_asset(u) for u in urls[:1] + urls[-1:]):
        raise CaptureFailed("frames are shared site images, not the stone")
    try:
        public = qm._public_host(urls[0])                  # one DNS check: every frame is in one folder
    except qm.Broken:
        public = False
    if not public:
        raise CaptureFailed("frame address isn't public")
    idx, top_pos = pick(len(urls), top)
    urls = [urls[i] for i in idx]
    if len(urls) < MIN_FRAMES:
        raise CaptureFailed(f"only {len(urls)} frame(s) — under the {MIN_FRAMES}-frame minimum")

    # 1. Fetch the kept frames only, in parallel (the shared pool limits load on the source)
    raw = list(_dl_pool.map(_fetch, urls))
    tm["fetch_s"] = round(time.time() - t0, 2)
    kept = [i for i, d in enumerate(raw) if d]            # positions (in rotation order) that downloaded
    missing = len(urls) - len(kept)
    facts.append(f"downloaded {len(kept)}/{len(urls)} frame(s) in {tm['fetch_s']:.1f}s")
    if not kept or missing > len(urls) * MAX_MISSING or len(kept) < MIN_FRAMES:
        raise CaptureFailed(f"{missing} of {len(urls)} frames wouldn't download")
    if not raw[top_pos]:
        raise CaptureFailed("the top frame (top_index) wouldn't download")

    # 2. Re-encode, in parallel (the top frame first: it fixes the size for all). Frames are
    #    numbered once the readable ones are known.
    t1 = time.time()
    try:
        first, size = clean_frame(raw[top_pos])
    except Exception:
        raise CaptureFailed("the top frame (top_index) couldn't be read as an image")
    others = [i for i in kept if i != top_pos]

    def enc(i):
        try:
            return clean_frame(raw[i], size)[0]
        except Exception:
            return None
    done = dict(zip(others, _enc_pool.map(enc, others)))
    done[top_pos] = first
    pos = [i for i in kept if done.get(i)]
    raw = None
    tm["encode_s"] = round(time.time() - t1, 2)
    new_top = pos.index(top_pos)                          # the spinner starts on top_index
    if len(pos) < max(MIN_FRAMES, len(urls) * (1 - MAX_MISSING)):
        raise CaptureFailed("too many frames couldn't be read as images")
    cleaned = [done[i] for i in pos]
    # 3. Upload, in parallel
    folder = uuid.uuid4().hex
    paths = [f"{folder}/{i:03d}.{FRAME_EXT}" for i in range(len(cleaned))]
    t2 = time.time()
    errs = list(_up_pool.map(lambda pd: _safe_upload(sb_url, key, *pd), zip(paths, cleaned)))
    tm["upload_s"] = round(time.time() - t2, 2)
    if any(errs):
        _remove(sb_url, key, paths)
        raise CaptureFailed(f"frame upload failed ({next(e for e in errs if e)})")
    tm["bytes"] = sum(len(c) for c in cleaned)
    tm["first_view_bytes"] = sum(len(cleaned[j]) for j in first_view(len(cleaned), new_top))
    facts.append(f"stored {len(cleaned)} frame(s), {size[0]}×{size[1]} WebP, {tm['bytes'] // 1024} KB total "
                 f"({tm['bytes'] // len(cleaned) // 1024} KB each; first view {tm['first_view_bytes'] // 1024} KB)")
    return folder, len(cleaned), new_top


def _safe_upload(sb_url, key, path, data):
    try:
        _upload_frame(sb_url, key, path, data)
        return None
    except Exception as e:
        return mask_text(e, 160)


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
    timing = {}                                        # filled by store_frames, kept when a capture fails
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
            folder, n, top = store_frames(sb_url, key, fs.urls, facts, fs.top, timing)
            log = {"method": fs.source, "frames": n, "source_frames": len(fs.urls),
                   "top_index": fs.top, "top_frame": top, "timing": timing}
            res = {"ok": True, "spin": {"id": folder, "n": n, "top": top, "v": CAPTURE_VERSION}, "video": "", "facts": facts,
                   "method": fs.method, "still": fs.still, "log": log,
                   "note": f"360 captured via {fs.method}: {n} frames"
                           + (f" (thinned evenly from {len(fs.urls)})" if n < len(fs.urls) else "")
                           + f", starts on top_index {fs.top}; pattern {fs.pattern}"}
    except CaptureFailed as e:
        facts = locals().get("facts") or []
        res = {"ok": False, "spin": None, "video": "", "facts": facts + [str(e)], "note": str(e),
               "fail_timing": timing}
    except Exception as e:
        facts = locals().get("facts") or []
        res = {"ok": False, "spin": None, "video": "", "facts": facts + [type(e).__name__],
               "note": f"capture error ({type(e).__name__})"}
    res["seconds"] = round(time.time() - t0, 1)
    if res.get("fail_timing") is not None:
        res["fail_timing"]["total_s"] = res["seconds"]
    if res.get("log"):
        tm = res["log"]["timing"]
        tm["total_s"] = res["seconds"]                     # the whole capture, API field checks included
        facts_line = (f"timing: fetch {tm.get('fetch_s', 0):.1f}s · re-encode {tm.get('encode_s', 0):.1f}s · "
                      f"upload {tm.get('upload_s', 0):.1f}s · total {tm['total_s']:.1f}s")
        res["facts"] = list(res["facts"]) + [facts_line]
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
