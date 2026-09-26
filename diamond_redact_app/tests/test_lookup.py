"""Part 3: direct lookup by IGI / GIA / stock number."""
from fakeapi import stone

import live_search as ls


def test_parse_messy_paste_duplicates_and_prefixes():
    text = ("Hi, can you price these?\n\tLG833689789, 833689789  GIA 2141438167;\r\n"
            "Stock#: AB-12345 | 2141438167\nbudget 4500, 1.52ct, tel 12, report 7461234567.")
    nums, dupes, over = ls.parse_numbers(text)
    assert nums == ["LG833689789", "2141438167", "AB-12345", "7461234567"]
    assert dupes == 2          # 833689789 (= LG833689789) and the second 2141438167
    assert over == 0


def test_parse_cap_100():
    nums, dupes, over = ls.parse_numbers("\n".join(str(100000000 + i) for i in range(130)))
    assert len(nums) == 100 and over == 30 and dupes == 0


def test_cert_variants():
    assert ls.cert_variants("LG833689789") == ["LG833689789", "833689789"]
    assert ls.cert_variants("833689789") == ["833689789", "LG833689789"]
    assert ls.cert_variants("AB-12345") == ["AB12345", "12345"]
    assert ls.cert_variants("2141438167") == ["2141438167", "LG2141438167"]


def _run(client, text):
    nums, _, _ = ls.parse_numbers(text)
    certs, stock = [], []
    for t in nums:
        certs += ls.cert_variants(t)
        stock.append(t)
    stones, rep = client.lookup(list(dict.fromkeys(certs)), list(dict.fromkeys(stock)))
    return {r["input"]: r for r in ls.lookup_report(nums, stones, rep, 1.37, 20, 100)}, stones, rep


STONES = [
    stone("a1", "LG833689789", "IGI", 150000, lg=True),                        # API stores the LG form
    stone("b1", "622001234", "IGI", 99000, lg=True),                           # API stores the plain form
    stone("g1", "2141438167", "GIA", 800000, lg=False),                        # GIA natural
    stone("g2", "2141438167", "GIA", 750000, lg=False, availability="ON_HOLD"),  # same cert, 2nd listing
    stone("s1", "7000000001", "GIA", 500000, lg=False, stock="AB-12345", video=False, image=False,
          availability="NOT_AVAILABLE"),
]


def test_lookup_end_to_end(fake_api):
    api, client = fake_api(STONES)
    rows, stones, rep = _run(client, "833689789\nLG622001234, GIA 2141438167\tAB-12345 9999999999 9999999999")
    assert rows["833689789"]["status"] == "Found"                 # IGI without the LG prefix
    assert rows["833689789"]["how"] == ["certificate number"]
    assert rows["LG622001234"]["status"] == "Found"               # IGI with the LG prefix
    g = rows["2141438167"]
    assert g["status"] == "Multiple matches"
    assert g["sids"] == ["g2", "g1"]                              # cheapest first
    assert "On hold" in g["badges"]
    assert rows["AB-12345"]["status"] == "Found" and rows["AB-12345"]["how"] == ["stock number"]
    assert "Unavailable" in rows["AB-12345"]["badges"]
    assert rows["9999999999"]["status"] == "Not found"
    assert len(rows) == 5                                         # the duplicate was removed
    # both natural and lab-grown were searched, and nothing else was filtered
    lits = [q for q in api.queries if "diamonds_by_query(query:" in q]
    assert any("labgrown: true" in q for q in lits) and any("labgrown: false" in q for q in lits)
    assert not any("has_image" in q or "shapes" in q for q in lits)
    assert rep["cert_filter"] == "certificate_numbers" and rep["stock_filter"] == "supplier_stock_ids"
    # internal fields came through the whitelist
    s1 = next(s for s in stones if s["sid"] == "s1")
    assert s1["stock_no"] == "AB-12345"
    a1 = next(s for s in stones if s["sid"] == "a1")
    assert a1["cert_file"].endswith("LG833689789.pdf") and a1["cert_file_field"] == "certificate.pdfUrl"


def test_lookup_media_badges_not_filtered(fake_api):
    api, client = fake_api(STONES)
    rows, stones, _ = _run(client, "AB-12345")
    s1 = next(s for s in stones if s["sid"] == "s1")
    assert ls.media_badges(s1) == ["No media"]                    # still shown, with a badge


def test_lookup_error_is_not_not_found(fake_api):
    api, client = fake_api(STONES)
    api.fail_on = "supplier_stock_ids"
    rows, _, rep = _run(client, "833689789 ZZ-99999")
    assert rows["833689789"]["status"] == "Found"
    assert rows["ZZ-99999"]["status"] == "Couldn't check"         # a failed read never shows as not found


def test_lookup_no_filters_is_an_error(fake_api):
    import pytest
    import stone_source
    from fakeapi import schema
    api, client = fake_api(STONES)
    sch = schema()
    for t in sch["__schema"]["types"]:
        if t["name"] == "DiamondQuery":
            t["inputFields"] = [f for f in t["inputFields"] if f["name"] in ("labgrown", "shapes")]
    api.schema = sch
    with pytest.raises(stone_source.SourceError):
        client.lookup(["833689789"], ["833689789"])


def test_lookup_diag_masks_numbers(fake_api):
    api, client = fake_api(STONES)
    rows, stones, rep = _run(client, "833689789 2141438167")
    nums = ["833689789", "2141438167"]
    d = ls._lookup_diag(client.diag, rep, nums, 0, 0, list(rows.values()), None, client.schema(), stones)
    import json
    txt = json.dumps(d, ensure_ascii=False)
    assert "833689789" not in txt and "2141438167" not in txt
    assert "···9789" in txt and "···8167" in txt
    assert "example-supplier" not in txt
