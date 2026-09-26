"""A fake stone-search GraphQL API: introspection, sign-in, search with filters, count query.
Patched in as stone_source.Client._post, so the real client code runs end to end."""
import json
import re

HOST = "https://cdn.example-supplier.test"


def _scalar(name):
    return {"kind": "SCALAR", "name": name, "ofType": None}


def _list(t):
    return {"kind": "LIST", "name": None, "ofType": t}


def _nn(t):
    return {"kind": "NON_NULL", "name": None, "ofType": t}


def _obj(name):
    return {"kind": "OBJECT", "name": name, "ofType": None}


def _f(name, t, args=()):
    return {"name": name, "description": None, "args": list(args), "type": t}


def _in(name, t):
    return {"name": name, "description": None, "type": t}


def schema(media_flags=("has_image", "has_v360", "has_video"), stock_filter=True, cert_pdf=True):
    s = _scalar("String")
    cert_fields = [_f(n, s) for n in ("id", "lab", "shape", "certNumber", "cut", "clarity", "polish", "symmetry",
                                       "color", "floInt", "floCol")]
    cert_fields += [_f(n, _scalar("Float")) for n in ("carats", "width", "length", "depth", "depthPercentage", "table")]
    if cert_pdf:
        cert_fields.append(_f("pdfUrl", s))
    dia_fields = [_f("id", _scalar("ID")), _f("video", s), _f("image", s), _f("availability", s),
                  _f("supplierStockId", s), _f("certificate", _obj("Certificate"))]
    item_fields = [_f("id", _scalar("ID")), _f("price", _scalar("Int")), _f("diamond", _obj("Diamond"))]
    res_fields = [_f("total_count", _scalar("Int")), _f("items", _list(_obj("Item")))]
    q_in = [_in("labgrown", _scalar("Boolean")), _in("shapes", _list(s)),
            _in("certificate_numbers", _list(s))]
    if stock_filter:
        q_in.append(_in("supplier_stock_ids", _list(s)))
    q_in += [_in(f, _scalar("Boolean")) for f in media_flags]
    qarg = {"name": "query", "type": {"kind": "INPUT_OBJECT", "name": "DiamondQuery", "ofType": None}}
    root = [_f("diamonds_by_query", _obj("Result"), [qarg, {"name": "offset", "type": _scalar("Int")},
                                                     {"name": "limit", "type": _scalar("Int")}]),
            _f("diamonds_by_query_count", _scalar("Int"), [qarg])]
    types = [{"kind": "OBJECT", "name": "Query", "fields": root},
             {"kind": "OBJECT", "name": "Result", "fields": res_fields},
             {"kind": "OBJECT", "name": "Item", "fields": item_fields},
             {"kind": "OBJECT", "name": "Diamond", "fields": dia_fields},
             {"kind": "OBJECT", "name": "Certificate", "fields": cert_fields},
             {"kind": "INPUT_OBJECT", "name": "DiamondQuery", "inputFields": q_in},
             {"kind": "SCALAR", "name": "String"}, {"kind": "SCALAR", "name": "Boolean"}]
    return {"__schema": {"queryType": {"name": "Query"}, "types": types}}


def stone(sid, cert, lab="IGI", price=100000, lg=True, stock=None, video=True, image=True, pdf=True,
          availability="AVAILABLE", carat=1.01, color="G", clarity="VS1", shape="ROUND"):
    return {"id": f"item-{sid}", "price": price, "_lg": lg,
            "diamond": {"id": sid, "availability": availability,
                        "video": f"{HOST}/v360/{sid}/view.html" if video else None,
                        "image": f"{HOST}/img/{sid}.jpg" if image else None,
                        "supplierStockId": stock or f"STK{sid}",
                        "certificate": {"id": f"c-{sid}", "lab": lab, "certNumber": cert, "shape": shape,
                                        "carats": carat, "color": color, "clarity": clarity, "cut": "EX",
                                        "polish": "EX", "symmetry": "EX", "floInt": "NON", "floCol": None,
                                        "length": 6.5, "width": 6.48, "depth": 4.0, "depthPercentage": 61.5,
                                        "table": 57.0,
                                        "pdfUrl": f"{HOST}/certs/{cert}.pdf" if pdf else None}}}


class FakeAPI:
    def __init__(self, stones, **schema_kw):
        self.stones = stones
        self.schema = schema(**schema_kw)
        self.queries = []
        self.fail_on = None          # substring of a query that makes the API error

    @staticmethod
    def _literal(q, start):
        i = q.index(start) + len(start)
        depth = 0
        for j in range(i, len(q)):
            if q[j] == "{":
                depth += 1
            elif q[j] == "}":
                depth -= 1
                if depth == 0:
                    lit = q[i:j + 1]
                    break
        lit = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', lit)
        return json.loads(lit)

    def match(self, qi):
        out = []
        for s in self.stones:
            d, c = s["diamond"], s["diamond"]["certificate"]
            if "labgrown" in qi and qi["labgrown"] != s["_lg"]:
                continue
            if "certificate_numbers" in qi and c["certNumber"] not in qi["certificate_numbers"]:
                continue
            if "supplier_stock_ids" in qi and d["supplierStockId"] not in qi["supplier_stock_ids"]:
                continue
            if qi.get("has_image") and not d["image"]:
                continue
            if qi.get("has_video") and not d["video"]:
                continue
            if qi.get("has_v360") and not (d["video"] and "v360" in d["video"]):
                continue
            out.append(s)
        return sorted(out, key=lambda s: s["price"])

    def post(self, client, query, variables=None, token=None):
        self.queries.append(query)
        if "__schema" in query:
            return self.schema
        if "authenticate" in query:
            return {"authenticate": {"username_and_password": {"token": "tok"}}}
        if self.fail_on and self.fail_on in query:
            from stone_source import SourceError
            raise SourceError("The stone search rejected the request. Try simpler criteria.")
        data = {}
        if "diamonds_by_query(query:" in query:
            qi = self._literal(query, "diamonds_by_query(query: ")
            m = re.search(r"offset: (\d+), limit: (\d+)", query)
            off, lim = int(m.group(1)), int(m.group(2))
            rows = self.match(qi)
            data["diamonds_by_query"] = {"total_count": len(rows),
                                         "items": [{k: v for k, v in s.items() if k != "_lg"} for s in rows[off:off + lim]]}
        if "diamonds_by_query_count(query:" in query:
            qi = self._literal(query, "diamonds_by_query_count(query: ")
            key = "n_total" if "n_total:" in query else "n"
            data[key] = len(self.match(qi))
        return data
