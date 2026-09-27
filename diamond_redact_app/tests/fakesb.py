"""A fake Supabase (REST tables + storage), patched in for requests.get/post/patch/head/delete."""
import json
import re
from urllib.parse import parse_qs, urlparse

SB = "https://srlbevzrkovruyerixdi.supabase.co"


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
                       "media_links": []}
        self.storage = {"quote-media": {f: b"x" for f in files}, "certificates": {}}
        self.block_reads = block_reads
        self.calls = []
        self.headers = []

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
        m = re.match(r"/storage/v1/object/([^/]+)/(.+)$", path)
        if m and method == "POST":
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
            rows.append(dict(body, created_at=body.get("created_at", "2026-09-26T00:00:00Z")))
            return Resp(201, [])
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
