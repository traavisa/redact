"""Stone-source GraphQL client for the Live Search tab.

Internal module. Nothing in here is shown to users verbatim: every error raised is a
SourceError with a neutral message, and results are reduced to an explicit whitelist
of fields before they leave this module (no supplier name, location or stock number).

Verified against the source's published API examples:
  - endpoint: POST <NIVODA_API_URL> (production .../api/diamonds)
  - auth:     { authenticate { username_and_password(username, password) { token } } }
  - calls:    Authorization: Bearer <token>
  - search:   diamonds_by_query(query: {...}, offset, limit <= 50, order: {type: price, direction: ASC})
              query keys documented: labgrown, shapes, sizes [{from,to}], color [enum], has_image, has_v360
              { total_count items { id price diamond { id video image availability certificate {...} } } }
Everything else (extra filter keys, enum names, extra certificate fields) is only used
after the live schema confirms it via GraphQL introspection.
"""
import json
import re
import time

import requests

PAGE_LIMIT = 50            # documented maximum per request
TOKEN_TTL = 4 * 3600       # re-authenticate well before the token is likely to expire
SCHEMA_TTL = 6 * 3600
TIMEOUT = 30


class SourceError(Exception):
    """User-safe error. Message never names the source."""


class _AuthError(Exception):
    pass


class Enum(str):
    """A GraphQL enum literal (rendered without quotes)."""


def gql_literal(v):
    """Python value -> GraphQL input literal. Strings are JSON-quoted, Enum values are bare."""
    if isinstance(v, Enum):
        if not re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", v):
            raise ValueError("bad enum literal")
        return str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(round(float(v), 4)) if isinstance(v, float) else str(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(gql_literal(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {gql_literal(x)}" for k, x in v.items()) + "}"
    raise ValueError("unsupported literal")


# ── Documented certificate fields (requested on every search) ───────────────────
CERT_FIELDS = ["id", "lab", "shape", "certNumber", "cut", "carats", "clarity", "polish",
               "symmetry", "color", "width", "length", "depth", "floInt", "floCol",
               "depthPercentage", "table"]
# Optional certificate fields — requested only if the live schema has them.
FANCY_FIELDS = ["f_color", "f_intensity", "f_overtone"]
AS_GROWN_FIELDS = ["as_grown", "asGrown", "treated", "treatment"]
# Optional query keys we can push server-side if the live schema confirms them
# (list-of-enum). Everything is still re-checked in code after fetching.
ENUM_LIST_FILTERS = ["color", "clarity", "cut", "polish", "symmetry", "labs"]


_DOMAIN_RE = re.compile(r"\b(?:https?://[^\s)\]]+|[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:net|com|io|org|co|dev|app|ai|cloud)\b(?:[/:][^\s)\]]*)?)", re.I)
_NAME_RE = re.compile(r"nivoda\w*", re.I)


def sanitise(text, secrets=()):
    """Error text safe to show/log: no source name, hosts/URLs or credentials."""
    t = str(text or "")
    for sec in secrets:
        if sec and len(sec) >= 4:          # very short values would mangle ordinary words
            t = t.replace(sec, "[redacted]")
    t = _NAME_RE.sub("[source]", _DOMAIN_RE.sub("[host]", t))
    return re.sub(r"\s+", " ", t).strip()[:300]


class Client:
    def __init__(self, url, username, password, state):
        """`state` is a dict-like (Streamlit session_state) used to cache the token per session."""
        self.url = url
        self.username = username
        self.password = password
        self.state = state
        self.reset_diag()

    def reset_diag(self):
        """Diagnostics for the last search (shown in the tab and logged). Always sanitised."""
        self.diag = {"sign_in": "not attempted", "api_errors": [], "pages": 0, "raw_items": 0,
                     "sample_price_raw": None, "sample_carat": None}

    def _err(self, text):
        msg = sanitise(text, (self.username, self.password))
        if msg and msg not in self.diag["api_errors"]:
            self.diag["api_errors"].append(msg)

    # ── transport ────────────────────────────────────────────────────────────
    def _post(self, query, variables=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = {"query": query}
        if variables:
            body["variables"] = variables
        try:
            r = requests.post(self.url, headers=headers, data=json.dumps(body), timeout=TIMEOUT)
        except requests.Timeout:
            self._err(f"Timed out after {TIMEOUT}s")
            raise SourceError("The stone search timed out. Try again, or narrow the criteria.")
        except requests.RequestException as e:
            self._err(f"Connection error: {type(e).__name__}")
            raise SourceError("Couldn't reach the stone search service. Check the connection and try again.")
        if r.status_code in (401, 403):
            self._err(f"HTTP {r.status_code}")
            raise _AuthError()
        if r.status_code >= 500:
            self._err(f"HTTP {r.status_code}")
            raise SourceError("The stone search service is having problems right now. Try again shortly.")
        try:
            data = r.json()
        except ValueError:
            self._err(f"HTTP {r.status_code}: response was not JSON")
            raise SourceError("The stone search service sent an unreadable response.")
        errs = data.get("errors") or []
        if errs:
            for e in errs:
                self._err(e.get("message", "") if isinstance(e, dict) else e)
            msg = " ".join(str(e.get("message", "")) for e in errs if isinstance(e, dict)).lower()
            if any(w in msg for w in ("auth", "token", "expired", "jwt", "login", "permission")):
                raise _AuthError()
            if not data.get("data"):
                raise SourceError("The stone search rejected the request. Try simpler criteria.")
        return data.get("data") or {}

    def _token(self, force=False):
        cached = self.state.get("_ls_tok")
        if cached and not force and time.time() - cached[1] < TOKEN_TTL:
            if self.diag["sign_in"] == "not attempted":
                self.diag["sign_in"] = "ok (cached token)"
            return cached[0]
        q = ("query Authenticate($username: String!, $password: String!) { authenticate { "
             "username_and_password(username: $username, password: $password) { token } } }")
        try:
            data = self._post(q, {"username": self.username, "password": self.password})
            tok = (((data.get("authenticate") or {}).get("username_and_password")) or {}).get("token")
        except _AuthError:
            tok = None
        if not tok:
            self.state.pop("_ls_tok", None)
            self.diag["sign_in"] = "FAILED"
            raise SourceError("Live Search couldn't sign in. Check the username and password in the settings.")
        self.state["_ls_tok"] = (tok, time.time())
        self.diag["sign_in"] = "ok (re-signed in after the token was refused)" if force else "ok (new token)"
        return tok

    def call(self, query):
        """Authenticated call; on an auth failure, re-authenticates once and retries."""
        try:
            return self._post(query, token=self._token())
        except _AuthError:
            try:
                return self._post(query, token=self._token(force=True))
            except _AuthError:
                self.diag["sign_in"] = "FAILED (token refused after re-sign-in)"
                raise SourceError("Live Search's sign-in was refused. Check the account settings.")

    # ── schema discovery (introspection) ─────────────────────────────────────
    _schema_cache = {}

    def schema(self):
        """Returns what the live schema confirms, or a documented-only fallback."""
        hit = Client._schema_cache.get(self.url)
        if hit and time.time() - hit[0] < SCHEMA_TTL:
            return hit[1]
        try:
            info = self._introspect()
            stamp = time.time()
        except Exception:
            # Introspection unavailable: fall back to the documented filters only,
            # and try again in ~10 minutes rather than holding the fallback for hours.
            info = {"verified": False, "filters": {}, "cert_extra": []}
            stamp = time.time() - SCHEMA_TTL + 600
        Client._schema_cache[self.url] = (stamp, info)
        return info

    def _introspect(self):
        tref = "kind name ofType { kind name ofType { kind name ofType { kind name } } }"
        q = ("{ __schema { queryType { name } types { kind name "
             f"fields {{ name args {{ name type {{ {tref} }} }} type {{ {tref} }} }} "
             f"inputFields {{ name type {{ {tref} }} }} enumValues {{ name }} }} }} }}")
        data = self.call(q)
        sch = data["__schema"]
        types = {t["name"]: t for t in sch["types"] if t.get("name")}

        def named(t):
            while t and t.get("kind") in ("NON_NULL", "LIST"):
                t = t.get("ofType")
            return t or {}

        def is_list(t):
            while t and t.get("kind") == "NON_NULL":
                t = t.get("ofType")
            return bool(t) and t.get("kind") == "LIST"

        root = types[sch["queryType"]["name"]]
        dq = next(f for f in root["fields"] if f["name"] == "diamonds_by_query")
        qarg = next(a for a in dq["args"] if a["name"] == "query")
        qin = types[named(qarg["type"])["name"]]
        fields = {f["name"]: f["type"] for f in (qin.get("inputFields") or [])}

        filters = {}
        if named(fields.get("labgrown", {})).get("name") == "Boolean":
            filters["labgrown"] = True
        if "shapes" in fields and is_list(fields["shapes"]) and named(fields["shapes"]).get("name") == "String":
            filters["shapes"] = True
        if "sizes" in fields and is_list(fields["sizes"]):
            st = types.get(named(fields["sizes"]).get("name"), {})
            names = {f["name"] for f in (st.get("inputFields") or [])}
            if {"from", "to"} <= names:
                filters["sizes"] = True
        for key in ENUM_LIST_FILTERS:
            t = fields.get(key)
            if t and is_list(t) and named(t).get("kind") == "ENUM":
                et = types.get(named(t)["name"], {})
                filters[key] = [e["name"] for e in (et.get("enumValues") or [])]

        # Walk the documented response path to the certificate type
        cert_extra = []
        try:
            ret = types[named(dq["type"])["name"]]
            items = next(f for f in ret["fields"] if f["name"] == "items")
            it = types[named(items["type"])["name"]]
            dia = next(f for f in it["fields"] if f["name"] == "diamond")
            dt = types[named(dia["type"])["name"]]
            cf = next(f for f in dt["fields"] if f["name"] == "certificate")
            ct = types[named(cf["type"])["name"]]
            scalars = {f["name"] for f in ct["fields"]
                       if named(f["type"]).get("kind") in ("SCALAR", "ENUM") and not is_list(f["type"])}
            cert_extra = [f for f in FANCY_FIELDS + AS_GROWN_FIELDS if f in scalars]
        except Exception:
            cert_extra = []
        return {"verified": True, "filters": filters, "cert_extra": cert_extra}

    # ── search ───────────────────────────────────────────────────────────────
    def search(self, query_input, max_stones=500):
        """Fetches up to `max_stones` stones (cheapest first) for a server-side query dict.
        Returns (whitelisted_stones, total_count)."""
        extra = self.schema().get("cert_extra", [])
        cert_sel = " ".join(CERT_FIELDS + extra)
        out, total, offset = [], 0, 0
        while offset < max_stones:
            q = ("query { diamonds_by_query(query: " + gql_literal(query_input) +
                 f", offset: {offset}, limit: {PAGE_LIMIT}, order: {{type: price, direction: ASC}}) {{ "
                 "total_count items { id price diamond { id video image availability "
                 "certificate { " + cert_sel + " } } } } }")
            data = self.call(q)
            res = data.get("diamonds_by_query") or {}
            self.diag["total_count"] = res.get("total_count")
            total = int(res.get("total_count") or 0)
            items = res.get("items") or []
            self.diag["pages"] += 1
            self.diag["raw_items"] += len(items)
            if self.diag["sample_price_raw"] is None:
                for i in items:
                    if isinstance(i, dict) and i.get("price") is not None:
                        self.diag["sample_price_raw"] = i.get("price")
                        self.diag["sample_carat"] = ((i.get("diamond") or {}).get("certificate") or {}).get("carats")
                        break
            out += [s for s in (whitelist(i) for i in items) if s]
            offset += PAGE_LIMIT
            if len(items) < PAGE_LIMIT or offset >= total:
                break
        return out, total


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def whitelist(item):
    """Keep ONLY allowed fields. Anything else in the response is dropped here."""
    if not isinstance(item, dict):
        return None
    d = item.get("diamond") or {}
    c = d.get("certificate") or {}
    sid = d.get("id") or item.get("id")
    if not sid:
        return None
    s = {
        "sid": str(sid),                              # internal only
        "price_raw": _num(item.get("price")),
        "image": d.get("image") or "",                # internal screen only
        "video": d.get("video") or "",                # internal screen only
        "availability": str(d.get("availability") or ""),
        "lab": str(c.get("lab") or ""),
        "cert_no": str(c.get("certNumber") or ""),
        "shape": str(c.get("shape") or ""),
        "carat": _num(c.get("carats")),
        "color": str(c.get("color") or ""),
        "clarity": str(c.get("clarity") or ""),
        "cut": str(c.get("cut") or ""),
        "polish": str(c.get("polish") or ""),
        "symmetry": str(c.get("symmetry") or ""),
        "flo": str(c.get("floInt") or ""),
        "flo_col": str(c.get("floCol") or ""),
        "length": _num(c.get("length")),
        "width": _num(c.get("width")),
        "depth_mm": _num(c.get("depth")),
        "depth_pct": _num(c.get("depthPercentage")),
        "table_pct": _num(c.get("table")),
    }
    for k in FANCY_FIELDS + AS_GROWN_FIELDS:
        if k in c:
            s[k] = c.get(k)
    return s
