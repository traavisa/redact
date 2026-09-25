"""360 capture: turns a stone's interactive 360 viewer page into our own numbered frames.

The frames are downloaded here, re-encoded (no metadata, max FRAME_WIDTH px wide) and
uploaded to the "quote-media" bucket as <random uuid>/000.jpg, 001.jpg, … so the quote
and share pages can play them with our own spinner (spin360.js) from
https://quote.alldiamondeverything.com/media/<uuid>/NNN.jpg. Nothing a client loads
then comes from a vendor.

Finding the frames, in order:
  1. "API fields": the certificate's 360 fields from the search API (v360 / product_videos:
     url, frame_count, top_index), passed in as `hint`. Frames are <url>/<n>.webp,
     n = 0 … frame_count-1; frame 0 and the last frame are checked before anything is used.
  2. Other stone data from the search API (a frame base URL + count, or a list of frame URLs).
  3. Viewer formats registered with @viewer_format, matched on the viewer URL's shape.
     Format A (/diamond/<certificate id>/video/<w>/<h>) is a "certificate lookup": the
     certificate's 360 fields are fetched from the search API by that ID (CERT_LOOKUP).
     Add a new format there when a new kind of viewer page turns up.
  4. Generic analysis of the viewer page: its HTML, inline and same-site scripts, JSON
     config and any nested viewer frame, looking for numbered frame URLs, URL templates
     ("…/" + i + ".jpg", `${i}.jpg`, {frame}), frame counts and frame base URLs.
     Candidate patterns are checked by downloading a frame before anything is used.
A direct video file (MP4 / WebM) found on the way is reported so the caller can prefer it.

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
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image, ImageOps

import quote_media as qm

# ── Capture code version ──────────────────────────────────────────────────────
# Bump when capture rules change. Everything captured records it (stone spin.v,
# media_links.spin_version); pages and Netlify functions only show captures from
# version >= TRUSTED_VERSION. Version 1 = the first build (page scraping, 8-frame minimum),
# which made the 11-frame shared-image captures; it never recorded a version.
# netlify/functions/capture-version.js must carry the same CAPTURE_VERSION.
CAPTURE_VERSION = 3
TRUSTED_VERSION = 2          # 2 = certificate 360 fields + 24-frame minimum (it wrote spin.top)


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
PROBE_WORKERS = 8
MAX_MISSING = 0.10           # up to 10% of frames may fail to download; they're skipped
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_SCRIPT_BYTES = 4 * 1024 * 1024
MAX_SCRIPTS = 6
MAX_COUNT_SEARCH = 1024
IMG_EXT = ("jpg", "jpeg", "png", "webp")
# Shared site images that are never a stone's frames (a page's own gallery, logos, …)
_SHARED_ASSET = re.compile(r"bridal_image|banner|logo|icon|sprite|placeholder|avatar|thumbnail", re.I)

# Certificate lookup by ID for format A viewer links: set by the app to a function
# cert_id -> (hint dict or None, reason). None means no lookup is available.
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
def _get_text(url, limit):
    """GET a page or script. Returns (status, content_type, text). Raises CaptureFailed."""
    if not qm._public_host(url):
        raise CaptureFailed("viewer address isn't public")
    try:
        r = requests.get(url, stream=True, timeout=qm.TIMEOUT, allow_redirects=True,
                         headers={"User-Agent": qm.UA, "Accept": "text/html,application/xhtml+xml,*/*"})
    except requests.Timeout:
        raise CaptureFailed("viewer page timed out")
    except requests.RequestException:
        raise CaptureFailed("couldn't connect to the viewer page")
    with r:
        buf = io.BytesIO()
        try:
            for chunk in r.iter_content(256 * 1024):
                buf.write(chunk)
                if buf.tell() > limit:
                    break
        except requests.RequestException:
            pass
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        text = buf.getvalue().decode(r.encoding or "utf-8", errors="replace")
        return r.status_code, ctype, text, r.url


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


# ── Frame URL templates ───────────────────────────────────────────────────────
class Template:
    """A numbered frame URL: prefix + number (zero-padded to `pad`) + suffix."""

    def __init__(self, prefix, suffix, pad=0, start=0):
        self.prefix, self.suffix, self.pad, self.start = prefix, suffix, pad, start

    def url(self, i):
        return f"{self.prefix}{str(i).zfill(self.pad) if self.pad else i}{self.suffix}"

    def show(self):
        return mask(self.prefix + ("{n:0%d}" % self.pad if self.pad else "{n}") + self.suffix)

    def key(self):
        return (self.prefix, self.suffix, self.pad, self.start)


class FrameSet:
    def __init__(self, urls, pattern, source, top=0, method=None, still=""):
        self.urls, self.pattern, self.source = urls, pattern, source
        self.top = top if 0 <= top < len(urls) else 0      # the frame the stone is shown from first
        self.method = method or source
        self.still = still                                 # the certificate's still image, if any


def _count_frames(t, known=None):
    """Number of frames from t.start on. Uses a known count when its last frame exists,
    otherwise finds the last frame by doubling then halving (frames are consecutive)."""
    if known and known >= MIN_FRAMES and _is_image(t.url(t.start + known - 1)):
        if not _is_image(t.url(t.start + known)):
            return known
    lo, hi = 1, 2          # frame index offset lo exists; hi unknown
    while hi <= MAX_COUNT_SEARCH and _is_image(t.url(t.start + hi - 1)):
        lo, hi = hi, hi * 2
    hi = min(hi, MAX_COUNT_SEARCH + 1)
    while hi - lo > 1:      # lo frames exist, hi frames don't
        mid = (lo + hi) // 2
        if _is_image(t.url(t.start + mid - 1)):
            lo = mid
        else:
            hi = mid
    return lo


def _templates_from_base(base, ext_hint=None):
    """Candidate templates for a frame folder / base URL."""
    base = base.split("#")[0]
    q = ""
    if "?" in base:
        base, q = base.split("?", 1)
        q = "?" + q
    exts = [ext_hint] if ext_hint else ["jpg", "png", "webp", "jpeg"]
    joins = [base + "/", base] if not base.endswith("/") else [base]
    out = []
    for j in joins:
        for ext in exts:
            for pad in (0, 3, 2, 4):
                for start in (0, 1):
                    out.append(Template(j, f".{ext}{q}", pad, start))
    return out


def _first_working(templates):
    """The first candidate template whose first frame is really an image."""
    seen, uniq = set(), []
    for t in templates:
        if t.key() not in seen:
            seen.add(t.key())
            uniq.append(t)
    uniq = uniq[:64]
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
        ok = list(ex.map(lambda t: _is_image(t.url(t.start)), uniq))
    for t, good in zip(uniq, ok):
        if good:
            return t
    return None


def _from_template(t, known, source, facts):
    if shared_asset(t.prefix):
        facts.append(f"pattern {t.show()} skipped: a shared site image folder, not the stone's frames")
        return None
    n = _count_frames(t, known)
    facts.append(f"pattern {t.show()} → {n} frame(s) found")
    if n < MIN_FRAMES:
        if n:
            facts.append(f"pattern {t.show()} rejected: {n} frame(s) is under the {MIN_FRAMES}-frame minimum")
        return None
    return FrameSet([t.url(t.start + i) for i in range(n)], t.show(), source)


# ── 1. Stone data from the search API ─────────────────────────────────────────
_COUNT_KEY = re.compile(r"(frame|image|img|still|pic)s?_?(count|total|num|number|cnt|len)|"
                        r"(count|num|number|total|no)_?(of_?)?(frame|image|still)s?|^frames$|^n_?frames$", re.I)
_BASE_KEY = re.compile(r"(base|renumbered|frame|still|image|img|folder|path|dir|prefix)s?_?(url|path|dir|folder|base)?$|"
                       r"^url$|^link$|^src$", re.I)


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def video_file_from_hint(hint):
    """A direct MP4 / WebM URL in the stone data, if any."""
    for _, v in _walk(hint or {}):
        if isinstance(v, str) and re.match(r"https?://", v) and re.search(r"\.(mp4|webm)(\?|$)", v, re.I):
            return v
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
        if fc > MAX_COUNT_SEARCH:
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
                            top=top, method=method, still=still_from_hint(hint))
        if not partial:
            facts.append(f"{method}: {where} ({mask(base)}) — no frames answered as {{n}}.webp/.jpg/.png")
    return None


def _from_hint(hint, facts):
    if not hint:
        return None
    flat = list(_walk(hint))
    urls = [(p, v) for p, v in flat if isinstance(v, str) and re.match(r"https?://", v)]
    # A list of frame image URLs
    lists = {}
    for p, v in urls:
        if re.search(r"\.(jpe?g|png|webp)(\?|$)", v, re.I) and "[" in p:
            lists.setdefault(p.rsplit("[", 1)[0], []).append(v)
    for p, vs in lists.items():
        if len(vs) >= MIN_FRAMES and not any(shared_asset(v) for v in vs):
            facts.append(f"stone data: {len(vs)} frame URLs in '{_clean_text(p)}'")
            return FrameSet(vs, mask(vs[0]) + f" (+{len(vs) - 1} more)", "stone data (frame list)")
    counts = [int(v) for p, v in flat if isinstance(v, (int, float, str)) and str(v).isdigit()
              and _COUNT_KEY.search(p.rsplit(".", 1)[-1]) and MIN_FRAMES <= int(v) <= MAX_COUNT_SEARCH]
    known = counts[0] if counts else None
    bases = [(p, v) for p, v in urls if _BASE_KEY.search(p.rsplit(".", 1)[-1])
             and not re.search(r"\.(mp4|webm|html?|js|css)(\?|$)", v, re.I)]
    facts.append(f"stone data: {len(urls)} link(s), frame count {known or 'not given'}")
    for p, v in bases:
        tmpl = _template_from_numbered(v)
        cands = [tmpl] if tmpl else []
        if not re.search(r"\.(jpe?g|png|webp)(\?|$)", v, re.I):
            cands += _templates_from_base(v)
        t = _first_working(cands)
        if t:
            fs = _from_template(t, known, f"stone data ('{_clean_text(p)}')", facts)
            if fs:
                return fs
        else:
            facts.append(f"stone data '{_clean_text(p)}' ({mask(v)}): no frame pattern answered")
    return None


# ── 3. Generic viewer page analysis ───────────────────────────────────────────
_NUMBERED = re.compile(r"((?:https?:)?//[^\s\"'<>()\\`]+?|/[^\s\"'<>()\\`]*?)(\d{1,4})(\.(?:jpe?g|png|webp))((?:\?[^\s\"'<>()\\`]*)?)", re.I)
_CONCAT = re.compile(r"([\"'`])((?:https?:)?//[^\"'`\s]*|/[^\"'`\s]*|[^\"'`\s]*/)\1\s*\+\s*[\w$.\[\]()+\- ]{1,40}?\s*\+\s*"
                     r"([\"'`])(\.(?:jpe?g|png|webp)[^\"'`\s]*)\3", re.I)
_TPL_LITERAL = re.compile(r"`([^`\s]*?)\$\{[^}]{1,40}\}(\.(?:jpe?g|png|webp)[^`\s]*)`", re.I)
_PLACEHOLDER = re.compile(r"((?:https?:)?//[^\s\"'<>`]+?)(\{\{?\s*(?:n|i|idx|index|frame|num|number|image)\s*\}?\}|%d|%0(\d)d|\{0\}|#{2,4})"
                          r"(\.(?:jpe?g|png|webp)[^\s\"'<>`]*)", re.I)
_COUNT_CFG = re.compile(r"[\"']?((?:frame|image|img|still)s?_?(?:count|total|num|number|cnt)|(?:total|num|number|no|count)_?(?:of_?)?"
                        r"(?:frame|image|still)s?|frames|nframes|n_frames|amount)[\"']?\s*[:=]\s*[\"']?(\d{1,4})\b", re.I)
_BASE_CFG = re.compile(r"[\"']?((?:base|renumbered|frames?|stills?|images?|img|folder|path|dir|prefix|media|asset)s?_?(?:url|path|dir|folder|base|root)?)"
                       r"[\"']?\s*[:=]\s*[\"']((?:https?:)?//[^\"'\s]+|/[^\"'\s]+)[\"']", re.I)
_VIDEO_FILE = re.compile(r"((?:https?:)?//[^\s\"'<>()\\`]+?\.(?:mp4|webm)(?:\?[^\s\"'<>()\\`]*)?)", re.I)
_SCRIPT_SRC = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']", re.I)
_INLINE_SCRIPT = re.compile(r"<script(?![^>]+src=)[^>]*>(.*?)</script>", re.I | re.S)
_IFRAME_SRC = re.compile(r"<iframe[^>]+src=[\"']([^\"']+)[\"']", re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_ENDPOINT = re.compile(r"[\"'`]((?:https?://[^\"'`\s]+)?/(?:api|v\d|graphql|json|data)[/?][^\"'`\s]{0,120})[\"'`]", re.I)


def _norm(text):
    """Un-escape JSON / JS so URLs are plain ('\\/' -> '/', '\\u002F' -> '/', '&amp;' -> '&')."""
    t = text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    t = re.sub(r"\\u0026|\\x26", "&", t)
    return unescape(t)


def _site(host):
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:])


def _template_from_numbered(u):
    m = re.match(r"^(.*?)(\d{1,4})(\.(?:jpe?g|png|webp))((?:\?.*)?)$", u, re.I)
    if not m:
        return None
    pad = len(m.group(2)) if m.group(2).startswith("0") and len(m.group(2)) > 1 else 0
    return Template(m.group(1), m.group(3) + m.group(4), pad, 0)


def _analyse(page_url, facts, depth=0):
    """Returns dict(explicit=[(template, numbers)], templates=[Template], counts=[int],
    bases=[url], videos=[url], endpoints=[str])."""
    status, ctype, html, final = _get_text(page_url, MAX_PAGE_BYTES)
    title = _TITLE.search(html)
    facts.append(f"viewer page: HTTP {status}, {ctype or 'no type'}, {len(html) // 1024} KB"
                 + (f", title '{_clean_text(title.group(1).strip()[:40])}'" if title else ""))
    if status >= 400:
        raise CaptureFailed(f"viewer page answered HTTP {status}")
    host = urlparse(final).hostname
    texts = [html] + _INLINE_SCRIPT.findall(html)
    srcs = [urljoin(final, s) for s in _SCRIPT_SRC.findall(html)]
    same = [s for s in srcs if _site(urlparse(s).hostname) == _site(host)]
    fetched = 0
    for s in same[:MAX_SCRIPTS]:
        try:
            st_, _, js, _ = _get_text(s, MAX_SCRIPT_BYTES)
            if st_ < 400:
                texts.append(js)
                fetched += 1
        except CaptureFailed:
            pass
    facts.append(f"scripts: {len(texts) - 1 - fetched} inline, {len(srcs)} linked "
                 f"({len(same)} same-site, {fetched} read)")
    res = {"explicit": [], "templates": [], "counts": [], "bases": [], "videos": [], "endpoints": []}
    groups = {}
    for raw in texts:
        t = _norm(raw)
        for m in _NUMBERED.finditer(t):
            pre = urljoin(final, m.group(1))
            num = m.group(2)
            pad = len(num) if num.startswith("0") and len(num) > 1 else 0
            groups.setdefault((pre, m.group(3) + m.group(4), pad), set()).add(int(num))
        for m in _CONCAT.finditer(t):
            res["templates"].append(Template(urljoin(final, m.group(2)), m.group(4)))
        for m in _TPL_LITERAL.finditer(t):
            if "${" not in m.group(1) and (m.group(1).startswith(("http", "//", "/"))):
                res["templates"].append(Template(urljoin(final, m.group(1)), m.group(2)))
        for m in _PLACEHOLDER.finditer(t):
            pad = int(m.group(3)) if m.group(3) else (len(m.group(2)) if m.group(2).startswith("#") else 0)
            res["templates"].append(Template(urljoin(final, m.group(1)), m.group(4), pad))
        res["counts"] += [int(m.group(2)) for m in _COUNT_CFG.finditer(t)
                          if MIN_FRAMES <= int(m.group(2)) <= MAX_COUNT_SEARCH]
        res["bases"] += [urljoin(final, m.group(2)) for m in _BASE_CFG.finditer(t)
                         if not re.search(r"\.(js|css|html?|json|mp4|webm|svg|ico|woff2?)(\?|$)", m.group(2), re.I)]
        res["videos"] += [urljoin(final, v) for v in _VIDEO_FILE.findall(t)]
        res["endpoints"] += _ENDPOINT.findall(t)
    for (pre, suf, pad), nums in groups.items():
        if len(nums) >= 2:
            res["explicit"].append((Template(pre, suf, pad, min(nums)), sorted(nums)))
    res["explicit"].sort(key=lambda x: -len(x[1]))
    facts.append(f"found: {sum(len(n) for _, n in res['explicit'])} numbered image link(s) in "
                 f"{len(res['explicit'])} group(s), {len(res['templates'])} URL template(s), "
                 f"frame count(s) {sorted(set(res['counts']))[:5] or 'none'}, {len(set(res['bases']))} base URL(s), "
                 f"{len(set(res['videos']))} video file(s)")
    if res["endpoints"]:
        eps = list(dict.fromkeys(mask(e) for e in res["endpoints"]))[:4]
        facts.append("data endpoints in scripts: " + ", ".join(eps))
    # A viewer nested in a frame: look inside it once
    frames = [urljoin(final, s) for s in _IFRAME_SRC.findall(html) if not s.startswith(("about:", "data:", "javascript:"))]
    if frames and depth == 0 and not (res["explicit"] or res["templates"] or res["bases"]):
        facts.append(f"page holds {len(frames)} nested frame(s): reading the first")
        try:
            sub = _analyse(frames[0], facts, depth + 1)
            for k in res:
                res[k] += sub[k]
        except CaptureFailed as e:
            facts.append(f"nested frame: {e}")
    return res


def _from_page(url, facts, id_hint=None):
    res = _analyse(url, facts)
    known = max(res["counts"]) if res["counts"] else None
    video = next(iter(dict.fromkeys(res["videos"])), "")
    # a) Numbered links in the page. Prefer groups whose URL mentions the viewer's own ID.
    explicit = res["explicit"]
    if id_hint:
        explicit = sorted(explicit, key=lambda x: (id_hint.lower() not in x[0].prefix.lower(), -len(x[1])))
    for t, nums in explicit[:4]:
        if (len(nums) < MIN_FRAMES and not known) or shared_asset(t.prefix):
            continue
        if _is_image(t.url(t.start)):
            fs = _from_template(t, max(known or 0, len(nums)), "viewer page (numbered links)", facts)
            if fs:
                return fs, video
    # b) URL templates in the scripts
    cands = []
    for t in res["templates"]:
        for start in (0, 1):
            for pad in ([t.pad] if t.pad else [0, 3, 2, 4]):
                cands.append(Template(t.prefix, t.suffix, pad, start))
    t = _first_working(cands) if cands else None
    if t:
        fs = _from_template(t, known, "viewer page (URL template)", facts)
        if fs:
            return fs, video
    # c) Frame base URL from the config
    bases = list(dict.fromkeys(res["bases"]))
    if id_hint:
        bases.sort(key=lambda b: id_hint.lower() not in b.lower())
    for b in bases[:4]:
        t = _first_working(_templates_from_base(b))
        if t:
            fs = _from_template(t, known, "viewer page (frame base URL)", facts)
            if fs:
                return fs, video
    return None, video


# ── 2. Known viewer formats ───────────────────────────────────────────────────
FORMATS = []   # [(name, matcher(url) -> match or None, extractor(url, match, facts) -> (FrameSet|None, video))]


def viewer_format(name, pattern):
    """Registers an extractor for viewer URLs whose path matches `pattern` (a regex)."""
    rx = re.compile(pattern, re.I)

    def deco(fn):
        FORMATS.append((name, lambda u: rx.search(urlparse(u).path), fn))
        return fn
    return deco


@viewer_format("format A (/diamond/<id>/video/<w>/<h>)", r"^/diamond/([^/]+)/video/\d+/\d+/?$")
def _format_a(url, m, facts):
    """Viewer pages at /diamond/<id>/video/<w>/<h>: <id> is the search API's certificate ID.
    The page itself only loads a script app, so its 360 fields are looked up by that ID."""
    cert_id = m.group(1)
    if CERT_LOOKUP is None:
        raise CaptureFailed("certificate lookup unavailable (search API not configured)")
    try:
        hint, reason = CERT_LOOKUP(cert_id)
    except Exception as e:
        hint, reason = None, f"certificate lookup error ({type(e).__name__})"
    facts.append(f"certificate lookup: {'found 360 fields' if hint else reason}")
    if not hint:
        raise CaptureFailed(reason if reason.startswith("certificate") else f"certificate lookup: {reason}")
    fs = _from_v360(hint, facts, "certificate lookup (API fields)")
    if fs is None:
        raise CaptureFailed("certificate lookup: its 360 fields gave no usable frames")
    return fs, ""


# ── Public API ────────────────────────────────────────────────────────────────
def find_frames(viewer_url, hint=None):
    """Returns (FrameSet or None, video_file_url or '', facts[list of str])."""
    facts = []
    try:
        fs = _from_v360(hint, facts, "API fields")
        # The certificate's own 360 fields are authoritative: if they fail their checks
        # (a frame missing, under the minimum) no looser guess is made from the same data.
        if fs is None and not _v360_candidates(hint):
            fs = _from_hint(hint, facts)
        if fs:
            return fs, "", facts
        for name, match, fn in FORMATS:
            m = match(viewer_url)
            if m:
                facts.append(f"viewer: {name}")
                fs, video = fn(viewer_url, m, facts)
                return fs, video, facts
        facts.append("viewer: unknown format, generic page analysis")
        fs, video = _from_page(viewer_url, facts)
        return fs, video, facts
    except CaptureFailed as e:
        facts.append(str(e))
        return None, "", facts


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
    first, size = clean_frame(good[0])
    cleaned, pos = [first], [kept[0]]
    for i, d in zip(kept[1:], good[1:]):
        try:
            cleaned.append(clean_frame(d, size)[0])
            pos.append(i)
        except Exception:
            pass
    # The top frame's new number (or the nearest kept frame if it failed)
    new_top = min(range(len(pos)), key=lambda j: abs(pos[j] - top_pos))
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


def capture(sb_url, key, viewer_url, hint=None):
    """Captures one viewer. Returns dict(ok, spin={'id','n'} | None, video='' | direct video URL
    found, note=<one-line diagnostic>, facts=[...]). Never raises."""
    with _cache_lock:
        if viewer_url in _cache:
            return _cache[viewer_url]
    t0 = time.time()
    try:
        fs, video, facts = find_frames(viewer_url, hint)
        if fs is None:
            # The last fact is the most specific reason (e.g. "viewer page answered HTTP 403")
            last = facts[-1] if facts else ""
            reason = last if not last.startswith(("found:", "scripts:", "viewer:", "viewer page:", "data endpoints", "pattern ", "page holds")) \
                else "no frame pattern found in the viewer page"
            res = {"ok": False, "spin": None, "video": video, "facts": facts, "note": reason}
        else:
            facts.append(f"method: {fs.method} — {len(fs.urls)} frames via {fs.source}, pattern {fs.pattern}")
            folder, n, top = store_frames(sb_url, key, fs.urls, facts, fs.top)
            res = {"ok": True, "spin": {"id": folder, "n": n, "top": top, "v": CAPTURE_VERSION}, "video": video, "facts": facts,
                   "method": fs.method, "still": fs.still,
                   "note": f"360 captured via {fs.method}: {n} frames"
                           + (f" (thinned evenly from {len(fs.urls)})" if n < len(fs.urls) else "")
                           + f", top frame {top}; pattern {fs.pattern}"}
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
    print("[spin-capture] " + json.dumps({"ok": res["ok"], "note": res["note"], "facts": res["facts"],
                                          "seconds": res["seconds"], "version": CAPTURE_VERSION,
                                          "commit": COMMIT}), flush=True)
    return res
