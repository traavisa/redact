"""Live Search tab: find stones from the live stone source, compare, and approve into a quote.

Rules this module follows:
  - The source is never named in the UI, error text or saved data ("Live Search" only).
  - Stones are whitelisted in stone_source.whitelist(); supplier fields are never requested,
    except the stock number ("Stock #", internal screen only, for direct lookup).
  - Certificate file links (introspection) stay internal: the "Cert" link on this screen, and
    on save a verified redacted copy on our own domain (cert_attach.py), or nothing and a note.
  - Search filters come only from the form, built by deterministic code. The AI can
    pre-fill the form ("Read request") but never runs or changes a search.
  - Saved quotes never carry the source's media URLs: save_quote() copies each image/video
    to our own /media/ address or swaps it for an opaque /v/ viewer link (quote_media.py).
"""
import datetime
import html
import json
import logging
import re
import traceback

import streamlit as st

import cert_attach
import pricing
import request_learning as rl
import stone_source as src

log = logging.getLogger("diamond_tools")

AI_MODEL = "claude-sonnet-5"
PARSE_MAX_TOKENS = 4000      # Read request answer; one automatic retry at double this if it is cut off
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
    "ls_type": "Lab-grown", "ls_shapes": [], "ls_ct_min": None, "ls_ct_max": None,
    "ls_col_mode": "White (D–Z)", "ls_col": ("D", "Z"), "ls_fancy_col": "Yellow", "ls_fancy_int": [],
    "ls_cla": ("FL", "I3"), "ls_cut": "Any", "ls_pol": "Any", "ls_sym": "Any",
    "ls_flo": [], "ls_labs": ["IGI"], "ls_pr_min": None, "ls_pr_max": None, "ls_pr_basis": "My cost",
    "ls_ratio_min": None, "ls_ratio_max": None, "ls_depth_min": None, "ls_depth_max": None,
    "ls_table_min": None, "ls_table_max": None, "ls_as_grown": False,
    "ls_length_min": None, "ls_length_max": None, "ls_width_min": None, "ls_width_max": None,
    "ls_height_min": None, "ls_height_max": None,               # mm; "height" = the stone's depth in mm
    "ls_hide_novid": True, "ls_hide_noimg": True,
}
FLEX_DEFAULTS = {"ls_fx_col": False, "ls_fx_cla": False, "ls_fx_ct": False, "ls_fx_ct_tol": "±0.05",
                 "ls_fx_budget": False, "ls_fx_lab": False, "ls_fx_flo": False}


def _init_state(default_markup, default_rate):
    ss = st.session_state
    for k, v in {**DEFAULTS, **FLEX_DEFAULTS}.items():
        if k not in ss:
            ss[k] = list(v) if isinstance(v, list) else v
    ss.setdefault("ls_markup", float(default_markup))
    ss.setdefault("ls_rate", float(default_rate))
    ss.setdefault("ls_notes", {})
    ss.setdefault("ls_picked", set())
    ss.setdefault("ls_over", {})          # sid -> what was typed in the stone's "Client price (CAD)" box
    ss.setdefault("ls_cert_up", {})       # sid -> {"name", "data"}: a certificate PDF uploaded for the stone
    ss.setdefault("ls_cert_chk", {})      # (sid, source) -> (ok, reason, layout): certificate pre-checks


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
        limit = max_tokens
        for attempt in (1, 2):
            r = client.messages.create(model=AI_MODEL, max_tokens=limit, system=sys_text,
                                       messages=msgs, **extra)
            u = getattr(r, "usage", None)
            log.info("live-search ai: stop_reason=%s input_tokens=%s output_tokens=%s max_tokens=%s attempt=%s",
                     r.stop_reason, getattr(u, "input_tokens", None), getattr(u, "output_tokens", None),
                     limit, attempt)
            if r.stop_reason == "refusal":
                raise AIError("The assistant declined this request.")
            if r.stop_reason == "max_tokens" and attempt == 1:
                limit = max_tokens * 2          # cut off: one automatic retry with more room
                continue
            if r.stop_reason == "max_tokens":
                raise AIError("Couldn't read this request, please try again.")
            break
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
    return {"type": "object", "additionalProperties": False, "required": ["value"],
            "properties": {"value": v, "note": {"type": "string"}}}


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
    ("length_min", "number", None, "minimum length in mm (the longer side)"),
    ("length_max", "number", None, "maximum length in mm (the longer side)"),
    ("width_min", "number", None, "minimum width in mm (the shorter side)"),
    ("width_max", "number", None, "maximum width in mm (the shorter side)"),
    ("height_min", "number", None, "minimum depth (height) in mm — NOT depth %"),
    ("height_max", "number", None, "maximum depth (height) in mm — NOT depth %"),
]
PARSE_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": [],      # compact: only fields with a value are returned
                "properties": {f[0]: _field_schema(f[1], f[2]) for f in PARSE_FIELDS}}
PARSE_SYSTEM = (
    "You read a jeweller's diamond request and map it onto a fixed search form for a diamond "
    "wholesaler in Canada. Be COMPACT: return a JSON object containing ONLY the form fields that have a "
    "value. Omit every field the request does not state and that cannot be reasonably inferred "
    "(never return nulls, blanks or placeholders, and never invent values). Each returned field is "
    "{value, note}.\n"
    "- Leave note out when the request gives the value directly (e.g. 'GIA', 'oval', 'G colour', 'under $8k').\n"
    "- Add a note when you had to turn vague wording into a value (e.g. '1.5ish' -> carat 1.40–1.60, "
    "'near colourless' -> G–J, 'eye clean' -> SI1 or better, 'no fluoro' -> None). A field with a note is "
    "shown to the user as interpreted. Keep every note under 10 words and end it with ', confirm' "
    "(e.g. '1.5ish → 1.40–1.60, confirm').\n"
    "- Single values: 'G colour' means colour_best=G and colour_worst=G; 'G or better' means "
    "colour_best=D, colour_worst=G. Same pattern for clarity.\n"
    "- Prices are in Canadian dollars. A budget from a jeweller for their customer is usually the "
    "client price; only use 'cost' if the request says it's the dealer's cost. If unclear, "
    "leave price_basis unstated.\n"
    "- Sizes are in millimetres: length is the longer side, width the shorter side, height (depth in mm) "
    "the stone's depth. 'at least 7mm wide' -> width_min=7 (stated, not interpreted); 'up to 9mm long' -> "
    "length_max=9. 'X x Y' or 'X by Y' means length X and width Y; 'around/about/approx' -> that size "
    "+/-0.2mm (interpreted; e.g. 'around 9 x 7' -> length 8.8-9.2, width 6.8-7.2, note '9 x 7 mm +/-0.2, confirm'); "
    "the same applies to a third number (X x Y x Z = length x width x depth in mm, so 'approx 5.14x4.97x3.50mm' "
    "gives length 4.94-5.34, width 4.77-5.17, height 3.30-3.70). "
    "A single size with no 'x' (e.g. '6.5mm round') is both length and width, +/-0.15 (interpreted). "
    "Depth in percent stays depth_min/depth_max; only a depth in mm is height_min/height_max.\n"
    "- IGI reports are usually lab-grown and GIA/HRD/AGS reports usually natural: if a lab is named but the "
    "type is not, set type from that with a short note ending ', confirm'.\n"
    "- Output JSON only.")


def ai_parse_request(api_key, text, rules="", examples=None):
    """rules: the dealer's house rules (authoritative); examples: saved worked examples
    ({"request_text", "criteria"} rows). Both are optional: without them it parses as before."""
    system = rl.system_prompt(PARSE_SYSTEM, rules,
                              rl.format_examples(examples or [], criteria_for_prompt))
    return _ai_call(api_key, system, "Client request:\n\n" + text, PARSE_SCHEMA, max_tokens=PARSE_MAX_TOKENS)


# Form fields the parser fills, by the friendly name used in saved examples and the correction log.
FIELD_NAMES = {
    "ls_type": "type", "ls_shapes": "shapes", "ls_ct_min": "carat_min", "ls_ct_max": "carat_max",
    "ls_col_mode": "colour_mode", "ls_col": "colour_range", "ls_fancy_col": "fancy_colour",
    "ls_fancy_int": "fancy_intensity", "ls_cla": "clarity_range", "ls_cut": "cut_min", "ls_pol": "polish_min",
    "ls_sym": "symmetry_min", "ls_flo": "fluorescence", "ls_labs": "labs", "ls_pr_min": "price_min",
    "ls_pr_max": "price_max", "ls_pr_basis": "price_basis", "ls_ratio_min": "ratio_min",
    "ls_ratio_max": "ratio_max", "ls_depth_min": "depth_min", "ls_depth_max": "depth_max",
    "ls_table_min": "table_min", "ls_table_max": "table_max", "ls_as_grown": "as_grown_only",
    "ls_length_min": "length_min", "ls_length_max": "length_max", "ls_width_min": "width_min",
    "ls_width_max": "width_max", "ls_height_min": "height_min", "ls_height_max": "height_max",
}


def _norm(v):
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, float):
        return round(v, 4)
    return v


def criteria_snapshot():
    """The current value of every parsed form field (JSON-friendly)."""
    ss = st.session_state
    return {k: _norm(ss.get(k, DEFAULTS.get(k))) for k in FIELD_NAMES}


def criteria_for_prompt(snap):
    """A saved snapshot as {friendly name: value}, leaving out fields still at their default."""
    return {FIELD_NAMES[k]: v for k, v in snap.items()
            if k in FIELD_NAMES and v != _norm(DEFAULTS.get(k)) and v not in (None, [], "")}


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
def apply_parse(result, rules_active=False, request_text=""):
    """Validated AI output -> form widget state. Unknown/invalid values are ignored.
    Returns (n_filled, notes). Never triggers a search. Remembers what was parsed (to spot later
    edits). rules_active: the house rules were in the prompt (natural with no lab then means GIA)."""
    ss = st.session_state
    for k, v in DEFAULTS.items():                 # a new request starts from a clean form
        ss[k] = list(v) if isinstance(v, list) else v
    notes, filled, set_keys = {}, 0, []

    def get(name):
        f = result.get(name) if isinstance(result, dict) else None
        if not isinstance(f, dict) or not f.get("stated", True) or f.get("value") is None:
            return None, False, ""
        note = str(f.get("note") or "")[:160]
        return f["value"], bool(f.get("interpreted", bool(note))), note

    def setk(key, value, interp, note):
        nonlocal filled
        ss[key] = value
        filled += 1
        set_keys.append(key)
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
    for dim in ("length", "width", "height"):
        num(f"{dim}_min", f"ls_{dim}_min", 0.5, 100)
        num(f"{dim}_max", f"ls_{dim}_max", 0.5, 100)
        lo, hi = ss[f"ls_{dim}_min"], ss[f"ls_{dim}_max"]
        if lo and hi and lo > hi:
            ss[f"ls_{dim}_min"], ss[f"ls_{dim}_max"] = hi, lo
    # The IGI default only makes sense for lab-grown: a natural request that names no lab searches any lab
    if ss.ls_type == "Natural" and not get("labs")[0]:
        if rules_active:      # house default: natural with no lab named -> GIA
            ss.ls_labs = ["GIA"]
            notes["ls_labs"] = "natural, no lab named → GIA, defaults rule"
        else:
            ss.ls_labs = []
    ss["ls_notes"] = notes
    ss["ls_parsed"] = criteria_snapshot()                 # to see later what was edited
    ss["ls_parsed_keys"] = [k for k in dict.fromkeys(set_keys) if k in FIELD_NAMES]
    ss["ls_parsed_req"] = request_text
    ss["ls_corr_done"] = {}
    ss["ls_ex_saved"] = None
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
        "length": (ss.ls_length_min, ss.ls_length_max), "width": (ss.ls_width_min, ss.ls_width_max),
        "height": (ss.ls_height_min, ss.ls_height_max),
        "as_grown": bool(ss.ls_as_grown) and ss.ls_type == "Lab-grown",
        "hide_vid": bool(ss.ls_hide_novid), "hide_img": bool(ss.ls_hide_noimg),
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
# used when introspection shows a type the criterion can be sent as (see stone_source._filter_spec).
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
    "Length": ["length", "lengths", "length_mm"],
    "Width": ["width", "widths", "width_mm"],
    "Depth (mm)": ["height", "height_mm", "depth_mm", "depthMm"],       # plus bare "depth", see _pick
    "As-grown": ["as_grown", "asGrown", "is_as_grown", "treated", "is_treated", "treatment",
                 "treatments", "lab_grown_treatment"],
}
# Flex option -> the criterion it widens (for "was this filtered server-side?")
FLEX_CRITERION = {"col": "Colour", "cla": "Clarity", "ct": "Carat", "budget": "Price",
                  "lab": "Lab", "flo": "Fluorescence"}
_OPEN_HI = {"Carat": 100.0, "Price": 100_000_000, "L/W ratio": 100.0, "Depth %": 100.0, "Table %": 100.0,
            "Length": 100.0, "Width": 100.0, "Depth (mm)": 100.0}


def _pick(have, crit, kinds):
    names = list(FILTER_FIELDS[crit])
    if crit == "Depth (mm)":
        # a bare "depth" filter is only the millimetre depth when a separate depth-% filter exists
        pct = [n for n in FILTER_FIELDS["Depth %"] if have.get(n) and have[n]["kind"] in ("range", "ranges")]
        if pct and "depth" not in pct:
            names.append("depth")
    for name in names:
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
    for crit, (lo, hi) in (("L/W ratio", c["ratio"]), ("Depth %", c["depth"]), ("Table %", c["table"]),
                           ("Length", c.get("length", (None, None))), ("Width", c.get("width", (None, None))),
                           ("Depth (mm)", c.get("height", (None, None)))):
        if lo or hi:
            f, spec = _pick(have, crit, ("range", "ranges"))
            if f:
                server(crit, f, _range_send(spec, lo or 0.0, hi or _OPEN_HI[crit]))
            else:
                client(crit)
    # Media filter (hide stones with no image / no video or 360)
    mp = media_plan(schema, c.get("hide_vid"), c.get("hide_img"))
    if c.get("hide_img"):
        if mp["image"]:
            server("Media: image", mp["image"], True, "hide stones with no image")
        else:
            client("Media: image", "no has-image filter in the schema")
    if c.get("hide_vid"):
        if mp["video"]:
            plan.append(("Media: video/360", "server",
                         " + ".join(f"{f}: true" for f in mp["video"]) + " — one search each, merged (cheapest first)"))
        else:
            client("Media: video/360", "no filter pair in the schema that means '360 or video'")
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
    # Dimensions in mm (hard), as listed for the stone (the same fields the server-side filters use):
    # length is normally the longer side, width the shorter, height = the depth in mm
    for (lo, hi), val, label in ((c.get("length", (None, None)), s.get("length"), "Length"),
                                 (c.get("width", (None, None)), s.get("width"), "Width"),
                                 (c.get("height", (None, None)), s.get("depth_mm"), "Depth (mm)")):
        if lo or hi:
            if val is None or (lo and val < lo - 1e-9) or (hi and val > hi + 1e-9):
                return fail(label)
    # As-grown (hard when the data says so; unknown is flagged on the card)
    if c["as_grown"] and as_grown_status(s) is False:
        return fail("As-grown only")
    return False, devs


FILTER_ORDER = ["Shape", "Carat range", "Fancy colour", "Fancy intensity", "Colour range", "Clarity range",
                "Cut min", "Polish min", "Symmetry min", "Fluorescence", "Lab", "Price", "Price min",
                "Price max", "L/W ratio", "Depth %", "Table %", "Length", "Width", "Depth (mm)", "As-grown only"]


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


# ── Media filter ──────────────────────────────────────────────────────────────
# Boolean query filters that mean "has an image" / "has a 360" / "has a video", by name.
MEDIA_FLAGS = {"image": ["has_image", "hasImage", "has_images", "with_image"],
               "v360": ["has_v360", "hasV360", "has_360", "with_v360"],
               "video": ["has_video", "hasVideo", "has_videos", "with_video"]}
_MEDIA_URL_RE = re.compile(r"^https?://", re.I)


def _media_value(v):
    """True if a media field holds something usable (a link, or an object/list carrying one)."""
    if isinstance(v, str):
        return bool(_MEDIA_URL_RE.match(v.strip()))
    if isinstance(v, dict):
        return any(_media_value(x) for k, x in v.items() if "url" in str(k).lower())
    if isinstance(v, list):
        return any(_media_value(x) for x in v)
    return False


def has_video_360(s):
    """A v360 frame set, a video file or a viewer URL."""
    if _media_value(s.get("video") or ""):
        return True
    m = dict(s.get("media") or {})
    m.update({"certificate." + k: v for k, v in (m.pop("certificate", None) or {}).items()})
    return any(_media_value(v) for k, v in m.items()
               if any(w in k.lower() for w in ("v360", "360", "video", "spin", "frame")))


def has_image(s):
    """An image URL (the stone's, or the certificate's still)."""
    return _media_value(s.get("image") or "") or _media_value(((s.get("media") or {}).get("certificate") or {}).get("image") or "")


def media_plan(schema, hide_vid, hide_img):
    """Which media checks the API can do. image: a has-image flag name or None. video: a list
    of flags whose searches together give exactly "has a 360 or a video" (one search per
    flag, merged), or None (checked here after fetching)."""
    have = schema.get("filters") or {}
    pick = lambda names: next((n for n in names if (have.get(n) or {}).get("kind") == "bool"), None)
    img = pick(MEDIA_FLAGS["image"]) if hide_img else None
    vid = None
    if hide_vid:
        v360, video = pick(MEDIA_FLAGS["v360"]), pick(MEDIA_FLAGS["video"])
        if v360 and video:
            vid = [v360, video]
    return {"image": img, "video": vid}


def media_keep(stones, hide_vid, hide_img):
    """(kept, removed) — removed counts stones hidden for no video/360 and for no image."""
    kept, rv, ri = [], 0, 0
    for s in stones:
        if hide_vid and not has_video_360(s):
            rv += 1
            continue
        if hide_img and not has_image(s):
            ri += 1
            continue
        kept.append(s)
    return kept, {"no_video_360": rv, "no_image": ri}


def media_badges(s):
    v, i = has_video_360(s), has_image(s)
    if not v and not i:
        return ["No media"]
    return [] if v and i else ["No video/360"] if i else ["No image"]


def availability_badge(s):
    a = _key(s.get("availability"))
    if not a or a in ("AVAILABLE", "INSTOCK", "YES", "TRUE"):
        return ""
    if "HOLD" in a or "MEMO" in a or "RESERV" in a:
        return "On hold"
    return "Unavailable" if any(w in a for w in ("NOT", "UNAVAIL", "SOLD", "NO")) else str(s["availability"]).replace("_", " ").title()


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
MODES = ["Criteria search", "Look up stones"]


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
    st.session_state["ls_over"] = {}                   # per-stone price overrides and uploaded certificates go with them
    st.session_state["ls_cert_up"] = {}
    for k in [k for k in st.session_state if str(k).startswith(("ls_pk_", "ls_tbl_", "ls_ov_", "ls_ovr_", "ls_cup_", "ls_cupx_"))]:
        del st.session_state[k]


NEW_SEARCH_CLEARS = ("ls_results", "ls_picks_ai", "ls_diag", "ls_lookup", "ls_lookup_diag", "ls_parse_msg",
                     "ls_parse_err", "ls_last_link", "ls_parsed", "ls_parsed_keys", "ls_parsed_req",
                     "ls_corr_done", "ls_ex_saved")


def _new_search():
    """The "New search" button: everything on the Live Search tab back to how it opens — the pasted
    request, all criteria (and flex boxes) at their defaults, results, selections, the look-up box and
    its report, top picks, and notes. Markup % and the USD→CAD rate, the chosen mode and the client
    picked for quotes stay as they are."""
    ss = st.session_state
    for k, v in {**DEFAULTS, **FLEX_DEFAULTS}.items():
        ss[k] = list(v) if isinstance(v, list) else v
    ss["ls_req"] = ""
    ss["ls_lk_text"] = ""
    ss["ls_notes"] = {}
    ss["ls_over"] = {}                                 # per-stone price overrides
    ss["ls_cert_up"] = {}                              # uploaded certificates
    ss["ls_cert_chk"] = {}
    for k in [k for k in ss if str(k).startswith(("ls_ov_", "ls_ovr_", "ls_cup_", "ls_cupx_"))]:
        del ss[k]
    ss["ls_ver"] = ss.get("ls_ver", 0) + 1            # the range sliders are re-created at their defaults
    for k in NEW_SEARCH_CLEARS:
        ss[k] = None
    ss.pop("ls_autosearch", None)
    _clear_picks()
    for k in [k for k in ss if str(k).startswith(("ls_sort", "ls_view", "ls_show_n"))]:
        del ss[k]


# ── House rules, saved examples, correction log (Supabase; see request_learning.py) ───────────────
def _store(deps):
    sb = deps.get("supabase")
    return rl.Store(*sb) if sb else None


def _load_rules(deps):
    """Read the saved rules into session state; returns True when they could be read."""
    ss = st.session_state
    try:
        store = _store(deps)
        if store is None:
            raise RuntimeError("no database configured")
        saved = store.load_rules()
        ss["ls_rules_saved"] = rl.DEFAULT_RULES if saved is None else saved
        ss["ls_rules_ok"] = True
    except Exception as e:
        log.warning("live-search: request rules could not be loaded: %s", e)
        ss["ls_rules_saved"] = rl.DEFAULT_RULES
        ss["ls_rules_ok"] = False
    return ss["ls_rules_ok"]


def _load_examples(deps):
    ss = st.session_state
    try:
        store = _store(deps)
        if store is None:
            raise RuntimeError("no database configured")
        ss["ls_examples"] = store.load_examples()
        ss["ls_examples_ok"] = True
    except Exception as e:
        log.warning("live-search: saved examples could not be loaded: %s", e)
        ss["ls_examples"], ss["ls_examples_ok"] = [], False
    return ss["ls_examples_ok"]


def _load_corrections(deps):
    ss = st.session_state
    try:
        ss["ls_corr_rows"] = _store(deps).load_corrections()
        ss["ls_corr_ok"] = True
    except Exception as e:
        log.warning("live-search: correction log could not be loaded: %s", e)
        ss["ls_corr_rows"], ss["ls_corr_ok"] = [], False


def _learn_init(deps):
    """Once per session: load rules, examples and the correction log (a failure never blocks anything)."""
    ss = st.session_state
    if "ls_rules_ok" not in ss:
        _load_rules(deps)
        _load_examples(deps)
        _load_corrections(deps)
    ss.setdefault("ls_rules_ta", ss.get("ls_rules_saved", rl.DEFAULT_RULES))


def _parse_inputs(deps, text):
    """(rules, similar examples, warnings) for one parse. Whatever can't be loaded is left out,
    logged, and reported in the warnings; the request is then read without it."""
    ss = st.session_state
    warns = []
    if not ss.get("ls_rules_ok"):
        if _load_rules(deps) and ss.get("ls_rules_ta") == rl.DEFAULT_RULES:
            ss["ls_rules_ta"] = ss["ls_rules_saved"]
    rules = ss.get("ls_rules_ta", "") if ss.get("ls_rules_ok") else ""
    if not ss.get("ls_rules_ok"):
        warns.append("request rules couldn't be loaded")
    if not _load_examples(deps):
        warns.append("saved examples couldn't be loaded")
    return rules, rl.similar_examples(text, ss.get("ls_examples") or []), warns


def _edited_fields():
    """[(key, parsed, now)] for the fields the parser filled that now hold something else."""
    ss = st.session_state
    parsed = ss.get("ls_parsed")
    if not parsed:
        return []
    now = criteria_snapshot()
    return [(k, parsed[k], now[k]) for k in (ss.get("ls_parsed_keys") or []) if now[k] != parsed[k]]


def _log_corrections(deps):
    """Record (once per value) each parser-filled field that was changed before searching."""
    ss = st.session_state
    done = ss.get("ls_corr_done")
    if done is None:
        return
    rows = []
    for k, was, now in _edited_fields():
        sig = json.dumps(now)
        if done.get(k) != sig:
            rows.append((k, sig, {"field": FIELD_NAMES[k], "parsed_value": json.dumps(was),
                                  "final_value": sig, "request_text": ss.get("ls_parsed_req") or "",
                                  "created_at": rl.now()}))
    if not rows:
        return
    try:
        _store(deps).add_corrections([r[2] for r in rows])
        done.update({k: sig for k, sig, _ in rows})
    except Exception as e:
        log.warning("live-search: corrections could not be logged: %s", e)


def _save_example(deps):
    ss = st.session_state
    snap = criteria_snapshot()
    try:
        _log_corrections(deps)
        _store(deps).add_example(ss.get("ls_parsed_req") or "", snap)
        ss["ls_ex_saved"] = snap
        ss["ls_ex_msg"] = "Saved as an example."
        _load_examples(deps)
    except Exception as e:
        log.warning("live-search: example could not be saved: %s", e)
        ss["ls_ex_msg"] = "Couldn't save the example (database problem). Try again."


def _delete_example(deps, ex_id):
    try:
        _store(deps).delete_example(ex_id)
        _load_examples(deps)
    except Exception as e:
        log.warning("live-search: example could not be deleted: %s", e)
        st.session_state["ls_ex_msg"] = "Couldn't delete the example (database problem)."


def _save_rules(deps):
    ss = st.session_state
    try:
        _store(deps).save_rules(ss.get("ls_rules_ta", ""))
        ss["ls_rules_saved"], ss["ls_rules_ok"] = ss.get("ls_rules_ta", ""), True
        ss["ls_rules_msg"] = "Rules saved."
    except Exception as e:
        log.warning("live-search: request rules could not be saved: %s", e)
        ss["ls_rules_msg"] = "Couldn't save the rules (database problem). Your edits are still in the box."


def _reset_rules():
    st.session_state["ls_rules_ta"] = rl.DEFAULT_RULES
    st.session_state["ls_rules_msg"] = "Default rules restored in the box. Click Save rules to keep them."


def _settings_panel(deps):
    ss = st.session_state
    with st.expander("Request settings: rules, examples, corrections"):
        st.markdown("**Request rules**")
        st.caption("Included in every Read request as authoritative rules that override the defaults.")
        if not ss.get("ls_rules_ok"):
            st.warning("The saved rules couldn't be loaded, so requests are read without them for now. "
                       "The box shows the standard rules; Save rules to store them.")
        st.text_area("Request rules", key="ls_rules_ta", height=420, label_visibility="collapsed")
        a, b, _ = st.columns([1, 1, 3])
        with a:
            if st.button("Save rules", type="primary", key="ls_rules_save"):
                _save_rules(deps)
                st.rerun()
        with b:
            st.button("Reset to standard", key="ls_rules_reset", on_click=_reset_rules)
        if ss.get("ls_rules_msg"):
            st.caption(ss.ls_rules_msg)

        st.markdown("**Saved examples**")
        if not ss.get("ls_examples_ok"):
            st.warning("Saved examples couldn't be loaded.")
        elif not ss.get("ls_examples"):
            st.caption("None yet. After Read request, edit a field and use “Save as example”.")
        if ss.get("ls_ex_msg"):
            st.caption(ss.ls_ex_msg)
        for ex in ss.get("ls_examples") or []:
            a, b = st.columns([6, 1])
            with a:
                st.markdown(f"“{_esc(ex.get('request_text'))}”")
                st.caption(json.dumps(criteria_for_prompt(ex.get("criteria") or {}), ensure_ascii=False))
            with b:
                st.button("Delete", key=f"ls_exdel_{ex.get('id')}", on_click=_delete_example,
                          args=(deps, ex.get("id")))

        st.markdown("**Common corrections**")
        st.caption("Fields Read request filled that you then changed before searching.")
        if st.button("Refresh", key="ls_corr_refresh"):
            _load_corrections(deps)
        if not ss.get("ls_corr_ok"):
            st.warning("The correction log couldn't be loaded.")
        pairs, fields = rl.top_corrections(ss.get("ls_corr_rows") or [])
        if pairs:
            st.dataframe([{"Field": f, "Parsed": was, "You changed it to": now, "Times": n}
                          for (f, was, now), n in pairs], hide_index=True, use_container_width=True)
            st.caption("By field: " + ", ".join(f"{f} ×{n}" for f, n in fields))
        elif ss.get("ls_corr_ok"):
            st.caption("No corrections recorded yet.")


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
    url, user, pw = (src.setting(get, k) for k in ("url", "user", "password"))
    missing = [n for n, v in (("LS_API_URL", url), ("LS_USERNAME", user), ("LS_PASSWORD", pw),
                              ("DEFAULT_USD_CAD", get("DEFAULT_USD_CAD"))) if not v]
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
    divisor = _f(src.setting(get, "divisor"), 100.0) or 100.0
    _init_state(_f(get("DEFAULT_MARKUP_PCT"), 0.0) or 0.0, rate_env)
    ss = st.session_state
    st.markdown(CSS, unsafe_allow_html=True)
    _amber_css()
    client = src.Client(url, user, pw, ss)

    a, b = st.columns([3, 1])
    with a:
        mode = st.radio("Mode", MODES, horizontal=True, key="ls_mode", label_visibility="collapsed")
    with b:
        st.button("New search", type="primary", key="ls_new_search", on_click=_new_search,
                  use_container_width=True,
                  help="Clears the request, criteria, results, selections, look-up and notes. "
                       "Markup % and the USD → CAD rate stay.")
    if mode == MODES[1]:
        _lookup_mode(client, ai_key, divisor, deps)
        return
    _learn_init(deps)

    # ── A. Request parsing ────────────────────────────────────────────────
    st.markdown('<div class="section-label">Client request (optional)</div>', unsafe_allow_html=True)
    req = st.text_area("Paste client request", key="ls_req", height=100,
                       placeholder="e.g. Oval 1.5ish, G-H, VS, GIA, budget around $9k to the client")
    if st.button("Read request", type="primary", key="ls_read", disabled=not (ai_key and req.strip())):
        with st.spinner("Reading the request…"):
            try:
                rules, examples, warns = _parse_inputs(deps, req.strip())
                n, notes = apply_parse(ai_parse_request(ai_key, req.strip(), rules=rules, examples=examples),
                                       rules_active=bool(rules.strip()), request_text=req.strip())
                ss.ls_parse_msg = (f"Filled {n} field(s) from the request"
                                   + (f" — {len(notes)} interpreted (amber): please confirm." if notes else ".")
                                   + (f" Read without: {', '.join(warns)}." if warns else "")
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
    _settings_panel(deps)
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

    with st.expander("Dimensions in mm (length, width, depth)",
                     expanded=any(ss.get(k) for k in ("ls_length_min", "ls_length_max", "ls_width_min",
                                                      "ls_width_max", "ls_height_min", "ls_height_max"))):
        st.caption("Stones outside these are left out (no flex). Length and width are as listed for the stone "
                   "(length is normally the longer side).")
        for label, lo, hi in (("Length", "ls_length_min", "ls_length_max"), ("Width", "ls_width_min", "ls_width_max"),
                              ("Depth (height)", "ls_height_min", "ls_height_max")):
            a, b = st.columns(2)
            with a:
                st.number_input(f"{label} min (mm)", min_value=0.0, step=0.1, format="%.2f", key=lo); _note(lo)
            with b:
                st.number_input(f"{label} max (mm)", min_value=0.0, step=0.1, format="%.2f", key=hi); _note(hi)

    a, b = st.columns(2)
    with a:
        st.checkbox("Hide stones with no video/360", key="ls_hide_novid")
    with b:
        st.checkbox("Hide stones with no image", key="ls_hide_noimg")

    # ── C. Pricing ───────────────────────────────────────────────────────
    rate, markup = _pricing_inputs()

    # ── D. Search ────────────────────────────────────────────────────────
    crit = read_criteria()
    fx = flex_state()
    auto = ss.pop("ls_autosearch", False)       # a flex offer button asks for a fresh search
    snap = criteria_snapshot() if ss.get("ls_parsed") else None
    if snap is not None and snap != ss.ls_parsed and snap != ss.get("ls_ex_saved"):
        st.button("Save as example", key="ls_ex_save", on_click=_save_example, args=(deps,),
                  help="Stores the original request and the criteria as they are now, to guide "
                       "future readings of similar requests.")
    if ss.get("ls_ex_msg") and ss.get("ls_parsed"):
        st.caption(ss.ls_ex_msg)
    searching = st.button("🔍  Search", type="primary", use_container_width=True, key="ls_search")
    if searching or auto:
        _log_corrections(deps)
        client.reset_diag()
        q, plan, schema, error, retry, minfo = None, [], {}, None, None, None
        with st.spinner("Searching live stones…"):
            try:
                schema = client.schema()
                q, plan = server_query(crit, fx, schema, rate, markup, divisor)
                mp = media_plan(schema, crit["hide_vid"], crit["hide_img"])
                union = {}

                def fetch(query):
                    """One search, or (video/360 filter server-side) one per flag, merged."""
                    if not mp["video"]:
                        return client.search(query, MAX_SCAN)
                    merged, cap, per = {}, False, {}
                    for f in mp["video"]:
                        got, _ = client.search({**query, f: True}, MAX_SCAN)
                        cap = cap or client.diag["cap_hit"]
                        per[f] = len(got)
                        for x in got:
                            merged.setdefault(x["sid"], x)
                    rows = sorted(merged.values(), key=lambda x: (x.get("price_raw") is None, x.get("price_raw") or 0))
                    client.diag["cap_hit"] = cap or len(rows) > MAX_SCAN
                    client.diag["real_total"] = None if client.diag["cap_hit"] else len(rows)
                    union.update(per=per, merged=len(rows))
                    return rows[:MAX_SCAN], client.diag["real_total"]

                stones, total = fetch(q)
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
                    stones, total = fetch(sent)
                minfo = _media_server_counts(client, mp, sent, total, union)
                ss.ls_results = {"q": q, "vq": mp["video"], "sent": sent, "stones": stones, "total": total,
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
            shown, removed = media_keep(ss.ls_results["stones"], crit["hide_vid"], crit["hide_img"])
            ex, fl, _ = bucket(shown, crit, fx, rate, markup, divisor, stats)
            stats["media_removed"] = removed
            priced = [r for r in ex + fl if r["pv"]] or [
                {**s, "devs": None} for s in ss.ls_results["stones"] if s.get("price_raw") is not None]
            if priced:
                sample = min(priced, key=lambda r: r["price_raw"])
        ss.ls_diag = _build_diag(client.diag, q, plan, retry, schema, error, ss.ls_results, stats,
                                 sample, rate, markup, divisor, minfo)
        _log_diag(ss.ls_diag)

    res = ss.get("ls_results")
    if res:
        try:
            sch = client.schema()
            current = (server_query(crit, fx, sch, rate, markup, divisor)[0],
                       media_plan(sch, crit["hide_vid"], crit["hide_img"])["video"])
        except Exception:
            current = (res["q"], res.get("vq"))
        if current != (res["q"], res.get("vq")):
            st.warning("The criteria changed since the last search. Click **Search** to refresh the results.")
        else:
            # Media-hidden stones are removed first: they never count as "Outside your criteria".
            shown, _ = media_keep(res["stones"], crit["hide_vid"], crit["hide_img"])
            exact, flexed, adds = bucket(shown, crit, fx, rate, markup, divisor)
            _results(exact, flexed, adds, res, crit, fx, ai_key, deps)


def _pricing_inputs():
    a, b = st.columns(2)
    with a:
        st.number_input("Markup %", min_value=0.0, step=1.0, format="%.1f", key="ls_markup",
                        help="Client price = cost × (1 + markup)")
    with b:
        st.number_input("USD → CAD rate", min_value=0.01, step=0.01, format="%.4f", key="ls_rate",
                        help="Live Search prices arrive in USD; they're converted to CAD with this rate.")
    return float(st.session_state.ls_rate), float(st.session_state.ls_markup)


def _media_server_counts(client, mp, sent, total, union):
    """How many stones the server-side media filters removed, from the API's own counts
    (None = not counted)."""
    info = {"image_flag": mp["image"], "video_flags": mp["video"], "server_removed": None,
            "union": union or None}
    if not (mp["image"] or mp["video"]):
        return info
    base = {k: v for k, v in sent.items() if k != mp["image"]}
    try:
        n_base = client.count(base)
    except Exception:
        n_base = None
    info["count_without_media_filters"] = n_base
    if isinstance(n_base, int) and isinstance(total, int):
        info["server_removed"] = max(0, n_base - total)
    return info


# ── Search diagnostics: server log only ([live-search] / [live-search-lookup]) ──────
_GRADE_ABBR = {"Excellent": "EX", "Very Good": "VG", "Good": "G", "Fair": "F", "Poor": "P"}


def _mask_numbers(text):
    """Report / stock numbers (6+ digits) shortened to their last 4 digits."""
    return re.sub(r"[A-Za-z]{0,3}\d{6,}", lambda m: "···" + m.group(0)[-4:], str(text or ""))


def _cert_file_diag(schema, stones):
    """Certificate file fields (introspection) and how many stones carry one. Hosts masked."""
    media = schema.get("media") or {}
    with_file = [x for x in stones or [] if x.get("cert_file")]
    ex = with_file[0]["cert_file"] if with_file else None
    return {"fields": [f"{w}.{n}: {t}" + (f" — {d}" if d else "") for w, n, t, d in media.get("cert_files") or []],
            "stones": len(stones or []), "with_file": len(with_file),
            "example": _mask_numbers(src._shape(ex)) if ex else None,
            "pdf_like": sum(1 for x in with_file if re.search(r"\.pdf(\?|$)", x["cert_file"], re.I)),
            "stock_fields": list(media.get("stock_fields") or []),
            "cert_filter": media.get("cert_filter"), "stock_filter": media.get("stock_filter")}


def _build_diag(cd, q, plan, retry, schema, error, res, stats, sample_stone, rate, markup, divisor, minfo=None):
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
        "media_filter": minfo,
        "cert_files": _cert_file_diag(schema, res["stones"] if res else []),
    }


def _log_diag(d, tag="live-search"):
    """One line per search in the server log (Render)."""
    try:
        print(f"[{tag}] " + json.dumps(d, default=str, separators=(",", ":")), flush=True)
    except Exception:
        pass


def _code(v):
    return str(v).replace("`", "'")


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
            links = []
            if s.get("video"):
                links.append(f'<a href="{_esc(s["video"])}" target="_blank" rel="noreferrer">▶ Video</a>')
            if s.get("cert_file"):   # internal: view the original before approving
                links.append(f'<a href="{_esc(s["cert_file"])}" target="_blank" rel="noreferrer">📄 Cert</a>')
            if links:
                st.markdown(f'<div style="font-size:12px;">{" · ".join(links)}</div>', unsafe_allow_html=True)
        with c2:
            ct = f"{s['carat']:.2f} ct " if s.get("carat") else ""
            badges = "".join(f'<span class="ls-badge">{_esc(b)}</span>' for _, b in s["devs"])
            if availability_badge(s):
                badges += f'<span class="ls-badge">{_esc(availability_badge(s))}</span>'
            for b in s.get("xbadges") or []:
                badges += f'<span class="ls-badge grey">{_esc(b)}</span>'
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
                f'{" · " + _esc(meas) if meas else ""}'
                f'{" · Stock # " + _esc(s["stock_no"]) if s.get("lookup") and s.get("stock_no") else ""}</div>'
                f'<div class="ls-line">{_esc(props)}</div>{badges}', unsafe_allow_html=True)
        with c3:
            if pv:
                usd_ct = f" · US${pv['usd_ct']:,.0f}/ct" if pv["usd_ct"] else ""
                f = _final(s)
                ct = s.get("carat") or 0
                client_ct = f"{_fmt_money(f['price'] / ct)}/ct" if (f["price"] and ct) else ""
                custom = ' <span class="ls-badge">custom</span>' if f["custom"] else ""
                st.markdown(
                    f'<div class="ls-price">Cost <b>{_fmt_money(pv["cost"])}</b><br>'
                    f'<span style="opacity:.6">US${pv["usd"]:,.0f} total{usd_ct}</span><br>'
                    f'Client <b>{_fmt_money(f["price"])}</b>{custom}<br>'
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
    for lbl, key in (("length", "length"), ("width", "width"), ("depth", "height")):
        lo, hi = c.get(key, (None, None))
        if lo or hi:
            parts.append(f"{lbl} {lo or 0:g}–{hi:g} mm" if hi else f"{lbl} {lo:g}+ mm")
    if c["as_grown"]:
        parts.append("as-grown only")
    return ", ".join(parts)


def _flex_label(k, fx):
    return f"Carat {'±0.10' if fx['ct_tol'] == 0.10 else '±0.05'}" if k == "ct" else FLEX_LABELS[k]


def _results(exact, flexed, adds, res, crit, fx, ai_key, deps):
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
    refs = {}
    for prefix, rows, section in (("E", shown_ex, "exact"), ("O", shown_fx, "outside")):
        for i, s in enumerate(rows, 1):
            refs[f"{prefix}{i}"] = (s, section)
    _top_picks(refs, exact + flexed, _criteria_text(crit, fx), ai_key)
    _quote_box(exact + flexed, deps)


def _top_picks(refs, all_rows, criteria_text, ai_key):
    ss = st.session_state
    st.markdown('<hr class="divider">', unsafe_allow_html=True)
    if st.button("✨  Suggest top picks", type="primary", key="ls_picks_btn",
                 disabled=not (ai_key and refs)):
        payload = [_ai_stone(ref, s, section) for ref, (s, section) in refs.items()]
        with st.spinner("Choosing top picks…"):
            try:
                out = ai_top_picks(ai_key, criteria_text, payload)
                picks = []
                for p in (out.get("picks") or [])[:5]:
                    ref = str(p.get("ref", "")).strip()
                    if ref in refs:           # only stones that were actually displayed
                        picks.append((ref, refs[ref][0]["sid"], refs[ref][1], str(p.get("reason", ""))[:240]))
                ss.ls_picks_ai = picks
            except AIError as e:
                st.error(str(e))
    if ss.get("ls_picks_ai"):
        by_sid = {s["sid"]: s for s in all_rows}
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
        "Cert file": s.get("cert_file") or None,
        "Measurements": _spec_bits(s)[3], "L/W": round(lw_ratio(s), 2) if lw_ratio(s) else None,
        "Depth %": s.get("depth_pct"), "Table %": s.get("table_pct"),
        "Cost CAD": round(s["pv"]["cost"]) if s["pv"] else None,
        "Cost USD": round(s["pv"]["usd"]) if s["pv"] else None,
        "Client CAD": _final(s)["price"] if s["pv"] else None,
        "Client CAD/ct": round(_final(s)["price"] / s["carat"]) if s["pv"] and s.get("carat") and _final(s)["price"] else None,
        "Differs": "; ".join(b for _, b in s["devs"]),
        "Notes": "; ".join(x for x in [availability_badge(s)] + list(s.get("xbadges") or []) if x),
        **({"Stock #": s.get("stock_no") or ""} if s.get("lookup") else {}),
    } for s in rows])
    if section in ("ex", "lk"):
        df = df.drop(columns=["Differs"])
    edited = st.data_editor(
        df, hide_index=True, key=f"ls_tbl_{section}",
        disabled=[c for c in df.columns if c != "Add"],
        column_config={"Image": st.column_config.ImageColumn("Image"),
                       "Cert file": st.column_config.LinkColumn("Cert file", display_text="Cert"),
                       "Add": st.column_config.CheckboxColumn("Add", help="Add to quote")})
    for s, add in zip(rows, list(edited["Add"])):
        (ss.ls_picked.add if add else ss.ls_picked.discard)(s["sid"])


def _final(s):
    """The stone's price with its own override (if any) applied to the main markup — see pricing.py."""
    ss = st.session_state
    pv = s.get("pv")
    key = f"ls_ov_{s['sid']}"
    text = ss[key] if key in ss else (ss.get("ls_over") or {}).get(s["sid"], "")
    return pricing.final(pv["cost"] if pv else None, float(ss.get("ls_markup") or 0.0), text)


def _cert_job(s, up=None):
    """The certificate job for cert_attach: the stone's certificate link, or the PDF uploaded for it."""
    job = {"url": s.get("cert_file") or "", "cert_no": s.get("cert_no") or "", "lab": norm_lab(s.get("lab"))}
    if up:
        job["data"] = up["data"]
    return job


def _quote_payload(s, show_price, final=None, cert_up=None):
    pv = s["pv"]
    final = final or _final(s)
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
        "price":         pricing.quote_price(final, show_price),      # the one final figure: never cost, markup or margin
        "currency":      "CAD",
        "price_type":    "stone",
        "cert_data":     {k: v for k, v in cert_data.items() if v},
        "ls_ref":        s["sid"],   # internal only; the share page's server strips unknown fields
        "_media":        s.get("media") or None,   # 360/video fields for capture; removed before saving
        # certificate file: redacted + verified on save (cert_attach.py); popped before saving
        "_cert":         _cert_job(s, cert_up),
    }


def _reset_override(sid):
    ss = st.session_state
    ss["ls_ov_" + sid] = ""
    ss.ls_over.pop(sid, None)


def _remove_cert_upload(sid):
    st.session_state.ls_cert_up.pop(sid, None)


def _cert_sig(s, up):
    return (s["sid"], ("up", len(up["data"]), hash(up["data"])) if up else ("url", s.get("cert_file") or ""))


def _cert_checks(chosen):
    """Pre-checks each selected stone's certificate (download, lab, numbers, layout) so a failure is
    known before saving, and the stone can get an Upload button. Results are remembered; the
    downloads run several at a time. Returns {sid: (ok, reason, layout)}."""
    ss = st.session_state
    cache = ss.ls_cert_chk
    todo = [s for s in chosen[:MAX_CERT_CHECKS] if _cert_sig(s, ss.ls_cert_up.get(s["sid"])) not in cache]
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        jobs = [_cert_job(s, ss.ls_cert_up.get(s["sid"])) for s in todo]      # built here: threads can't read session state
        sigs = [_cert_sig(s, ss.ls_cert_up.get(s["sid"])) for s in todo]
        with st.spinner(f"Checking {len(todo)} certificate(s)…"):
            with ThreadPoolExecutor(max_workers=8) as ex:
                res = list(ex.map(cert_attach.precheck, jobs))
        for sig, r in zip(sigs, res):
            cache[sig] = r
    return {s["sid"]: cache.get(_cert_sig(s, ss.ls_cert_up.get(s["sid"]))) for s in chosen}


def _stone_label(s):
    return (f"{s.get('carat') or 0:.2f} ct {shape_group(s['shape'])} "
            f"···{''.join(filter(str.isdigit, s.get('cert_no', '')))[-4:]}")


def _stone_panel(s, check):
    """One selected stone: its own client price and its certificate. Returns its final price dict."""
    ss = st.session_state
    sid, pv = s["sid"], s["pv"]
    key = "ls_ov_" + sid
    ss.setdefault(key, ss.ls_over.get(sid, ""))
    with st.container(border=True):
        st.markdown(f"**{_esc(_stone_label(s))}** · {_esc(s.get('lab') or '')}", unsafe_allow_html=True)
        default = pricing.final(pv["cost"] if pv else None, float(ss.ls_markup), "")["default"]
        c1, c2 = st.columns([2, 3])
        with c1:
            st.text_input("Client price (CAD)", key=key, placeholder=f"{default:,.0f}" if default is not None else "no price",
                          help="Leave empty for the default (main markup). Type a price like 4500, or a markup "
                               "for this stone only like 32%.")
        typed = ss[key]
        if typed.strip():
            ss.ls_over[sid] = typed
        else:
            ss.ls_over.pop(sid, None)
        f = pricing.final(pv["cost"] if pv else None, float(ss.ls_markup), typed)
        with c2:
            if f["custom"]:
                st.markdown('<span class="ls-badge">custom</span>' + (f" {_esc(f['note'])}" if f["note"] else ""),
                            unsafe_allow_html=True)
                st.button("Reset to default", key="ls_ovr_" + sid, on_click=_reset_override, args=(sid,))
            elif default is not None:
                st.caption(f"Default: {_fmt_money(default)} (markup {ss.ls_markup:g}%)")
            if f["error"]:
                st.caption(f"⚠️ {f['error']} — the default price is used.")
            if f["margin"] is not None:      # internal screen only: never saved
                st.caption(f"Margin {_fmt_money(f['margin'])} · {f['margin_pct']:+.1f}% over cost "
                           f"(cost {_fmt_money(pv['cost'])})")
        if f["below_cost"]:
            st.warning(f"Below cost: {_fmt_money(f['price'])} is under the {_fmt_money(pv['cost'])} this stone costs. "
                       "You can still save it.")
        _cert_panel(s, check)
    return f


def accept_cert_upload(s, name, data):
    """(ok, why): an uploaded certificate is kept for the stone (and attached when the quote is saved)
    only when its lab, numbers and layout check out; the same detection and redaction rules as every
    certificate then apply on save, ending in the fail-closed verification."""
    ok, why, layout = cert_attach.precheck(_cert_job(s, {"data": data}))
    if ok:
        st.session_state.ls_cert_up[s["sid"]] = {"name": str(name or "certificate.pdf"), "data": data}
    return ok, why


def _cert_panel(s, check):
    ss = st.session_state
    sid = s["sid"]
    up = ss.ls_cert_up.get(sid)
    if up:
        ok, why, layout = check or (False, "not checked", "")
        st.caption(f"📄 Uploaded certificate: {up['name']} — {layout} layout, redacted and verified when the quote is saved")
        st.button("Remove uploaded certificate", key=f"ls_cupx_{sid}", on_click=_remove_cert_upload, args=(sid,))
        return
    if check is None:
        st.caption("📄 Certificate: checked when the quote is saved")
    elif check[0]:
        st.caption(f"📄 Certificate: found ({check[2]}) — redacted and verified when the quote is saved")
        return
    else:
        st.warning(f"📄 Certificate can't be pulled automatically: {check[1]}")
    f = st.file_uploader("Upload certificate (PDF)", type="pdf", key=f"ls_cup_{sid}",
                         help="Goes through the same layout detection, redaction (client logo in place of the QR code) "
                              "and verification as every certificate, and is attached when the quote is saved.")
    if f is not None:
        ok, why = accept_cert_upload(s, f.name, f.getvalue())
        if ok:
            st.rerun()
        else:
            st.error(f"This PDF can't be used: {why}")


MAX_CERT_CHECKS = 60


def _quote_box(rows, deps):
    ss = st.session_state
    st.markdown('<hr class="divider">', unsafe_allow_html=True)
    st.markdown('<div class="section-label">Approve into quote</div>', unsafe_allow_html=True)
    by_sid = {s["sid"]: s for s in rows}
    chosen = [by_sid[sid] for sid in list(ss.ls_picked) if sid in by_sid]
    finals, checks = {}, {}
    if not chosen:
        st.caption("Tick **Add to quote** on the stones you want, then choose the client here.")
    else:
        st.caption(f"{len(chosen)} stone(s) selected. Each has its own client price (the main markup is the default) "
                   "and certificate.")
        checks = _cert_checks(chosen)
        for s in chosen:
            finals[s["sid"]] = _stone_panel(s, checks.get(s["sid"]))
    q_client = deps["client_selector"]("ls_client", "ls_client_sel")
    a, b = st.columns(2)
    with a:
        exp = st.slider("Link expires (days)", 1, 30, 15, key="ls_exp")
    with b:
        show_price = st.checkbox("Show client price (CAD) on quote", value=True, key="ls_show_price")
    if st.button(f"🔗  Add to quote ({len(chosen)} stone{'s' if len(chosen) != 1 else ''})", type="primary",
                 use_container_width=True, key="ls_add_quote", disabled=not chosen):
        payload = [_quote_payload(s, show_price, finals.get(s["sid"]), ss.ls_cert_up.get(s["sid"])) for s in chosen]
        with st.spinner("Creating quote and copying media…"):
            link = deps["save_quote"](q_client, payload, exp)
        if link:
            for p in payload:
                deps["add_history"](p["orig_filename"], f"···{p['cert_last4']} → {link}", p["cert_type"], q_client)
            ss.quote_link = link
            ss.capture_tab = "ls"
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
        if deps.get("show_capture_status"):
            deps["show_capture_status"]("ls")                # "360 processing: 2 of 3 done" + capture log
        if ss.get("cert_log"):
            with st.expander("Certificate log — one line per stone", expanded=False):
                st.dataframe([{"Stone": c.get("stone"), "Source": c.get("source"), "Lab/format": c.get("lab_format"),
                               "Redaction": c.get("redaction"), "Verification": c.get("verification"),
                               "Attached": "yes" if c.get("pdf_url") else "no",
                               "Why not": c.get("reason") or c.get("note")}
                              for c in ss.cert_log], hide_index=True, use_container_width=True)


# ── Direct lookup by stone number ─────────────────────────────────────────────
MAX_LOOKUP = 100
_TOKEN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
_PREFIXED_RE = re.compile(r"([A-Z]{1,4})-?(\d{5,})")
CERT_PREFIXES = ("LG",)          # tried in front of a plain number (IGI lab-grown numbers)


def parse_numbers(text, cap=MAX_LOOKUP):
    """Number-like tokens from a messy paste (emails, spreadsheets): runs of letters, digits
    and inner hyphens, at least 5 characters with at least 4 digits. Duplicates (including
    the same certificate number with and without its prefix) are removed, first one kept.
    Returns (numbers, duplicates_removed, over_cap)."""
    seen, out, total = set(), [], 0
    for m in _TOKEN_RE.finditer(str(text or "")):
        t = m.group(0)
        if len(t) < 5 or sum(ch.isdigit() for ch in t) < 4:
            continue
        total += 1
        k = canonical(t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out[:cap], total - len(out), max(0, len(out) - cap)


def canonical(token):
    t = _key(token)
    m = _PREFIXED_RE.fullmatch(t)
    return m.group(2) if m and m.group(1) in ("LG", "GIA", "IGI", "HRD", "AGS", "GCAL") else t


def cert_variants(token):
    """Certificate numbers to try for one input: as typed, and with / without a prefix."""
    t = _key(token)
    m = _PREFIXED_RE.fullmatch(t)
    if m:
        return list(dict.fromkeys([t, m.group(2)]))
    if t.isdigit():
        return [t] + [p + t for p in CERT_PREFIXES]
    return [t]


def _digits(v):
    return re.sub(r"\D", "", str(v or ""))


def match_how(token, s):
    """How a stone matches one input number: 'certificate number', 'stock number' or ''."""
    cn = _key(s.get("cert_no"))
    if cn and (cn in cert_variants(token) or (len(_digits(cn)) >= 6 and _digits(cn) == _digits(canonical(token))
                                              and _digits(canonical(token)) == canonical(token))):
        return "certificate number"
    if s.get("stock_no") and _key(s["stock_no"]) == _key(token):
        return "stock number"
    return ""


def lookup_report(numbers, stones, rep, rate, markup, divisor):
    """Per input number: status, matched stones (cheapest first) and how they matched."""
    errs = {c["by"] for c in rep.get("calls") or [] if c.get("error")}
    rows = []
    for t in numbers:
        hits = [(s, match_how(t, s)) for s in stones]
        hits = [(s, h) for s, h in hits if h]
        hits.sort(key=lambda x: (x[0].get("price_raw") is None, x[0].get("price_raw") or 0))
        if len(hits) > 1:
            status = "Multiple matches"
        elif hits:
            status = "Found"
        elif errs:
            status = "Couldn't check"
        else:
            status = "Not found"
        rows.append({"input": t, "status": status, "sids": [s["sid"] for s, _ in hits],
                     "how": sorted({h for _, h in hits}),
                     "badges": [availability_badge(s) for s, _ in hits if availability_badge(s)]})
    return rows


def _mask(t):
    d = str(t or "")
    return "···" + d[-4:] if len(d) > 4 else d


def _lookup_mode(client, ai_key, divisor, deps):
    ss = st.session_state
    st.markdown('<div class="section-label">Look up stones by number</div>', unsafe_allow_html=True)
    text = st.text_area("IGI / GIA certificate numbers or Stock #s", key="ls_lk_text", height=140,
                        placeholder="Paste numbers separated by new lines, commas, spaces or tabs — "
                                    "straight from an email or spreadsheet is fine.")
    numbers, dupes, over = parse_numbers(text)
    if text.strip():
        st.caption(f"{len(numbers)} number(s) to look up" + (f" · {dupes} duplicate(s) removed" if dupes else "")
                   + (f" · **only the first {MAX_LOOKUP} are looked up** ({over} more left out)" if over else ""))
    rate, markup = _pricing_inputs()
    if st.button("🔎  Look up", type="primary", use_container_width=True, key="ls_lk_go", disabled=not numbers):
        client.reset_diag()
        certs_, stock = [], []
        for t in numbers:
            certs_ += cert_variants(t)
            stock.append(t)
        error, rep, stones = None, {}, []
        with st.spinner(f"Looking up {len(numbers)} number(s)…"):
            try:
                stones, rep = client.lookup(list(dict.fromkeys(certs_)), list(dict.fromkeys(stock)))
            except src.SourceError as e:
                error = str(e)
        rows = lookup_report(numbers, stones, rep, rate, markup, divisor) if not error else []
        keep = {sid for r in rows for sid in r["sids"]}
        ss.ls_lookup = None if error else {"numbers": numbers, "rows": rows,
                                           "stones": [x for x in stones if x["sid"] in keep],
                                           "over": over, "dupes": dupes}
        _clear_picks()
        for r in rows:                      # each number's cheapest listing is pre-selected
            if r["sids"]:
                ss.ls_picked.add(r["sids"][0])
        ss.ls_picks_ai = None
        ss.ls_lookup_diag = _lookup_diag(client.diag, rep, numbers, dupes, over, rows, error,
                                         client.schema() if not error else {}, stones)
        _log_diag(ss.ls_lookup_diag, "live-search-lookup")
    if ss.get("ls_lookup_diag") and ss.ls_lookup_diag.get("user_error"):
        st.error(ss.ls_lookup_diag["user_error"])
    lk = ss.get("ls_lookup")
    if lk:
        _lookup_results(lk, rate, markup, divisor, ai_key, deps)


def _lookup_results(lk, rate, markup, divisor, ai_key, deps):
    by_sid = {}
    for x in lk["stones"]:
        by_sid[x["sid"]] = {**x, "pv": price_view(x, rate, markup, divisor), "devs": [], "lookup": True,
                            "xbadges": media_badges(x)}
    st.markdown('<hr class="divider">', unsafe_allow_html=True)
    counts = {k: sum(r["status"] == k for r in lk["rows"]) for k in ("Found", "Multiple matches", "Not found",
                                                                      "Couldn't check")}
    st.markdown("**" + " · ".join(f"{k}: {v}" for k, v in counts.items() if v or k != "Couldn't check") + "**")
    if lk.get("over"):
        st.warning(f"Only the first {MAX_LOOKUP} numbers were looked up; {lk['over']} more were left out.")
    if counts["Couldn't check"]:
        st.error("Part of the lookup failed, so the numbers marked **Couldn't check** may exist. Try again.")
    lines = []
    for r in lk["rows"]:
        stones = [by_sid[sid] for sid in r["sids"] if sid in by_sid]
        extra = ""
        if stones:
            first = stones[0]
            desc = (f"{first.get('carat') or 0:.2f} ct {shape_group(first['shape'])} {_spec_bits(first)[0]} "
                    f"{norm_clarity(first.get('clarity'))} · {first.get('lab') or ''}")
            extra = f" — {desc} · by {', '.join(r['how'])}"
            if len(stones) > 1:
                extra += f" · {len(stones)} listings, cheapest first (pre-selected)"
            badges = sorted(set(r["badges"]))
            if badges:
                extra += " · " + ", ".join(f"**{b}**" for b in badges)
        icon = {"Found": "✅", "Multiple matches": "🔁", "Not found": "❌", "Couldn't check": "⚠️"}[r["status"]]
        lines.append(f"- {icon} `{_code(r['input'])}` — **{r['status']}**{extra}")
    st.markdown("\n".join(lines))
    missing = [r["input"] for r in lk["rows"] if r["status"] in ("Not found", "Couldn't check")]
    if missing:
        st.caption("Copy not-found list (use the copy button on the right of the box):")
        st.code("\n".join(missing), language=None)

    rows = list(by_sid.values())
    if not rows:
        return
    a, b, c_ = st.columns([2.2, 1.4, 1.2])
    with a:
        sort = st.selectbox("Sort by", SORTS[:4], index=0, key="ls_sort_lk")
    with b:
        view = st.radio("View", ["Cards", "Table"], index=0, horizontal=True, key="ls_view")
    with c_:
        show_n = int(st.number_input("Show", min_value=5, max_value=300, value=100, step=5, key="ls_show_n_lk"))
    groups = []                                    # listings grouped under their number
    placed = set()
    for r in lk["rows"]:
        g = [by_sid[sid] for sid in r["sids"] if sid in by_sid and sid not in placed]
        placed.update(x["sid"] for x in g)
        if g:
            groups.append((r, g))
    if sort == SORTS[0]:
        shown = [x for _, g in groups for x in g][:show_n]
    else:
        shown = _sort(rows, sort, {"ct_min": None, "ct_max": None})[:show_n]
    st.markdown(f'<div class="section-label" style="margin-top:.8rem">Found stones · {len(rows)}'
                f'{f" (showing {len(shown)})" if len(rows) > len(shown) else ""}</div>', unsafe_allow_html=True)
    _section(shown, "lk", view)
    refs = {f"E{i}": (x, "exact") for i, x in enumerate(shown, 1)}
    _top_picks(refs, rows, f"Direct lookup of {len(lk['numbers'])} stone number(s); no criteria", ai_key)
    _quote_box(rows, deps)


def _lookup_diag(cd, rep, numbers, dupes, over, rows, error, schema, stones):
    """Sanitised: numbers masked to their last 4 characters, no hosts or supplier data."""
    return {
        "time_utc": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "sign_in": cd.get("sign_in"),
        "api_errors": list(cd.get("api_errors") or []),
        "user_error": error,
        "numbers": len(numbers), "duplicates_removed": dupes, "over_cap": over,
        "filters": {k: rep.get(k) for k in ("cert_filter", "stock_filter", "type_filter", "stock_fields",
                                             "schema_verified")},
        "calls": rep.get("calls") or [],
        "api_returned": cd.get("raw_items", 0), "pages": cd.get("pages", 0),
        "results": [{"input": _mask(r["input"]), "status": r["status"], "listings": len(r["sids"]),
                     "by": r["how"]} for r in rows],
        "counts": {k: sum(r["status"] == k for r in rows) for k in ("Found", "Multiple matches", "Not found",
                                                                     "Couldn't check")},
        "cert_files": _cert_file_diag(schema, stones) if schema else None,
    }
