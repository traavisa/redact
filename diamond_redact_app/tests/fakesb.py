"""A fake Supabase (REST tables + storage), patched in for requests.get/post/patch/head/delete."""
import json
import os
import re
from urllib.parse import parse_qs, urlparse

SB = "https://srlbevzrkovruyerixdi.supabase.co"
SETUP_SQL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "supabase", "quote_media_setup.sql")


def bucket_mime_types():
    """allowed_mime_types of the quote-media bucket exactly as supabase/quote_media_setup.sql sets them."""
    sql = open(SETUP_SQL).read()
    m = re.search(r"array\[([^\]]*)\]", sql[sql.index("insert into storage.buckets"):])
    return re.findall(r"'([^']+)'", m.group(1))


class Resp:
    def __init__(self, status=200, body=None, content=b"", headers=None):
        self.status_code = status
        self._body = body
        self.content = content if content else (json.dumps(body).encode() if body is not None else b"")
        self.text = self.content.decode("utf-8", "replace")
        self.headers = headers or {}

    def json(self):
        return self._body if self._body is not None else json.loads(self.text)

    def iter_content(self, n):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSupabase:
    def __init__(self, quotes=(), files=(), block_reads=False):
        self.tables = {"quotes": [dict(q) for q in quotes], "custom_clients": [], "redact_history": [],
                       "media_links": [], "request_settings": [], "request_examples": [],
                       "request_corrections": []}
        self.storage = {"quote-media": {f: b"x" for f in files}, "certificates": {}}
        self.block_reads = block_reads
        self.calls = []
        self.headers = []
        self.allowed_mime = {"quote-media": bucket_mime_types()}     # the real bucket rules; None = anything

    # ── routing ──
    def request(self, method, url, params=None, json_body=None, data=None, headers=None):
        self.calls.append((method, url))
        self.headers.append((method, url, dict(headers or {})))
        u = urlparse(url)
        if not url.startswith(SB):
            return None
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        q.update(params or {})
        path = u.path
        if path.startswith("/rest/v1/"):
            return self._rest(method, path[len("/rest/v1/"):], q, json_body if json_body is not None else
                              (json.loads(data) if data else None))
        m = re.match(r"/storage/v1/object/list/([^/]+)$", path)
        if m and method == "POST":
            return self._list(m.group(1), json_body or {})
        m = re.match(r"/storage/v1/object/public/([^/]+)/(.+)$", path)
        if m:
            ok = m.group(2) in self.storage.get(m.group(1), {})
            return Resp(200, content=b"x") if ok else Resp(400, {"statusCode": "404", "error": "not_found"})
        m = re.match(r"/storage/v1/object/([^/]+)$", path)
        if m and method == "DELETE":                                  # bulk delete: {"prefixes": [names]}
            for name in (json.loads(data) if data else {}).get("prefixes", []):
                self.storage.get(m.group(1), {}).pop(name, None)
            return Resp(200, [])
        m = re.match(r"/storage/v1/object/([^/]+)/(.+)$", path)
        if m and method == "POST":
            ctype = str((headers or {}).get("Content-Type", "")).split(";")[0].strip()
            allowed = self.allowed_mime.get(m.group(1))
            if allowed is not None and ctype not in allowed:
                return Resp(415, {"statusCode": "415", "error": "invalid_mime_type",
                                  "message": f"mime type {ctype} is not supported"})
            self.storage.setdefault(m.group(1), {})[m.group(2)] = data
            return Resp(200, {"Key": m.group(2)})
        return Resp(404, {})

    def _rest(self, method, table, q, body):
        rows = self.tables.setdefault(table, [])
        if method == "GET":
            if self.block_reads:
                return Resp(401, {"message": "permission denied"})
            out = rows
            for k, v in q.items():
                if isinstance(v, str) and v.startswith("eq."):
                    out = [r for r in out if str(r.get(k)) == v[3:]]
            if q.get("order", "").startswith("created_at.desc"):
                out = sorted(out, key=lambda r: r.get("created_at", ""), reverse=True)
            off = int(q.get("offset", 0))
            lim = int(q.get("limit", 100000))
            return Resp(200, json.loads(json.dumps(out[off:off + lim])))
        if method == "POST":
            if table == "request_settings" and any(r.get("key") == body.get("key") for r in rows):   # upsert
                [r.update(body) for r in rows if r.get("key") == body.get("key")]
                return Resp(201, [])
            for b in (body if isinstance(body, list) else [body]):
                row = dict(b, created_at=b.get("created_at", "2026-09-26T00:00:00Z"))
                if table.startswith("request_") and table != "request_settings":
                    row["id"] = f"id{len(rows) + 1}-{len(self.calls)}"
                rows.append(row)
            return Resp(201, [])
        if method == "DELETE":
            if self.block_reads:
                return Resp(401, {"message": "permission denied"})
            keep = [r for r in rows if not all(str(r.get(k)) == v[3:] for k, v in q.items() if str(v).startswith("eq."))]
            rows[:] = keep
            return Resp(204, None)
        if method == "PATCH":
            hit = [r for r in rows if all(str(r.get(k)) == v[3:] for k, v in q.items() if str(v).startswith("eq."))]
            for r in hit:
                r.update(body)
            return Resp(200, hit)
        return Resp(405, {})

    def _list(self, bucket, body):
        prefix = body.get("prefix") or ""
        names = set()
        for n in self.storage.get(bucket, {}):
            if prefix:
                if n.startswith(prefix + "/"):
                    names.add((n[len(prefix) + 1:], True))
            else:
                names.add((n.split("/")[0], "/" not in n))
        rows = [{"name": n, "id": "x" if is_file else None} for n, is_file in sorted(names)]
        off, lim = body.get("offset", 0), body.get("limit", 1000)
        return Resp(200, rows[off:off + lim])

    def install(self, monkeypatch):
        import requests
        real = {m: getattr(requests, m) for m in ("get", "post", "patch", "head", "delete")}

        def make(method):
            def fn(url, *a, params=None, json=None, data=None, headers=None, **kw):
                r = self.request(method.upper(), url, params=params, json_body=json, data=data, headers=headers)
                if r is None:
                    raise requests.ConnectionError("no network in tests")
                return r
            return fn
        for m in real:
            monkeypatch.setattr(requests, m, make(m))
            fn = make(m)
            monkeypatch.setattr(requests.Session, m, lambda _self, url, *a, _fn=fn, **kw: _fn(url, *a, **kw))
        return self
