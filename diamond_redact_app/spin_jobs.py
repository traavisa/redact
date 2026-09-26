"""Runs 360 captures for quotes: at save time (with a time limit) and for existing quotes.

Save flow (see app.save_quote):
  - Each stone whose video is a viewer page already has an opaque /v/ link (quote_media).
  - start() begins capturing those stones in the background; wait() gives them up to
    SAVE_BUDGET seconds. Stones done by then are saved with their frames ("spin") and no
    viewer link at all.
  - The quote is saved. finish() then waits for the rest and updates the saved quote
    (spin added, /v/ link removed) as each one completes.
  - The /v/ token's row in media_links also gets the frames, so a /v/ link that was
    already sent (e.g. in a customer link) shows our spinner instead of the vendor page.

Status for the app's media note lives in STATUS (per quote id) and is printed to the log.
"""
import base64
import datetime
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as fwait

import requests

import quote_media as qm
import spin_capture as sc

SAVE_BUDGET = 25          # seconds a save waits for captures before saving with /v/ links
CAPTURE_WORKERS = 3       # stones captured at the same time (each downloads frames in parallel too)
LEGACY_VIEWER = "https://video.alldiamondeverything.com/?u="

_pool = ThreadPoolExecutor(max_workers=CAPTURE_WORKERS, thread_name_prefix="spin")
STATUS = {}               # quote id -> {label: note}
UPGRADE = {"running": False, "done": 0, "total": 0, "results": [], "started": None, "finished": None, "code": ""}
_lock = threading.Lock()


def _h(key):
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def _log(kind, data):
    try:
        print(f"[{kind}] " + json.dumps(data, default=str), flush=True)
    except Exception:
        pass


# ── media_links: frames for a /v/ token, and a lookup cache by vendor URL ─────
def token_of(link):
    link = str(link or "")
    if link.startswith(qm.VIEWER_LINK_BASE):
        t = link[len(qm.VIEWER_LINK_BASE):].split("?")[0].strip("/")
        return t if t and all(c in qm.TOKEN_ALPHABET for c in t) else ""
    return ""


def vendor_url_for(sb_url, key, link):
    """The vendor URL behind one of our viewer links (server-side lookup), or ''."""
    t = token_of(link)
    if t:
        try:
            r = requests.get(f"{sb_url}/rest/v1/media_links", params={"token": f"eq.{t}", "select": "vendor_url",
                                                                     "limit": "1"}, headers=_h(key), timeout=10)
            rows = r.json() if r.status_code == 200 else []
            return rows[0]["vendor_url"] if rows else ""
        except Exception:
            return ""
    if str(link).startswith(LEGACY_VIEWER):
        enc = str(link)[len(LEGACY_VIEWER):].split("&")[0]
        try:
            from urllib.parse import unquote
            enc = unquote(enc)
            u = base64.b64decode(enc + "=" * (-len(enc) % 4)).decode("utf-8")
            return u if u.startswith(("http://", "https://")) else ""
        except Exception:
            return ""
    return ""


# ── Which captures can be shown ───────────────────────────────────────────────
# Trusted = captured by capture code >= sc.TRUSTED_VERSION with at least MIN_FRAMES frames.
# Captures by the first build (no version, no top frame; e.g. 11 shared bridal_image
# pictures) are never shown or reused: the stone falls back to its original viewer link
# until the 360 upgrade redoes it. The Netlify functions apply the same rule.
def trusted_spin(sp):
    if not isinstance(sp, dict) or not sp.get("id"):
        return False
    try:
        n, v = int(sp.get("n") or 0), int(sp.get("v") or 0)
    except (TypeError, ValueError):
        return False
    return n >= sc.MIN_FRAMES and (v >= sc.TRUSTED_VERSION or ("top" in sp and sp.get("top") is not None))


def _row_trusted(row):
    try:
        n, v = int(row.get("spin_frames") or 0), int(row.get("spin_version") or 0)
    except (TypeError, ValueError):
        return False
    return bool(row.get("spin_id")) and n >= sc.MIN_FRAMES and (v >= sc.TRUSTED_VERSION or row.get("spin_top") is not None)


def _spin_row(row):
    if not _row_trusted(row):
        return None
    n = int(row["spin_frames"])
    try:
        top = int(row.get("spin_top") or 0)
    except (TypeError, ValueError):
        top = 0
    return {"id": row["spin_id"], "n": n, "top": top if 0 <= top < n else 0,
            "v": int(row.get("spin_version") or sc.TRUSTED_VERSION)}


_SPIN_COLS = ("spin_id,spin_frames,spin_top,spin_version", "spin_id,spin_frames,spin_top", "spin_id,spin_frames")


def known_spin(sb_url, key, vendor_url):
    """Trusted frames already captured for this vendor URL (from any earlier quote), or None."""
    for cols in _SPIN_COLS:
        try:
            r = requests.get(f"{sb_url}/rest/v1/media_links", params={
                "vendor_url": f"eq.{vendor_url}", "spin_id": "not.is.null", "select": cols},
                headers=_h(key), timeout=10)
            if r.status_code != 200:
                continue                                   # a column doesn't exist yet: try fewer
            for row in r.json():
                sp = _spin_row(row)
                if sp:
                    return sp
            return None
        except Exception:
            return None
    return None


def vendor_url_for_spin(sb_url, key, spin_id):
    """The viewer a stored capture came from (the media_links row that holds it now, or
    held it before a re-capture), or ''. Nothing is ever deleted from media_links."""
    for params in ({"or": f"(spin_id.eq.{spin_id},old_spin_ids.cs.{{{spin_id}}})"}, {"spin_id": f"eq.{spin_id}"}):
        try:
            r = requests.get(f"{sb_url}/rest/v1/media_links", params={**params, "select": "vendor_url", "limit": "1"},
                             headers=_h(key), timeout=10)
            if r.status_code != 200:
                continue
            rows = r.json()
            return rows[0]["vendor_url"] if rows else ""
        except Exception:
            return ""
    return ""


def record_spin(sb_url, key, vendor_url, spin):
    """Stores the frames on every media_links row for this vendor URL (so its /v/ links
    show our spinner). A capture it replaces is kept in old_spin_ids, so share links that
    carry the old frames can still be matched. Returns False if the SQL wasn't run."""
    body = {"spin_id": spin["id"], "spin_frames": spin["n"], "spin_top": spin.get("top", 0),
            "spin_version": sc.CAPTURE_VERSION}
    try:
        r = requests.get(f"{sb_url}/rest/v1/media_links", params={"vendor_url": f"eq.{vendor_url}",
                         "select": "token,spin_id,old_spin_ids"}, headers=_h(key), timeout=10)
        rows = r.json() if r.status_code == 200 else None
    except Exception:
        rows = None
    ok = False
    for row in (rows if rows is not None else [None]):
        full = dict(body)
        if row is not None:
            olds = [x for x in (row.get("old_spin_ids") or []) if x]
            if row.get("spin_id") and row["spin_id"] != spin["id"] and row["spin_id"] not in olds:
                olds.append(row["spin_id"])
            full["old_spin_ids"] = olds
        where = {"token": f"eq.{row['token']}"} if row is not None else {"vendor_url": f"eq.{vendor_url}"}
        # Newest columns first; older databases get what they have
        for drop in ((), ("old_spin_ids",), ("old_spin_ids", "spin_version"), ("old_spin_ids", "spin_version", "spin_top")):
            try:
                r = requests.patch(f"{sb_url}/rest/v1/media_links", params=where,
                                   headers={**_h(key), "Content-Type": "application/json", "Prefer": "return=minimal"},
                                   json={k: v for k, v in full.items() if k not in drop}, timeout=10)
                if r.status_code in (200, 204):
                    ok = True
                    break
            except Exception:
                break
    return ok


# ── Version gate: never capture with code older than what the site expects ─────
SITE_VERSION_URL = "https://quote.alldiamondeverything.com/.netlify/functions/capture-version"
_ver_cache = {"at": 0.0, "res": None}


def version_check(force=False):
    """(ok, message). ok only when this code's CAPTURE_VERSION is at least the version the
    site (Netlify, deployed from main) says is current. Fails closed if it can't be read."""
    if not force and _ver_cache["res"] and _ver_cache["res"][0] and time.time() - _ver_cache["at"] < 60:   # only "ok" is cached
        return _ver_cache["res"]
    try:
        r = requests.get(SITE_VERSION_URL, timeout=8, headers={"Cache-Control": "no-cache"})
        site = int(r.json().get("version")) if r.status_code == 200 else None
    except Exception:
        site = None
    if site is None:
        res = (False, f"couldn't read the site's capture version — this app runs {sc.CODE_TAG}")
    elif sc.CAPTURE_VERSION < site:
        res = (False, f"this app runs {sc.CODE_TAG} but the site is on v{site}: the new code isn't "
                      "deployed here yet (Render still building?) — wait for the deploy, then reload")
    else:
        res = (True, f"{sc.CODE_TAG}; site expects v{site}")
    _ver_cache.update(at=time.time(), res=res)
    return res


def capture_one(sb_url, key, vendor_url, hint=None):
    """Capture with the database cache in front. Returns the spin_capture result dict,
    plus 'mp4' = our hosted video URL when a direct video file was found and copied, and
    'still' = our hosted copy of the certificate's still image when there is one."""
    spin = known_spin(sb_url, key, vendor_url)
    if spin:
        res = {"ok": True, "spin": spin, "note": f"360 frames reused from an earlier capture ({spin['n']} frames)",
               "facts": ["frames already captured for this viewer"], "video": ""}
        still = sc.still_from_hint(hint)
        return _host_still(sb_url, key, res, still) if still else res
    # 1. A direct video file in the stone data beats frames
    mp4 = sc.video_file_from_hint(hint)
    if mp4:
        v = qm.process(sb_url, key, mp4, "video")
        if v["how"] == "hosted":
            return {"ok": True, "spin": None, "mp4": v["url"], "note": "direct video file found in the stone data — copied",
                    "facts": [f"video file {sc.mask(mp4)}"], "video": mp4}
    res = dict(sc.capture(sb_url, key, vendor_url, hint))
    # (No video file is ever taken from a viewer page: that was a viewer's own promo clip.)
    res["video"] = ""
    if res["ok"]:
        if not record_spin(sb_url, key, vendor_url, res["spin"]):
            res["facts"] = res["facts"] + ["media_links has no spin columns yet (run the SQL update)"]
        if res.get("still"):
            res = _host_still(sb_url, key, res, res["still"])
    if not str(res.get("still") or "").startswith(qm.MEDIA_BASE):
        res.pop("still", None)                              # never keep a vendor address
    return res


def _host_still(sb_url, key, res, url):
    """Re-hosts the certificate's still image (our /media/ copy goes in res['still'])."""
    v = qm.process(sb_url, key, url, "image")
    res = dict(res)
    if v.get("how") == "hosted":
        res["still"] = v["url"]
        res["facts"] = list(res.get("facts") or []) + ["certificate still image copied"]
    else:
        res.pop("still", None)
        res["facts"] = list(res.get("facts") or []) + [f"certificate still image not copied ({v.get('note') or 'unknown'})"]
    return res


def apply(stone, res):
    """Puts a successful result on a stone dict (in place). Returns True if changed.
    The stone keeps our own /v/ link in media_ref (never served) so it can be redone later."""
    if res.get("mp4"):
        prev = str(stone.get("video_url") or "")
        if token_of(prev) or prev.startswith(LEGACY_VIEWER):
            stone["media_ref"] = prev                  # the original viewer, kept (never served)
        stone["video_url"] = res["mp4"]
        stone["video_source"] = "api"                  # from the stone's own API fields
        stone.pop("spin", None)
        return True
    if res.get("ok") and res.get("spin"):
        sp = res["spin"]
        n = int(sp["n"])
        top = int(sp.get("top") or 0)
        prev = str(stone.get("video_url") or "")
        if token_of(prev) or prev.startswith(LEGACY_VIEWER):
            stone["media_ref"] = prev
        elif res.get("ref") and not stone.get("media_ref"):
            stone["media_ref"] = res["ref"]
        stone["spin"] = {"id": sp["id"], "n": n, "top": top if 0 <= top < n else 0,
                         "v": int(sp.get("v") or sc.CAPTURE_VERSION)}
        stone["video_url"] = ""
        still = str(res.get("still") or "")
        if still.startswith(qm.MEDIA_BASE):
            stone["image_url"] = still                      # the certificate's own still image
        elif not stone.get("image_url") or _is_frame(stone.get("image_url")):
            stone["image_url"] = f"{qm.MEDIA_BASE}{sp['id']}/{stone['spin']['top']:03d}.jpg"   # the top frame
        return True
    return False


def _is_frame(u):
    import re
    return bool(re.fullmatch(re.escape(qm.MEDIA_BASE) + r"[a-f0-9]{32}/\d{3}\.jpg", str(u or "")))


# ── Save-time flow ────────────────────────────────────────────────────────────
class SaveJobs:
    """Captures for one quote being saved."""

    def __init__(self, sb_url, key, qid):
        self.sb_url, self.key, self.qid = sb_url, key, qid
        self.jobs = []                  # (index, label, link_saved, future)
        self.skipped = []
        self.saved = threading.Event()
        self.save_ok = False

    def start(self, index, label, vendor_url, link_saved, hint=None):
        ok, why = version_check() if SPIN_ENABLED else (False, "360 capture is switched OFF (SPIN_ENABLED)")
        if not ok:                                      # keep the /v/ link; never capture with old code
            self.skipped.append(f"{label}: 360 capture skipped — {why}; saved with a private viewer link")
            _set_status(self.qid, label, "360 capture skipped: " + why)
            return
        fut = _pool.submit(capture_one, self.sb_url, self.key, vendor_url, hint)
        self.jobs.append((index, label, link_saved, fut))
        _set_status(self.qid, label, "capturing 360 frames…")

    def wait(self, stones, budget=SAVE_BUDGET):
        """Waits up to `budget` seconds; applies finished captures to `stones` in place.
        Returns notes, one per stone."""
        notes = list(self.skipped)
        if not self.jobs:
            return notes
        fwait([f for *_, f in self.jobs], timeout=budget)
        for i, label, _, fut in self.jobs:
            if fut.done():
                res = _result(fut)
                apply(stones[i], res)
                notes.append(_note(label, res))
                _set_status(self.qid, label, _note_text(res), res.get("facts"))
            else:
                notes.append(f"{label}: 360 capture still running — saved with a private viewer link for now; "
                             "the quote updates itself when capture finishes (see Capture status)")
        return notes

    def finish(self, saved_ok):
        """Call once the quote is saved (or failed). Pending captures update the saved quote."""
        self.save_ok = saved_ok
        pending = [j for j in self.jobs if not j[3].done()]
        if not pending or not saved_ok:
            return
        threading.Thread(target=self._finish, args=(pending,), daemon=True).start()

    def _finish(self, pending):
        for i, label, link_saved, fut in pending:
            res = _result(fut, timeout=900)
            if res.get("ok"):
                ok = patch_stone(self.sb_url, self.key, self.qid, i, link_saved, res)
                if not ok:
                    res = {**res, "note": res["note"] + " — but the saved quote couldn't be updated"}
            _set_status(self.qid, label, _note_text(res), res.get("facts"))
            _log("spin-save", {"quote": self.qid, "stone": label, "ok": res.get("ok"), "note": res.get("note")})


def _result(fut, timeout=None):
    try:
        return fut.result(timeout=timeout)
    except Exception as e:
        return {"ok": False, "spin": None, "note": f"capture error ({type(e).__name__})", "facts": []}


def _note_text(res):
    if res.get("ok"):
        return res["note"]
    return f"360 capture failed: {res.get('note')} — using private viewer link"


def _note(label, res):
    return f"{label}: {_note_text(res)}"


def _set_status(qid, label, note, facts=None):
    with _lock:
        STATUS.setdefault(qid, {})[label] = {"note": note, "facts": list(facts or []),
                                             "at": datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S")}


def status(qid):
    with _lock:
        return {k: dict(v) for k, v in (STATUS.get(qid) or {}).items()}


# ── Updating saved quotes ─────────────────────────────────────────────────────
_quote_locks = {}


def _qlock(qid):
    with _lock:
        return _quote_locks.setdefault(qid, threading.Lock())


def patch_stone(sb_url, key, qid, index, link_saved, res, expect_spin=None):
    """Read-modify-write of one stone in a saved quote. Only changes the stone if it still
    has the media we saved (so a manual edit isn't overwritten): the same video link, or,
    when redoing a bad capture, the same frames folder."""
    with _qlock(qid):
        try:
            r = requests.get(f"{sb_url}/rest/v1/quotes", params={"id": f"eq.{qid}", "select": "stones", "limit": "1"},
                             headers=_h(key), timeout=15)
            rows = r.json() if r.status_code == 200 else []
            if not rows:
                return False
            stones = rows[0].get("stones") or []
            if index >= len(stones):
                return False
            cur = stones[index]
            if expect_spin:
                if (cur.get("spin") or {}).get("id") != expect_spin:
                    return False
            elif (cur.get("video_url") or "") != (link_saved or ""):
                return False
            if not apply(stones[index], res):
                return False
            r = requests.patch(f"{sb_url}/rest/v1/quotes", params={"id": f"eq.{qid}"},
                               headers={**_h(key), "Content-Type": "application/json", "Prefer": "return=minimal"},
                               json={"stones": stones}, timeout=15)
            return r.status_code in (200, 204)
        except Exception:
            return False


# ── Re-processing existing quotes ─────────────────────────────────────────────
def bad_spin(stone):
    """A stored capture that must not be shown: from the first build (no version) or under
    the frame minimum (e.g. the 11 shared bridal_image pictures)."""
    sp = stone.get("spin")
    return isinstance(sp, dict) and bool(sp.get("id")) and not trusted_spin(sp)


def upgradable(quotes):
    """[(quote_id, index, label, link, bad_spin_id)] for stones in unexpired quotes that still
    use a viewer link, or whose earlier capture is bad (under the frame minimum)."""
    now = datetime.datetime.now(datetime.UTC)
    out = []
    for q in quotes or []:
        try:
            exp = datetime.datetime.fromisoformat(str(q.get("expires_at")).replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=datetime.UTC)
            if exp < now:
                continue
        except Exception:
            pass
        for i, s in enumerate(q.get("stones") or []):
            v = str(s.get("video_url") or "")
            label = f"{q.get('client') or 'No client'} · ···{s.get('cert_last4') or i + 1}"
            if bad_spin(s):
                out.append((q["id"], i, label + f" (redo: bad {s['spin'].get('n')}-frame capture"
                            + (")" if s["spin"].get("v") else " by the first build)"),
                            str(s.get("media_ref") or ""), s["spin"]["id"]))
            elif not s.get("spin") and (token_of(v) or v.startswith(LEGACY_VIEWER)):
                out.append((q["id"], i, label, v, None))
    return out


def start_upgrade(sb_url, key, quotes):
    """Background re-processing of every upgradable stone. Returns (started, message).
    Refuses when this code is older than the site's current capture version."""
    if not SPIN_ENABLED:
        return False, "Refused: 360 capture is switched OFF (SPIN_ENABLED)"
    ok, why = version_check(force=True)
    if not ok:
        _log("spin-upgrade", {"refused": why})
        return False, "Refused: " + why
    items = upgradable(quotes)
    with _lock:
        if UPGRADE["running"]:
            return False, "An upgrade is already running."
        UPGRADE.update(running=True, done=0, total=len(items), results=[],
                       started=datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S"), finished=None)
        UPGRADE["code"] = sc.CODE_TAG
    threading.Thread(target=_upgrade, args=(sb_url, key, items), daemon=True).start()
    return True, f"Started: {len(items)} stone(s), {sc.CODE_TAG}"


def _upgrade(sb_url, key, items):
    def one(item):
        qid, i, label, link, bad = item
        vendor = vendor_url_for(sb_url, key, link) if link else ""
        if not vendor and bad:
            vendor = vendor_url_for_spin(sb_url, key, bad)
        if not vendor:
            res = {"ok": False, "note": "original viewer link not found in the database", "facts": []}
        elif qm.to_embed(vendor) != vendor or "youtube" in vendor:
            res = {"ok": False, "note": "a YouTube video, not a 360 viewer — left as is", "facts": []}
        else:
            res = capture_one(sb_url, key, vendor)
            if res.get("ok") and bad and not link:
                try:                                   # keep a way back to the original viewer on the stone
                    res = {**res, "ref": qm._viewer_link(sb_url, key, vendor)}
                except Exception:
                    pass
            if res.get("ok") and not patch_stone(sb_url, key, qid, i, link, res, expect_spin=bad):
                res = {**res, "ok": False, "note": res["note"] + " — but the quote couldn't be updated"}
        with _lock:
            UPGRADE["done"] += 1
            UPGRADE["results"].append({"label": label, "quote": qid, "ok": bool(res.get("ok")),
                                       "note": _note_text(res), "facts": list(res.get("facts") or [])})
        _log("spin-upgrade", {"quote": qid, "stone": i, "ok": res.get("ok"), "note": res.get("note"),
                              "version": sc.CAPTURE_VERSION, "commit": sc.COMMIT})

    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="spin-up") as ex:   # leaves _pool free for saves
            list(ex.map(one, items))
    finally:
        with _lock:
            UPGRADE["running"] = False
            UPGRADE["finished"] = datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S")


def upgrade_status():
    with _lock:
        return {**UPGRADE, "results": list(UPGRADE["results"])}


# ── Audit: every stone with a capture that must not be shown ─────────────────
def fetch_all_quotes(sb_url, key, page=500, cap=20000):
    out, off = [], 0
    while off < cap:
        r = requests.get(f"{sb_url}/rest/v1/quotes", params={"select": "id,client,created_at,expires_at,stones",
                         "order": "created_at.desc", "limit": str(page), "offset": str(off)}, headers=_h(key), timeout=30)
        rows = r.json() if r.status_code == 200 else []
        out += rows
        if len(rows) < page:
            break
        off += page
    return out


def audit(sb_url, key, quotes=None):
    """Every quote stone whose capture must not be shown, whether its original viewer URL is
    still on record, and what clients see now. Also counts /v/ links holding such captures."""
    quotes = quotes if quotes is not None else fetch_all_quotes(sb_url, key)
    now = datetime.datetime.now(datetime.UTC)
    rows = []
    for q in quotes:
        try:
            exp = datetime.datetime.fromisoformat(str(q.get("expires_at")).replace("Z", "+00:00"))
            expired = (exp if exp.tzinfo else exp.replace(tzinfo=datetime.UTC)) < now
        except Exception:
            expired = False
        for i, s in enumerate(q.get("stones") or []):
            if not bad_spin(s):
                continue
            sp = s["spin"]
            ref = str(s.get("media_ref") or s.get("video_url") or "")
            vendor = (vendor_url_for(sb_url, key, ref) if ref else "") or vendor_url_for_spin(sb_url, key, sp["id"])
            rows.append({"quote": q["id"], "client": q.get("client") or "", "created": str(q.get("created_at") or "")[:16],
                         "expired": expired, "stone": i + 1, "last4": s.get("cert_last4") or "",
                         "frames": sp.get("n"), "version": sp.get("v") or 1,
                         "original_viewer": "on record" if vendor else "NOT FOUND",
                         "shown_now": "original viewer (fallback)" if vendor else "no 360 (image only)"})
    links = {"total": 0, "untrusted": 0}
    for cols in ("token,spin_id,spin_frames,spin_top,spin_version", "token,spin_id,spin_frames,spin_top", "token,spin_id,spin_frames"):
        try:
            r = requests.get(f"{sb_url}/rest/v1/media_links", params={"spin_id": "not.is.null", "select": cols},
                             headers=_h(key), timeout=30)
            if r.status_code != 200:
                continue
            lr = r.json()
            links = {"total": len(lr), "untrusted": sum(1 for x in lr if not _row_trusted(x))}
            break
        except Exception:
            break
    return {"stones": rows, "links": links, "quotes_scanned": len(quotes), "quotes": quotes,
            "at": datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S")}


# ── Kill switch (Render env var SPIN_ENABLED; set by the app). Default OFF. ───
SPIN_ENABLED = False


# ── One-time revert: bad captures -> original viewer ─────────────────────────
class RevertError(Exception):
    """A read or write the revert depends on failed. str(e) says exactly what to fix."""


KEY_HELP = ("set SUPABASE_SERVICE_KEY on Render (Environment) to the service_role key from "
            "Supabase → Project Settings → API (not the anon key), then redeploy")


def key_problem(key):
    """A reason the key can't be the service key, or ''."""
    if not key:
        return "SUPABASE_SERVICE_KEY is empty — " + KEY_HELP
    if key.startswith("sb_publishable_"):
        return "SUPABASE_SERVICE_KEY is a publishable (public) key — " + KEY_HELP
    if key.count(".") == 2:
        try:
            part = key.split(".")[1]
            role = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("role")
            if role != "service_role":
                return f"SUPABASE_SERVICE_KEY has role '{role}', not service_role — " + KEY_HELP
        except Exception:
            pass
    return ""


def _read_all(sb_url, key, table, select, what, order=None):
    out, off = [], 0
    while True:
        params = {"select": select, "limit": "1000", "offset": str(off)}
        if order:
            params["order"] = order
        try:
            r = requests.get(f"{sb_url}/rest/v1/{table}", params=params, headers=_h(key), timeout=30)
        except Exception as e:
            raise RevertError(f"reading {what} failed ({type(e).__name__}) — check the connection to Supabase")
        if r.status_code in (401, 403):
            raise RevertError(f"reading {what} was refused (HTTP {r.status_code}) — " + KEY_HELP)
        if r.status_code != 200:
            raise RevertError(f"reading {what} failed: HTTP {r.status_code} {r.text[:160]}")
        rows = r.json()
        out += rows
        if len(rows) < 1000:
            return out
        off += 1000


def _file_hash(sb_url, path):
    """sha1 of one stored file (streamed), or None if it can't be read."""
    import hashlib
    try:
        with requests.get(f"{sb_url}/storage/v1/object/public/{qm.BUCKET}/{path}", stream=True, timeout=(10, 60)) as r:
            if r.status_code != 200:
                return None
            h, size = hashlib.sha1(), 0
            for chunk in r.iter_content(256 * 1024):
                h.update(chunk)
                size += len(chunk)
            return h.hexdigest() if size else None
    except Exception:
        return None


def _frame_hash(sb_url, spin_id):
    return _file_hash(sb_url, f"{spin_id}/000.jpg")


# Stone lookup in the search API for the revert (set by the app): stone dict ->
# (info {"cert_id", "viewer_url"} or None, reason, method)
STONE_LOOKUP = None


def stone_identity(s):
    """What makes two quote stones the same diamond: stock ID, else certificate number, else last 4."""
    import re
    if s.get("ls_ref"):
        return "id:" + str(s["ls_ref"])
    m = re.search(r"([A-Za-z0-9-]{5,})\s*$", str(s.get("orig_filename") or ""))
    if m and any(ch.isdigit() for ch in m.group(1)):
        return "cert:" + m.group(1)
    return "last4:" + str(s.get("cert_last4") or "") if s.get("cert_last4") else ""


def cert_number_of(s):
    import re
    m = re.search(r"([0-9][A-Za-z0-9-]{4,})\s*$", str(s.get("orig_filename") or ""))
    return m.group(1) if m else ""


def _media_name(u):
    u = str(u or "")
    return u[len(qm.MEDIA_BASE):] if u.startswith(qm.MEDIA_BASE) else ""


def revert_bad(sb_url, key, include_all=False):
    """Finds, in EVERY quote and media_links row, media that isn't the stone's own and puts
    the stone's original viewer link back:
      - 360 captures with fewer than MIN_FRAMES frames, frames shared with a capture of a
        different viewer (a site's own images, e.g. bridal_image*), or frames that can't be
        read (include_all: every capture);
      - re-hosted VIDEO files that are the same file (URL or content) as the video of a
        different stone — a viewer's generic promo clip;
      - re-hosted IMAGES that are the same file as a different stone's image (removed).
    The original viewer comes from, in order: the stone's kept /v/ link (media_ref); the
    media_links row of its capture; the media_links row whose viewer URL has the stone's
    certificate ID (from the search API by stock ID / certificate number); a fresh /v/ link
    for the stone's own viewer from the API. Every stone says which method worked.
    Raises RevertError on any blocked or empty read: never reports 0 by mistake."""
    kp = key_problem(key)
    if kp:
        raise RevertError(kp)
    quotes = _read_all(sb_url, key, "quotes", "id,client,created_at,expires_at,stones", "quotes", "created_at.desc")
    if not quotes:
        raise RevertError("the quotes table returned 0 rows — the key can't read it: " + KEY_HELP)
    try:
        links = _read_all(sb_url, key, "media_links",
                          "token,vendor_url,spin_id,spin_frames,spin_top,spin_version,old_spin_ids", "media_links")
    except RevertError as e:
        if "HTTP 400" in str(e):
            raise RevertError("media_links is missing columns: run sections 3, 4 and 5 of "
                              "supabase/quote_media_setup.sql in the Supabase SQL Editor first")
        raise
    uses_links = any(token_of(s.get("video_url")) or token_of(s.get("media_ref")) or s.get("spin")
                     or _media_name(s.get("video_url")) for q in quotes for s in (q.get("stones") or []))
    if not links and uses_links:
        raise RevertError("media_links returned 0 rows although quotes use viewer links — the key can't "
                          "read it: " + KEY_HELP)
    by_token = {l["token"]: l for l in links}

    def rows_for(spin_id):
        return [l for l in links if l.get("spin_id") == spin_id or spin_id in (l.get("old_spin_ids") or [])]

    # ── 1. 360 captures ──
    spins = {}
    for q in quotes:
        for s in q.get("stones") or []:
            sp = s.get("spin")
            if isinstance(sp, dict) and sp.get("id"):
                e = spins.setdefault(sp["id"], {"n": sp.get("n"), "vendors": set()})
                ref = by_token.get(token_of(s.get("media_ref")) or token_of(s.get("video_url")))
                if ref:
                    e["vendors"].add(ref["vendor_url"])
    for l in links:
        if l.get("spin_id"):
            spins.setdefault(l["spin_id"], {"n": l.get("spin_frames"), "vendors": set()})["vendors"].add(l["vendor_url"])
    for sid, e in spins.items():
        for l in rows_for(sid):
            e["vendors"].add(l["vendor_url"])

    # ── 2. Re-hosted videos and images, by file ──
    files = {}                                  # media file name -> {"kind", "stones": set(identity)}
    for q in quotes:
        for s in q.get("stones") or []:
            ident = stone_identity(s) or f"q:{q['id']}"
            for fld, kind in (("video_url", "video"), ("image_url", "image")):
                name = _media_name(s.get(fld))
                if name and "/" not in name:    # frames of a capture are handled above
                    files.setdefault(name, {"kind": kind, "stones": set()})["stones"].add(ident)

    paths = [f"{sid}/000.jpg" for sid in spins] + list(files)
    with ThreadPoolExecutor(max_workers=8) as ex:
        hashes = dict(zip(paths, ex.map(lambda p: _file_hash(sb_url, p), paths)))

    if paths and all(hashes.get(p) is None for p in paths):
        raise RevertError(f"none of the {len(paths)} stored media files could be read from the quote-media bucket — "
                          "check the bucket is public (supabase/quote_media_setup.sql section 1)")
    by_hash = {}
    for sid in spins:
        h = hashes.get(f"{sid}/000.jpg")
        if h:
            by_hash.setdefault(("spin", h), set()).update(spins[sid]["vendors"] or {f"?{sid}"})
    for name, f in files.items():
        h = hashes.get(name) or f"name:{name}"
        by_hash.setdefault((f["kind"], h), set()).update(f["stones"])

    def spin_why(sid):
        e = spins.get(sid) or {}
        try:
            n = int(e.get("n") or 0)
        except (TypeError, ValueError):
            n = 0
        if n < sc.MIN_FRAMES:
            return f"360 capture: {n} frames (under {sc.MIN_FRAMES})"
        h = hashes.get(f"{sid}/000.jpg")
        if h is None:
            return "360 capture: frames can't be read"
        if len(by_hash.get(("spin", h), ())) >= 2:
            return f"360 capture: same frames as {len(by_hash[('spin', h)]) - 1} other viewer(s)"
        return "360 capture (all captures option)" if include_all else ""

    def file_why(name):
        f = files[name]
        h = hashes.get(name)
        group = by_hash.get((f["kind"], h or f"name:{name}"), set())
        if len(group) >= 2:
            return (f"{f['kind']}: same file as {len(group) - 1} other stone(s) — generic "
                    + ("promo clip" if f["kind"] == "video" else "picture") + ", not this stone's")
        return ""

    bad_spin_ids = {sid: w for sid, w in ((sid, spin_why(sid)) for sid in spins) if w}
    bad_files = {n: w for n, w in ((n, file_why(n)) for n in files) if w}

    # ── 3. Put each affected stone's own viewer back ──
    lookups = {}

    def original_viewer(s, sid=None):
        """(link, method) for the stone's original viewer, or ("", reason)."""
        ref = str(s.get("media_ref") or "")
        if token_of(ref) or ref.startswith(LEGACY_VIEWER):
            return ref, "kept /v/ link on the stone"
        if sid:
            c = [l for l in rows_for(sid) if l.get("token")]
            if c:
                return qm.VIEWER_LINK_BASE + c[0]["token"], "media_links (the capture's /v/ link)"
        if STONE_LOOKUP is None:
            return "", "no /v/ link on record, and the search API isn't configured for a lookup"
        ident = stone_identity(s)
        if ident not in lookups:
            try:
                lookups[ident] = STONE_LOOKUP(s)
            except Exception as e:
                lookups[ident] = (None, f"lookup error ({type(e).__name__})", "")
        info, reason, method = lookups[ident]
        if not info:
            return "", f"no /v/ link on record; API lookup: {reason}"
        cid = info["cert_id"].lower()
        c = [l for l in links if cid and cid in str(l.get("vendor_url") or "").lower() and l.get("token")]
        if c:
            return qm.VIEWER_LINK_BASE + c[0]["token"], f"media_links (/v/ link made when first saved; found via {method})"
        if info.get("viewer_url"):
            try:
                return qm._viewer_link(sb_url, key, info["viewer_url"]), f"fresh /v/ link for its own viewer (via {method})"
            except Exception as e:
                return "", f"couldn't create a /v/ link ({type(e).__name__})"
        return "", f"API lookup ({method}) found the stone but no 360 viewer link"

    rows, changed_quotes = [], {}
    for q in quotes:
        stones = q.get("stones") or []
        for i, s in enumerate(stones):
            base = {"Quote": q["id"], "Client": q.get("client") or "", "Stone": f"{i + 1} · ···{s.get('cert_last4') or ''}"}
            sp = s.get("spin") if isinstance(s.get("spin"), dict) else None
            vname = _media_name(s.get("video_url"))
            iname = _media_name(s.get("image_url"))
            why = []
            if sp and sp.get("id") in bad_spin_ids:
                why.append(bad_spin_ids[sp["id"]])
            if vname in bad_files:
                why.append(bad_files[vname])
            if why:
                link, method = original_viewer(s, sp.get("id") if sp else None)
                row = {**base, "Problem": "; ".join(why), "What": sp and sp.get("id", "")[:8] or vname[:12]}
                if not link:
                    row.update({"Restored to": "", "Method": method, "Result": "ERROR: " + method})
                else:
                    if sp and sp.get("id") in bad_spin_ids:
                        s.pop("spin", None)
                        s["spin_reverted"] = sp["id"]
                        if iname.startswith(sp["id"] + "/"):
                            s.pop("image_url", None)
                    if vname in bad_files:
                        s["video_reverted"] = vname           # record only; never served
                    if vname in bad_files or not (vname.endswith((".mp4", ".webm"))):
                        s["video_url"] = link
                    s.pop("video_source", None)
                    changed_quotes[q["id"]] = q
                    row.update({"Restored to": link, "Method": method, "Result": "pending"})
                rows.append(row)
            if iname in bad_files:
                s.pop("image_url", None)
                s["image_reverted"] = iname
                changed_quotes[q["id"]] = q
                rows.append({**base, "Problem": bad_files[iname], "What": iname[:12], "Restored to": "(image removed)",
                             "Method": "generic image removed", "Result": "pending"})

    for qid, q in changed_quotes.items():
        try:
            r = requests.patch(f"{sb_url}/rest/v1/quotes", params={"id": f"eq.{qid}"},
                               headers={**_h(key), "Content-Type": "application/json", "Prefer": "return=representation"},
                               json={"stones": q["stones"]}, timeout=20)
            res = "reverted" if r.status_code == 200 and r.json() else f"ERROR: quote not updated (HTTP {r.status_code})"
        except Exception as e:
            res = f"ERROR: quote not updated ({type(e).__name__})"
        for row in rows:
            if row["Quote"] == qid and row["Result"] == "pending":
                row["Result"] = res

    # ── 4. media_links: clear bad captures (vendor_url kept, id kept in old_spin_ids) ──
    lrows = []
    for l in links:
        sid = l.get("spin_id")
        if not sid or sid not in bad_spin_ids:
            continue
        olds = [x for x in (l.get("old_spin_ids") or []) if x]
        if sid not in olds:
            olds.append(sid)
        try:
            r = requests.patch(f"{sb_url}/rest/v1/media_links", params={"token": f"eq.{l['token']}"},
                               headers={**_h(key), "Content-Type": "application/json", "Prefer": "return=representation"},
                               json={"spin_id": None, "spin_frames": None, "spin_top": None, "spin_version": None,
                                     "old_spin_ids": olds}, timeout=20)
            res = "reverted" if r.status_code == 200 and r.json() else f"ERROR: not updated (HTTP {r.status_code})"
        except Exception as e:
            res = f"ERROR: not updated ({type(e).__name__})"
        lrows.append({"Viewer link": qm.VIEWER_LINK_BASE + l["token"], "Capture": sid[:8],
                      "Frames": l.get("spin_frames"), "Problem": bad_spin_ids[sid], "Result": res})

    unreadable = [n for n in files if hashes.get(n) is None]
    summary = {"quotes_read": len(quotes), "links_read": len(links), "captures_found": len(spins),
               "files_checked": len(files), "files_unreadable": len(unreadable),
               "bad_captures": len(bad_spin_ids),
               "generic_videos": sum(1 for n in bad_files if files[n]["kind"] == "video"),
               "generic_images": sum(1 for n in bad_files if files[n]["kind"] == "image"),
               "stones_reverted": sum(r["Result"] == "reverted" for r in rows),
               "links_reverted": sum(r["Result"] == "reverted" for r in lrows),
               "errors": sum(r["Result"].startswith("ERROR") for r in rows + lrows),
               "at": datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S")}
    _log("spin-revert", summary)
    return {"rows": rows, "links": lrows, "summary": summary}
