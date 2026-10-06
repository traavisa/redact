"""House rules, saved examples and the correction log for Live Search "Read request".

All of it lives in Supabase (see supabase/request_learning_setup.sql) and is read with the
service key from the server only. Every function here raises on a failed call; the caller
(live_search.py) decides what to do, and for parsing that is: carry on without it and log it.
"""
import collections
import datetime
import json
import logging
import math
import re

import requests

log = logging.getLogger("diamond_tools")

RULES_KEY = "request_rules"
EXAMPLES_IN_PROMPT = 5

DEFAULT_RULES = """CARAT SIZES
- A whole or half size (1, 1.5, 2, 2.5, 3, 3.5, 4 ct etc.) means that size up to +0.10 ct, never below. "3ct" = 3.00–3.10. "1.5ct" = 1.50–1.60.
- An in-between size (e.g. 1.20, 1.30, 1.70, 2.80) means from 0.05 ct below to 0.10 ct above. "1.30" = 1.25–1.40. "2.80" = 2.75–2.90.
- "ish", "around", "approx" or "about" widens the top of the range to +0.30 ct. "3ct ish" = 3.00–3.30. For in-between sizes keep the 0.05 below: "1.30ish" = 1.25–1.60.
- An explicit range the client gives (e.g. "1.8–2.2") is used exactly as given.

GRADES
- "Ex/Ex" or "VG/VG" (two grades) means Polish and Symmetry minimums only (Excellent, or Very Good and better). Cut is left blank.
- "3EX", "3X", "Ex/Ex/Ex" or "triple excellent" means Cut, Polish and Symmetry all Excellent, for ROUND stones only. For any other shape, it means Polish and Symmetry Excellent only, with cut left blank, because only rounds have a cut grade.
- "Ideal" cut counts as meeting an Excellent cut requirement.

DEFAULTS
- Unless the request says otherwise, assume Lab-grown with an IGI certificate. If it says natural (or mined/earth-grown) and names no lab, use Natural with GIA."""

RULES_PROMPT = (
    "\n\nHOUSE RULES (from the dealer; AUTHORITATIVE — they override every default, assumption and "
    "example in these instructions, and you must apply them exactly):\n"
    "{rules}\n\n"
    "Every value you set by applying a house rule gets a SHORT note (under 10 words) of the form "
    "'<request wording> → <value>, <rule name> rule', e.g. '3ct → 3.00–3.10, size rule', "
    "'ex/ex/ex cushion → polish/sym EX, grade rule', 'no type → lab-grown, IGI, defaults rule'. "
    "For house-rule values use this note INSTEAD of ending with ', confirm'. A field a rule says to leave "
    "blank (e.g. cut) is simply omitted.")
EXAMPLES_PROMPT = (
    "\n\nWORKED EXAMPLES: past requests from this dealer with the criteria they finally settled on "
    "after correcting earlier readings. Use them as guidance for similar wording; where an example "
    "conflicts with the HOUSE RULES, the house rules win.\n{examples}")


# ── Supabase (REST) ──────────────────────────────────────────────────────────
class Store:
    def __init__(self, url, key):
        self.url, self.key = url, key

    def _h(self, extra=None):
        return {"apikey": self.key, "Authorization": f"Bearer {self.key}", **(extra or {})}

    def _check(self, r, what):
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"{what} failed: HTTP {r.status_code}")

    def rows(self, table, query=""):
        r = requests.get(f"{self.url}/rest/v1/{table}?{query}&select=*", headers=self._h(), timeout=10)
        self._check(r, f"{table} read")
        out = r.json()
        if not isinstance(out, list):
            raise RuntimeError(f"{table} read failed: unexpected response")
        return out

    def insert(self, table, payload):
        r = requests.post(f"{self.url}/rest/v1/{table}", data=json.dumps(payload), timeout=10,
                          headers=self._h({"Content-Type": "application/json", "Prefer": "return=minimal"}))
        self._check(r, f"{table} write")

    def upsert(self, table, payload):
        r = requests.post(f"{self.url}/rest/v1/{table}", data=json.dumps(payload), timeout=10,
                          headers=self._h({"Content-Type": "application/json",
                                           "Prefer": "resolution=merge-duplicates,return=minimal"}))
        self._check(r, f"{table} write")

    def delete(self, table, flt):
        r = requests.delete(f"{self.url}/rest/v1/{table}?{flt}", headers=self._h(), timeout=10)
        self._check(r, f"{table} delete")

    # rules
    def load_rules(self):
        """The saved rules text, or None when none was ever saved (use the defaults)."""
        rows = self.rows("request_settings", f"key=eq.{RULES_KEY}")
        return str(rows[0].get("value") or "") if rows else None

    def save_rules(self, text):
        self.upsert("request_settings", {"key": RULES_KEY, "value": text, "updated_at": now()})

    # examples
    def load_examples(self):
        return self.rows("request_examples", "order=created_at.desc&limit=500")

    def add_example(self, request_text, criteria):
        self.insert("request_examples", {"request_text": request_text, "criteria": criteria,
                                         "created_at": now()})

    def delete_example(self, ex_id):
        self.delete("request_examples", f"id=eq.{ex_id}")

    # corrections
    def add_corrections(self, rows):
        if rows:
            self.insert("request_corrections", rows)

    def load_corrections(self):
        return self.rows("request_corrections", "order=created_at.desc&limit=2000")


def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


# ── Similar examples (simple term overlap) ───────────────────────────────────
_STOP = {"a", "an", "and", "the", "of", "for", "to", "in", "on", "with", "please", "need", "want", "looking",
         "any", "or", "is", "are", "we", "i", "my", "client", "customer", "stone", "stones", "diamond",
         "diamonds", "can", "you", "pls", "hi", "hello", "thanks", "thank"}


def terms(text):
    """Lower-case words and numbers; '3ct' -> '3', 'ex/ex/ex' -> ex ex ex; common stop words dropped."""
    out = set()
    for t in re.findall(r"[a-z]+|\d+(?:\.\d+)?", str(text or "").lower()):
        if t in _STOP or t == "ct" or t == "carat" or t == "cts":
            continue
        out.add(t)
    return out


def similar_examples(text, examples, n=EXAMPLES_IN_PROMPT):
    """The n saved examples whose request shares the most terms with `text` (best first, overlap > 0)."""
    mine = terms(text)
    scored = []
    for i, ex in enumerate(examples):
        theirs = terms(ex.get("request_text"))
        shared = len(mine & theirs)
        if shared:
            scored.append((shared / math.sqrt(len(mine) * len(theirs)), -i, ex))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [s[2] for s in scored[:n]]


def format_examples(examples, show):
    """Prompt text for worked examples; `show` turns an example's criteria into a short dict."""
    blocks = []
    for k, ex in enumerate(examples, 1):
        blocks.append(f"Example {k}\nRequest: {ex.get('request_text', '')}\n"
                      f"Final criteria: {json.dumps(show(ex.get('criteria') or {}), ensure_ascii=False)}")
    return "\n\n".join(blocks)


def system_prompt(base, rules="", examples_text=""):
    out = base
    if rules and rules.strip():
        out += RULES_PROMPT.format(rules=rules.strip())
    if examples_text:
        out += EXAMPLES_PROMPT.format(examples=examples_text)
    return out


# ── Corrections ──────────────────────────────────────────────────────────────
def top_corrections(rows, n=10):
    """Most frequent (field, parsed -> final) corrections, plus corrections per field."""
    pairs = collections.Counter((r.get("field"), r.get("parsed_value"), r.get("final_value")) for r in rows)
    fields = collections.Counter(r.get("field") for r in rows)
    return pairs.most_common(n), fields.most_common()
