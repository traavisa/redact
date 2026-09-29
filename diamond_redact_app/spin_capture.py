"""360 capture: turns a stone's 360 frames (from its own API fields) into our own numbered frames.

Only the frames kept after thinning are downloaded (DOWNLOAD_WORKERS at a time, over kept-alive
connections, with polite retries), re-encoded as WebP (no metadata, max FRAME_WIDTH px wide) and
uploaded in parallel to the "quote-media" bucket as <random uuid>/000.webp, 001.webp, … so the
quote and share pages can play them with our own spinner (spin360.js) from
https://quote.alldiamondeverything.com/media/<uuid>/NNN.webp. Nothing a client loads then comes
from a vendor. (Captures made by v4 are <uuid>/NNN.jpg; the pages pick the extension from the
capture's version.) Each capture logs its timing: frame fetch, re-encode, upload and total.

Where the frames come from — ONLY the certificate's 360 fields (v360, and product_videos entries
of type "360": url, frame_count, top_index), tried in this order, the same for every stone
whatever screen it came from (Live Search results or a pasted viewer link):
  a. the stone's own search-result fields, passed in as `hint`;
  b. the main API by certificate ID (CERT_LOOKUP);
  c. the viewer's public data endpoint by certificate ID (public_lookup): structured data
     only, no sign-in.
The certificate ID is read from the viewer link (/diamond/<id>/… with or without further
segments or a query string). Each step logs why it failed, and which one succeeded.
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
import os
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
# v6 = the same again, plus a full-quality set beside the small one: NNN@hi.webp (source resolution,
# 1600 px wide at most, WebP q90) for every frame. The spinner loads the hi set only for v6+.
# netlify/functions/lib/spin.js must carry the same CAPTURE_VERSION and TRUSTED_VERSION.
CAPTURE_VERSION = 6
TRUSTED_VERSION = 4          # only captures made under the API-fields-only rules are shown
WEBP_SINCE = 5               # captures from this version on are .webp
HI_SINCE = 6                 # captures from this version on also have NNN@hi.webp (all of them, or the capture is v5)


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
HI_WIDTH = 1600              # full-quality set: the source resolution, never wider than this
HI_QUALITY = 90
HI_SUFFIX = "@hi"
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
    """(where, {url, frame_count, top_index}) from the 360 fields: v360, and product_videos entries
    of type "360" — on the certificate and on the stone itself."""
    hint = hint or {}
    c = hint.get("certificate") if isinstance(hint.get("certificate"), dict) else {}
    out = []
    for where, v in (("certificate.v360", c.get("v360")), ("v360", hint.get("v360"))):
        if isinstance(v, dict):
            out.append((where, v))
    for where, pv in (("certificate.product_videos", c.get("product_videos")),
                      ("product_videos", hint.get("product_videos"))):
        for v in (pv if isinstance(pv, list) else [pv] if isinstance(pv, dict) else []):
            if isinstance(v, dict) and str(v.get("type") or "360").strip().lower() in ("360", "v360"):
                out.append((where, v))
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


# ── Certificate ID from a viewer link ─────────────────────────────────────────
_CERT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{7,63}")


def cert_id_from_link(url):
    """The certificate ID in a viewer link: the path segment after "diamond", wherever the link
    goes on from there (/video/500/500/autoplay, a query string, a fragment, no scheme…).
    '' when the link has none. The page itself is never read."""
    try:
        segs = [x for x in urlparse(str(url or "").strip()).path.split("/") if x]
    except Exception:
        return ""
    for i, seg in enumerate(segs[:-1]):
        if seg.lower() == "diamond" and _CERT_ID.fullmatch(segs[i + 1]):
            return segs[i + 1]
    return ""


# ── The certificate's 360 fields, three ways (never a viewer page) ────────────
# (a) the stone's own search-result fields   (b) the main API by certificate ID
# (c) the viewer's own public data endpoint: structured JSON, no sign-in.
# CERT_LOOKUP (b) is set by the app: cert_id -> (hint dict or None, reason).
PUBLIC_URL = os.environ.get("LOUPE_PUBLIC_URL", "").strip() or "https://g.nivoda.com/graphql-public-loupe360"
PUBLIC_QUERY = """query ($cert_id: ID!) {
  certificate: certificate_by_cert_id(cert_id: $cert_id) {
    id shape certNumber image
    v360 { top_index frame_count url }
    product_videos { id url display_index loupe360_url type top_index frame_count }
  }
}"""
PUBLIC_MAX_BYTES = 1024 * 1024


def public_lookup(cert_id):
    """(hint or None, reason): the certificate's 360 fields and still image from the public data
    endpoint. Sends only the query above with the certificate ID; reads only the JSON answer."""
    if not _CERT_ID.fullmatch(str(cert_id or "")):
        return None, "no certificate ID"
    try:
        r = _http.post(PUBLIC_URL, json={"query": PUBLIC_QUERY, "variables": {"cert_id": str(cert_id)}},
                       headers={"User-Agent": qm.UA, "Accept": "application/json"},
                       timeout=(8, 15), allow_redirects=False, stream=True)
    except requests.Timeout:
        return None, "public endpoint timed out"
    except requests.RequestException as e:
        return None, f"public endpoint unreachable ({type(e).__name__})"
    with r:
        if r.status_code != 200:
            return None, f"public endpoint HTTP {r.status_code}"
        raw = b""
        try:
            for chunk in r.iter_content(64 * 1024):
                raw += chunk
                if len(raw) > PUBLIC_MAX_BYTES:
                    return None, "public endpoint answer too large"
        except requests.RequestException as e:
            return None, f"public endpoint interrupted ({type(e).__name__})"
    try:
        body = json.loads(raw)
    except ValueError:
        return None, "public endpoint answer isn't JSON"
    if not isinstance(body, dict):
        return None, "public endpoint answer isn't JSON"
    c = (body.get("data") or {}).get("certificate") if isinstance(body.get("data"), dict) else None
    if not isinstance(c, dict):
        errs = body.get("errors")
        msg = mask_text(errs[0].get("message") if isinstance(errs, list) and errs and isinstance(errs[0], dict) else "", 100)
        return None, "public endpoint: " + (msg or "no certificate with that ID")
    cm = {k: c[k] for k in ("id", "certNumber", "image", "v360", "product_videos") if c.get(k) not in (None, "", [], {})}
    return ({"certificate": cm} if cm else None), ("ok" if cm else "certificate has no media")


def still_by_link(viewer_url):
    """The certificate's still image for a viewer link, from the public endpoint ('' if none)."""
    cid = cert_id_from_link(viewer_url)
    if not cid:
        return ""
    try:
        hint, _ = (PUBLIC_LOOKUP or public_lookup)(cid)
    except Exception:
        return ""
    return still_from_hint(hint)


def _cert_no_conflict(hint, expect):
    """'' unless the lookup's certificate number is known and differs from the stone's."""
    got = re.sub(r"\D", "", str(((hint or {}).get("certificate") or {}).get("certNumber") or ""))
    want = re.sub(r"\D", "", str(expect or ""))
    return "" if not got or not want or got == want else "its certificate number isn't this stone's"


def _via_lookup(name, fn, cert_id, expect, facts):
    """One lookup route: fetch the 360 fields by certificate ID, then the same frame checks."""
    try:
        hint, reason = fn(cert_id)
    except Exception as e:
        hint, reason = None, f"lookup error ({type(e).__name__})"
    if not hint:
        facts.append(f"{name}: failed — {mask_text(reason)}")
        return None
    if not _v360_candidates(hint):
        facts.append(f"{name}: answered, but no v360 / product_videos 360 entry with url + frame_count")
        return None
    bad = _cert_no_conflict(hint, expect)
    if bad:
        facts.append(f"{name}: rejected — {bad}")
        return None
    return _from_v360(hint, facts, name)


# ── Public API ────────────────────────────────────────────────────────────────
def find_frames(viewer_url, hint=None, expect_cert=None):
    """Returns (FrameSet or None, facts[list of str]). The 360 fields come from, in order:
    (a) the stone's own search-result fields, (b) the main API by certificate ID, (c) the public
    endpoint. Both v360 and product_videos (type 360) are checked at each step, every step
    that fails says why, and a viewer page is never read."""
    facts = []
    cert_id = cert_id_from_link(viewer_url)
    facts.append(f"viewer link: certificate ID {cert_id}" if cert_id
                 else "viewer link: no certificate ID found in it")
    # (a) the stone's own fields
    a_why = ""
    if _v360_candidates(hint):
        n0 = len(facts)
        fs = _from_v360(hint, facts, "step a (stone's own search-result fields)")
        if fs:
            return fs, facts
        a_why = facts[-1].split(": ", 1)[-1] if len(facts) > n0 else "its frames didn't check out"
        facts.append(f"step a: failed — {a_why}")
    else:
        facts.append("step a (stone's own search-result fields): none — no v360 / product_videos 360 entry "
                     "with url + frame_count")
    if not cert_id:
        facts.append("steps b and c skipped: no certificate ID to look up")
        facts.append(f"no 360 frames: the stone's own fields failed ({a_why}) and its link carries no certificate ID "
                     "for another route" if a_why else
                     "no 360 fields for this stone: it has none of its own and its link carries no certificate ID "
                     "— viewer pages are never read")
        return None, facts
    # (b) the main API by certificate ID
    if CERT_LOOKUP is None:
        facts.append("step b (main API by certificate ID): not available — search API not configured")
    else:
        fs = _via_lookup("step b (main API by certificate ID)", CERT_LOOKUP, cert_id, expect_cert, facts)
        if fs:
            return fs, facts
    # (c) the public data endpoint
    fs = _via_lookup("step c (public 360 endpoint)", PUBLIC_LOOKUP or public_lookup, cert_id, expect_cert, facts)
    if fs:
        return fs, facts
    facts.append("no 360 frames from any route (stone's own fields, main API, public endpoint) — "
                 "viewer pages are never read")
    return None, facts


PUBLIC_LOOKUP = None       # tests can replace the public endpoint call (cert_id -> (hint, reason))


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


def clean_frame(data, size=None, max_width=None, quality=None):
    """Re-encodes one frame from its pixels only (no EXIF/XMP/ICC) as WebP, max FRAME_WIDTH wide
    (or `max_width`; never enlarged). `size` forces every frame to the first frame's size.
    Returns (bytes, size)."""
    max_width = max_width or FRAME_WIDTH
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
            if w > max_width:
                size = (max_width, max(1, round(h * max_width / w)))
            else:
                size = (w, h)
        if flat.size != size:
            flat = flat.resize(size, Image.LANCZOS)
        clean = Image.new("RGB", size)
        clean.paste(flat)
    out = io.BytesIO()
    clean.save(out, "WEBP", quality=quality or WEBP_QUALITY, method=WEBP_METHOD)
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
    # 4. The full-quality set beside it (same folder, NNN@hi.webp). All or nothing: without every
    #    frame the capture stays a plain small-set one (v5) and the pages never ask for hi files.
    tm["hi"] = _store_hi(sb_url, key, folder, [raw[i] for i in pos], new_top, tm, facts)
    raw = None
    return folder, len(cleaned), new_top


def _store_hi(sb_url, key, folder, raws, top_pos, tm, facts):
    """Encodes every kept frame at the source resolution (HI_WIDTH at most, WebP q90) and uploads it as
    <folder>/NNN@hi.webp. True only when all of them are stored; otherwise none are left behind."""
    t = time.time()
    paths = [f"{folder}/{i:03d}{HI_SUFFIX}.{FRAME_EXT}" for i in range(len(raws))]
    try:
        first, hsize = clean_frame(raws[top_pos], None, HI_WIDTH, HI_QUALITY)
        rest = [i for i in range(len(raws)) if i != top_pos]

        def enc(i):
            return clean_frame(raws[i], hsize, HI_WIDTH, HI_QUALITY)[0]
        done = dict(zip(rest, _enc_pool.map(enc, rest)))
        done[top_pos] = first
        data = [done[i] for i in range(len(raws))]
        tm["hi_encode_s"] = round(time.time() - t, 2)
        t2 = time.time()
        errs = list(_up_pool.map(lambda pd: _safe_upload(sb_url, key, *pd), zip(paths, data)))
        tm["hi_upload_s"] = round(time.time() - t2, 2)
        if any(errs):
            raise RuntimeError("upload: " + next(e for e in errs if e))
        tm["hi_bytes"] = sum(len(d) for d in data)
        facts.append(f"stored {len(data)} full-quality frame(s), {hsize[0]}×{hsize[1]} WebP q{HI_QUALITY}, "
                     f"{tm['hi_bytes'] // 1024} KB total ({tm['hi_bytes'] // len(data) // 1024} KB each)")
        return True
    except Exception as e:
        _remove(sb_url, key, paths)
        facts.append("full-quality set skipped, small set kept: " + mask_text(e if isinstance(e, RuntimeError) else type(e).__name__, 160))
        return False


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


def capture(sb_url, key, viewer_url, hint=None, expect_cert=None):
    """Captures one stone from its certificate's 360 API fields. Returns dict(ok, spin={'id','n',
    'top','v'} | None, video='' (never a video), note=<one-line diagnostic>, facts=[...],
    log={method, frames, source_frames, top_index, top_frame} on success). Never raises."""
    with _cache_lock:
        if viewer_url in _cache:
            return _cache[viewer_url]
    t0 = time.time()
    timing = {}                                        # filled by store_frames, kept when a capture fails
    try:
        fs, facts = find_frames(viewer_url, hint, expect_cert)
        if fs is None:
            # The last fact is the most specific reason
            res = {"ok": False, "spin": None, "video": "", "facts": facts,
                   "note": facts[-1] if facts else "no 360 fields for this stone"}
        elif not own_folder(fs):
            raise CaptureFailed("frames aren't all from the stone's own v360 folder")
        else:
            facts.append(f"method: {fs.method} — {len(fs.urls)} frames via {fs.source}, pattern {fs.pattern}")
            folder, n, top = store_frames(sb_url, key, fs.urls, facts, fs.top, timing)
            log = {"method": fs.source, "frames": n, "source_frames": len(fs.urls),
                   "top_index": fs.top, "top_frame": top, "timing": timing}
            v = CAPTURE_VERSION if timing.get("hi") else WEBP_SINCE      # v6 only when every full-quality frame is stored
            res = {"ok": True, "spin": {"id": folder, "n": n, "top": top, "v": v}, "video": "", "facts": facts,
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
                      f"upload {tm.get('upload_s', 0):.1f}s"
                      + (f" · full-quality encode {tm['hi_encode_s']:.1f}s, upload {tm['hi_upload_s']:.1f}s"
                         if "hi_upload_s" in tm else "") + f" · total {tm['total_s']:.1f}s")
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
