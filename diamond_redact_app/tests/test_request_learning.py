"""Read request: house rules, saved examples, correction log (fake Supabase, scripted fake model).

The model is faked: these tests prove the prompt carries the rules/examples and that whatever the
model returns lands in the form (amber, nothing searched). They cannot prove the real model applies
the rules correctly; that needs a live key.
"""
import json

import live_search as ls
import request_learning as rl
from test_app import boot, env, texts  # noqa: F401  (env is a fixture)
from test_changes import _parse_result as P


def fake_model(monkeypatch, answers):
    """Replace the Anthropic call: records the system prompt, answers from `answers[request text]`."""
    seen = []

    def call(key, system, user, schema, max_tokens=4000):
        text = user.split("Client request:\n\n", 1)[1]
        seen.append({"system": system, "text": text})
        return answers[text]
    monkeypatch.setattr(ls, "_ai_call", call)
    return seen


def read(at, text):
    at.text_area(key="ls_req").input(text).run()
    at.button(key="ls_read").click().run()
    assert not at.exception, at.exception


def vals(at):
    g = lambda k: at.number_input(key=k).value
    return (g("ls_ct_min"), g("ls_ct_max"), at.radio(key="ls_type").value, at.multiselect(key="ls_labs").value,
            at.selectbox(key="ls_cut").value, at.selectbox(key="ls_pol").value, at.selectbox(key="ls_sym").value)


def has_button(at, key):
    return any(b.key == key for b in at.button)


CASES = {   # request -> (what the model is scripted to return, what the form must then show)
    "3ct oval": (
        P(type=("lab-grown", "no type given → lab-grown, defaults rule"), shapes=["Oval"],
          carat_min=(3.0, "3ct → 3.00–3.10, size rule"), carat_max=(3.1, "3ct → 3.00–3.10, size rule"),
          labs=(["IGI"], "no lab given → IGI, defaults rule")),
        (3.0, 3.1, "Lab-grown", ["IGI"], "Any", "Any", "Any")),
    "1.5ish round 3ex": (
        P(shapes=["Round"], carat_min=(1.5, "1.5ish → 1.50–1.80, size rule"),
          carat_max=(1.8, "1.5ish → 1.50–1.80, size rule"),
          cut_min=("Excellent", "3ex round → cut EX, grade rule"), polish_min=("Excellent", "3ex → EX, grade rule"),
          symmetry_min=("Excellent", "3ex → EX, grade rule")),
        (1.5, 1.8, "Lab-grown", ["IGI"], "Excellent", "Excellent", "Excellent")),
    "1.30 cushion ex/ex/ex": (
        P(shapes=["Cushion"], carat_min=(1.25, "1.30 → 1.25–1.40, size rule"),
          carat_max=(1.4, "1.30 → 1.25–1.40, size rule"),
          polish_min=("Excellent", "ex/ex/ex cushion → polish EX, cut blank, grade rule"),
          symmetry_min=("Excellent", "ex/ex/ex cushion → symmetry EX, grade rule")),
        (1.25, 1.4, "Lab-grown", ["IGI"], "Any", "Excellent", "Excellent")),
    "2.80 emerald VG/VG": (
        P(shapes=["Emerald"], carat_min=(2.75, "2.80 → 2.75–2.90, size rule"),
          carat_max=(2.9, "2.80 → 2.75–2.90, size rule"),
          polish_min=("Very Good", "VG/VG → polish VG+, grade rule"),
          symmetry_min=("Very Good", "VG/VG → symmetry VG+, grade rule")),
        (2.75, 2.9, "Lab-grown", ["IGI"], "Any", "Very Good", "Very Good")),
    "natural 2ct round": (
        P(type="natural", shapes=["Round"], labs=(["GIA"], "natural, no lab → GIA, defaults rule"),
          carat_min=(2.0, "2ct → 2.00–2.10, size rule"), carat_max=(2.1, "2ct → 2.00–2.10, size rule")),
        (2.0, 2.1, "Natural", ["GIA"], "Any", "Any", "Any")),
    "1.30ish pear": (
        P(shapes=["Pear"], carat_min=(1.25, "1.30ish → 1.25–1.60, size rule"),
          carat_max=(1.6, "1.30ish → 1.25–1.60, size rule")),
        (1.25, 1.6, "Lab-grown", ["IGI"], "Any", "Any", "Any")),
}


def test_rules_prefilled_in_every_prompt_and_the_six_cases_fill_amber(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    sb, api = env
    seen = fake_model(monkeypatch, {k: v[0] for k, v in CASES.items()})
    at = boot()
    assert not at.exception, at.exception
    assert at.text_area(key="ls_rules_ta").value == rl.DEFAULT_RULES        # pre-filled with the exact rules
    for must in ('"3ct" = 3.00–3.10', '"1.30ish" = 1.25–1.60', "triple excellent", "for ROUND stones only",
                 "assume Lab-grown with an IGI certificate"):
        assert must in rl.DEFAULT_RULES
    for req, (_, want) in CASES.items():
        read(at, req)
        assert vals(at) == want, req
        notes = at.session_state["ls_notes"]
        assert "size rule" in notes["ls_ct_min"] and "size rule" in notes["ls_ct_max"]        # amber + the rule used
        assert "Nothing has been searched yet" in texts(at)
        assert seen[-1]["text"] == req
        assert "HOUSE RULES" in seen[-1]["system"] and rl.DEFAULT_RULES.strip() in seen[-1]["system"]
        assert "AUTHORITATIVE" in seen[-1]["system"]
    assert not [q for q in api.queries if "diamonds_by_query" in q]      # nothing is searched automatically
    # unknowns stay blank: the 3ct oval names no colour, clarity or price
    read(at, "3ct oval")
    assert at.number_input(key="ls_pr_max").value is None and at.session_state["ls_cla"] == ("FL", "I3")


def test_natural_with_no_lab_gets_gia_even_if_the_model_leaves_it_out(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    fake_model(monkeypatch, {"natural 2ct round": P(type="natural", shapes=["Round"])})
    at = boot()
    read(at, "natural 2ct round")
    assert at.multiselect(key="ls_labs").value == ["GIA"] and "defaults rule" in at.session_state["ls_notes"]["ls_labs"]


def test_edited_rules_are_saved_and_used(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    sb, api = env
    seen = fake_model(monkeypatch, {"x": P()})
    at = boot()
    at.text_area(key="ls_rules_ta").input("MY RULE: 3ct means 3.00-3.50").run()
    at.button(key="ls_rules_save").click().run()
    assert sb.tables["request_settings"][0]["value"] == "MY RULE: 3ct means 3.00-3.50"
    read(at, "x")
    assert "MY RULE: 3ct means 3.00-3.50" in seen[-1]["system"]
    assert rl.DEFAULT_RULES.strip() not in seen[-1]["system"]
    assert boot().text_area(key="ls_rules_ta").value == "MY RULE: 3ct means 3.00-3.50"   # next session loads it


def test_save_as_example_then_similar_examples_are_retrieved_and_deletable(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    sb, api = env
    ans = {"3ct oval g vs": P(shapes=["Oval"], carat_min=(3.0, "3ct → 3.00–3.10, size rule"), carat_max=3.1),
           "oval 3ct vs": P()}
    seen = fake_model(monkeypatch, ans)
    at = boot()
    read(at, "3ct oval g vs")
    assert not has_button(at, "ls_ex_save")                              # nothing edited yet
    at.number_input(key="ls_ct_max").set_value(3.2).run()
    assert has_button(at, "ls_ex_save")
    at.button(key="ls_ex_save").click().run()
    ex = sb.tables["request_examples"]
    assert len(ex) == 1 and ex[0]["request_text"] == "3ct oval g vs"
    assert ex[0]["criteria"]["ls_ct_max"] == 3.2 and ex[0]["criteria"]["ls_shapes"] == ["Oval"]
    assert not has_button(at, "ls_ex_save")                              # saved: gone until the next edit
    assert "3ct oval g vs" in texts(at)                                  # listed in settings
    # more saved examples; the 5 most similar go into the prompt
    for i in range(6):
        sb.tables["request_examples"].append({"id": f"p{i}", "request_text": f"pear {i} f vs2 gia",
                                              "criteria": {"ls_shapes": ["Pear"]}, "created_at": "2026-09-20T00:00:00Z"})
    for i in range(6):
        sb.tables["request_examples"].append({"id": f"o{i}", "request_text": f"oval 3ct vs{i}",
                                              "criteria": {"ls_shapes": ["Oval"], "ls_ct_min": 3.0},
                                              "created_at": f"2026-09-1{i}T00:00:00Z"})
    sb.tables["request_examples"].append({"id": "z", "request_text": "round 5 d vvs", "criteria": {},
                                          "created_at": "2026-09-10T00:00:00Z"})
    read(at, "oval 3ct vs")
    system = seen[-1]["system"]
    assert "WORKED EXAMPLES" in system and system.count("\nRequest: ") == 5
    assert "Request: 3ct oval g vs" in system and "pear" not in system and "round 5" not in system
    assert '"carat_max": 3.2' in system and '"shapes": ["Oval"]' in system
    # delete from the settings list
    at.button(key="ls_exdel_o0").click().run()
    assert "o0" not in [r.get("id") for r in sb.tables["request_examples"]]
    assert not has_button(at, "ls_exdel_o0")


def test_similarity_is_simple_term_overlap():
    exs = [{"request_text": "1.5ct oval G VS"}, {"request_text": "pear shape please"}, {"request_text": "round D IF 2ct"}]
    got = rl.similar_examples("oval 1.5 carat vs", exs)
    assert [e["request_text"] for e in got] == ["1.5ct oval G VS"]
    assert rl.similar_examples("nothing alike", exs) == []
    assert len(rl.similar_examples("oval", [{"request_text": "oval"}] * 9)) == 5


def test_corrections_are_logged_for_changed_parser_fields_only(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    sb, api = env
    fake_model(monkeypatch, {"3ct oval": CASES["3ct oval"][0]})
    at = boot()
    read(at, "3ct oval")
    at.number_input(key="ls_ct_max").set_value(3.3)                       # changed a parser-filled field
    at.number_input(key="ls_pr_max").set_value(9000.0).run()              # filled a blank one: not a correction
    at.button(key="ls_search").click().run()
    rows = sb.tables["request_corrections"]
    assert len(rows) == 1, rows
    r = rows[0]
    assert (r["field"], json.loads(r["parsed_value"]), json.loads(r["final_value"]), r["request_text"]) == \
        ("carat_max", 3.1, 3.3, "3ct oval")
    at.button(key="ls_search").click().run()                              # same final value: not logged twice
    assert len(sb.tables["request_corrections"]) == 1
    at.number_input(key="ls_ct_max").set_value(3.4).run()
    at.button(key="ls_search").click().run()
    assert [json.loads(x["final_value"]) for x in sb.tables["request_corrections"]] == [3.3, 3.4]


def test_common_corrections_view_shows_the_most_frequent(env, monkeypatch):
    sb, api = env
    for i, (f, was, now) in enumerate([("carat_max", "3.1", "3.3")] * 3 + [("cut_min", '"Excellent"', '"Very Good"')]):
        sb.tables["request_corrections"].append({"field": f, "parsed_value": was, "final_value": now,
                                                 "request_text": f"r{i}", "created_at": "2026-09-2%dT00:00:00Z" % i})
    at = boot()
    df = at.dataframe[0].value
    assert list(df["Field"])[0] == "carat_max" and list(df["Times"])[0] == 3
    assert "carat_max ×3" in texts(at)


def test_failed_supabase_reads_fall_back_to_parsing_as_before_and_log(env, monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    sb, api = env
    sb.block_reads = True
    seen = fake_model(monkeypatch, {"3ct oval": CASES["3ct oval"][0]})
    with caplog.at_level("WARNING", logger="diamond_tools"):
        at = boot()
        assert not at.exception, at.exception
        read(at, "3ct oval")
    assert at.number_input(key="ls_ct_min").value == 3.0                  # still parsed and filled
    assert "HOUSE RULES" not in seen[-1]["system"] and "WORKED EXAMPLES" not in seen[-1]["system"]
    assert "Read without" in texts(at) and "request rules" in texts(at)
    assert "request rules could not be loaded" in caplog.text and "saved examples could not be loaded" in caplog.text
    assert "couldn't be loaded" in texts(at)
    # reads recover: the next parse picks the rules back up
    sb.block_reads = False
    read(at, "3ct oval")
    assert "HOUSE RULES" in seen[-1]["system"]


def test_failed_correction_log_write_never_blocks_the_search(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    sb, api = env
    fake_model(monkeypatch, {"3ct oval": CASES["3ct oval"][0]})
    at = boot()
    read(at, "3ct oval")
    sb.block_reads = True                                                 # fake rejects deletes/reads; force writes to fail too
    monkeypatch.setattr(rl.Store, "add_corrections", lambda self, rows: (_ for _ in ()).throw(RuntimeError("down")))
    at.number_input(key="ls_ct_max").set_value(3.3).run()
    at.button(key="ls_search").click().run()
    assert not at.exception, at.exception
