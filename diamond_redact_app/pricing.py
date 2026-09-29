"""Per-stone client price overrides for Live Search (internal screen only).

The main Markup % is the default: client price = cost × (1 + markup/100). A stone can override it
with what is typed in its "Client price (CAD)" box:
  - a number ("4500", "$4,500", "CA$ 4500.50") = that exact client price;
  - a percentage ("32%") = a markup for that stone only, on its own cost.
Nothing typed = the default. Changing the main markup therefore only moves stones that have no
override (a "%" override keeps its own percentage; a price override keeps its price).

final() returns the price used for the quote, plus margin figures for the screen. Cost, markup and
margin are never part of what is saved: quote_price() gives the one whole-dollar figure.
"""
import re

_CLEAN = re.compile(r"(?i)\s|,|ca\$|cad|us\$|\$")


class BadPrice(ValueError):
    """Text that is neither a price nor a percentage; str(e) is a short hint."""


def parse(text):
    """None (nothing typed), ("price", amount) or ("pct", markup percent). Raises BadPrice."""
    t = str(text or "").strip()
    if not t:
        return None
    pct = t.endswith("%")
    num = _CLEAN.sub("", t[:-1] if pct else t)
    if not re.fullmatch(r"-?\d+(\.\d+)?|-?\.\d+", num):
        raise BadPrice("Type a price like 4500, or a markup like 32%")
    v = float(num)
    if pct:
        if v <= -100:
            raise BadPrice("A markup can't be −100% or lower")
        return ("pct", v)
    if v <= 0:
        raise BadPrice("A price must be more than 0")
    return ("price", v)


def final(cost, markup, text):
    """The price for one stone.
    cost: its cost in CAD (None when the source has no price); markup: the main Markup %;
    text: what is typed in the stone's price box.
    Returns dict(price, default, custom, kind, error, margin, margin_pct, below_cost, note)."""
    default = cost * (1 + markup / 100.0) if cost is not None else None
    out = {"price": default, "default": default, "custom": False, "kind": None, "error": "",
           "margin": None, "margin_pct": None, "below_cost": False, "note": ""}
    try:
        ov = parse(text)
    except BadPrice as e:
        ov, out["error"] = None, str(e)
    if ov:
        kind, v = ov
        if kind == "pct":
            if cost is None:
                out["error"] = "This stone has no cost, so a % can't be applied — type a price"
                ov = None
            else:
                out["price"] = cost * (1 + v / 100.0)
                out["note"] = f"{v:g}% markup → CA${out['price']:,.0f}"
        else:
            out["price"] = v
        if ov:
            out["custom"], out["kind"] = True, kind
    if out["price"] is not None:
        out["price"] = round(out["price"])           # the quote carries whole dollars, as it always has
    if cost is not None and out["price"] is not None:
        out["margin"] = out["price"] - cost
        out["margin_pct"] = (out["margin"] / cost * 100.0) if cost else None
        out["below_cost"] = out["price"] < cost - 0.5
    return out


def quote_price(f, show_price=True):
    """What the saved quote carries: the final whole-dollar price as text, or '' (never cost/markup/margin)."""
    return str(int(f["price"])) if (show_price and f and f["price"] is not None) else ""
