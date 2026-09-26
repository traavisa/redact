"""ONE-TIME FIX (remove after it has run): clears a stone's video_url / image_url when it
points at a /media/ file that no longer exists in storage (e.g. the deleted promo clip).

Fail closed: a blocked or empty read raises DeadMediaError (shown as an error, never as
"0 found"). A file counts as gone only when storage answers "not found" for it AND it is
missing from the bucket listing; any other answer leaves the stone untouched and is
reported as "couldn't check".
"""
import datetime
import json
from concurrent.futures import ThreadPoolExecutor

import requests

import quote_media as qm

FIELDS = ("video_url", "image_url")
KEY_HELP = ("check SUPABASE_SERVICE_KEY on Render is the service_role key from Supabase → "
            "Project Settings → API")


class DeadMediaError(Exception):
    """A read the fix depends on failed. str(e) says what to fix; nothing was changed."""


def _h(key):
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def media_name(u):
    u = str(u or "")
    return u[len(qm.MEDIA_BASE):].split("?")[0] if u.startswith(qm.MEDIA_BASE) else ""


def read_quotes(sb_url, key):
    out, off = [], 0
    while True:
        try:
            r = requests.get(f"{sb_url}/rest/v1/quotes", headers=_h(key), timeout=30, params={
                "select": "id,client,stones", "order": "created_at.desc", "limit": "1000", "offset": str(off)})
        except Exception as e:
            raise DeadMediaError(f"reading quotes failed ({type(e).__name__}) — check the connection to Supabase")
        if r.status_code != 200:
            raise DeadMediaError(f"reading quotes failed: HTTP {r.status_code} — " + KEY_HELP)
        rows = r.json()
        out += rows
        if len(rows) < 1000:
            break
        off += 1000
    if not out:
        raise DeadMediaError("the quotes table returned 0 rows — the key can't read it: " + KEY_HELP)
    return out


def list_folder(sb_url, key, prefix=""):
    """File names (with the prefix) in one folder of the quote-media bucket."""
    out, off = [], 0
    while True:
        try:
            r = requests.post(f"{sb_url}/storage/v1/object/list/{qm.BUCKET}",
                              headers={**_h(key), "Content-Type": "application/json"},
                              json={"prefix": prefix, "limit": 1000, "offset": off}, timeout=30)
        except Exception as e:
            raise DeadMediaError(f"listing storage failed ({type(e).__name__})")
        if r.status_code != 200:
            raise DeadMediaError(f"listing storage failed: HTTP {r.status_code} — " + KEY_HELP)
        rows = r.json()
        out += [(prefix + "/" if prefix else "") + x["name"] for x in rows if x.get("name") and x.get("id")]
        if len(rows) < 1000:
            return out
        off += 1000


def probe(sb_url, name):
    """'exists', 'missing' or 'unknown (…)' from the public URL."""
    try:
        r = requests.head(f"{sb_url}/storage/v1/object/public/{qm.BUCKET}/{name}", timeout=(10, 30),
                          allow_redirects=True)
    except Exception as e:
        return f"unknown ({type(e).__name__})"
    if r.status_code == 200:
        return "exists"
    if r.status_code in (400, 404):
        return "missing"
    return f"unknown (HTTP {r.status_code})"


def run(sb_url, key):
    """Finds and clears dead /media/ links in every quote. Returns a report dict."""
    quotes = read_quotes(sb_url, key)
    refs = {}                                # file name -> [(quote id, stone index, field)]
    for q in quotes:
        for i, s in enumerate(q.get("stones") or []):
            for f in FIELDS:
                n = media_name(s.get(f))
                if n:
                    refs.setdefault(n, []).append((q["id"], i, f))
    listed = set(list_folder(sb_url, key))
    if refs and not listed:
        raise DeadMediaError(f"{len(refs)} stored file(s) are referenced but the storage listing came back "
                             "empty — storage can't be read, so nothing was changed")
    for folder in sorted({n.rsplit("/", 1)[0] for n in refs if "/" in n}):
        listed |= set(list_folder(sb_url, key, folder))
    names = sorted(refs)
    with ThreadPoolExecutor(max_workers=8) as ex:
        state = dict(zip(names, ex.map(lambda n: probe(sb_url, n), names)))
    dead = {n for n in names if state[n] == "missing" and n not in listed}
    unknown = {n: state[n] for n in names if state[n].startswith("unknown")
               or (state[n] == "missing" and n in listed)}

    by_q = {}
    for n in dead:
        for qid, i, f in refs[n]:
            by_q.setdefault(qid, []).append((i, f, n))
    clients = {q["id"]: q.get("client") or "" for q in quotes}
    rows = []
    for qid, items in sorted(by_q.items()):
        try:                                   # a fresh copy, just before changing it
            r = requests.get(f"{sb_url}/rest/v1/quotes", params={"id": f"eq.{qid}", "select": "id,stones"},
                             headers=_h(key), timeout=20)
            fresh = (r.json() or [None])[0] if r.status_code == 200 else None
        except Exception:
            fresh = None
        if fresh is None:
            rows += [_row(qid, clients, i, f, n, None, "ERROR: quote not readable") for i, f, n in items]
            continue
        stones = fresh.get("stones") or []
        changed = []
        for i, f, n in items:
            s = stones[i] if i < len(stones) else {}
            if media_name(s.get(f)) == n:
                s[f] = ""
                changed.append((i, f, n, s))
        if not changed:
            continue
        try:
            r = requests.patch(f"{sb_url}/rest/v1/quotes", params={"id": f"eq.{qid}"},
                               headers={**_h(key), "Content-Type": "application/json", "Prefer": "return=representation"},
                               json={"stones": stones}, timeout=20)
            res = "cleared" if r.status_code == 200 and r.json() else f"ERROR: not updated (HTTP {r.status_code})"
        except Exception as e:
            res = f"ERROR: not updated ({type(e).__name__})"
        rows += [_row(qid, clients, i, f, n, s, res) for i, f, n, s in changed]
    report = {"at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S"),
              "quotes_read": len(quotes), "files_referenced": len(names), "files_listed": len(listed),
              "dead_files": sorted(dead), "unchecked": unknown, "rows": rows,
              "cleared": sum(r["result"] == "cleared" for r in rows),
              "errors": sum(r["result"].startswith("ERROR") for r in rows)}
    try:
        print("[dead-media] " + json.dumps({k: v for k, v in report.items() if k != "rows"} |
                                           {"rows": [[r["quote"], r["stone"], r["field"], r["file"], r["result"]]
                                                     for r in rows]}, default=str), flush=True)
    except Exception:
        pass
    return report


def _row(qid, clients, i, f, n, s, res):
    return {"quote": qid, "client": clients.get(qid, ""), "stone": i + 1,
            "last4": str((s or {}).get("cert_last4") or ""), "field": f, "file": n, "result": res}
