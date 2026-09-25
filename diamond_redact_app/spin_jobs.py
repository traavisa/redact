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
UPGRADE = {"running": False, "done": 0, "total": 0, "results": [], "started": None, "finished": None}
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


def known_spin(sb_url, key, vendor_url):
    """Frames already captured for this vendor URL (from any earlier quote), or None."""
    try:
        r = requests.get(f"{sb_url}/rest/v1/media_links", params={
            "vendor_url": f"eq.{vendor_url}", "spin_id": "not.is.null", "select": "spin_id,spin_frames",
            "limit": "1"}, headers=_h(key), timeout=10)
        rows = r.json() if r.status_code == 200 else []
        if rows and rows[0].get("spin_id") and int(rows[0].get("spin_frames") or 0) >= sc.MIN_FRAMES:
            return {"id": rows[0]["spin_id"], "n": int(rows[0]["spin_frames"])}
    except Exception:
        pass
    return None


def record_spin(sb_url, key, vendor_url, spin):
    """Stores the frames on every media_links row for this vendor URL (so its /v/ links
    show our spinner). Returns False if the table has no spin columns yet (SQL not run)."""
    try:
        r = requests.patch(f"{sb_url}/rest/v1/media_links", params={"vendor_url": f"eq.{vendor_url}"},
                           headers={**_h(key), "Content-Type": "application/json", "Prefer": "return=minimal"},
                           json={"spin_id": spin["id"], "spin_frames": spin["n"]}, timeout=10)
        return r.status_code in (200, 204)
    except Exception:
        return False


def capture_one(sb_url, key, vendor_url, hint=None):
    """Capture with the database cache in front. Returns the spin_capture result dict,
    plus 'mp4' = our hosted video URL when a direct video file was found and copied."""
    spin = known_spin(sb_url, key, vendor_url)
    if spin:
        return {"ok": True, "spin": spin, "note": f"360 frames reused from an earlier capture ({spin['n']} frames)",
                "facts": ["frames already captured for this viewer"], "video": ""}
    # 1. A direct video file in the stone data beats frames
    mp4 = sc.video_file_from_hint(hint)
    if mp4:
        v = qm.process(sb_url, key, mp4, "video")
        if v["how"] == "hosted":
            return {"ok": True, "spin": None, "mp4": v["url"], "note": "direct video file found in the stone data — copied",
                    "facts": [f"video file {sc.mask(mp4)}"], "video": mp4}
    res = sc.capture(sb_url, key, vendor_url, hint)
    # 2. A direct video file referenced by the viewer page
    if res.get("video"):
        v = qm.process(sb_url, key, res["video"], "video")
        if v["how"] == "hosted":
            return {**res, "ok": True, "spin": None, "mp4": v["url"],
                    "note": "direct video file found in the viewer page — copied"}
    if res["ok"]:
        if not record_spin(sb_url, key, vendor_url, res["spin"]):
            res["facts"] = res["facts"] + ["media_links has no spin columns yet (run the SQL update)"]
    return res


def apply(stone, res):
    """Puts a successful result on a stone dict (in place). Returns True if changed."""
    if res.get("mp4"):
        stone["video_url"] = res["mp4"]
        stone.pop("spin", None)
        return True
    if res.get("ok") and res.get("spin"):
        stone["spin"] = {"id": res["spin"]["id"], "n": int(res["spin"]["n"])}
        stone["video_url"] = ""
        return True
    return False


# ── Save-time flow ────────────────────────────────────────────────────────────
class SaveJobs:
    """Captures for one quote being saved."""

    def __init__(self, sb_url, key, qid):
        self.sb_url, self.key, self.qid = sb_url, key, qid
        self.jobs = []                  # (index, label, link_saved, future)
        self.saved = threading.Event()
        self.save_ok = False

    def start(self, index, label, vendor_url, link_saved, hint=None):
        fut = _pool.submit(capture_one, self.sb_url, self.key, vendor_url, hint)
        self.jobs.append((index, label, link_saved, fut))
        _set_status(self.qid, label, "capturing 360 frames…")

    def wait(self, stones, budget=SAVE_BUDGET):
        """Waits up to `budget` seconds; applies finished captures to `stones` in place.
        Returns notes, one per stone."""
        if not self.jobs:
            return []
        fwait([f for *_, f in self.jobs], timeout=budget)
        notes = []
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


def patch_stone(sb_url, key, qid, index, link_saved, res):
    """Read-modify-write of one stone in a saved quote. Only changes the stone if it still
    has the media we saved (so a manual edit isn't overwritten)."""
    with _qlock(qid):
        try:
            r = requests.get(f"{sb_url}/rest/v1/quotes", params={"id": f"eq.{qid}", "select": "stones", "limit": "1"},
                             headers=_h(key), timeout=15)
            rows = r.json() if r.status_code == 200 else []
            if not rows:
                return False
            stones = rows[0].get("stones") or []
            if index >= len(stones) or (stones[index].get("video_url") or "") != (link_saved or ""):
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
def upgradable(quotes):
    """[(quote_id, index, label, link)] for stones still on a viewer link, in unexpired quotes."""
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
            if s.get("spin") or not (token_of(v) or v.startswith(LEGACY_VIEWER)):
                continue
            label = f"{q.get('client') or 'No client'} · ···{s.get('cert_last4') or i + 1}"
            out.append((q["id"], i, label, v))
    return out


def start_upgrade(sb_url, key, quotes):
    """Background re-processing of every upgradable stone. Returns False if already running."""
    items = upgradable(quotes)
    with _lock:
        if UPGRADE["running"]:
            return False
        UPGRADE.update(running=True, done=0, total=len(items), results=[],
                       started=datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S"), finished=None)
    threading.Thread(target=_upgrade, args=(sb_url, key, items), daemon=True).start()
    return True


def _upgrade(sb_url, key, items):
    def one(item):
        qid, i, label, link = item
        vendor = vendor_url_for(sb_url, key, link)
        if not vendor:
            res = {"ok": False, "note": "viewer link not found in the database", "facts": []}
        elif qm.to_embed(vendor) != vendor or "youtube" in vendor:
            res = {"ok": False, "note": "a YouTube video, not a 360 viewer — left as is", "facts": []}
        else:
            res = capture_one(sb_url, key, vendor)
            if res.get("ok") and not patch_stone(sb_url, key, qid, i, link, res):
                res = {**res, "ok": False, "note": res["note"] + " — but the quote couldn't be updated"}
        with _lock:
            UPGRADE["done"] += 1
            UPGRADE["results"].append({"label": label, "quote": qid, "ok": bool(res.get("ok")),
                                       "note": _note_text(res), "facts": list(res.get("facts") or [])})
        _log("spin-upgrade", {"quote": qid, "stone": i, "ok": res.get("ok"), "note": res.get("note")})

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
