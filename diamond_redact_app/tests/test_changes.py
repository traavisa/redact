"""This round: new client, spreadsheet upload removed, Live Search defaults / dimensions / New search,
timezone-aware timestamps, setting names."""
import base64
import datetime
import io
import json
import os
import re
import subprocess

import fitz
import pytest
from PIL import Image

import cert_attach as C
import fakecerts as F
import live_search as ls
import stone_source
from fakeapi import FakeAPI, schema, stone
from test_app import APP, PDFS, boot, env, texts  # noqa: F401  (env is a fixture)

HERE = os.path.dirname(APP)
ROOT = os.path.dirname(HERE)
SUPPLIER = re.compile(r"nivoda|example-supplier", re.I)


def src(name):
    with open(os.path.join(HERE, name)) as f:
        return f.read()


# ── Part B: the new client ────────────────────────────────────────────────────
def _app_logo_b64(name):
    m = re.search(r'^CLIENT_%s_B64 = "([A-Za-z0-9+/=]+)"' % name, src("app.py"), re.M)
    return m.group(1) if m else None


def test_new_client_is_set_up_like_the_others():
    a = src("app.py")
    assert '"Spence": CLIENT_Spence_B64,' in a                                     # CLIENT_LOGOS
    order = re.search(r"CLIENT_ORDER = \[(.*?)\]", a).group(1)
    assert '"Spence", "Pure Diamond"' in order and order.count('"Spence"') == 1     # CLIENT_ORDER
    b64 = _app_logo_b64("Spence")
    png = base64.b64decode(b64)
    assert Image.open(io.BytesIO(png)).format == "PNG"
    with open(os.path.join(ROOT, "logos", "Spence.png"), "rb") as f:               # logo file next to the other client logos
        assert f.read() == png
    assert not os.path.exists(os.path.join(ROOT, "new_logos"))
    q = open(os.path.join(ROOT, "quote.html")).read()                              # quote page (and, from it, the share page)
    m = re.search(r'^  "Spence": "data:image/png;base64,([A-Za-z0-9+/=]+)",$', q, re.M)
    assert m and m.group(1) == b64


def _node(js):
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_quote_page_has_the_logo_and_share_page_carries_only_it():
    js = f"""
    const fs = require('fs');
    const q = fs.readFileSync('{ROOT}/quote.html', 'utf8');
    const start = q.indexOf('const CLIENT_LOGOS = {{');
    const block = q.slice(start, q.indexOf('\\n}};', start) + 3);
    const CLIENT_LOGOS = eval('(' + block.replace('const CLIENT_LOGOS = ', '').replace(/;$/, '') + ')');
    const {{ handler }} = require('{ROOT}/netlify/functions/customer-page.js');
    handler({{ path: '/c/spence/abc123', headers: {{ host: 'quote.alldiamondeverything.com' }} }}).then(r => {{
      process.stdout.write(JSON.stringify({{ has: !!CLIENT_LOGOS['Spence'], fallback: 'Pure Carbon Group' in CLIENT_LOGOS,
        status: r.statusCode, body: r.body }}));
    }});
    """
    d = json.loads(_node(js))
    assert d["has"] and d["fallback"] and d["status"] == 200
    body = d["body"]
    assert 'const STORE_NAME = "Spence"' in body and "Spence — Diamond Selection" in body
    assert 'const STORE_LOGO = "data:image/png;base64,' + _app_logo_b64("Spence")[:40] in body
    assert "https://quote.alldiamondeverything.com/og/c/spence.jpg" in body       # link preview image (our domain, 1200x630 card)
    assert "Nash Jewellers" not in body and "Bijouterie" not in body                # no other client's name
    assert not SUPPLIER.search(re.sub(r"https?://\S+", "", body))


def test_new_client_in_both_pickers(env):
    at = boot()
    at.button(key="ovl_t2_client_Spence").click().run()
    assert not at.exception, at.exception
    assert at.session_state["q_client_sel"] == "Spence"
    assert "Quoting for **Spence**" in texts(at)                                    # Create quote shows the logo card
    # Live Search: the picker appears with the results
    at.radio(key="ls_mode").set_value("Look up stones").run()
    at.text_area(key="ls_lk_text").input("LG833689789").run()
    at.button(key="ls_lk_go").click().run()
    assert any(b.key == "ovl_ls_client_Spence" for b in at.button)
    at.button(key="ovl_ls_client_Spence").click().run()
    assert at.session_state["ls_client_sel"] == "Spence"


def test_certificate_qr_gets_the_new_clients_logo(env):
    sb, api = env
    at = boot()
    at.session_state["ls_client_sel"] = "Spence"
    at.radio(key="ls_mode").set_value("Look up stones").run()
    at.text_area(key="ls_lk_text").input("LG833689789").run()
    at.button(key="ls_lk_go").click().run()
    at.button(key="ls_add_quote").click().run()
    assert not at.exception, at.exception
    saved = sb.tables["quotes"][-1]
    assert saved["client"] == "Spence"
    pdf = next(iter(sb.storage["certificates"].values()))
    logo = Image.open(io.BytesIO(base64.b64decode(_app_logo_b64("Spence")))).convert("RGBA")
    assert C.verify(pdf, {"833689789"}, "IGI", logo) == []                          # QR area shows exactly Spence's logo
    assert "QR area" in C.verify(pdf, {"833689789"}, "IGI", F.logo())               # ...and not somebody else's


def test_create_quote_certificates_use_the_new_clients_logo():
    logo = Image.open(io.BytesIO(base64.b64decode(_app_logo_b64("Spence")))).convert("RGBA")
    for raw, sel, layout in ((F.igi(), "IGI", "IGI"), (F.gia(), "GIA", "GIA")):
        red, used = C.redact_for_quote(raw, sel, logo)
        assert used == layout
        d = fitz.open(stream=red, filetype="pdf")
        assert d[0].get_image_info()                                                # the logo is on the page


# ── Part D: spreadsheet upload removed ────────────────────────────────────────
def test_spreadsheet_upload_is_gone_and_create_quote_still_works(env):
    a = src("app.py")
    for word in ("bulk", "Bulk", "read_excel", "read_csv", ".xlsx", "spreadsheet", "N-Template", "BULK_COLS"):
        assert word not in a, word
    assert not os.path.exists(os.path.join(HERE, "templates"))
    assert "openpyxl" not in src("requirements.txt")
    at = boot()
    assert not at.exception, at.exception
    t = re.sub(r"<style>.*?</style>", "", texts(at), flags=re.S)
    assert "Bulk quote from spreadsheet" not in t and "template" not in t.lower() and "spreadsheet" not in t.lower()
    assert "Download Lab-grown template" not in t and ".xlsx" not in t
    # Create quote: certificate type, client, currency, expiry, 3 diamonds, generate button
    assert at.radio(key="q_cur").value == "CAD" and at.slider(key="q_exp").value == 15
    assert "Diamonds — up to 3" in t and "Diamond 3" in t
    assert "Upload a filled template or N-Template (.xlsx or .csv)" not in t
    gen = at.button(key="gen_quote")
    assert gen.disabled                                                              # nothing uploaded yet
    at.button(key="ovl_t2_c_GIA").click().run()
    assert at.session_state["cert_type"] == "GIA"
    at.button(key="ovl_t2_client_Nash Jewellers").click().run()
    assert at.session_state["q_client_sel"] == "Nash Jewellers" and not at.exception


# ── Part E: Live Search ───────────────────────────────────────────────────────
def test_defaults_when_the_tab_opens(env):
    at = boot()
    assert at.radio(key="ls_type").value == "Lab-grown"
    assert at.multiselect(key="ls_labs").value == ["IGI"]
    assert at.checkbox(key="ls_hide_novid").value is True and at.checkbox(key="ls_hide_noimg").value is True
    assert at.multiselect(key="ls_shapes").value == [] and at.number_input(key="ls_width_min").value is None
    # still changeable per search
    at.radio(key="ls_type").set_value("Natural").run()
    at.multiselect(key="ls_labs").set_value(["GIA"]).run()
    assert at.radio(key="ls_type").value == "Natural" and at.multiselect(key="ls_labs").value == ["GIA"]
    # and a fresh tab searches on those defaults
    sb, api = env
    at = boot()
    _search(at)
    q = [x for x in api.queries if "diamonds_by_query(query:" in x]
    assert q and "labgrown: true" in q[0]
    assert at.session_state["ls_results"]["crit"]["labs"] == ["IGI"] and at.session_state["ls_results"]["crit"]["lab_grown"]


LG = dict(lg=True)
DIM_STONES = [
    stone("d1", "LG700000001", "IGI", 100000, **LG, length=9.1, width=6.9, depth=4.2),     # too narrow
    stone("d2", "LG700000002", "IGI", 110000, **LG, length=9.2, width=7.2, depth=4.4),     # ok
    stone("d3", "LG700000003", "IGI", 120000, **LG, length=9.4, width=7.5, depth=4.9),     # too deep
    stone("d4", "LG700000004", "IGI", 130000, **LG, length=9.0, width=7.4, depth=4.5),     # ok
    stone("d5", "LG700000005", "IGI", 140000, **LG, length=10.6, width=7.0, depth=4.3),    # too long
]


def _search(at):
    at.button(key="ls_search").click().run()
    assert not at.exception, at.exception


def _kept(at):
    """Stones that reach 'Exact matches' (after the in-app checks)."""
    res = at.session_state["ls_results"]
    crit = res["crit"]
    fx = {"col": False, "cla": False, "ct": False, "budget": False, "lab": False, "flo": False, "ct_tol": 0.05}
    exact, flexed, _ = ls.bucket(res["stones"], crit, fx, 1.37, 20.0, 100.0)
    return sorted(x["sid"] for x in exact)


def test_dimensions_filtered_server_side_when_the_schema_supports_it(env):
    sb, api = env
    api.stones = DIM_STONES
    api.schema = schema(range_filters=("length", "width", "height"))
    stone_source.Client._schema_cache.clear()
    at = boot()
    at.number_input(key="ls_width_min").set_value(7.0)
    at.number_input(key="ls_width_max").set_value(7.6)
    at.number_input(key="ls_length_max").set_value(10.0)
    at.number_input(key="ls_height_max").set_value(4.6).run()
    _search(at)
    q = [x for x in api.queries if "diamonds_by_query(query:" in x]
    assert q and "width: {from: 7.0, to: 7.6}" in q[0] and "length: {from: 0.0, to: 10.0}" in q[0]
    assert "height: {from: 0.0, to: 4.6}" in q[0]
    plan = {p[0]: p[1] for p in at.session_state["ls_diag"]["plan"]}
    assert plan["Width"] == plan["Length"] == plan["Depth (mm)"] == "server"
    fetched = sorted(s["sid"] for s in at.session_state["ls_results"]["stones"])
    assert fetched == ["d2", "d4"]                                # the server already left out d1, d3, d5
    assert _kept(at) == ["d2", "d4"]


def test_dimensions_filtered_in_the_app_when_the_schema_cant(env):
    sb, api = env
    api.stones = DIM_STONES
    stone_source.Client._schema_cache.clear()
    at = boot()
    at.number_input(key="ls_width_min").set_value(7.0)
    at.number_input(key="ls_width_max").set_value(7.6)
    at.number_input(key="ls_length_max").set_value(10.0)
    at.number_input(key="ls_height_max").set_value(4.6).run()
    _search(at)
    plan = {p[0]: p[1] for p in at.session_state["ls_diag"]["plan"]}
    assert plan["Width"] == plan["Length"] == plan["Depth (mm)"] == "client"
    assert len(at.session_state["ls_results"]["stones"]) == 5     # all fetched, filtered here
    assert _kept(at) == ["d2", "d4"]
    removed = dict(at.session_state["ls_diag"]["client_side"]["removed"])
    assert removed == {"Width": 2, "Length": 1} or removed.get("Width") or removed.get("Depth (mm)")
    shown = texts(at)
    assert "LG700000003" not in shown and "LG700000001" not in shown       # excluded stones aren't listed
    # no flex: the dimension boxes have no flex option
    assert not any("dimension" in c.label.lower() for c in at.checkbox)


def test_stones_without_measurements_are_excluded_when_a_dimension_is_set():
    s = {"shape": "ROUND", "color": "G", "length": None, "width": None, "depth_mm": None}
    base = {"lab_grown": True, "shapes": [], "ct_min": None, "ct_max": None, "fancy": False, "col": ("D", "Z"),
            "fancy_col": "Yellow", "fancy_int": [], "cla": ("FL", "I3"), "cut": "Any", "pol": "Any", "sym": "Any",
            "flo": [], "labs": [], "pr_min": None, "pr_max": None, "pr_client": False, "ratio": (None, None),
            "depth": (None, None), "table": (None, None), "as_grown": False}
    fx = {"col": False, "cla": False, "ct": False, "budget": False, "lab": False, "flo": False, "ct_tol": 0.05}
    assert ls.classify(s, {**base}, fx, None) == (False, [])
    assert ls.classify(s, {**base, "width": (7.0, None)}, fx, None)[0] == "Width"
    ok = {**s, "length": 9.0, "width": 7.0, "depth_mm": 4.0}
    assert ls.classify(ok, {**base, "width": (7.0, None), "length": (None, 9.0), "height": (3.5, 4.5)}, fx, None) == (False, [])
    assert ls.classify(ok, {**base, "height": (4.1, None)}, fx, None)[0] == "Depth (mm)"


def _parse_result(**stated):
    names = [f[0] for f in ls.PARSE_FIELDS]
    out = {n: {"stated": False, "value": None, "interpreted": False, "note": ""} for n in names}
    for n, v in stated.items():
        interp = isinstance(v, tuple)
        val, note = v if interp else (v, "")
        out[n] = {"stated": True, "value": val, "interpreted": interp, "note": note}
    return out


def test_read_request_fills_dimensions_and_overrides_defaults(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    result = {}
    monkeypatch.setattr(ls, "ai_parse_request", lambda key, text, **kw: result["r"])
    at = boot()
    # "GIA natural, at least 7mm wide"
    result["r"] = _parse_result(type="natural", labs=["GIA"], width_min=7)
    at.text_area(key="ls_req").input("GIA natural, at least 7mm wide").run()
    at.button(key="ls_read").click().run()
    assert not at.exception, at.exception
    assert at.radio(key="ls_type").value == "Natural" and at.multiselect(key="ls_labs").value == ["GIA"]
    assert at.number_input(key="ls_width_min").value == 7.0 and at.number_input(key="ls_width_max").value is None
    # "around 9 x 7": interpreted, marked for confirmation
    result["r"] = _parse_result(length_min=(8.8, "9 x 7 mm ±0.2, confirm"), length_max=(9.2, "9 x 7 mm ±0.2, confirm"),
                                width_min=(6.8, "9 x 7 mm ±0.2, confirm"), width_max=(7.2, "9 x 7 mm ±0.2, confirm"),
                                height_max=4.5)
    at.text_area(key="ls_req").input("oval around 9 x 7").run()
    at.button(key="ls_read").click().run()
    assert not at.exception, at.exception
    assert (at.number_input(key="ls_length_min").value, at.number_input(key="ls_length_max").value) == (8.8, 9.2)
    assert (at.number_input(key="ls_width_min").value, at.number_input(key="ls_width_max").value) == (6.8, 7.2)
    assert at.number_input(key="ls_height_max").value == 4.5
    assert at.radio(key="ls_type").value == "Lab-grown" and at.multiselect(key="ls_labs").value == ["IGI"]   # defaults back
    assert set(at.session_state["ls_notes"]) == {"ls_length_min", "ls_length_max", "ls_width_min", "ls_width_max"}
    assert "confirm" in texts(at)
    # natural with no lab named: the IGI default doesn't stay
    result["r"] = _parse_result(type="natural")
    at.text_area(key="ls_req").input("natural round").run()
    at.button(key="ls_read").click().run()
    assert at.radio(key="ls_type").value == "Natural" and at.multiselect(key="ls_labs").value == ["GIA"]   # house rule: natural -> GIA
    # swapped or absurd values are handled
    result["r"] = _parse_result(width_min=8, width_max=6, length_min=0.1, height_min=500)
    at.text_area(key="ls_req").input("x").run()
    at.button(key="ls_read").click().run()
    assert (at.number_input(key="ls_width_min").value, at.number_input(key="ls_width_max").value) == (6.0, 8.0)
    assert at.number_input(key="ls_length_min").value is None and at.number_input(key="ls_height_min").value is None


def test_read_request_prompt_asks_for_sizes():
    fields = {f[0] for f in ls.PARSE_FIELDS}
    assert {"length_min", "length_max", "width_min", "width_max", "height_min", "height_max"} <= fields
    p = ls.PARSE_SYSTEM
    assert "at least 7mm wide" in p and "around 9 x 7" in p and "width_min=7" in p
    assert set(ls.PARSE_SCHEMA["properties"]) == fields and ls.PARSE_SCHEMA["required"] == []   # compact output


def _assert_fresh(at, markup=33.0, rate=1.5):
    ss = at.session_state
    assert ss["ls_req"] == "" and ss["ls_lk_text"] == ""
    for k, v in ls.DEFAULTS.items():
        got = ss[k]
        assert (tuple(got) if isinstance(v, tuple) else got) == v, k
    for k, v in ls.FLEX_DEFAULTS.items():
        assert ss[k] == v, k
    assert ss["ls_results"] is None and ss["ls_picks_ai"] is None and ss["ls_lookup"] is None
    assert ss["ls_lookup_diag"] is None and ss["ls_diag"] is None and ss["ls_last_link"] is None
    assert ss["ls_notes"] == {} and ss["ls_parse_msg"] is None and ss["ls_parse_err"] is None
    assert ss["ls_picked"] == set()
    assert ss["ls_markup"] == markup and ss["ls_rate"] == rate                    # kept
    assert not [k for k in list(ss) if str(k).startswith(("ls_pk_", "ls_tbl_"))]


def test_new_search_resets_the_criteria_results_picks_and_notes(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(ls, "ai_parse_request", lambda key, text, **kw: _parse_result(
        type="natural", labs=["GIA"], width_min=(7, "at least 7 wide, confirm")))
    monkeypatch.setattr(ls, "ai_top_picks", lambda key, crit, stones: {"picks": [
        {"ref": "E1", "reason": "best value"}]})
    sb, api = env
    api.stones = DIM_STONES
    at = boot()
    at.number_input(key="ls_markup").set_value(33.0)
    at.number_input(key="ls_rate").set_value(1.5).run()
    at.text_area(key="ls_req").input("GIA natural 7mm wide").run()
    at.button(key="ls_read").click().run()
    assert at.session_state["ls_notes"] and "Filled" in texts(at)
    # every kind of criterion changed
    at.radio(key="ls_type").set_value("Lab-grown")
    at.multiselect(key="ls_labs").set_value(["IGI", "GIA"])
    at.multiselect(key="ls_shapes").set_value(["Round"])
    at.number_input(key="ls_ct_min").set_value(0.5)
    at.checkbox(key="ls_fx_col").check()
    at.checkbox(key="ls_hide_noimg").uncheck()
    at.number_input(key="ls_length_min").set_value(8.0)
    at.number_input(key="ls_width_min").set_value(7.0)
    at.number_input(key="ls_width_max").set_value(7.3).run()
    at.select_slider(key=f"ls_cla_w{at.session_state['ls_ver']}").set_value(("VVS2", "SI1")).run()
    _search(at)
    assert at.session_state["ls_results"] and _kept(at) == ["d2", "d5"]
    at.button(key="ls_picks_btn").click().run()
    assert at.session_state["ls_picks_ai"] and "best value" in texts(at)
    box = [c for c in at.checkbox if str(c.key).startswith("ls_pk_")][0]
    box.check().run()
    assert at.session_state["ls_picked"]
    at.number_input(key="ls_markup").set_value(41.0).run()          # changed after the search: still kept
    btn = at.button(key="ls_new_search")
    assert btn.label == "New search"
    btn.click().run()
    assert not at.exception, at.exception
    _assert_fresh(at, markup=41.0)
    # what's on screen agrees
    assert at.radio(key="ls_type").value == "Lab-grown" and at.multiselect(key="ls_labs").value == ["IGI"]
    assert at.multiselect(key="ls_shapes").value == [] and at.number_input(key="ls_ct_min").value is None
    assert at.number_input(key="ls_length_min").value is None and at.number_input(key="ls_width_min").value is None
    assert at.number_input(key="ls_width_max").value is None
    assert at.checkbox(key="ls_fx_col").value is False and at.checkbox(key="ls_hide_noimg").value is True
    assert at.text_area(key="ls_req").value == ""
    assert at.select_slider(key=f"ls_cla_w{at.session_state['ls_ver']}").value == ("FL", "I3")
    assert at.number_input(key="ls_markup").value == 41.0 and at.number_input(key="ls_rate").value == 1.5
    t = texts(at)
    assert "Exact matches" not in t and "best value" not in t and "Filled" not in t
    assert not [b for b in at.button if str(b.key).startswith("ls_apply_")]
    _search(at)                                                       # and a search after it works from the defaults
    assert len(at.session_state["ls_results"]["stones"]) == 5


def test_new_search_clears_the_lookup_box_and_report(env):
    sb, api = env
    at = boot()
    at.number_input(key="ls_rate").set_value(1.4).run()
    at.radio(key="ls_mode").set_value("Look up stones").run()
    at.text_area(key="ls_lk_text").input("LG833689789\n2141438167").run()
    at.button(key="ls_lk_go").click().run()
    assert at.session_state["ls_lookup"] and "Found: 1" in texts(at) and at.session_state["ls_picked"]
    at.button(key="ls_new_search").click().run()
    assert not at.exception, at.exception
    _assert_fresh(at, markup=20.0, rate=1.4)
    assert at.radio(key="ls_mode").value == "Look up stones"          # the mode stays
    assert at.text_area(key="ls_lk_text").value == "" and "Found:" not in texts(at)
    assert "number(s) to look up" not in texts(at)


def test_new_search_button_is_in_both_modes(env):
    at = boot()
    assert any(b.key == "ls_new_search" for b in at.button)
    at.radio(key="ls_mode").set_value("Look up stones").run()
    assert any(b.key == "ls_new_search" for b in at.button)


# ── Part A: timestamps and setting names ──────────────────────────────────────
def test_no_naive_utc_calls_left():
    for f in ("app.py", "live_search.py", "spin_jobs.py", "quote_media.py", "cert_attach.py", "stone_source.py"):
        assert "utcnow" not in src(f), f


def test_timestamps_keep_the_stored_format(env):
    sb, api = env
    at = boot()
    at.radio(key="ls_mode").set_value("Look up stones").run()
    at.text_area(key="ls_lk_text").input("LG833689789").run()
    at.button(key="ls_lk_go").click().run()
    at.button(key="ls_add_quote").click().run()
    assert not at.exception, at.exception
    fmt = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,6})?Z$")           # what utcnow().isoformat() + "Z" gave
    exp = sb.tables["quotes"][-1]["expires_at"]
    hist = sb.tables["redact_history"][-1]["created_at"]
    assert fmt.match(exp) and fmt.match(hist)
    now = datetime.datetime.now(datetime.timezone.utc)
    h = datetime.datetime.fromisoformat(hist[:-1]).replace(tzinfo=datetime.timezone.utc)
    e = datetime.datetime.fromisoformat(exp[:-1]).replace(tzinfo=datetime.timezone.utc)
    assert abs((now - h).total_seconds()) < 60                                    # really UTC
    assert abs((e - now).total_seconds() - 15 * 86400) < 60                       # 15 days by default
    # the app's own readers still parse them, old rows too
    for s in (exp, hist, "2026-07-01T12:00:00.123456Z", "2026-07-01T12:00:00Z"):
        datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        datetime.datetime.fromisoformat(s.replace("Z", ""))


def test_setting_names_neutral_first_old_names_still_work():
    def getter(d):
        return lambda n: d.get(n, "")
    assert stone_source.setting(getter({"LS_API_URL": "new", "NIVODA_API_URL": "old"}), "url") == "new"
    assert stone_source.setting(getter({"NIVODA_API_URL": "old"}), "url") == "old"
    assert stone_source.setting(getter({"LS_USERNAME": "", "NIVODA_USERNAME": "old"}), "user") == "old"     # empty = unset
    assert stone_source.setting(getter({"LS_PRICE_DIVISOR": "100"}), "divisor") == "100"
    assert stone_source.SETTING_NAMES == {"url": ("LS_API_URL", "NIVODA_API_URL"),
                                          "user": ("LS_USERNAME", "NIVODA_USERNAME"),
                                          "password": ("LS_PASSWORD", "NIVODA_PASSWORD"),
                                          "divisor": ("LS_PRICE_DIVISOR", "NIVODA_PRICE_DIVISOR")}


# ── Follow-up 2: logos of clients added in the app, on quote / share pages and link previews ────────
def _custom_logo(color=(200, 30, 30), fmt="PNG"):
    b = io.BytesIO()
    Image.new("RGB", (60, 60), color).save(b, fmt)
    return base64.b64encode(b.getvalue()).decode()


ZED = _custom_logo()
CLIENTS_JS = """
const fs = require('fs');
const rows = %s;
global.fetch = async (url, opts) => {
  if (%s) throw new Error('database down');
  if (!String(url).includes('/rest/v1/custom_clients')) throw new Error('unexpected ' + url);
  if (!opts.headers.apikey) throw new Error('no key');
  return { ok: true, json: async () => rows };
};
process.env.SUPABASE_URL = 'https://db.test'; process.env.SUPABASE_SERVICE_KEY = 'k';
const logo = require('%s/netlify/functions/client-logo.js');
const page = require('%s/netlify/functions/customer-page.js');
const H = { host: 'quote.alldiamondeverything.com' };
(async () => {
  const out = {};
  for (const p of %s) {
    const r = await logo.handler({ path: '/.netlify/functions/client-logo/' + p });
    out[p] = { status: r.statusCode, type: (r.headers || {})['Content-Type'], b64: r.body, cache: (r.headers || {})['Cache-Control'] };
  }
  out.share = (await page.handler({ path: '/c/%s/abc', headers: H })).body;
  process.stdout.write(JSON.stringify(out));
})();
"""


def _clients_run(rows, down=False, slugs=("zed-and-co", "spence", "nope"), share="zed-and-co"):
    js = CLIENTS_JS % (json.dumps(rows), "true" if down else "false", ROOT, ROOT, json.dumps(list(slugs)), share)
    return json.loads(_node(js))


def _builtin_b64(name):
    return _app_logo_b64(name) if name == "Spence" else None


def test_client_logo_function_order_database_then_builtin_then_pure_carbon():
    d = _clients_run([{"name": "Zed & Co", "logo_b64": ZED}])
    z = d["zed-and-co"]
    assert z["status"] == 200 and z["type"] == "image/png" and z["b64"] == ZED and "max-age" in z["cache"]
    assert d["spence"]["b64"] == _app_logo_b64("Spence")                        # built-in
    q = open(os.path.join(ROOT, "quote.html")).read()
    pcg = re.search(r'"Pure Carbon Group": "data:image/png;base64,([A-Za-z0-9+/=]+)"', q).group(1)
    assert d["nope"]["status"] == 200 and d["nope"]["b64"] == pcg               # unknown: Pure Carbon
    # an in-app record with a built-in client's name never replaces the built-in logo
    d = _clients_run([{"name": "Spence", "logo_b64": ZED}])
    assert d["spence"]["b64"] == _app_logo_b64("Spence")
    # database down or table empty: built-in and Pure Carbon still work
    d = _clients_run([], down=True)
    assert d["spence"]["b64"] == _app_logo_b64("Spence") and d["zed-and-co"]["b64"] == pcg
    # a row that isn't an image is ignored
    d = _clients_run([{"name": "Zed & Co", "logo_b64": base64.b64encode(b"<svg onload=x>").decode()}])
    assert d["zed-and-co"]["b64"] == pcg


def test_share_page_and_link_preview_for_a_client_added_in_the_app():
    d = _clients_run([{"name": "Zed & Co", "logo_b64": ZED}, {"name": "Other <b>", "logo_b64": ZED}])
    body = d["share"]
    assert 'const STORE_NAME = "Zed & Co"' in body and "Zed &amp; Co — Diamond Selection" in body
    assert f'const STORE_LOGO = "data:image/png;base64,{ZED}"' in body
    assert "https://quote.alldiamondeverything.com/og/c/zed-and-co.jpg" in body
    assert "Other" not in body and "Nash Jewellers" not in body                  # only this client
    assert not SUPPLIER.search(re.sub(r"https?://\S+", "", body))
    # a built-in client's share page is unchanged apart from the preview image source
    d = _clients_run([], share="nash-jewellers")
    assert 'const STORE_NAME = "Nash Jewellers"' in d["share"]


def test_quote_page_reads_the_logo_from_our_domain_with_builtin_backup():
    q = open(os.path.join(ROOT, "quote.html")).read()
    assert "'/client-logo/' + clientSlug(s.client)" in q and "img.onerror" in q
    assert "CLIENT_LOGOS[s.client] || CLIENT_LOGOS['Pure Carbon Group']" in q    # backup if the request fails
    fn = re.search(r"function clientSlug\(name\) \{.*?\n\}", q, re.S).group(0)
    js = fn + """
    const { slugOf } = require('%s/netlify/functions/lib/clients');
    const bad = ["Zed & Co", "Janina's", "Foe & Dear", "Barclay\u2019s", "  A  B ", "Touch of Gold", "", "Nash Jewellers"]
      .filter((n) => (clientSlug(n) || 'pure-carbon-group') !== (slugOf(n) || 'pure-carbon-group'));
    process.stdout.write(JSON.stringify(bad));
    """ % ROOT
    assert json.loads(_node(js)) == []                                            # page and server agree on every slug
    assert "quote_system" not in open(os.path.join(ROOT, "netlify.toml")).read()
    assert '"/client-logo/*"' in open(os.path.join(ROOT, "netlify.toml")).read()


def test_old_copy_is_deleted():
    assert not os.path.exists(os.path.join(HERE, "quote_system"))


def test_client_added_in_the_app_gets_its_logo_on_certificates_and_order_matches_the_pages(env):
    import streamlit as st
    sb, api = env
    st.cache_data.clear()                                                       # the app caches the client list for 5 minutes
    sb.tables["custom_clients"].append({"name": "Zed & Co", "logo_b64": ZED})
    for who in ("Zed & Co",):
        sb.storage["certificates"].clear()
        at = boot()
        at.session_state["ls_client_sel"] = who
        at.radio(key="ls_mode").set_value("Look up stones").run()
        at.text_area(key="ls_lk_text").input("LG833689789").run()
        at.button(key="ls_lk_go").click().run()
        at.button(key="ls_add_quote").click().run()
        assert not at.exception, at.exception
        pdf = next(iter(sb.storage["certificates"].values()))
        logo = Image.open(io.BytesIO(base64.b64decode(ZED))).convert("RGBA")
        assert C.verify(pdf, {"833689789"}, "IGI", logo) == [], who
    at = boot()
    assert any(b.key == "ovl_t2_client_Zed & Co" for b in at.button)             # in the picker too
