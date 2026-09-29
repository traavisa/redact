"""Runs 360 captures for quotes in the background after saving. Dormant while the kill switch is off.

Kill switch: the Render env var SPIN_ENABLED (see mode()):
  - unset / anything else = OFF: no capture; quotes keep their private /v/ viewer links.
  - "test" = capture ONLY when saving a quote for TEST_CLIENT ("Pure Carbon Group");
    every other client's quote is saved exactly as with capture off.
  - "true" = capture for every client.

Save flow (see app.save_quote) — the save never waits for a capture:
  - Each stone whose video is a viewer page already has an opaque /v/ link (quote_media).
  - start() queues a capture for each of those stones (all stones of a quote in parallel);
    the quote is saved at once with its /v/ links and its link is shown straight away.
  - finish() then updates the saved quote as EACH stone completes, in whatever order they
    finish (spin added, /v/ link removed). progress() gives "2 of 3 done" for the app.
  - The /v/ token's row in media_links also gets the frames, so a /v/ link that was
    already sent (e.g. in a customer link) shows our spinner instead of the vendor page.

Status for the app's media note lives in STATUS (per quote id) and is printed to the log.
"""
import datetime
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import quote_media as qm
import spin_capture as sc

CAPTURE_WORKERS = 4       # stones captured at the same time (frame downloads/uploads share
                          # spin_capture's pools, so the source never sees more than its limit)
LEGACY_VIEWER = "https://video.alldiamondeverything.com/?u="

_pool = ThreadPoolExecutor(max_workers=CAPTURE_WORKERS, thread_name_prefix="spin")
STATUS = {}               # quote id -> {label: note}
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


# ── Kill switch ────────────────────────────────────────────────────────────────
TEST_CLIENT = "Pure Carbon Group"


def mode(value):
    """SPIN_ENABLED setting -> "on" ("true"), "test" ("test") or "off" (anything else)."""
    v = str(value or "").strip().lower()
    return "on" if v == "true" else "test" if v == "test" else "off"


def capture_allowed(client):
    """True when a quote saved for `client` may be captured under the current SPIN_MODE."""
    if SPIN_MODE == "on":
        return True
    return SPIN_MODE == "test" and str(client or "").strip().casefold() == TEST_CLIENT.casefold()


# ── Which captures can be shown ───────────────────────────────────────────────
# Trusted = captured by the current capture code (version >= sc.TRUSTED_VERSION: the
# certificate's 360 API fields only) with at least MIN_FRAMES frames. Older captures are
# never shown or reused. The Netlify functions (lib/spin.js) apply the same rule.
def trusted_spin(sp):
    if not isinstance(sp, dict) or not sp.get("id"):
        return False
    try:
        n, v = int(sp.get("n") or 0), int(sp.get("v") or 0)
    except (TypeError, ValueError):
        return False
    return n >= sc.MIN_FRAMES and v >= sc.TRUSTED_VERSION


def _row_trusted(row):
    try:
        n, v = int(row.get("spin_frames") or 0), int(row.get("spin_version") or 0)
    except (TypeError, ValueError):
        return False
    return bool(row.get("spin_id")) and n >= sc.MIN_FRAMES and v >= sc.TRUSTED_VERSION


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


_SPIN_COLS = ("spin_id,spin_frames,spin_top,spin_version",)      # without spin_version nothing is trusted


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


def record_spin(sb_url, key, vendor_url, spin):
    """Stores the frames on every media_links row for this vendor URL (so its /v/ links
    show our spinner). A capture it replaces is kept in old_spin_ids, so share links that
    carry the old frames can still be matched. Returns False if the SQL wasn't run."""
    body = {"spin_id": spin["id"], "spin_frames": spin["n"], "spin_top": spin.get("top", 0),
            "spin_version": int(spin.get("v") or sc.CAPTURE_VERSION)}
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
    """Capture with the database cache in front (only captures by the current code are reused).
    Returns the spin_capture result dict, plus 'still' = our hosted copy of the certificate's
    still image when there is one. Frames come only from the certificate's 360 API fields."""
    spin = known_spin(sb_url, key, vendor_url)
    if spin:
        res = {"ok": True, "spin": spin, "note": f"360 frames reused from an earlier capture ({spin['n']} frames)",
               "facts": ["frames already captured for this viewer by the current capture code"], "video": "",
               "log": {"method": "reused an earlier capture by the current code", "frames": spin["n"],
                       "source_frames": None, "top_index": None, "top_frame": spin.get("top", 0)}}
        still = sc.still_from_hint(hint)
        return _host_still(sb_url, key, res, still) if still else res
    res = dict(sc.capture(sb_url, key, vendor_url, hint))
    res["video"] = ""                                   # never a video file
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
            stone["image_url"] = f"{qm.MEDIA_BASE}{sp['id']}/{stone['spin']['top']:03d}.{frame_ext(stone['spin']['v'])}"   # the top frame
        return True
    return False


def _is_frame(u):
    import re
    return bool(re.fullmatch(re.escape(qm.MEDIA_BASE) + r"[a-f0-9]{32}/\d{3}\.(jpg|webp)", str(u or "")))


def frame_ext(v):
    """A capture's frame file extension, from its capture version (v5+ = WebP)."""
    try:
        return "webp" if int(v) >= sc.WEBP_SINCE else "jpg"
    except (TypeError, ValueError):
        return "jpg"


# ── Save-time flow ────────────────────────────────────────────────────────────
class SaveJobs:
    """Captures for one quote being saved."""

    def __init__(self, sb_url, key, qid, client=""):
        self.sb_url, self.key, self.qid = sb_url, key, qid
        self.allowed = capture_allowed(client)
        self.jobs = []                  # (index, label, link_saved, future)

    def start(self, index, label, vendor_url, link_saved, hint=None):
        """Queues one stone's capture. Returns at once: nothing here touches the network."""
        if not self.allowed:                            # capture off for this quote: as if never asked
            return
        fut = _pool.submit(_checked_capture, self.sb_url, self.key, vendor_url, hint)
        self.jobs.append((index, label, link_saved, fut))
        _set_status(self.qid, label, PENDING)

    def finish(self, saved_ok):
        """Call once the quote is saved (or failed). Each capture updates the saved quote as soon
        as it completes. If the save failed, nothing is updated."""
        if not self.jobs:
            return
        if not saved_ok:
            for _, label, _, _ in self.jobs:
                _set_status(self.qid, label, "quote not saved — 360 capture result not used")
            return
        threading.Thread(target=self._finish, daemon=True).start()

    def _finish(self):
        futs = {fut: (i, label, link_saved) for i, label, link_saved, fut in self.jobs}
        for fut in as_completed(futs, timeout=None):
            i, label, link_saved = futs[fut]
            res = _result(fut)
            if res.get("ok"):
                ok = patch_stone(self.sb_url, self.key, self.qid, i, link_saved, res)
                if not ok:
                    res = {**res, "note": res["note"] + " — but the saved quote couldn't be updated"}
            _set_status(self.qid, label, _note_text(res), res.get("facts"), res.get("log"))
            _log("spin-save", {"quote": self.qid, "stone": label, "ok": res.get("ok"), "note": res.get("note")})
            tm = (res.get("log") or {}).get("timing")
            if tm:
                _log("spin-timing", {"quote": self.qid, "stone": label, "frames": res["log"]["frames"], **tm})
            elif not res.get("ok"):                     # a failed capture: why (storage's own message, masked) and how far it got
                _log("spin-timing", {"quote": self.qid, "stone": label, "ok": False,
                                     "error": sc.mask_text(res.get("note"), 200),
                                     **(res.get("fail_timing") or {})})


PENDING = "capturing 360 frames…"


def _checked_capture(sb_url, key, vendor_url, hint):
    """The version gate, then the capture — both in the background, so a save never waits."""
    ok, why = version_check()
    if not ok:                                          # keep the /v/ link; never capture with old code
        return {"ok": False, "spin": None, "note": f"skipped — {why}", "facts": [why]}
    return capture_one(sb_url, key, vendor_url, hint)


def progress(qid):
    """(done, total) captures for a quote."""
    stat = status(qid)
    return sum(v["note"] != PENDING for v in stat.values()), len(stat)


def _result(fut, timeout=None):
    try:
        return fut.result(timeout=timeout)
    except Exception as e:
        return {"ok": False, "spin": None, "note": f"capture error ({type(e).__name__})", "facts": []}


def _note_text(res):
    if res.get("ok"):
        return res["note"]
    return f"360 capture failed: {res.get('note')} — using private viewer link"


def _set_status(qid, label, note, facts=None, log=None):
    with _lock:
        STATUS.setdefault(qid, {})[label] = {"note": note, "facts": list(facts or []), "log": log,
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


# ── Kill switch (Render env var SPIN_ENABLED, read by the app via mode()). Default OFF. ──
SPIN_MODE = "off"
