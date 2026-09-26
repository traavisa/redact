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
Everything else (extra filter keys, enum names, the count query, extra certificate fields)
is only used after the live schema confirms it via GraphQL introspection. Introspection
also records the schema's own descriptions of the price / total / labgrown fields so the
diagnostics can show what the source says they mean.
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
# Candidate boolean "is lab-grown" fields on the diamond / certificate types. If one exists it's
# requested so the diagnostics can confirm what labgrown:false actually returned.
LABGROWN_FLAG_FIELDS = ["labgrown", "lab_grown", "labGrown", "is_labgrown", "isLabgrown", "is_lab_grown"]
# Field names on the search item / response that are worth reporting for the price check.
_PRICE_WORDS = ("price", "discount", "markup", "value", "cost", "currency", "fee", "delivered")


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
                     "total_count": None, "count_query_total": None, "real_total": None,
                     "cap_hit": False, "labgrown_flags": None}

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
    _media_off = {}      # url -> True once the API refused the extra media fields

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
            info = {"verified": False, "filters": DOCUMENTED_FILTERS, "cert_extra": [],
                    "count_query": None, "lg_flag": None, "docs": {}}
            stamp = time.time() - SCHEMA_TTL + 600
        Client._schema_cache[self.url] = (stamp, info)
        return info

    def _introspect(self):
        tref = "kind name ofType { kind name ofType { kind name ofType { kind name } } }"
        q = ("{ __schema { queryType { name } types { kind name "
             f"fields {{ name description args {{ name type {{ {tref} }} }} type {{ {tref} }} }} "
             f"inputFields {{ name description type {{ {tref} }} }} enumValues {{ name }} }} }} }}")
        data = self.call(q)
        sch = data["__schema"]
        types = {t["name"]: t for t in sch["types"] if t.get("name")}
        root = types[sch["queryType"]["name"]]
        dq = next(f for f in root["fields"] if f["name"] == "diamonds_by_query")
        qarg = next(a for a in dq["args"] if a["name"] == "query")
        qin_name = _named(qarg["type"]).get("name")
        qin = types[qin_name]
        docs = {}

        # Every query input field, described generically; live_search decides what to use.
        filters = {}
        for f in qin.get("inputFields") or []:
            spec = _filter_spec(f["type"], types)
            if spec:
                spec["type"] = _type_str(f["type"])
                filters[f["name"]] = spec
            if f.get("description"):
                docs[f"query.{f['name']}"] = f["description"]

        # A separate count query taking the same query input (the real total for the search)
        count_query = None
        for f in root["fields"]:
            if f["name"] == "diamonds_by_query" or "count" not in f["name"].lower():
                continue
            qa = next((a for a in f.get("args") or [] if a["name"] == "query"), None)
            if (qa and _named(qa["type"]).get("name") == qin_name
                    and _named(f["type"]).get("name") in ("Int", "Float")
                    and all(a["name"] == "query" or _named(a["type"]).get("kind") != "NON_NULL"
                            for a in f["args"])):
                if count_query is None or f["name"] == "diamonds_by_query_count":
                    count_query = f["name"]
                    if f.get("description"):
                        docs[f"{f['name']}"] = f["description"]

        # Walk the documented response path: result -> items -> diamond -> certificate
        cert_extra, lg_flag = [], None
        try:
            ret = types[_named(dq["type"])["name"]]
            for f in ret["fields"]:
                if f.get("description") and (f["name"] == "total_count" or "count" in f["name"].lower()):
                    docs[f"result.{f['name']}"] = f["description"]
            items = next(f for f in ret["fields"] if f["name"] == "items")
            it = types[_named(items["type"])["name"]]
            for f in it["fields"]:
                if any(w in f["name"].lower() for w in _PRICE_WORDS):
                    docs[f"item.{f['name']}"] = f.get("description") or "(no description in the schema)"
            dia = next(f for f in it["fields"] if f["name"] == "diamond")
            dt = types[_named(dia["type"])["name"]]
            cf = next(f for f in dt["fields"] if f["name"] == "certificate")
            ct = types[_named(cf["type"])["name"]]

            def scalars(t):
                return {f["name"]: f for f in t["fields"]
                        if _named(f["type"]).get("kind") in ("SCALAR", "ENUM") and not _is_list(f["type"])}
            media_sel, media_desc = _media_selection(dt, types)
            cert_media_sel, cert_media_desc = _cert_media_selection(ct, types)
            cert_lookup = _cert_lookup_field(root, ct.get("name") or _named(cf["type"]).get("name"))
            stone_lookup = _stone_lookup_field(root, dt.get("name"), it.get("name"))
            cert_filter = next((n for n, sp in filters.items()
                                if "cert" in n.lower() and sp.get("kind") == "list_str"), None)
            cs, ds = scalars(ct), scalars(dt)
            cert_extra = [f for f in FANCY_FIELDS + AS_GROWN_FIELDS if f in cs]
            for where, fs in (("diamond", ds), ("certificate", cs)):
                name = next((n for n in LABGROWN_FLAG_FIELDS
                             if n in fs and _named(fs[n]["type"]).get("name") == "Boolean"), None)
                if name:
                    lg_flag = (where, name)
                    if fs[name].get("description"):
                        docs[f"{where}.{name}"] = fs[name]["description"]
                    break
        except Exception:
            cert_extra = []
        docs = {k: sanitise(v) for k, v in docs.items()}
        try:
            media = {"sel": media_sel, "keys": _sel_keys(media_sel), "desc": [sanitise(d) for d in media_desc],
                     "cert_sel": cert_media_sel, "cert_keys": _sel_keys(cert_media_sel),
                     "cert_desc": [sanitise(d) for d in cert_media_desc], "cert_lookup": cert_lookup,
                     "stone_lookup": stone_lookup, "cert_filter": cert_filter}
        except NameError:
            media = {"sel": "", "keys": [], "desc": [], "cert_sel": "", "cert_keys": [], "cert_desc": [],
                     "cert_lookup": None, "stone_lookup": None, "cert_filter": None}
        return {"verified": True, "filters": filters, "cert_extra": cert_extra,
                "count_query": count_query, "lg_flag": lg_flag, "docs": docs, "media": media}

    # ── certificate lookup (360 capture fallback) ────────────────────────────
    def certificate_media(self, cert_id):
        """Media fields for one certificate by its ID. Returns (hint dict or None, reason)."""
        if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", str(cert_id or "")):
            return None, "no certificate ID in the viewer link"
        sch = self.schema()
        media = sch.get("media") or {}
        lk = media.get("cert_lookup")
        if not sch.get("verified"):
            return None, "schema not confirmed, so no certificate lookup"
        if not lk:
            return None, "the API has no certificate-by-ID query"
        if not media.get("cert_sel"):
            return None, "the API's certificate has no 360 fields"
        if not re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*!?", lk["type"]):
            return None, "unexpected lookup argument type"
        q = (f"query ($v: {lk['type']}) {{ c: {lk['field']}({lk['arg']}: $v) {{ id {media['cert_sel']} }} }}")
        try:
            data = self._post(q, {"v": str(cert_id)}, token=self._token())
        except _AuthError:
            return None, "certificate lookup: sign-in refused"
        except SourceError as e:
            return None, f"certificate lookup failed ({sanitise(e)})"
        c = (data or {}).get("c")
        if not c:
            return None, "certificate lookup: no certificate with that ID"
        cm = {k: c.get(k) for k in media.get("cert_keys") or [] if c.get(k) not in (None, "", [], {})}
        return ({"certificate": cm} if cm else None), ("ok" if cm else "certificate has no 360 data")

    # ── one stone by its stock ID or certificate number (revert / recovery) ──
    def stone_viewer(self, stock_id=None, cert_number=None):
        """The stone's certificate ID and its own 360 viewer link (product_videos loupe360_url,
        type 360). Returns (info dict or None, reason, method). info: cert_id, viewer_url."""
        sch = self.schema()
        media = sch.get("media") or {}
        if not sch.get("verified"):
            return None, "schema not confirmed", ""
        sel = f"certificate {{ id certNumber {media.get('cert_sel') or ''} }}"
        tries = []
        lk = media.get("stone_lookup")
        if stock_id and lk and re.fullmatch(r"[A-Za-z0-9-]{4,80}", str(stock_id)) \
                and re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*!?", lk["type"]):
            body = sel if lk["returns"] == "diamond" else f"diamond {{ id {sel} }}"
            tries.append(("stock ID lookup", f"query ($v: {lk['type']}) {{ d: {lk['field']}({lk['arg']}: $v) {{ {body} }} }}",
                          {"v": str(stock_id)}, lambda d: [((d or {}).get("d") or {}).get("diamond", (d or {}).get("d"))]))
        cf = media.get("cert_filter")
        if cert_number and cf and re.fullmatch(r"[A-Za-z0-9-]{4,40}", str(cert_number)):
            q = ("query { d: diamonds_by_query(query: " + gql_literal({cf: [str(cert_number)]}) +
                 f", offset: 0, limit: 5) {{ items {{ diamond {{ id {sel} }} }} }} }}")
            tries.append(("certificate number search", q, None,
                          lambda d: [i.get("diamond") for i in (((d or {}).get("d") or {}).get("items") or [])]))
        if not tries:
            return None, ("no stock ID / certificate number on the stone, or the API has no lookup for them"), ""
        last = ""
        for method, q, variables, pick in tries:
            try:
                data = self._post(q, variables, token=self._token())
            except (SourceError, _AuthError) as e:
                last = f"{method} failed ({sanitise(e) or 'sign-in refused'})"
                continue
            for dia in pick(data):
                c = (dia or {}).get("certificate") or {}
                if cert_number and method.startswith("certificate") and str(c.get("certNumber")) != str(cert_number):
                    continue
                viewer = ""
                for v in (c.get("product_videos") or []):
                    if isinstance(v, dict) and str(v.get("type") or "").lower() in ("360", "v360") and v.get("loupe360_url"):
                        viewer = v["loupe360_url"]
                        break
                if c.get("id"):
                    return {"cert_id": str(c["id"]), "viewer_url": viewer}, "ok", method
            last = last or f"{method}: stone not found"
        return None, last, ""

    # ── search ───────────────────────────────────────────────────────────────
    def search(self, query_input, max_stones=500):
        """Fetches up to `max_stones` stones (cheapest first) for a server-side query dict,
        PAGE_LIMIT per request, until every match is fetched or the cap is reached.
        Returns (whitelisted_stones, real_total). real_total is the API's own count for the
        query (count query if the schema has one, else the result's total_count), or None
        if the API didn't give a trustworthy count."""
        sch = self.schema()
        extra = sch.get("cert_extra", [])
        lg = sch.get("lg_flag")
        cert_sel = " ".join(CERT_FIELDS + extra + ([lg[1]] if lg and lg[0] == "certificate" else []))
        dia_sel = "id video image availability" + (f" {lg[1]}" if lg and lg[0] == "diamond" else "")
        media = sch.get("media") or {}
        media_on = bool(media.get("sel") or media.get("cert_sel")) and not Client._media_off.get(self.url)
        plain = (dia_sel, cert_sel)
        if media_on:
            dia_sel += (" " + media["sel"]) if media.get("sel") else ""
            cert_sel += (" " + media["cert_sel"]) if media.get("cert_sel") else ""
        qlit = gql_literal(query_input)
        out, offset, fetched = [], 0, 0
        flags = {"natural": 0, "lab-grown": 0, "unknown": 0} if lg else None
        while offset < max_stones:
            limit = min(PAGE_LIMIT, max_stones - offset)
            count_sel = (f" n_total: {sch['count_query']}(query: {qlit})"
                         if offset == 0 and sch.get("count_query") else "")
            q = ("query { diamonds_by_query(query: " + qlit +
                 f", offset: {offset}, limit: {limit}, order: {{type: price, direction: ASC}}) {{ "
                 f"total_count items {{ id price diamond {{ {dia_sel} "
                 "certificate { " + cert_sel + " } } } }" + count_sel + " }")
            try:
                data = self.call(q)
            except SourceError as e:
                if not (media_on and offset == 0 and "rejected" in str(e)):
                    raise
                # The extra media fields were refused: search again exactly as before without them
                Client._media_off[self.url] = True
                media_on = False
                dia_sel, cert_sel = plain
                self._err("media fields refused; searched without them")
                continue
            res = data.get("diamonds_by_query") or {}
            if offset == 0:
                self.diag["total_count"] = res.get("total_count")
                if "n_total" in data:
                    self.diag["count_query_total"] = data.get("n_total")
            items = res.get("items") or []
            self.diag["pages"] += 1
            self.diag["raw_items"] += len(items)
            fetched += len(items)
            for i in items:
                if flags is not None and isinstance(i, dict):
                    d = i.get("diamond") or {}
                    v = (d if lg[0] == "diamond" else (d.get("certificate") or {})).get(lg[1])
                    flags["lab-grown" if v is True else "natural" if v is False else "unknown"] += 1
            out += [s for s in (whitelist(i, media.get("keys") if media_on else None,
                                          media.get("cert_keys") if media_on else None) for i in items) if s]
            offset += len(items)
            # total_count is not used to stop paging: only a short page (or the cap) ends it.
            if len(items) < limit:
                break
        self.diag["labgrown_flags"] = flags
        self.diag["media"] = media_report(out, media if media_on else {}, sch.get("verified"))
        self.diag["cap_hit"] = fetched >= max_stones
        real = _int(self.diag["count_query_total"])
        if real is None:
            tc = _int(self.diag["total_count"])
            # A total_count below what was actually fetched isn't the real total
            if tc is not None and tc >= fetched and not (self.diag["cap_hit"] and tc == fetched):
                real = tc
            elif not self.diag["cap_hit"]:
                real = fetched      # paged to the end: everything that matches was fetched
        self.diag["real_total"] = real
        return out, real


# Filters assumed from the published examples when the schema can't be read.
DOCUMENTED_FILTERS = {"labgrown": {"kind": "bool", "type": "Boolean (documented)"},
                      "shapes": {"kind": "list_str", "type": "[String] (documented)"},
                      "sizes": {"kind": "ranges", "from": "from", "to": "to", "int": False,
                                "type": "[{from, to}] (documented)"}}


def _named(t):
    while t and t.get("kind") in ("NON_NULL", "LIST"):
        t = t.get("ofType")
    return t or {}


def _is_list(t):
    while t and t.get("kind") == "NON_NULL":
        t = t.get("ofType")
    return bool(t) and t.get("kind") == "LIST"


def _type_str(t):
    if not t:
        return "?"
    if t.get("kind") == "NON_NULL":
        return _type_str(t.get("ofType")) + "!"
    if t.get("kind") == "LIST":
        return "[" + _type_str(t.get("ofType")) + "]"
    return t.get("name") or "?"


_NUM = ("Float", "Int", "BigInt", "Decimal", "Long")


def _range_spec(tt):
    """Input object with a from/to (or min/max) numeric pair -> spec, else None."""
    sub = {f["name"]: _named(f["type"]).get("name") for f in (tt.get("inputFields") or [])}
    for a, b in (("from", "to"), ("min", "max"), ("gte", "lte")):
        if sub.get(a) in _NUM and sub.get(b) in _NUM:
            return {"from": a, "to": b, "int": sub[a] == "Int" and sub[b] == "Int"}
    return None


def _filter_spec(t, types):
    """Describes a query input field in terms live_search can map criteria onto."""
    n = _named(t)
    kind, name = n.get("kind"), n.get("name")
    lst = _is_list(t)
    if kind == "SCALAR" and name == "Boolean" and not lst:
        return {"kind": "bool"}
    if kind == "SCALAR" and name == "String" and lst:
        return {"kind": "list_str"}
    if kind == "ENUM":
        vals = [e["name"] for e in (types.get(name, {}).get("enumValues") or [])]
        return {"kind": "list_enum" if lst else "enum", "values": vals}
    if kind == "INPUT_OBJECT":
        r = _range_spec(types.get(name, {}))
        if r:
            return {"kind": "ranges" if lst else "range", **r}
    return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def whitelist(item, media_keys=None, cert_media_keys=None):
    """Keep ONLY allowed fields. Anything else in the response is dropped here.
    `media_keys`: extra media fields (introspection-confirmed) kept under s["media"] for
    360 capture on save. They never reach a saved quote."""
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
    m = {k: d.get(k) for k in (media_keys or []) if d.get(k) not in (None, "", [], {})}
    cm = {k: c.get(k) for k in (cert_media_keys or []) if c.get(k) not in (None, "", [], {})}
    if cm:
        m["certificate"] = cm
    if m:
        s["media"] = m
    return s


# ── Certificate media: 360 frames (v360 / product_videos) and the still image ────
CERT_MEDIA_WANTED = {"image": None, "v360": ["top_index", "frame_count", "url"],
                     "product_videos": ["url", "type", "frame_count", "top_index", "loupe360_url"]}


def _cert_media_selection(ct, types):
    """Selection for the certificate's media fields, using only what the live schema has."""
    fields = {f["name"]: f for f in ct.get("fields") or [] if not _needs_args(f)}
    sel, desc = [], []
    for name, subs in CERT_MEDIA_WANTED.items():
        f = fields.get(name)
        if not f:
            continue
        n = _named(f["type"])
        if subs is None:
            if n.get("kind") in ("SCALAR", "ENUM"):
                sel.append(name)
                desc.append(f"certificate.{name}: {_type_str(f['type'])}")
            continue
        if n.get("kind") != "OBJECT":
            continue
        have = {g["name"]: g for g in (types.get(n.get("name"), {}).get("fields") or [])
                if _named(g["type"]).get("kind") in ("SCALAR", "ENUM") and not _needs_args(g)}
        use = [x for x in subs if x in have]
        if "url" in use and "frame_count" in use:
            sel.append(name + " { " + " ".join(use) + " }")
            desc.append(f"certificate.{name} ({_type_str(f['type'])}) {{ " +
                        ", ".join(f"{x}: {_type_str(have[x]['type'])}" for x in use) + " }")
    return " ".join(sel), desc


def _stone_lookup_field(root, diamond_type_name, item_type_name):
    """A root query that returns one stone by its ID (e.g. get_diamond_by_id(diamond_id: ID!)),
    returning the diamond type or the search item type. Returns {"field","arg","type","returns"}."""
    best = None
    for f in root.get("fields") or []:
        rt = _named(f["type"]).get("name")
        if _is_list(f["type"]) or rt not in (diamond_type_name, item_type_name):
            continue
        req = [a for a in f.get("args") or [] if (a.get("type") or {}).get("kind") == "NON_NULL"]
        if len(req) != 1 or _named(req[0]["type"]).get("name") not in ("ID", "String"):
            continue
        cand = {"field": f["name"], "arg": req[0]["name"], "type": _type_str(req[0]["type"]),
                "returns": "diamond" if rt == diamond_type_name else "item"}
        score = ("diamond" in f["name"].lower()) + ("id" in f["name"].lower()) + ("id" in req[0]["name"].lower())
        if best is None or score > best[0]:
            best = (score, cand)
    return best[1] if best else None


def _cert_lookup_field(root, cert_type_name):
    """A root query that returns one certificate by its ID (e.g. certificate_by_cert_id(cert_id: ID!)).
    Returns {"field", "arg", "type"} or None."""
    best = None
    for f in root.get("fields") or []:
        if _named(f["type"]).get("name") != cert_type_name or _is_list(f["type"]):
            continue
        req = [a for a in f.get("args") or [] if (a.get("type") or {}).get("kind") == "NON_NULL"]
        if len(req) != 1 or _named(req[0]["type"]).get("name") not in ("ID", "String"):
            continue
        cand = {"field": f["name"], "arg": req[0]["name"], "type": _type_str(req[0]["type"])}
        if f["name"] == "certificate_by_cert_id":
            return cand
        if best is None and "id" in req[0]["name"].lower():
            best = cand
    return best


# ── Media fields (360 frames, video files, extra images) ────────────────────────
_MEDIA_WORDS = ("video", "image", "img", "v360", "360", "frame", "mp4", "media", "gallery", "photo",
                "picture", "still", "spin", "asset", "view")


def _needs_args(f):
    return any((a.get("type") or {}).get("kind") == "NON_NULL" for a in (f.get("args") or []))


def _media_selection(dt, types):
    """Extra diamond fields that look like media, from the live schema. Returns
    (selection string, descriptions). Objects get their scalar sub-fields."""
    sel, desc = [], []
    for f in dt.get("fields") or []:
        name = f["name"]
        if name in ("certificate",) or _needs_args(f) or not any(w in name.lower() for w in _MEDIA_WORDS):
            continue
        n = _named(f["type"])
        if n.get("kind") in ("SCALAR", "ENUM"):
            desc.append(f"{name}: {_type_str(f['type'])}" + (f" — {f['description']}" if f.get("description") else ""))
            if name not in ("video", "image"):
                sel.append(name)
        elif n.get("kind") == "OBJECT":
            sub = [g for g in (types.get(n.get("name"), {}).get("fields") or [])
                   if _named(g["type"]).get("kind") in ("SCALAR", "ENUM") and not _needs_args(g)][:25]
            if sub:
                sel.append(name + " { " + " ".join(g["name"] for g in sub) + " }")
                desc.append(f"{name} ({_type_str(f['type'])}) {{ " + ", ".join(
                    f"{g['name']}: {_type_str(g['type'])}" for g in sub) + " }"
                    + (f" — {f['description']}" if f.get("description") else ""))
    return " ".join(sel), desc


def _sel_keys(sel):
    """Top-level field names of a selection string: 'a b { c d } e' -> ['a', 'b', 'e']."""
    keys, depth = [], 0
    for tok in re.findall(r"[{}]|[_A-Za-z][_0-9A-Za-z]*", sel or ""):
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth -= 1
        elif depth == 0:
            keys.append(tok)
    return keys


def _shape(v):
    """Value shape for diagnostics: hosts and long IDs masked."""
    if isinstance(v, str) and re.match(r"https?://", v):
        from spin_capture import mask
        return mask(v)
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_shape(x)}" for k, x in list(v.items())[:12]) + "}"
    if isinstance(v, list):
        return f"[{len(v)} item(s)" + (f", first {_shape(v[0])}" if v else "") + "]"
    return sanitise(repr(v))[:40]


def media_report(stones, media, verified):
    """What the stone data holds for media, for the diagnostics (sanitised)."""
    n = len(stones)
    lk = (media or {}).get("cert_lookup")
    rep = {"schema_verified": bool(verified), "stones": n,
           "fields": list((media or {}).get("desc") or []) + list((media or {}).get("cert_desc") or []),
           "cert_lookup": f"{lk['field']}({lk['arg']}: {lk['type']})" if lk else None}
    vids = [s.get("video") for s in stones if s.get("video")]
    files = [v for v in vids if re.search(r"\.(mp4|webm|mov)(\?|$)", v, re.I)]
    rep["video"] = {"set": len(vids), "files": len(files), "pages": len(vids) - len(files),
                    "example": _shape(vids[0]) if vids else None}
    seen = {}
    for s in stones:
        m = dict(s.get("media") or {})
        for k, v in (m.pop("certificate", None) or {}).items():
            m["certificate." + k] = v
        for k, v in m.items():
            e = seen.setdefault(k, {"count": 0, "example": _shape(v)})
            e["count"] += 1
    rep["extra"] = seen
    return rep
