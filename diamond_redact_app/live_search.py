"""Live Search tab: find stones from the live stone source, compare, and approve into a quote.

Rules this module follows:
  - The source is never named in the UI, error text or saved data ("Live Search" only).
  - Stones are whitelisted in nivoda_client.whitelist(); supplier fields are never requested.
  - Search filters come only from the form, built by deterministic code. The AI can
    pre-fill the form ("Read request") but never runs or changes a search.
  - Saved quotes never carry the source's media URLs: save_quote() copies each image/video
    to our own /media/ address or swaps it for an opaque /v/ viewer link (quote_media.py).
"""
import datetime
import html
import json
import re
import traceback

import streamlit as st

import nivoda_client as src

AI_MODEL = "claude-sonnet-5"
MAX_SCAN = 500              # stones fetched per search (cheapest first), 10 pages of 50

# ── Vocabulary ────────────────────────────────────────────────────────────────
TYPES = ["Natural", "Lab-grown"]
SHAPE_GROUPS = {   # UI shape -> accepted shape names (from the source's documented list)
    "Round": ["ROUND"],
    "Oval": ["OVAL", "OVAL MIXED CUT"],
    "Cushion": ["CUSHION", "CUSHION MODIFIED", "CUSHION BRILLIANT", "CUSHION B"],
    "Emerald": ["EMERALD", "SQUARE EMERALD"],
    "Pear": ["PEAR", "PEAR MODIFIED BRILLIANT"],
    "Princess": ["PRINCESS"],
    "Radiant": ["RADIANT", "SQUARE RADIANT"],
    "Marquise": ["MARQUISE"],
    "Asscher": ["ASSCHER", "ASCHER"],
    "Heart": ["HEART"],
    "Baguette": ["BAGUETTE", "TAPERED BAGUETTE"],
    "Trilliant": ["TRILLIANT", "TRIANGULAR"],
    "Old European": ["OLD EUROPEAN", "EUROPEAN", "EUROPEAN CUT"],
    "Old Miner": ["OLD MINER"],
}
SHAPES = list(SHAPE_GROUPS)
COLOURS = list("DEFGHIJKLMNOPQRSTUVWXYZ")
CLARITIES = ["FL", "IF", "VVS1", "VVS2", "VS1", "VS2", "SI1", "SI2", "SI3", "I1", "I2", "I3"]
MIN_GRADES = ["Any", "Excellent", "Very Good", "Good", "Fair"]
GRADE_RANK = {"Excellent": 4, "Very Good": 3, "Good": 2, "Fair": 1, "Poor": 0}
FLUORS = ["None", "Faint", "Medium", "Strong", "Very Strong"]
LABS = ["GIA", "IGI", "HRD", "GCAL", "AGS"]
FANCY_COLOURS = ["Yellow", "Pink", "Blue", "Green", "Orange", "Brown", "Purple", "Red",
                 "Grey", "Black", "Violet", "Chameleon"]
INTENSITIES = ["Faint", "Very Light", "Light", "Fancy Light", "Fancy", "Fancy Intense",
               "Fancy Vivid", "Fancy Deep", "Fancy Dark"]
SORTS = ["Price (low → high)", "Price (high → low)", "Carat (high → low)",
         "Price per carat (low → high)", "Closest to target carat"]

_GRADE_ALIASES = {"EX": "Excellent", "EXC": "Excellent", "EXCELLENT": "Excellent", "ID": "Excellent",
                  "IDEAL": "Excellent", "VG": "Very Good", "VERYGOOD": "Very Good", "G": "Good",
                  "GD": "Good", "GOOD": "Good", "F": "Fair", "FR": "Fair", "FAIR": "Fair",
                  "P": "Poor", "PR": "Poor", "POOR": "Poor"}
_FLUOR_ALIASES = {"N": "None", "NON": "None", "NONE": "None", "NIL": "None",
                  "F": "Faint", "FNT": "Faint", "FAINT": "Faint", "SL": "Faint", "SLIGHT": "Faint",
                  "VSL": "Faint", "VERYSLIGHT": "Faint",
                  "M": "Medium", "MED": "Medium", "MEDIUM": "Medium",
                  "S": "Strong", "ST": "Strong", "STG": "Strong", "STRONG": "Strong",
                  "VS": "Very Strong", "VST": "Very Strong", "VSTG": "Very Strong", "VERYSTRONG": "Very Strong"}


def _key(s):
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def norm_grade(v):
    return _GRADE_ALIASES.get(_key(v), "") if v else ""


def norm_fluor(v):
    return _FLUOR_ALIASES.get(_key(v), "") if v else ""


def norm_clarity(v):
    k = _key(v)
    return k if k in CLARITIES else ""


def norm_colour(v):
    k = str(v or "").strip().upper()
    return k if k in COLOURS else ""


def norm_lab(v):
    return str(v or "").strip().upper()


def shape_group(v):
    k = str(v or "").strip().upper().replace("_", " ")
    for g, names in SHAPE_GROUPS.items():
        if k in names:
            return g
    return str(v or "").title()


def fancy_text(s):
    parts = [s.get("f_intensity"), s.get("f_overtone"), s.get("f_color")]
    txt = " ".join(str(p) for p in parts if p)
    if not txt and not norm_colour(s.get("color")):
        txt = s.get("color") or ""
    return re.sub(r"\s+", " ", txt.replace("_", " ")).strip().title()


def fancy_intensity(txt):
    t = f" {txt.upper()} "
    hits = [i for i in INTENSITIES if f" {i.upper()} " in t]
    return max(hits, key=len) if hits else ""


def as_grown_status(s):
    """True / False / None (unknown)."""
    for k in ("as_grown", "asGrown"):
        if isinstance(s.get(k), bool):
            return s[k]
    if isinstance(s.get("treated"), bool):
        return not s["treated"]
    t = s.get("treatment")
    if isinstance(t, str):
        return _key(t) in ("", "NONE", "ASGROWN", "NOTREATMENT", "UNTREATED")
    return None


# ── Settings / state ──────────────────────────────────────────────────────────
def _f(v, default=None):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


DEFAULTS = {
    "ls_type": "Natural", "ls_shapes": [], "ls_ct_min": None, "ls_ct_max": None,
    "ls_col_mode": "White (D–Z)", "ls_col": ("D", "Z"), "ls_fancy_col": "Yellow", "ls_fancy_int": [],
    "ls_cla": ("FL", "I3"), "ls_cut": "Any", "ls_pol": "Any", "ls_sym": "Any",
    "ls_flo": [], "ls_labs": [], "ls_pr_min": None, "ls_pr_max": None, "ls_pr_basis": "My cost",
    "ls_ratio_min": None, "ls_ratio_max": None, "ls_depth_min": None, "ls_depth_max": None,
    "ls_table_min": None, "ls_table_max": None, "ls_as_grown": False,
}
FLEX_DEFAULTS = {"ls_fx_col": False, "ls_fx_cla": False, "ls_fx_ct": False, "ls_fx_ct_tol": "±0.05",
                 "ls_fx_budget": False, "ls_fx_lab": False, "ls_fx_flo": False}


def _init_state(default_markup, default_rate):
    ss = st.session_state
    for k, v in {**DEFAULTS, **FLEX_DEFAULTS}.items():
        if k not in ss:
            ss[k] = v
    ss.setdefault("ls_markup", float(default_markup))
    ss.setdefault("ls_rate", float(default_rate))
    ss.setdefault("ls_notes", {})
    ss.setdefault("ls_picked", set())


# ── AI helpers (Anthropic) ────────────────────────────────────────────────────
class AIError(Exception):
    pass


def _ai_call(api_key, system, user, schema, max_tokens=4000):
    """One Messages call returning parsed JSON. Structured output first; falls back to a
    plain JSON instruction if the schema is refused. Raises AIError with a friendly message."""
    try:
        import anthropic
    except ImportError:
        raise AIError("The AI library isn't installed on the server (anthropic).")
    client = anthropic.Anthropic(api_key=api_key, timeout=90.0, max_retries=2)
    msgs = [{"role": "user", "content": user}]

    def run(sys_text, **extra):
        r = client.messages.create(model=AI_MODEL, max_tokens=max_tokens, system=sys_text,
                                   messages=msgs, **extra)
        if r.stop_reason == "refusal":
            raise AIError("The assistant declined this request.")
        if r.stop_reason == "max_tokens":
            raise AIError("The assistant's answer was cut off. Try a shorter request.")
        text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise AIError("The assistant didn't return a usable answer. Try again.")
        return json.loads(m.group(0))

    try:
        try:
            return run(system, output_config={"effort": "low",
                       "format": {"type": "json_schema", "schema": schema}})
        except anthropic.BadRequestError:
            # Schema not accepted: ask for the same JSON in plain instructions instead.
            return run(system + "\n\nReply with ONLY a JSON object matching this JSON schema:\n"
                       + json.dumps(schema))
    except AIError:
        raise
    except anthropic.AuthenticationError:
        raise AIError("The AI key (ANTHROPIC_API_KEY) was rejected.")
    except anthropic.RateLimitError:
        raise AIError("The assistant is busy (rate limited). Wait a moment and try again.")
    except (anthropic.APITimeoutError, anthropic.APIConnectionError):
        raise AIError("Couldn't reach the assistant. Try again.")
    except anthropic.APIStatusError:
        raise AIError("The assistant had a problem answering. Try again.")
    except ValueError:
        raise AIError("The assistant's answer couldn't be read. Try again.")


def _field_schema(kind, opts=None):
    if kind == "enum":
        v = {"type": "string", "enum": opts}
    elif kind == "enums":
        v = {"type": "array", "items": {"type": "string", "enum": opts}}
    elif kind == "number":
        v = {"type": "number"}
    else:
        v = {"type": "boolean"}
    return {"type": "object", "additionalProperties": False,
            "required": ["stated", "value", "interpreted", "note"],
            "properties": {"stated": {"type": "boolean"}, "value": v,
                           "interpreted": {"type": "boolean"}, "note": {"type": "string"}}}


PARSE_FIELDS = [   # (name, kind, options, description)
    ("type", "enum", ["natural", "lab-grown"], "natural or lab-grown"),
    ("shapes", "enums", SHAPES, "shape(s) asked for"),
    ("carat_min", "number", None, "minimum carat"),
    ("carat_max", "number", None, "maximum carat"),
    ("colour_mode", "enum", ["white", "fancy"], "white D–Z grades, or a fancy colour"),
    ("colour_best", "enum", COLOURS, "best (highest) acceptable white colour grade, e.g. D"),
    ("colour_worst", "enum", COLOURS, "lowest acceptable white colour grade, e.g. H"),
    ("fancy_colour", "enum", FANCY_COLOURS, "fancy colour hue"),
    ("fancy_intensity", "enums", INTENSITIES, "acceptable fancy intensities"),
    ("clarity_best", "enum", CLARITIES, "best acceptable clarity"),
    ("clarity_worst", "enum", CLARITIES, "lowest acceptable clarity"),
    ("cut_min", "enum", MIN_GRADES[1:], "minimum cut grade"),
    ("polish_min", "enum", MIN_GRADES[1:], "minimum polish"),
    ("symmetry_min", "enum", MIN_GRADES[1:], "minimum symmetry"),
    ("fluorescence", "enums", FLUORS, "allowed fluorescence levels"),
    ("labs", "enums", LABS, "acceptable grading labs"),
    ("price_min", "number", None, "minimum price in CAD"),
    ("price_max", "number", None, "maximum price / budget in CAD"),
    ("price_basis", "enum", ["cost", "client"], "whether the price is the dealer's cost or the client's price"),
    ("ratio_min", "number", None, "minimum length/width ratio"),
    ("ratio_max", "number", None, "maximum length/width ratio"),
    ("depth_min", "number", None, "minimum depth %"),
    ("depth_max", "number", None, "maximum depth %"),
    ("table_min", "number", None, "minimum table %"),
    ("table_max", "number", None, "maximum table %"),
    ("as_grown_only", "bool", None, "lab-grown only: as-grown (untreated) stones only"),
]
PARSE_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": [f[0] for f in PARSE_FIELDS],
                "properties": {f[0]: _field_schema(f[1], f[2]) for f in PARSE_FIELDS}}
PARSE_SYSTEM = (
    "You read a jeweller's diamond request and map it onto a fixed search form for a diamond "
    "wholesaler in Canada. For EVERY form field return an object {stated, value, interpreted, note}.\n"
    "- stated=false when the request does not state it and it cannot be reasonably inferred. "
    "Then value is a placeholder that will be ignored, interpreted=false, note=\"\". Never invent values.\n"
    "- stated=true and interpreted=false when the request gives the value directly (e.g. 'GIA', 'oval', "
    "'G colour', 'under $8k').\n"
    "- stated=true and interpreted=true when you had to turn vague wording into a value "
    "(e.g. '1.5ish' -> carat 1.40–1.60, 'near colourless' -> G–J, 'eye clean' -> SI1 or better, "
    "'no fluoro' -> None). The note must say what you assumed and end with ', confirm' "
    "(e.g. '1.5ish → 1.40–1.60, confirm'). Keep notes under 80 characters.\n"
    "- Single values: 'G colour' means colour_best=G and colour_worst=G; 'G or better' means "
    "colour_best=D, colour_worst=G. Same pattern for clarity.\n"
    "- Prices are in Canadian dollars. A budget from a jeweller for their customer is usually the "
    "client price; only use 'cost' if the request says it's the dealer's cost. If unclear, "
    "leave price_basis unstated.\n"
    "- Output JSON only.")


def ai_parse_request(api_key, text):
    return _ai_call(api_key, PARSE_SYSTEM, "Client request:\n\n" + text, PARSE_SCHEMA)


PICKS_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["picks"],
                "properties": {"picks": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["ref", "reason"],
                    "properties": {"ref": {"type": "string"}, "reason": {"type": "string"}}}}}}
PICKS_SYSTEM = (
    "You help a diamond wholesaler choose stones to offer a jeweller. You are given the search "
    "criteria and a list of stones, each with a ref. Choose the best 3 to 5 stones FROM THIS LIST ONLY "
    "(fewer if the list is shorter) and give one short reason each (max 20 words) — value for money, "
    "proportions, closeness to the brief. Stones in section 'outside' do NOT fully meet the criteria: "
    "if you pick one, start its reason with 'Outside criteria:' and say how it differs. "
    "Never invent stones or refs. Output JSON only.")


def ai_top_picks(api_key, criteria_text, stones):
    payload = json.dumps(stones, separators=(",", ":"))
    return _ai_call(api_key, PICKS_SYSTEM,
                    f"Search criteria: {criteria_text}\n\nStones:\n{payload}", PICKS_SCHEMA, 3000)


# ── Parse result -> form ──────────────────────────────────────────────────────
def apply_parse(result):
    """Validated AI output -> form widget state. Unknown/invalid values are ignored.
    Returns (n_filled, notes). Never triggers a search."""
    ss = st.session_state
    for k, v in DEFAULTS.items():                 # a new request starts from a clean form
        ss[k] = list(v) if isinstance(v, list) else v
    notes, filled = {}, 0

    def get(name):
        f = result.get(name) if isinstance(result, dict) else None
        if not isinstance(f, dict) or not f.get("stated") or f.get("value") is None:
            return None, False, ""
        return f["value"], bool(f.get("interpreted")), str(f.get("note") or "")[:160]

    def setk(key, value, interp, note):
        nonlocal filled
        ss[key] = value
        filled += 1
        if interp:
            notes[key] = note or "Interpreted from the request, confirm"

    def num(name, key, lo=0.0, hi=1e9):
        v, i, n = get(name)
        v = _f(v)
        if v is not None and lo <= v <= hi:
            setk(key, round(v, 2), i, n)

    v, i, n = get("type")
    if v in ("natural", "lab-grown"):
        setk("ls_type", "Lab-grown" if v == "lab-grown" else "Natural", i, n)
    v, i, n = get("shapes")
    if isinstance(v, list) and [x for x in v if x in SHAPES]:
        setk("ls_shapes", [x for x in SHAPES if x in v], i, n)
    num("carat_min", "ls_ct_min", 0.01, 100)
    num("carat_max", "ls_ct_max", 0.01, 100)

    mode, mi, mn = get("colour_mode")
    if mode == "fancy":
        setk("ls_col_mode", "Fancy colour", mi, mn)
        v, i, n = get("fancy_colour")
        if v in FANCY_COLOURS:
            setk("ls_fancy_col", v, i, n)
        v, i, n = get("fancy_intensity")
        if isinstance(v, list) and [x for x in v if x in INTENSITIES]:
            setk("ls_fancy_int", [x for x in INTENSITIES if x in v], i, n)
    else:
        b, bi, bn = get("colour_best")
        w, wi, wn = get("colour_worst")
        if b in COLOURS or w in COLOURS:
            b = b if b in COLOURS else "D"
            w = w if w in COLOURS else "Z"
            if COLOURS.index(b) > COLOURS.index(w):
                b, w = w, b
            setk("ls_col", (b, w), bi or wi, bn or wn)

    b, bi, bn = get("clarity_best")
    w, wi, wn = get("clarity_worst")
    if b in CLARITIES or w in CLARITIES:
        b = b if b in CLARITIES else "FL"
        w = w if w in CLARITIES else "I3"
        if CLARITIES.index(b) > CLARITIES.index(w):
            b, w = w, b
        setk("ls_cla", (b, w), bi or wi, bn or wn)

    for name, key in (("cut_min", "ls_cut"), ("polish_min", "ls_pol"), ("symmetry_min", "ls_sym")):
        v, i, n = get(name)
        if v in MIN_GRADES[1:]:
            setk(key, v, i, n)
    for name, key, opts in (("fluorescence", "ls_flo", FLUORS), ("labs", "ls_labs", LABS)):
        v, i, n = get(name)
        if isinstance(v, list) and [x for x in v if x in opts]:
            setk(key, [x for x in opts if x in v], i, n)
    num("price_min", "ls_pr_min", 0, 1e8)
    num("price_max", "ls_pr_max", 0, 1e8)
    v, i, n = get("price_basis")
    if v in ("cost", "client"):
        setk("ls_pr_basis", "Client price" if v == "client" else "My cost", i, n)
    num("ratio_min", "ls_ratio_min", 0.5, 5)
    num("ratio_max", "ls_ratio_max", 0.5, 5)
    num("depth_min", "ls_depth_min", 0, 100)
    num("depth_max", "ls_depth_max", 0, 100)
    num("table_min", "ls_table_min", 0, 100)
    num("table_max", "ls_table_max", 0, 100)
    v, i, n = get("as_grown_only")
    if v is True:
        setk("ls_as_grown", True, i, n)
    ss["ls_notes"] = notes
    ss["ls_ver"] = ss.get("ls_ver", 0) + 1       # re-create the range sliders with the new values
    return filled, notes


# ── Criteria (from the form only) ─────────────────────────────────────────────
def _range(v, full):
    v = tuple(v) if isinstance(v, (list, tuple)) else (v, v)
    if len(v) == 1:
        v = (v[0], v[0])
    return v if all(x in full for x in v) else (full[0], full[-1])


def read_criteria():
    ss = st.session_state
    return {
        "lab_grown": ss.ls_type == "Lab-grown",
        "shapes": list(ss.ls_shapes),
        "ct_min": ss.ls_ct_min, "ct_max": ss.ls_ct_max,
        "fancy": ss.ls_col_mode == "Fancy colour",
        "col": _range(ss.ls_col, COLOURS), "fancy_col": ss.ls_fancy_col, "fancy_int": list(ss.ls_fancy_int),
        "cla": _range(ss.ls_cla, CLARITIES),
        "cut": ss.ls_cut, "pol": ss.ls_pol, "sym": ss.ls_sym,
        "flo": list(ss.ls_flo), "labs": list(ss.ls_labs),
        "pr_min": ss.ls_pr_min, "pr_max": ss.ls_pr_max, "pr_client": ss.ls_pr_basis == "Client price",
        "ratio": (ss.ls_ratio_min, ss.ls_ratio_max), "depth": (ss.ls_depth_min, ss.ls_depth_max),
        "table": (ss.ls_table_min, ss.ls_table_max),
        "as_grown": bool(ss.ls_as_grown) and ss.ls_type == "Lab-grown",
    }


def flex_state():
    ss = st.session_state
    return {"col": ss.ls_fx_col, "cla": ss.ls_fx_cla, "ct": ss.ls_fx_ct, "budget": ss.ls_fx_budget,
            "lab": ss.ls_fx_lab, "flo": ss.ls_fx_flo,
            "ct_tol": 0.10 if ss.ls_fx_ct_tol == "±0.10" else 0.05}


FLEX_LABELS = {"col": "Colour ±1 grade", "cla": "Clarity ±1 grade", "ct": "Carat ±",
               "budget": "Budget +10%", "lab": "Any lab", "flo": "Allow faint fluorescence"}
FLEX_KEYS = {"col": "ls_fx_col", "cla": "ls_fx_cla", "ct": "ls_fx_ct", "budget": "ls_fx_budget",
             "lab": "ls_fx_lab", "flo": "ls_fx_flo"}


def _widen(seq, lo, hi, by):
    i, j = seq.index(lo), seq.index(hi)
    return seq[max(0, i - by): min(len(seq), j + 1 + by)]


# Query-input field names each criterion can use, in order of preference. A field is only
# used when introspection shows a type the criterion can be sent as (see nivoda_client._filter_spec).
FILTER_FIELDS = {
    "Type": ["labgrown"],
    "Shape": ["shapes", "shape"],
    "Carat": ["sizes", "size", "carats", "carat"],
    "Colour": ["color", "colors", "colour"],
    "Fancy colour": ["fancy_colors", "fancy_color", "fancyColors", "fancyColor", "f_color"],
    "Fancy intensity": ["fancy_intensity", "fancy_intensities", "fancyIntensity", "f_intensity"],
    "Clarity": ["clarity", "clarities"],
    "Cut": ["cut"], "Polish": ["polish"], "Symmetry": ["symmetry"],
    "Fluorescence": ["fluorescence", "flouresence", "fluorescence_intensity", "fluorescenceIntensity",
                     "floInt", "flo"],
    "Lab": ["labs", "lab", "certificate_lab"],
    "Price": ["dollar_value", "price", "price_range", "total_price"],
    "L/W ratio": ["ratio", "length_width_ratio", "lw_ratio", "l_w_ratio"],
    "Depth %": ["depth_percentage", "depthPercentage", "depth_percent", "depth_pct"],
    "Table %": ["table_percentage", "tablePercentage", "table_percent", "table_pct", "table"],
    "As-grown": ["as_grown", "asGrown", "is_as_grown", "treated", "is_treated", "treatment",
                 "treatments", "lab_grown_treatment"],
}
# Flex option -> the criterion it widens (for "was this filtered server-side?")
FLEX_CRITERION = {"col": "Colour", "cla": "Clarity", "ct": "Carat", "budget": "Price",
                  "lab": "Lab", "flo": "Fluorescence"}
_OPEN_HI = {"Carat": 100.0, "Price": 100_000_000, "L/W ratio": 100.0, "Depth %": 100.0, "Table %": 100.0}


def _pick(have, crit, kinds):
    for name in FILTER_FIELDS[crit]:
        spec = have.get(name)
        if spec and spec["kind"] in kinds:
            return name, spec
    return None, None


def _enum_send(spec, values):
    """Values -> what to send for an enum field ('enum' fields can only take one value)."""
    vals = [src.Enum(v) for v in values]
    if spec["kind"] == "list_enum":
        return vals
    return vals[0] if len(vals) == 1 else None


def _range_send(spec, lo, hi):
    if spec.get("int"):
        lo, hi = int(lo // 1), int(-(-hi // 1))
    else:
        lo, hi = round(lo, 4), round(hi, 4)
    r = {spec["from"]: lo, spec["to"]: hi}
    return [r] if spec["kind"] == "ranges" else r


def price_usd_bounds(c, fx, rate, markup):
    """Form price range (CAD, chosen basis) -> USD bounds of the stone's source price,
    widened only by the Budget +10% flex. Returns (lo, hi) with None for an open end."""
    if not (c["pr_min"] or c["pr_max"]) or rate <= 0:
        return None
    k = rate * ((1 + markup / 100.0) if c["pr_client"] else 1.0)
    hi = c["pr_max"] * (1.10 if fx["budget"] else 1.0) if c["pr_max"] else None
    return ((c["pr_min"] / k) if c["pr_min"] else None, (hi / k) if hi else None)


def server_query(c, fx, schema, rate, markup, divisor):
    """Server-side query for a search: the form's criteria exactly, widened only where the
    matching flex box is ticked (by exactly the band classify() puts in 'Outside your
    criteria'). Criteria the schema can't filter are left to the client-side check.
    Everything is re-checked in code afterwards.
    Returns (query, plan): plan is a list of (criterion, 'server'|'client', detail)."""
    have = schema.get("filters") or {}
    q, plan = {}, []

    def server(crit, field, value, detail=""):
        q[field] = value
        plan.append((crit, "server", f"{field} ({have[field].get('type', '?')}){': ' + detail if detail else ''}"))

    def client(crit, why="no matching filter in the schema"):
        plan.append((crit, "client", why))

    # Type
    f, spec = _pick(have, "Type", ("bool",))
    if f:
        server("Type", f, c["lab_grown"], "lab-grown" if c["lab_grown"] else "natural")
    else:
        client("Type")
    # Shape
    if c["shapes"]:
        names = [n for g in c["shapes"] for n in SHAPE_GROUPS[g]]
        f, spec = _pick(have, "Shape", ("list_str", "list_enum", "enum"))
        if f and spec["kind"] == "list_str":
            server("Shape", f, names, "names from the documented shape list")
        elif f:
            by_key = {_key(v): v for v in spec["values"]}
            groups = {g: [by_key[_key(n)] for n in SHAPE_GROUPS[g] if _key(n) in by_key] for g in c["shapes"]}
            vals = list(dict.fromkeys(v for g in c["shapes"] for v in groups[g]))
            unmapped = [n for n in names if _key(n) not in by_key]
            val = _enum_send(spec, vals) if all(groups.values()) else None
            if val is not None:
                server("Shape", f, val, "not in the schema's list: " + ", ".join(unmapped) if unmapped else "")
            else:
                client("Shape", f"{f}: no schema value for " + ", ".join(g for g, v in groups.items() if not v)
                       if not all(groups.values()) else f"{f} takes a single value")
        else:
            client("Shape")
    # Carat (flex widens by the chosen tolerance)
    if c["ct_min"] or c["ct_max"]:
        f, spec = _pick(have, "Carat", ("ranges", "range"))
        if f:
            tol = fx["ct_tol"] if fx["ct"] else 0.0
            lo = max(0.0, (c["ct_min"] or 0) - tol) if c["ct_min"] else 0.0
            hi = (c["ct_max"] + tol) if c["ct_max"] else _OPEN_HI["Carat"]
            server("Carat", f, _range_send(spec, lo, hi), f"widened ±{tol:.2f} (flex)" if tol else "exact")
        else:
            client("Carat")

    def enum_range(crit, seq, rng, full, flex):
        if tuple(rng) == full:
            return
        want = _widen(seq, rng[0], rng[1], 1 if flex else 0)
        f, spec = _pick(have, crit, ("list_enum", "enum"))
        val = _enum_send(spec, want) if f and set(want) <= set(spec["values"]) else None
        if val is not None:
            server(crit, f, val, "widened ±1 grade (flex)" if flex else "exact")
        else:
            client(crit, f"{f}: schema values don't cover {want[0]}–{want[-1]}" if f else "no matching filter in the schema")

    # Colour / fancy colour
    if not c["fancy"]:
        enum_range("Colour", COLOURS, c["col"], ("D", "Z"), fx["col"])
    else:
        f, spec = _pick(have, "Fancy colour", ("list_enum",))
        vals = [v for v in (spec["values"] if f else []) if _key(c["fancy_col"]) in _key(v)]
        if vals:
            server("Fancy colour", f, _enum_send(spec, vals))
        else:
            client("Fancy colour")
        if c["fancy_int"]:
            f, spec = _pick(have, "Fancy intensity", ("list_enum",))
            m = {v: fancy_intensity(v.replace("_", " ")) for v in (spec["values"] if f else [])}
            vals = [v for v, i in m.items() if i in c["fancy_int"]]
            if f and vals and set(c["fancy_int"]) <= set(m.values()):
                server("Fancy intensity", f, _enum_send(spec, vals))
            else:
                client("Fancy intensity")
    # Clarity
    enum_range("Clarity", CLARITIES, c["cla"], ("FL", "I3"), fx["cla"])
    # Cut / polish / symmetry minimums (no flex)
    for crit, key in (("Cut", "cut"), ("Polish", "pol"), ("Symmetry", "sym")):
        if c[key] == "Any":
            continue
        f, spec = _pick(have, crit, ("list_enum",))
        ok = sorted(e for e in (spec["values"] if f else [])
                    if GRADE_RANK.get(norm_grade(e), -1) >= GRADE_RANK[c[key]])
        if ok:
            server(crit, f, _enum_send(spec, ok), f"{c[key]} or better")
        else:
            client(crit)
    # Fluorescence (flex adds Faint)
    if c["flo"] and set(c["flo"]) != set(FLUORS):
        allowed = set(c["flo"]) | ({"Faint"} if fx["flo"] else set())
        f, spec = _pick(have, "Fluorescence", ("list_enum",))
        m = {v: norm_fluor(v) for v in (spec["values"] if f else [])}
        vals = [v for v, lvl in m.items() if lvl in allowed]
        if f and allowed <= set(m.values()):
            server("Fluorescence", f, _enum_send(spec, vals), "+ Faint (flex)" if fx["flo"] and "Faint" not in c["flo"] else "")
        else:
            client("Fluorescence", f"{f}: schema values don't cover {', '.join(sorted(allowed - set(m.values())))}"
                   if f else "no matching filter in the schema")
    # Lab (flex = any lab, so nothing is sent)
    if c["labs"]:
        if fx["lab"]:
            client("Lab", "Any lab (flex) — not filtered")
        else:
            f, spec = _pick(have, "Lab", ("list_enum", "enum"))
            vals = [v for v in (spec["values"] if f else []) if norm_lab(v) in c["labs"]]
            val = _enum_send(spec, vals) if f and set(c["labs"]) <= {norm_lab(v) for v in vals} else None
            if val is not None:
                server("Lab", f, val)
            else:
                client("Lab", f"{f}: schema values don't cover {', '.join(c['labs'])}" if f else "no matching filter in the schema")
    # Price (CAD form range -> source USD; flex widens the max by 10%)
    b = price_usd_bounds(c, fx, rate, markup)
    if b:
        f, spec = _pick(have, "Price", ("range", "ranges"))
        if f:
            usd_unit = f.startswith("dollar")
            mult = 1.0 if usd_unit else divisor
            lo = (b[0] or 0) * mult
            hi = b[1] * mult if b[1] else _OPEN_HI["Price"] * mult
            server("Price", f, _range_send(spec, lo, hi),
                   f"US${b[0] or 0:,.0f}–{'US$' + format(b[1], ',.0f') if b[1] else 'any'}"
                   f"{' (budget +10% flex)' if fx['budget'] and c['pr_max'] else ''}; "
                   + ("sent in dollars" if usd_unit else f"sent in raw price units (× {divisor:g})"))
        else:
            client("Price")
    # Proportions
    for crit, (lo, hi) in (("L/W ratio", c["ratio"]), ("Depth %", c["depth"]), ("Table %", c["table"])):
        if lo or hi:
            f, spec = _pick(have, crit, ("range", "ranges"))
            if f:
                server(crit, f, _range_send(spec, lo or 0.0, hi or _OPEN_HI[crit]))
            else:
                client(crit)
    # As-grown (lab-grown only)
    if c["as_grown"]:
        sent = False
        for name in FILTER_FIELDS["As-grown"]:
            spec = have.get(name)
            if not spec:
                continue
            if spec["kind"] == "bool":
                server("As-grown", name, "treat" not in name.lower())
                sent = True
            elif spec["kind"] in ("list_enum", "enum"):
                vals = [v for v in spec["values"] if _key(v) in ("ASGROWN", "NONE", "UNTREATED", "NOTREATMENT")]
                val = _enum_send(spec, vals) if vals else None
                if val is not None:
                    server("As-grown", name, val)
                    sent = True
            if sent:
                break
        if not sent:
            client("As-grown")
    return q, plan


def price_filter_mismatch(stones, bounds, divisor):
    """True if the returned stones' prices don't respect the server-side price range we sent,
    i.e. the filter's units aren't what we assumed."""
    lo, hi = bounds
    usd = [s["price_raw"] / divisor for s in stones if s.get("price_raw") is not None]
    if not usd:
        return False
    bad = [u for u in usd if (lo and u < lo * 0.98 - 1) or (hi and u > hi * 1.02 + 1)]
    return len(bad) > 0.05 * len(usd)


def price_view(s, rate, markup, divisor):
    raw = s.get("price_raw")
    if raw is None or divisor <= 0:
        return None
    usd = raw / divisor
    cost = usd * rate
    client = cost * (1 + markup / 100.0)
    ct = s.get("carat") or 0
    return {"usd": usd, "cost": cost, "client": client,
            "usd_ct": usd / ct if ct else None, "cost_ct": cost / ct if ct else None,
            "client_ct": client / ct if ct else None}


def lw_ratio(s):
    l, w = s.get("length"), s.get("width")
    if l and w:
        return max(l, w) / min(l, w)
    return None


def _fmt_money(v):
    return f"CA${v:,.0f}"


def classify(s, c, fx, pv):
    """Returns (hard_fail, deviations). hard_fail is False, or the name of the first hard filter
    that removed the stone. deviations: list of (flex_key, badge) the stone needs.
    A stone with no deviations is an exact match."""
    devs = []
    fail = lambda name: (name, [])
    # Shape
    if c["shapes"] and shape_group(s["shape"]) not in c["shapes"]:
        return fail("Shape")
    # Carat
    ct = s.get("carat")
    lo, hi = c["ct_min"], c["ct_max"]
    if lo or hi:
        if ct is None:
            return fail("Carat range")
        if (lo and ct < lo - 1e-9) or (hi and ct > hi + 1e-9):
            tol = fx["ct_tol"]
            if (lo and ct < lo - tol - 1e-9) or (hi and ct > hi + tol + 1e-9):
                return fail("Carat range")
            devs.append(("ct", f"Carat {ct:.2f} (asked {lo or 0:.2f}–{hi:.2f})" if hi
                         else f"Carat {ct:.2f} (asked {lo:.2f}+)"))
    # Colour
    if c["fancy"]:
        txt = fancy_text(s)
        if not txt or c["fancy_col"].upper() not in txt.upper():
            return fail("Fancy colour")
        if c["fancy_int"] and fancy_intensity(txt) not in c["fancy_int"]:
            return fail("Fancy intensity")
    else:
        col = norm_colour(s.get("color"))
        if not col:
            return fail("Colour range")
        a, b = COLOURS.index(c["col"][0]), COLOURS.index(c["col"][1])
        i = COLOURS.index(col)
        if not a <= i <= b:
            if (a - 1 <= i <= b + 1):
                devs.append(("col", f"Colour {col} (asked {c['col'][0]}–{c['col'][1]})"
                             if c["col"][0] != c["col"][1] else f"Colour {col} (asked {c['col'][0]})"))
            else:
                return fail("Colour range")
    # Clarity
    if tuple(c["cla"]) != ("FL", "I3"):
        cl = norm_clarity(s.get("clarity"))
        if not cl:
            return fail("Clarity range")
        a, b = CLARITIES.index(c["cla"][0]), CLARITIES.index(c["cla"][1])
        i = CLARITIES.index(cl)
        if not a <= i <= b:
            if a - 1 <= i <= b + 1:
                devs.append(("cla", f"Clarity {cl} (asked {c['cla'][0]}–{c['cla'][1]})"
                             if c["cla"][0] != c["cla"][1] else f"Clarity {cl} (asked {c['cla'][0]})"))
            else:
                return fail("Clarity range")
    # Cut / polish / symmetry minimums (hard)
    for crit, field, label in (("cut", "cut", "Cut min"), ("pol", "polish", "Polish min"),
                               ("sym", "symmetry", "Symmetry min")):
        if c[crit] != "Any" and GRADE_RANK.get(norm_grade(s.get(field)), -1) < GRADE_RANK[c[crit]]:
            return fail(label)
    # Fluorescence
    if c["flo"] and set(c["flo"]) != set(FLUORS):
        fl = norm_fluor(s.get("flo"))
        if fl not in c["flo"]:
            if fl == "Faint":
                devs.append(("flo", f"Fluorescence Faint (asked {', '.join(c['flo'])})"))
            else:
                return fail("Fluorescence")
    # Lab
    if c["labs"]:
        lab = norm_lab(s.get("lab"))
        if lab not in c["labs"]:
            if not lab:
                return fail("Lab")
            devs.append(("lab", f"Lab {lab} (asked {', '.join(c['labs'])})"))
    # Price (CAD, chosen basis)
    if c["pr_min"] or c["pr_max"]:
        if not pv:
            return fail("Price")
        p = pv["client"] if c["pr_client"] else pv["cost"]
        basis = "client price" if c["pr_client"] else "cost"
        if c["pr_min"] and p < c["pr_min"]:
            return fail("Price min")
        if c["pr_max"] and p > c["pr_max"]:
            if p <= c["pr_max"] * 1.10:
                devs.append(("budget", f"{basis.capitalize()} {_fmt_money(p)} (budget {_fmt_money(c['pr_max'])})"))
            else:
                return fail("Price max")
    # Proportions (hard)
    for (lo, hi), val, label in ((c["ratio"], lw_ratio(s), "L/W ratio"), (c["depth"], s.get("depth_pct"), "Depth %"),
                                 (c["table"], s.get("table_pct"), "Table %")):
        if lo or hi:
            if val is None or (lo and val < lo) or (hi and val > hi):
                return fail(label)
    # As-grown (hard when the data says so; unknown is flagged on the card)
    if c["as_grown"] and as_grown_status(s) is False:
        return fail("As-grown only")
    return False, devs


FILTER_ORDER = ["Shape", "Carat range", "Fancy colour", "Fancy intensity", "Colour range", "Clarity range",
                "Cut min", "Polish min", "Symmetry min", "Fluorescence", "Lab", "Price", "Price min",
                "Price max", "L/W ratio", "Depth %", "Table %", "As-grown only"]


def bucket(stones, c, fx, rate, markup, divisor, stats=None):
    """Splits fetched stones into exact / flexed (per enabled options) and counts what each
    disabled flex option would add. Each stone lands in at most one section.
    If `stats` (a dict) is given, it's filled with per-filter removal counts for diagnostics."""
    enabled = {k for k in FLEX_LABELS if fx[k]}
    exact, flexed, adds = [], [], {k: 0 for k in FLEX_LABELS}
    removed, needs_flex = {}, 0
    for s in stones:
        pv = price_view(s, rate, markup, divisor)
        hard, devs = classify(s, c, fx, pv)
        if hard:
            removed[hard] = removed.get(hard, 0) + 1
            continue
        row = {**s, "pv": pv, "devs": devs}
        keys = {k for k, _ in devs}
        if not keys:
            exact.append(row)
        elif keys <= enabled:
            flexed.append(row)
        else:
            needs_flex += 1
            missing = keys - enabled
            if len(missing) == 1:
                adds[missing.pop()] += 1
    if stats is not None:
        stats.update({"removed": [(n, removed[n]) for n in FILTER_ORDER if removed.get(n)],
                      "needs_flex": needs_flex, "exact": len(exact), "flexed": len(flexed)})
    return exact, flexed, adds


# ── UI ────────────────────────────────────────────────────────────────────────
CSS = """
<style>
.ls-note { font-size: 11.5px; color: #b45309; background: rgba(245,158,11,0.12);
  border-left: 3px solid #f59e0b; padding: 4px 8px; border-radius: 4px; margin: -6px 0 10px; }
.ls-badge { display: inline-block; font-size: 10.5px; font-weight: 600; padding: 2px 7px; border-radius: 4px;
  background: rgba(245,158,11,0.15); color: #b45309; margin: 2px 4px 2px 0; }
.ls-badge.grey { background: rgba(128,128,128,0.15); color: inherit; opacity: 0.8; }
.ls-title { font-weight: 600; font-size: 14.5px; }
.ls-line { font-size: 12px; opacity: 0.75; line-height: 1.55; }
.ls-price { font-size: 12px; line-height: 1.5; text-align: right; }
.ls-price b { font-size: 14px; }
.ls-pick { border-left: 3px solid #c9a84c; padding: 6px 10px; margin: 6px 0; font-size: 13px; }
</style>
"""
REQUIRED = ["NIVODA_API_URL", "NIVODA_USERNAME", "NIVODA_PASSWORD", "DEFAULT_USD_CAD"]


def _esc(v):
    return html.escape(str(v if v is not None else ""))


def _note(key):
    n = st.session_state.get("ls_notes", {}).get(key)
    if n:
        st.markdown(f'<div class="ls-note">≈ {_esc(n)}</div>', unsafe_allow_html=True)


def _amber_css():
    keys = st.session_state.get("ls_notes", {})
    if not keys:
        return
    ks = [f".st-key-{_widget_key(k)}" for k in keys]
    boxes = ", ".join(f'{k} div[data-baseweb="input"], {k} div[data-baseweb="select"] > div' for k in ks)
    st.markdown(f"<style>{', '.join(ks)} {{ border-left: 3px solid #f59e0b; padding-left: 8px; }}"
                f"{boxes} {{ background-color: rgba(245,158,11,0.16) !important;"
                f" border: 1px solid #f59e0b !important; }}"
                f"{', '.join(k + ' input' for k in ks)} {{ background-color: transparent !important; }}</style>",
                unsafe_allow_html=True)


RANGE_KEYS = ("ls_col", "ls_cla")


def _widget_key(key):
    # Range sliders use a versioned widget key: a range select_slider needs an explicit
    # `value=`, so "Read request" re-creates them (new version) instead of writing to them.
    return f"{key}_w{st.session_state.get('ls_ver', 0)}" if key in RANGE_KEYS else key


def _range_slider(label, options, key):
    ss = st.session_state
    ss[key] = st.select_slider(label, options, value=_range(ss[key], options), key=_widget_key(key))
    _note(key)


def _set_flex(key):
    st.session_state[key] = True
    st.session_state["ls_autosearch"] = True   # the server-side query changes, so fetch again


def _toggle_pick(sid, key):
    p = st.session_state.ls_picked
    (p.add if st.session_state.get(key) else p.discard)(sid)


def _clear_picks():
    st.session_state.ls_picked = set()
    for k in [k for k in st.session_state if str(k).startswith(("ls_pk_", "ls_tbl_"))]:
        del st.session_state[k]


def render(deps):
    """deps: get_setting, save_quote, add_history, client_selector (all from app.py)."""
    try:
        _render(deps)
    except Exception as e:
        # Let Streamlit's own control-flow exceptions (rerun/stop) through
        if type(e).__name__ in ("RerunException", "StopException", "RerunData"):
            raise
        traceback.print_exc()   # server log only
        st.error("Live Search hit an unexpected problem. Try again; if it keeps happening, "
                 "refresh the page. The rest of the app is unaffected.")


def _render(deps):
    get = deps["get_setting"]
    missing = [k for k in REQUIRED if not get(k)]
    rate_env = _f(get("DEFAULT_USD_CAD"))
    if "DEFAULT_USD_CAD" not in missing and not (rate_env and rate_env > 0):
        missing.append("DEFAULT_USD_CAD (must be a number, e.g. 1.37)")
    if missing:
        st.info("**Live Search isn't set up yet.** Add these environment variables in Render → "
                "Environment, then redeploy:\n\n" + "\n".join(f"- `{m}`" for m in missing) +
                "\n\nOptional: `ANTHROPIC_API_KEY` (Read request / Suggest top picks), "
                "`DEFAULT_MARKUP_PCT`.")
        return
    ai_key = get("ANTHROPIC_API_KEY")
    divisor = _f(get("NIVODA_PRICE_DIVISOR"), 100.0) or 100.0
    _init_state(_f(get("DEFAULT_MARKUP_PCT"), 0.0) or 0.0, rate_env)
    ss = st.session_state
    st.markdown(CSS, unsafe_allow_html=True)
    _amber_css()
    client = src.Client(get("NIVODA_API_URL"), get("NIVODA_USERNAME"), get("NIVODA_PASSWORD"), ss)

    # ── A. Request parsing ────────────────────────────────────────────────
    st.markdown('<div class="section-label">Client request (optional)</div>', unsafe_allow_html=True)
    req = st.text_area("Paste client request", key="ls_req", height=100,
                       placeholder="e.g. Oval 1.5ish, G-H, VS, GIA, budget around $9k to the client")
    if st.button("Read request", type="primary", key="ls_read", disabled=not (ai_key and req.strip())):
        with st.spinner("Reading the request…"):
            try:
                n, notes = apply_parse(ai_parse_request(ai_key, req.strip()))
                ss.ls_parse_msg = (f"Filled {n} field(s) from the request"
                                   + (f" — {len(notes)} interpreted (amber): please confirm." if notes else ".")
                                   + " Nothing has been searched yet.")
                ss.ls_parse_err = None
            except AIError as e:
                ss.ls_parse_msg, ss.ls_parse_err = None, str(e)
        st.rerun()
    if ss.get("ls_parse_err"):
        st.error(ss.ls_parse_err)
    if not ai_key:
        st.caption("Add ANTHROPIC_API_KEY to enable reading requests and top picks.")
    if ss.get("ls_parse_msg"):
        st.info(ss.ls_parse_msg)
        if ss.get("ls_notes") and st.button("Clear highlights", type="primary", key="ls_clr_notes"):
            ss.ls_notes = {}
            st.rerun()
    st.markdown('<hr class="divider">', unsafe_allow_html=True)

    # ── B. Criteria form ──────────────────────────────────────────────────
    st.markdown('<div class="section-label">Criteria</div>', unsafe_allow_html=True)
    a, b = st.columns(2)
    with a:
        st.radio("Type", TYPES, horizontal=True, key="ls_type"); _note("ls_type")
    with b:
        if ss.ls_type == "Lab-grown":
            st.checkbox("As-grown only", key="ls_as_grown"); _note("ls_as_grown")
    st.multiselect("Shape(s)", SHAPES, key="ls_shapes", placeholder="Any shape"); _note("ls_shapes")

    a, b, fxc = st.columns([2, 2, 2])
    with a:
        st.number_input("Carat min", min_value=0.0, step=0.01, format="%.2f", key="ls_ct_min"); _note("ls_ct_min")
    with b:
        st.number_input("Carat max", min_value=0.0, step=0.01, format="%.2f", key="ls_ct_max"); _note("ls_ct_max")
    with fxc:
        st.checkbox("Flex carat", key="ls_fx_ct")
        st.radio("Carat flex", ["±0.05", "±0.10"], horizontal=True, key="ls_fx_ct_tol",
                 label_visibility="collapsed")

    st.radio("Colour", ["White (D–Z)", "Fancy colour"], horizontal=True, key="ls_col_mode"); _note("ls_col_mode")
    if ss.ls_col_mode == "White (D–Z)":
        a, fxc = st.columns([4, 2])
        with a:
            _range_slider("Colour range (best → lowest)", COLOURS, "ls_col")
        with fxc:
            st.checkbox("Colour ±1 grade", key="ls_fx_col")
    else:
        a, b = st.columns(2)
        with a:
            st.selectbox("Fancy colour", FANCY_COLOURS, key="ls_fancy_col"); _note("ls_fancy_col")
        with b:
            st.multiselect("Intensity", INTENSITIES, key="ls_fancy_int", placeholder="Any intensity"); _note("ls_fancy_int")

    a, fxc = st.columns([4, 2])
    with a:
        _range_slider("Clarity range (best → lowest)", CLARITIES, "ls_cla")
    with fxc:
        st.checkbox("Clarity ±1 grade", key="ls_fx_cla")

    a, b, c_ = st.columns(3)
    with a:
        st.selectbox("Cut min", MIN_GRADES, key="ls_cut"); _note("ls_cut")
    with b:
        st.selectbox("Polish min", MIN_GRADES, key="ls_pol"); _note("ls_pol")
    with c_:
        st.selectbox("Symmetry min", MIN_GRADES, key="ls_sym"); _note("ls_sym")
    st.caption("A minimum excludes stones with no grade (most fancy shapes have no cut grade).")

    a, fxc = st.columns([4, 2])
    with a:
        st.multiselect("Fluorescence allowed", FLUORS, key="ls_flo", placeholder="Any fluorescence"); _note("ls_flo")
    with fxc:
        st.checkbox("Allow faint fluorescence", key="ls_fx_flo")
    a, fxc = st.columns([4, 2])
    with a:
        st.multiselect("Lab", LABS, key="ls_labs", placeholder="Any lab"); _note("ls_labs")
    with fxc:
        st.checkbox("Any lab", key="ls_fx_lab")

    a, b, fxc = st.columns([2, 2, 2])
    with a:
        st.number_input("Price min (CAD)", min_value=0.0, step=100.0, format="%.0f", key="ls_pr_min"); _note("ls_pr_min")
    with b:
        st.number_input("Price max (CAD)", min_value=0.0, step=100.0, format="%.0f", key="ls_pr_max"); _note("ls_pr_max")
    with fxc:
        st.checkbox("Budget +10%", key="ls_fx_budget")
    st.radio("Price range is", ["My cost", "Client price"], horizontal=True, key="ls_pr_basis"); _note("ls_pr_basis")

    with st.expander("Proportions (L/W ratio, depth %, table %)",
                     expanded=any(ss.get(k) for k in ("ls_ratio_min", "ls_ratio_max", "ls_depth_min",
                                                      "ls_depth_max", "ls_table_min", "ls_table_max"))):
        for label, lo, hi, step in (("L/W ratio", "ls_ratio_min", "ls_ratio_max", 0.01),
                                    ("Depth %", "ls_depth_min", "ls_depth_max", 0.1),
                                    ("Table %", "ls_table_min", "ls_table_max", 0.1)):
            a, b = st.columns(2)
            with a:
                st.number_input(f"{label} min", min_value=0.0, step=step, format="%.2f", key=lo); _note(lo)
            with b:
                st.number_input(f"{label} max", min_value=0.0, step=step, format="%.2f", key=hi); _note(hi)

    # ── C. Pricing ───────────────────────────────────────────────────────
    a, b = st.columns(2)
    with a:
        st.number_input("Markup %", min_value=0.0, step=1.0, format="%.1f", key="ls_markup",
                        help="Client price = cost × (1 + markup)")
    with b:
        st.number_input("USD → CAD rate", min_value=0.01, step=0.01, format="%.4f", key="ls_rate",
                        help="Live Search prices arrive in USD; they're converted to CAD with this rate.")
    rate, markup = float(ss.ls_rate), float(ss.ls_markup)

    # ── D. Search ────────────────────────────────────────────────────────
    crit = read_criteria()
    fx = flex_state()
    auto = ss.pop("ls_autosearch", False)       # a flex offer button asks for a fresh search
    if st.button("🔍  Search", type="primary", use_container_width=True, key="ls_search") or auto:
        client.reset_diag()
        q, plan, schema, error, retry = None, [], {}, None, None
        with st.spinner("Searching live stones…"):
            try:
                schema = client.schema()
                q, plan = server_query(crit, fx, schema, rate, markup, divisor)
                stones, total = client.search(q, MAX_SCAN)
                sent = q
                pf = next((p[2].split(" ")[0] for p in plan if p[0] == "Price" and p[1] == "server"), None)
                bounds = price_usd_bounds(crit, fx, rate, markup)
                if pf and (not stones or price_filter_mismatch(stones, bounds, divisor)):
                    # The price filter's units can't be trusted for this search: fetch again
                    # without it and check price client-side only.
                    retry = {"reason": "no stones returned" if not stones else
                             "returned prices were outside the range sent (units differ)",
                             "first_attempt": {k: client.diag[k] for k in
                                               ("pages", "raw_items", "total_count", "count_query_total")}}
                    client.diag.update(pages=0, raw_items=0, total_count=None, count_query_total=None)
                    sent = {k: v for k, v in q.items() if k != pf}
                    stones, total = client.search(sent, MAX_SCAN)
                ss.ls_results = {"q": q, "sent": sent, "stones": stones, "total": total,
                                 "fetched": client.diag["raw_items"], "cap_hit": client.diag["cap_hit"],
                                 "server_crit": {p[0] for p in plan if p[1] == "server"} - (
                                     {"Price"} if retry else set()),
                                 "verified": schema.get("verified"), "crit": crit}
                ss.ls_picks_ai = None
            except src.SourceError as e:
                ss.ls_results = None
                error = str(e)
                st.error(error)
        stats, sample = {}, None
        if ss.ls_results:
            ex, fl, _ = bucket(ss.ls_results["stones"], crit, fx, rate, markup, divisor, stats)
            priced = [r for r in ex + fl if r["pv"]] or [
                {**s, "devs": None} for s in ss.ls_results["stones"] if s.get("price_raw") is not None]
            if priced:
                sample = min(priced, key=lambda r: r["price_raw"])
        ss.ls_diag = _build_diag(client.diag, q, plan, retry, schema, error, ss.ls_results, stats,
                                 sample, rate, markup, divisor)
        _log_diag(ss.ls_diag)

    res = ss.get("ls_results")
    live_stats = None
    if res:
        try:
            current_q = server_query(crit, fx, client.schema(), rate, markup, divisor)[0]
        except Exception:
            current_q = res["q"]
        if current_q != res["q"]:
            st.warning("The criteria changed since the last search. Click **Search** to refresh the results.")
        else:
            live_stats = {}
            exact, flexed, adds = bucket(res["stones"], crit, fx, rate, markup, divisor, live_stats)
            _results(exact, flexed, adds, res, crit, fx, ai_key, deps)
    _diagnostics(ss.get("ls_diag"), live_stats)


# ── Search diagnostics ────────────────────────────────────────────────────────
_GRADE_ABBR = {"Excellent": "EX", "Very Good": "VG", "Good": "G", "Fair": "F", "Poor": "P"}


def _build_diag(cd, q, plan, retry, schema, error, res, stats, sample_stone, rate, markup, divisor):
    """Everything here is sanitised: no source name, hosts, credentials or supplier data."""
    sample = None
    if sample_stone:
        raw = sample_stone.get("price_raw")
        usd = float(raw) / divisor
        ct = sample_stone.get("carat")
        devs = sample_stone.get("devs")
        sample = {"raw_price": int(raw) if float(raw).is_integer() else raw, "divisor": divisor, "usd": round(usd, 2), "usd_cad_rate": rate,
                  "cost_cad": round(usd * rate, 2), "client_cad": round(usd * rate * (1 + markup / 100.0), 2),
                  "carat": ct, "usd_per_ct": round(usd / ct, 2) if ct else None,
                  "cost_cad_per_ct": round(usd * rate / ct, 2) if ct else None,
                  "stone": " · ".join(x for x in (
                      shape_group(sample_stone.get("shape")), _spec_bits(sample_stone)[0],
                      norm_clarity(sample_stone.get("clarity")) or str(sample_stone.get("clarity") or ""),
                      "/".join(_GRADE_ABBR.get(norm_grade(sample_stone.get(k)), "—") for k in ("cut", "polish", "symmetry")),
                      norm_lab(sample_stone.get("lab"))) if x),
                  "which": ("cheapest exact match" if devs == [] else
                            "cheapest stone in 'Outside your criteria'" if devs else
                            "cheapest stone returned (none matched the form)")}
    total_src = ("count query" if cd.get("count_query_total") is not None and cd.get("real_total") is not None
                 else "total_count field" if cd.get("real_total") is not None and cd.get("real_total") == src._int(cd.get("total_count"))
                 else "all pages fetched" if cd.get("real_total") is not None else "not reported")
    return {
        "time_utc": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "sign_in": cd.get("sign_in"),
        "api_errors": list(cd.get("api_errors") or []),
        "user_error": error,
        "schema_verified": bool(schema.get("verified")),
        "plan": [list(p) for p in plan],
        "count_query": schema.get("count_query"),
        "price_retry": retry,
        "server_query": {"query": json.loads(json.dumps(res["sent"] if res else q)) if q is not None else None,
                         "offset": f"0, 50, 100… ({cd.get('pages', 0)} page(s))", "limit": src.PAGE_LIMIT,
                         "order": {"type": "price", "direction": "ASC"}},
        "api_total": cd.get("real_total"),
        "api_total_source": total_src,
        "api_total_count_field": cd.get("total_count"),
        "api_count_query": cd.get("count_query_total"),
        "api_returned": cd.get("raw_items", 0),
        "pages": cd.get("pages", 0),
        "cap": MAX_SCAN,
        "cap_hit": bool(cd.get("cap_hit")),
        "kept_after_whitelist": len(res["stones"]) if res else 0,
        "client_side": stats or {},
        "sample_price": sample,
        "schema_docs": dict(schema.get("docs") or {}),
        "labgrown_flag_field": ".".join(schema["lg_flag"]) if schema.get("lg_flag") else None,
        "labgrown_flags": cd.get("labgrown_flags"),
        "media": cd.get("media"),
    }


def _log_diag(d):
    """One line per search in the server log (Render)."""
    try:
        print("[live-search] " + json.dumps(d, default=str, separators=(",", ":")), flush=True)
    except Exception:
        pass


def _code(v):
    return str(v).replace("`", "'")


def _diagnostics(d, live_stats=None):
    if not d:
        return
    with st.expander("Search diagnostics", expanded=False):
        st.caption(f"Last search: {d['time_utc']} UTC. The same details are written to the server log.")
        ok = str(d["sign_in"]).startswith("ok")
        st.markdown(f"**1. Sign-in:** {'✅' if ok else '❌'} {d['sign_in']}")
        if d["api_errors"]:
            st.markdown("**Errors returned by the API:**\n\n" + "\n".join(f"- `{_code(e)}`" for e in d["api_errors"]))
        else:
            st.markdown("**Errors returned by the API:** none")
        if d.get("user_error"):
            st.markdown(f"**Shown to you:** {d['user_error']}")

        st.markdown("**2. Criteria → server-side filters** ("
                    + ("schema confirmed by introspection" if d["schema_verified"]
                       else "documented filters only; schema not confirmed") + "). "
                    "Exact form values; widened only where a flex box is ticked. "
                    "Every criterion is re-checked here after fetching.")
        if d.get("plan"):
            st.markdown("\n".join(f"- **{_esc(c)}**: {'server-side' if w == 'server' else 'client-side only'}"
                                   f" — `{_code(det)}`" for c, w, det in d["plan"]))
        if d.get("price_retry"):
            r = d["price_retry"]
            st.markdown(f"⚠️ **Price filter dropped:** {r['reason']}; searched again without it and "
                        f"checked price here instead (first attempt: {r['first_attempt']['raw_items']} stone(s)).")
        st.code(json.dumps(d["server_query"], indent=2), language="json")
        st.caption("Sign-in sends only a username and password, which are never shown or logged.")

        total = d.get("api_total")
        tot_txt = (f"{total:,} matching server-side (from the {d['api_total_source']})" if isinstance(total, int)
                   else "the API didn't report a usable total")
        st.markdown(f"**3. Returned by the API:** {d['api_returned']} stone(s) in {d['pages']} page(s) of up to "
                    f"{src.PAGE_LIMIT}; {tot_txt}"
                    + (f"; **stopped at the {d['cap']}-stone cap**" if d.get("cap_hit") else "; all matches fetched")
                    + f". {d['kept_after_whitelist']} usable after the field whitelist.")
        st.caption(f"Count query: {d.get('count_query') or 'none in the schema'} → {d.get('api_count_query')} · "
                   f"total_count field → {d.get('api_total_count_field')}")

        stats = live_stats if live_stats is not None else d["client_side"]
        if stats:
            basis = "current form and flex settings" if live_stats is not None else "settings at search time"
            lines = [f"- {name}: removed {n}" for name, n in stats.get("removed", [])] or ["- nothing removed"]
            st.markdown(f"**4. Client-side re-check** (in order; {basis}):\n\n" + "\n".join(lines)
                        + f"\n\nOutside criteria, needs a flex option that's off: {stats.get('needs_flex', 0)} · "
                        f"Exact: {stats.get('exact', 0)} · Outside your criteria (shown): {stats.get('flexed', 0)}")
        else:
            st.markdown("**4. Client-side re-check:** no stones to check.")

        sp = d.get("sample_price")
        if sp:
            per_ct = (f" · US\\${sp['usd_per_ct']:,.2f}/ct · CA\\${sp['cost_cad_per_ct']:,.2f}/ct cost"
                      if sp.get("cost_cad_per_ct") else "")
            st.markdown(f"**5. Sample price** ({sp['which']}: {sp['carat'] or '?'} ct {_esc(sp['stone'])}): "
                        f"raw `{_code(sp['raw_price'])}` ÷ {sp['divisor']:g} = US\\${sp['usd']:,.2f} "
                        f"× {sp['usd_cad_rate']:g} = **CA\\${sp['cost_cad']:,.2f} cost** "
                        f"(client CA\\${sp['client_cad']:,.2f}){per_ct}.")
            st.caption("Check this stone's price on the platform. If it's 100× off, change the price-divisor environment variable (see the setup notes).")
        else:
            st.markdown("**5. Sample price:** none (no priced stones returned).")

        docs = d.get("schema_docs") or {}
        keys = [k for k in docs if k.startswith(("item.", "result.", "query.labgrown", "query.dollar", "query.price"))
                or k.endswith("count") or k.startswith(("diamond.", "certificate."))]
        lines = [f"- `{_code(k)}`: {_esc(docs[k])}" for k in keys]
        flags = d.get("labgrown_flags")
        if flags:
            lines.append(f"- Stones by the `{_code(d['labgrown_flag_field'])}` flag: natural {flags['natural']}, "
                         f"lab-grown {flags['lab-grown']}, not stated {flags['unknown']}")
        else:
            lines.append("- No lab-grown flag field in the stone data to cross-check the type filter.")
        st.markdown("**6. What the schema says about price / totals / type:**\n\n" + "\n".join(lines))

        md = d.get("media")
        if md:
            v = md.get("video") or {}
            lines = [f"- `{_code(f)}`" for f in md.get("fields") or []] or [
                "- none found" if md.get("schema_verified") else "- schema not confirmed, so no extra fields requested"]
            got = [f"- `{_code(k)}`: set on {e['count']} of {md['stones']} stone(s), e.g. `{_code(e['example'])}`"
                   for k, e in (md.get("extra") or {}).items()] or ["- no extra media values on these stones"]
            st.markdown("**7. Media in the stone data** (for 360 capture; hosts masked):\n\n"
                        "Media fields in the schema:\n\n" + "\n".join(lines)
                        + f"\n\nVideo links: {v.get('set', 0)} of {md['stones']} stone(s) — {v.get('files', 0)} direct "
                        f"video file(s), {v.get('pages', 0)} viewer page(s)"
                        + (f", e.g. `{_code(v['example'])}`" if v.get("example") else "")
                        + "\n\nCertificate lookup by ID (fallback for other stones and pasted viewer links): "
                        + (f"`{_code(md['cert_lookup'])}`" if md.get("cert_lookup") else "none in the schema")
                        + "\n\nValues returned:\n\n" + "\n".join(got))


def _sort(rows, how, crit):
    big = float("inf")
    cost = lambda r: r["pv"]["cost"] if r["pv"] else big
    if how == SORTS[1]:
        return sorted(rows, key=lambda r: -(r["pv"]["cost"] if r["pv"] else -big))
    if how == SORTS[2]:
        return sorted(rows, key=lambda r: (-(r.get("carat") or 0), cost(r)))
    if how == SORTS[3]:
        return sorted(rows, key=lambda r: (r["pv"]["cost_ct"] if r["pv"] and r["pv"]["cost_ct"] else big))
    if how == SORTS[4]:
        lo, hi = crit["ct_min"], crit["ct_max"]
        target = ((lo or hi) + (hi or lo)) / 2 if (lo or hi) else None
        if target:
            return sorted(rows, key=lambda r: (abs((r.get("carat") or 0) - target), cost(r)))
    return sorted(rows, key=cost)


def _spec_bits(s):
    txt = fancy_text(s)
    colour = norm_colour(s.get("color")) or txt or s.get("color") or "—"
    grades = " · ".join(f"{lbl} {norm_grade(s.get(f)) or '—'}"
                        for lbl, f in (("Cut", "cut"), ("Pol", "polish"), ("Sym", "symmetry")))
    fl = norm_fluor(s.get("flo")) or (s.get("flo") or "—")
    meas = (f"{s['length']:.2f} x {s['width']:.2f} x {s['depth_mm']:.2f} mm"
            if s.get("length") and s.get("width") and s.get("depth_mm") else "")
    r = lw_ratio(s)
    return colour, grades, fl, meas, r


def _card(s, section):
    pv = s["pv"]
    colour, grades, fl, meas, r = _spec_bits(s)
    with st.container(border=True):
        c1, c2, c3 = st.columns([1.1, 3.2, 1.6])
        with c1:
            if s.get("image"):
                try:
                    st.image(s["image"], width=110)
                except Exception:
                    st.caption("No image")
            else:
                st.caption("No image")
            if s.get("video"):
                st.markdown(f'<a href="{_esc(s["video"])}" target="_blank" rel="noreferrer" '
                            f'style="font-size:12px;">▶ Video</a>', unsafe_allow_html=True)
        with c2:
            ct = f"{s['carat']:.2f} ct " if s.get("carat") else ""
            badges = "".join(f'<span class="ls-badge">{_esc(b)}</span>' for _, b in s["devs"])
            if st.session_state.ls_as_grown and as_grown_status(s) is None:
                badges += '<span class="ls-badge grey">As-grown not confirmed</span>'
            props = " · ".join(x for x in (
                f"L/W {r:.2f}" if r else "",
                f"Depth {s['depth_pct']:.1f}%" if s.get("depth_pct") else "",
                f"Table {s['table_pct']:.0f}%" if s.get("table_pct") else "") if x)
            st.markdown(
                f'<div class="ls-title">{_esc(ct)}{_esc(shape_group(s["shape"]))} · {_esc(colour)} · '
                f'{_esc(norm_clarity(s.get("clarity")) or s.get("clarity") or "—")}</div>'
                f'<div class="ls-line">{_esc(grades)} · Fluor {_esc(fl)}</div>'
                f'<div class="ls-line">{_esc(s.get("lab") or "—")} {_esc(s.get("cert_no"))}'
                f'{" · " + _esc(meas) if meas else ""}</div>'
                f'<div class="ls-line">{_esc(props)}</div>{badges}', unsafe_allow_html=True)
        with c3:
            if pv:
                usd_ct = f" · US${pv['usd_ct']:,.0f}/ct" if pv["usd_ct"] else ""
                client_ct = f"{_fmt_money(pv['client_ct'])}/ct" if pv["client_ct"] else ""
                st.markdown(
                    f'<div class="ls-price">Cost <b>{_fmt_money(pv["cost"])}</b><br>'
                    f'<span style="opacity:.6">US${pv["usd"]:,.0f} total{usd_ct}</span><br>'
                    f'Client <b>{_fmt_money(pv["client"])}</b><br>'
                    f'<span style="opacity:.6">{client_ct}</span></div>', unsafe_allow_html=True)
            else:
                st.caption("No price")
            st.checkbox("Add to quote", value=s["sid"] in st.session_state.ls_picked,
                        key=f"ls_pk_{s['sid']}", on_change=_toggle_pick, args=(s["sid"], f"ls_pk_{s['sid']}"))


def _criteria_text(c, fx):
    parts = ["Lab-grown" if c["lab_grown"] else "Natural"]
    if c["shapes"]:
        parts.append("/".join(c["shapes"]))
    if c["ct_min"] or c["ct_max"]:
        parts.append(f"{c['ct_min'] or 0:.2f}–{c['ct_max']:.2f} ct" if c["ct_max"] else f"{c['ct_min']:.2f}+ ct")
    if c["fancy"]:
        parts.append(f"Fancy {c['fancy_col']}" + (f" ({', '.join(c['fancy_int'])})" if c["fancy_int"] else ""))
    elif tuple(c["col"]) != ("D", "Z"):
        parts.append(f"colour {c['col'][0]}–{c['col'][1]}")
    if tuple(c["cla"]) != ("FL", "I3"):
        parts.append(f"clarity {c['cla'][0]}–{c['cla'][1]}")
    for k, lbl in (("cut", "cut"), ("pol", "polish"), ("sym", "symmetry")):
        if c[k] != "Any":
            parts.append(f"{lbl} ≥ {c[k]}")
    if c["flo"]:
        parts.append("fluor " + "/".join(c["flo"]))
    if c["labs"]:
        parts.append("/".join(c["labs"]))
    if c["pr_min"] or c["pr_max"]:
        basis = "client price" if c["pr_client"] else "cost"
        parts.append(f"{basis} {_fmt_money(c['pr_min'] or 0)}–{_fmt_money(c['pr_max']) if c['pr_max'] else 'any'}")
    if c["as_grown"]:
        parts.append("as-grown only")
    return ", ".join(parts)


def _flex_label(k, fx):
    return f"Carat {'±0.10' if fx['ct_tol'] == 0.10 else '±0.05'}" if k == "ct" else FLEX_LABELS[k]


def _results(exact, flexed, adds, res, crit, fx, ai_key, deps):
    ss = st.session_state
    st.markdown('<hr class="divider">', unsafe_allow_html=True)
    fetched, total = res.get("fetched", len(res["stones"])), res.get("total")
    if res.get("cap_hit"):
        of = f"{total:,}" if isinstance(total, int) and total > fetched else "more than " + f"{fetched:,}"
        st.caption(f"Checked the {fetched:,} lowest-priced of {of} stones matching these criteria "
                   f"(the {MAX_SCAN}-stone limit). Narrow the criteria to see beyond them.")
    elif isinstance(total, int):
        st.caption(f"{total:,} stone(s) match these criteria; all were checked.")
    if not res.get("verified"):
        st.caption("Some criteria are checked here after fetching rather than by the stone search itself.")

    n_ex = len(exact)
    if n_ex < 5:
        # Flex options on criteria filtered server-side weren't fetched, so their gain is
        # unknown until a new search; client-side-only ones are counted from this fetch.
        active = {"col": not crit["fancy"] and tuple(crit["col"]) != ("D", "Z"),
                  "cla": tuple(crit["cla"]) != ("FL", "I3"), "ct": bool(crit["ct_min"] or crit["ct_max"]),
                  "budget": bool(crit["pr_max"]), "lab": bool(crit["labs"]),
                  "flo": bool(crit["flo"]) and "Faint" not in crit["flo"] and set(crit["flo"]) != set(FLUORS)}
        server_side = res.get("server_crit", set())
        offers = []
        for k in FLEX_LABELS:
            if fx[k] or not active[k]:
                continue
            if FLEX_CRITERION[k] in server_side:
                offers.append((k, None))
            elif adds.get(k):
                offers.append((k, adds[k]))
        msg = (f"**{n_ex} exact match{'es' if n_ex != 1 else ''}.** " +
               ("Flex options that may add stones (they search again; extra stones are shown separately, "
                "never mixed in):" if offers else "No single flex option would add stones."))
        st.warning(msg)
        if offers:
            cols = st.columns(min(len(offers), 3))
            for i, (k, n) in enumerate(offers):
                with cols[i % len(cols)]:
                    st.button(f"{_flex_label(k, fx)}  " + (f"(+{n})" if n else "(search again)"),
                              key=f"ls_apply_{k}", type="primary",
                              on_click=_set_flex, args=(FLEX_KEYS[k],), use_container_width=True)

    a, b, c_ = st.columns([2.2, 1.4, 1.2])
    with a:
        sort = st.selectbox("Sort by", SORTS, index=0, key="ls_sort")
    with b:
        view = st.radio("View", ["Cards", "Table"], index=0, horizontal=True, key="ls_view")
    with c_:
        show_n = int(st.number_input("Show per section", min_value=5, max_value=200, value=25, step=5,
                                     key="ls_show_n"))
    shown_ex = _sort(exact, sort, crit)[:show_n]
    shown_fx = _sort(flexed, sort, crit)[:show_n]

    st.markdown(f'<div class="section-label" style="margin-top:.8rem">Exact matches · {n_ex}'
                f'{f" (showing {len(shown_ex)})" if n_ex > len(shown_ex) else ""}</div>', unsafe_allow_html=True)
    _section(shown_ex, "ex", view)
    if any(fx[k] for k in FLEX_LABELS):
        st.markdown(f'<div class="section-label" style="margin-top:1rem">Outside your criteria · {len(flexed)}'
                    f'{f" (showing {len(shown_fx)})" if len(flexed) > len(shown_fx) else ""}</div>',
                    unsafe_allow_html=True)
        _section(shown_fx, "fx", view)

    # ── Top picks (AI, from displayed stones only) ──────────────────────────
    st.markdown('<hr class="divider">', unsafe_allow_html=True)
    refs = {}
    for prefix, rows, section in (("E", shown_ex, "exact"), ("O", shown_fx, "outside")):
        for i, s in enumerate(rows, 1):
            refs[f"{prefix}{i}"] = (s, section)
    if st.button("✨  Suggest top picks", type="primary", key="ls_picks_btn",
                 disabled=not (ai_key and refs)):
        payload = [_ai_stone(ref, s, section) for ref, (s, section) in refs.items()]
        with st.spinner("Choosing top picks…"):
            try:
                out = ai_top_picks(ai_key, _criteria_text(crit, fx), payload)
                picks = []
                for p in (out.get("picks") or [])[:5]:
                    ref = str(p.get("ref", "")).strip()
                    if ref in refs:           # only stones that were actually displayed
                        picks.append((ref, refs[ref][0]["sid"], refs[ref][1], str(p.get("reason", ""))[:240]))
                ss.ls_picks_ai = picks
            except AIError as e:
                st.error(str(e))
    if ss.get("ls_picks_ai"):
        by_sid = {s["sid"]: s for s in exact + flexed}
        for ref, sid, section, reason in ss.ls_picks_ai:
            s = by_sid.get(sid)
            if not s:
                continue
            colour, _, _, _, _ = _spec_bits(s)
            tag = ('<span class="ls-badge">Outside criteria</span>' if section == "outside" else "")
            price = f" · client {_fmt_money(s['pv']['client'])}" if s["pv"] else ""
            st.markdown(f'<div class="ls-pick">{tag}<b>{s.get("carat") or 0:.2f} ct {_esc(shape_group(s["shape"]))} '
                        f'{_esc(colour)} {_esc(norm_clarity(s.get("clarity")))}</b> · {_esc(s.get("lab"))}{_esc(price)}'
                        f'<br><span style="opacity:.8">{_esc(reason)}</span></div>', unsafe_allow_html=True)

    _quote_box(exact + flexed, deps)


def _ai_stone(ref, s, section):
    """Whitelisted fields only — no ids, cert numbers, media or source data."""
    colour, _, fl, meas, r = _spec_bits(s)
    pv = s["pv"] or {}
    return {"ref": ref, "section": section, "shape": shape_group(s["shape"]),
            "carat": s.get("carat"), "colour": colour, "clarity": norm_clarity(s.get("clarity")),
            "cut": norm_grade(s.get("cut")), "polish": norm_grade(s.get("polish")),
            "symmetry": norm_grade(s.get("symmetry")), "fluorescence": fl, "lab": norm_lab(s.get("lab")),
            "lw_ratio": round(r, 2) if r else None, "depth_pct": s.get("depth_pct"),
            "table_pct": s.get("table_pct"),
            "cost_cad": round(pv["cost"]) if pv else None, "client_cad": round(pv["client"]) if pv else None,
            "differs": [b for _, b in s["devs"]]}


def _section(rows, section, view):
    ss = st.session_state
    if not rows:
        st.caption("None.")
        return
    if view == "Cards":
        for s in rows:
            _card(s, section)
        return
    import pandas as pd
    df = pd.DataFrame([{
        "Add": s["sid"] in ss.ls_picked,
        "Image": s.get("image") or None,
        "Shape": shape_group(s["shape"]), "Carat": s.get("carat"),
        "Colour": _spec_bits(s)[0], "Clarity": norm_clarity(s.get("clarity")) or s.get("clarity"),
        "Cut": norm_grade(s.get("cut")), "Pol": norm_grade(s.get("polish")), "Sym": norm_grade(s.get("symmetry")),
        "Fluor": norm_fluor(s.get("flo")) or s.get("flo"), "Lab": s.get("lab"), "Cert": s.get("cert_no"),
        "Measurements": _spec_bits(s)[3], "L/W": round(lw_ratio(s), 2) if lw_ratio(s) else None,
        "Depth %": s.get("depth_pct"), "Table %": s.get("table_pct"),
        "Cost CAD": round(s["pv"]["cost"]) if s["pv"] else None,
        "Cost USD": round(s["pv"]["usd"]) if s["pv"] else None,
        "Client CAD": round(s["pv"]["client"]) if s["pv"] else None,
        "Client CAD/ct": round(s["pv"]["client_ct"]) if s["pv"] and s["pv"]["client_ct"] else None,
        "Differs": "; ".join(b for _, b in s["devs"]),
    } for s in rows])
    if section == "ex":
        df = df.drop(columns=["Differs"])
    edited = st.data_editor(
        df, hide_index=True, key=f"ls_tbl_{section}",
        disabled=[c for c in df.columns if c != "Add"],
        column_config={"Image": st.column_config.ImageColumn("Image"),
                       "Add": st.column_config.CheckboxColumn("Add", help="Add to quote")})
    for s, add in zip(rows, list(edited["Add"])):
        (ss.ls_picked.add if add else ss.ls_picked.discard)(s["sid"])


def _quote_payload(s, show_price):
    pv = s["pv"]
    lab = norm_lab(s.get("lab"))
    fancy = not norm_colour(s.get("color"))
    colour, _, _, meas, r = _spec_bits(s)
    fl = norm_fluor(s.get("flo"))
    if fl and fl != "None" and s.get("flo_col") and _key(s["flo_col"]) not in ("", "NONE", "N"):
        fl = f"{fl} {str(s['flo_col']).title()}"
    cert_data = {
        "shape": shape_group(s["shape"]), "carat": f"{s['carat']:.2f}" if s.get("carat") else "",
        "color": colour if colour != "—" else "", "clarity": norm_clarity(s.get("clarity")) or s.get("clarity", ""),
        "cut": norm_grade(s.get("cut")), "polish": norm_grade(s.get("polish")),
        "symmetry": norm_grade(s.get("symmetry")), "fluorescence": fl,
        "measurements": meas.replace(" mm", ""), "ratio": f"{r:.2f}" if r else "",
    }
    return {
        "cert_last4":    "".join(filter(str.isdigit, s.get("cert_no", "")))[-4:],
        "orig_filename": f"Live Search · {lab} {s.get('cert_no', '')}".strip(),
        "cert_type":     "GIA Colour" if (lab == "GIA" and fancy) else lab,
        # Raw links go no further than save_quote(), which re-hosts them (or drops them) before saving.
        "video_url":     s.get("video") or "",
        "image_url":     s.get("image") or "",
        "pdf_url":       "",
        "price":         str(int(round(pv["client"]))) if (show_price and pv) else "",
        "currency":      "CAD",
        "price_type":    "stone",
        "cert_data":     {k: v for k, v in cert_data.items() if v},
        "ls_ref":        s["sid"],   # internal only; the share page's server strips unknown fields
        "_media":        s.get("media") or None,   # 360/video fields for capture; removed before saving
    }


def _quote_box(rows, deps):
    ss = st.session_state
    st.markdown('<hr class="divider">', unsafe_allow_html=True)
    st.markdown('<div class="section-label">Approve into quote</div>', unsafe_allow_html=True)
    by_sid = {s["sid"]: s for s in rows}
    chosen = [by_sid[sid] for sid in list(ss.ls_picked) if sid in by_sid]
    if not chosen:
        st.caption("Tick **Add to quote** on the stones you want, then choose the client here.")
    else:
        st.caption(f"{len(chosen)} stone(s) selected: " + ", ".join(
            f"{s.get('carat') or 0:.2f} ct {shape_group(s['shape'])} ···{''.join(filter(str.isdigit, s.get('cert_no', '')))[-4:]}"
            for s in chosen))
    q_client = deps["client_selector"]("ls_client", "ls_client_sel")
    a, b = st.columns(2)
    with a:
        exp = st.slider("Link expires (days)", 1, 30, 15, key="ls_exp")
    with b:
        show_price = st.checkbox("Show client price (CAD) on quote", value=True, key="ls_show_price")
    if st.button(f"🔗  Add to quote ({len(chosen)} stone{'s' if len(chosen) != 1 else ''})", type="primary",
                 use_container_width=True, key="ls_add_quote", disabled=not chosen):
        payload = [_quote_payload(s, show_price) for s in chosen]
        with st.spinner("Creating quote and copying media…"):
            link = deps["save_quote"](q_client, payload, exp)
        if link:
            for p in payload:
                deps["add_history"](p["orig_filename"], f"···{p['cert_last4']} → {link}", p["cert_type"], q_client)
            ss.quote_link = link
            ss.ls_last_link = (link, q_client, len(payload))
            _clear_picks()
            st.rerun()
        else:
            st.error("Couldn't save the quote. Check the database connection and try again.")
    if ss.get("ls_last_link"):
        link, who, n = ss.ls_last_link
        st.success(f"✅ Quote created for {who} · {n} diamond(s)")
        st.code(link, language=None)
        if ss.get("media_notes"):
            st.caption("Media note — the quote was saved; these items were handled differently:\n\n"
                       + "\n".join(f"- {m}" for m in ss.media_notes))
