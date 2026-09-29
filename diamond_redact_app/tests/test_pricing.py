"""Per-stone client price overrides (Live Search and Look up stones)."""
import json
import re

import pytest

import pricing
from fakeapi import stone
from test_app import boot, env, texts  # noqa: F401  (env is a fixture)

USD_CAD, MARKUP = 1.37, 20.0
# 1.37 × 1500.00 USD = CA$2,055 cost; +20% = CA$2,466 default client price
COST = 1500.0 * USD_CAD


# ── The rules ─────────────────────────────────────────────────────────────────
def test_default_is_the_main_markup():
    f = pricing.final(COST, 20.0, "")
    assert f["price"] == round(COST * 1.2) == 2466 and not f["custom"] and f["error"] == ""
    assert f["margin"] == pytest.approx(2466 - COST) and f["margin_pct"] == pytest.approx(20.0, abs=0.05)


@pytest.mark.parametrize("text, price", [("4500", 4500), ("$4,500", 4500), ("CA$ 4500.40", 4500), ("cad 4,500", 4500),
                                          ("  3000 ", 3000)])
def test_typing_a_price_overrides_that_stone(text, price):
    f = pricing.final(COST, 20.0, text)
    assert f["price"] == price and f["custom"] and f["kind"] == "price"


@pytest.mark.parametrize("text, markup", [("32%", 32), ("32 %", 32), ("12.5%", 12.5), ("0%", 0), ("-5%", -5)])
def test_typing_a_percentage_applies_a_per_stone_markup(text, markup):
    f = pricing.final(COST, 20.0, text)
    assert f["price"] == round(COST * (1 + markup / 100)) and f["custom"] and f["kind"] == "pct"
    assert f["note"].endswith(f"→ CA${f['price']:,.0f}")


def test_main_markup_change_moves_only_the_stones_without_an_override():
    before = [pricing.final(COST, 20.0, t)["price"] for t in ("", "4500", "32%")]
    after = [pricing.final(COST, 50.0, t)["price"] for t in ("", "4500", "32%")]
    assert before[0] != after[0] and after[0] == round(COST * 1.5)
    assert before[1] == after[1] == 4500                         # a typed price stays
    assert before[2] == after[2] == round(COST * 1.32)           # a typed % stays that stone's own markup


@pytest.mark.parametrize("text", ["abc", "12x", "%", "0", "-3", "-100%", "1e3", "$$"])
def test_bad_text_falls_back_to_the_default_with_a_hint(text):
    f = pricing.final(COST, 20.0, text)
    assert f["price"] == round(COST * 1.2) and not f["custom"] and f["error"]


def test_below_cost_is_flagged_but_allowed():
    f = pricing.final(COST, 20.0, "1800")
    assert f["price"] == 1800 and f["below_cost"] and f["margin"] < 0 and f["margin_pct"] < 0
    assert pricing.final(COST, 20.0, "-10%")["below_cost"]
    assert not pricing.final(COST, 20.0, "0%")["below_cost"]     # exactly at cost (after rounding) is fine


def test_stone_without_a_cost_takes_a_price_but_not_a_percentage():
    assert pricing.final(None, 20.0, "")["price"] is None
    f = pricing.final(None, 20.0, "3000")
    assert f["price"] == 3000 and f["custom"] and f["margin"] is None
    g = pricing.final(None, 20.0, "10%")
    assert g["price"] is None and g["error"] and not g["custom"]


def test_the_quote_gets_only_the_final_price():
    f = pricing.final(COST, 20.0, "32%")
    assert pricing.quote_price(f) == str(round(COST * 1.32))
    assert pricing.quote_price(f, show_price=False) == "" and pricing.quote_price(pricing.final(None, 20.0, "")) == ""


# ── In the app ────────────────────────────────────────────────────────────────
@pytest.fixture
def priced(env):
    sb, api = env
    api.stones = [stone("p1", "LG833689789", "IGI", 150000), stone("p2", "2141438167", "GIA", 90000, lg=False)]
    return sb, api


def _select(at, numbers="LG833689789 2141438167"):
    at.radio(key="ls_mode").set_value("Look up stones").run()
    at.text_area(key="ls_lk_text").input(numbers).run()
    at.button(key="ls_lk_go").click().run()
    assert not at.exception, at.exception


def test_placeholder_shows_the_default_price_and_margin_is_shown(priced):
    at = boot()
    _select(at)
    box = at.text_input(key="ls_ov_p1")
    assert box.label == "Client price (CAD)" and box.value == ""
    assert box.placeholder == f"{COST * 1.2:,.0f}"                           # the grey default
    t = texts(at)
    assert "Default: CA$2,466 (markup 20%)" in t
    assert "Margin CA$411 · +20.0% over cost (cost CA$2,055)" in t
    assert "custom" not in re.sub(r"<style>.*?</style>", "", t, flags=re.S).lower().replace("customer", "")


def test_override_by_price_and_by_percentage_reset_and_markup_change(priced):
    at = boot()
    _select(at)
    at.text_input(key="ls_ov_p1").input("4500").run()
    t = texts(at)
    assert at.session_state["ls_over"] == {"p1": "4500"} and "custom" in t
    assert "Margin CA$2,445 · +119.0% over cost" in t
    at.text_input(key="ls_ov_p2").input("32%").run()
    cost2 = 900.0 * USD_CAD
    t = texts(at)
    assert f"32% markup → CA${cost2 * 1.32:,.0f}" in t
    assert set(at.session_state["ls_over"]) == {"p1", "p2"}
    # main markup change recalculates only the stones without an override
    at.number_input(key="ls_markup").set_value(50.0).run()
    assert at.text_input(key="ls_ov_p1").placeholder == f"{COST * 1.5:,.0f}"     # defaults follow the main markup
    assert at.session_state["ls_over"] == {"p1": "4500", "p2": "32%"}
    # one-click reset
    at.button(key="ls_ovr_p1").click().run()
    assert at.session_state["ls_over"] == {"p2": "32%"} and at.text_input(key="ls_ov_p1").value == ""
    assert "Default: " in texts(at)


def test_below_cost_warns_but_can_be_saved_with_final_prices_only(priced):
    sb, api = priced
    at = boot()
    _select(at)
    at.text_input(key="ls_ov_p1").input("1800").run()
    warn = " ".join(w.value for w in at.warning)
    assert "Below cost: CA$1,800 is under the CA$2,055 this stone costs" in warn
    at.text_input(key="ls_ov_p2").input("-10%").run()
    assert "Below cost" in " ".join(w.value for w in at.warning)
    at.text_input(key="ls_ov_p2").input("32%").run()
    at.button(key="ls_add_quote").click().run()
    assert not at.exception, at.exception
    saved = sb.tables["quotes"][-1]
    prices = {s["cert_last4"]: s["price"] for s in saved["stones"]}
    assert prices == {"9789": "1800", "8167": str(round(900 * USD_CAD * 1.32))}
    blob = json.dumps(saved)
    for word in ("cost", "markup", "margin", "override", "ls_over", "1.32", "2055", "2466"):
        assert word not in blob.lower(), word
    # every key of a saved stone is one the quote has always had
    assert not {"cost", "markup", "margin", "price_cost"} & set(saved["stones"][0])
    assert not at.session_state["ls_over"]                                        # saved stones' overrides are cleared


def test_price_can_be_hidden_on_the_quote(priced):
    sb, api = priced
    at = boot()
    _select(at, "LG833689789")
    at.text_input(key="ls_ov_p1").input("4500").run()
    at.checkbox(key="ls_show_price").uncheck().run()
    at.button(key="ls_add_quote").click().run()
    assert sb.tables["quotes"][-1]["stones"][0]["price"] == ""


def test_card_and_table_show_the_custom_price(priced):
    at = boot()
    _select(at)
    at.text_input(key="ls_ov_p1").input("4500").run()
    t = texts(at)
    assert "CA$4,500" in t and 'class="ls-badge">custom' in t


def test_new_search_clears_overrides(priced):
    at = boot()
    _select(at)
    at.text_input(key="ls_ov_p1").input("4500").run()
    at.number_input(key="ls_markup").set_value(35.0).run()
    at.button(key="ls_new_search").click().run()
    assert at.session_state["ls_over"] == {} and at.session_state["ls_markup"] == 35.0    # the main markup stays
    _select(at)
    assert at.text_input(key="ls_ov_p1").value == ""


def test_criteria_search_has_the_same_price_boxes(priced):
    at = boot()
    at.radio(key="ls_type").set_value("Natural")
    at.multiselect(key="ls_labs").set_value([]).run()
    at.button(key="ls_search").click().run()
    assert not at.exception, at.exception
    keys = [t.key for t in at.text_input if t.key and t.key.startswith("ls_ov_")]
    assert keys == []                                                             # nothing selected yet
    at.checkbox(key="ls_pk_p2").check().run()
    assert at.text_input(key="ls_ov_p2").label == "Client price (CAD)"


def test_create_quote_already_prices_each_stone_on_its_own(env):
    at = boot()
    assert len([t for t in at.text_input if t.label == "Your price (optional)"]) == 3      # one price box per diamond
    assert not [n for n in at.number_input if n.label == "Markup %" and n.key.startswith("q")]  # no single markup there
