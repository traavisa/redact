"""Part 0: the one-time clean-up of links to deleted /media/ files."""
import pytest

import dead_media
from fakesb import SB, FakeSupabase

M = "https://quote.alldiamondeverything.com/media/"
PROMO = "b4752b32efe5496f8f1a024122b7ea97.mp4"
LIVE_V, LIVE_I, GONE_I = "1" * 32 + ".mp4", "2" * 32 + ".jpg", "3" * 32 + ".jpg"
FRAME = "4" * 32 + "/000.jpg"
QUOTES = [
    {"id": "6mhcsgni", "client": "Harlings", "created_at": "2026-09-01", "stones": [
        {"cert_last4": "1111", "video_url": M + PROMO, "image_url": M + LIVE_I, "price": "900"},
        {"cert_last4": "2222", "video_url": M + LIVE_V, "image_url": M + GONE_I}]},
    {"id": "okquote1", "client": "NFR", "created_at": "2026-09-02", "stones": [
        {"cert_last4": "3333", "video_url": "https://video.alldiamondeverything.com/v/abcdefgh2345",
         "image_url": M + FRAME}]},
]


def test_clears_only_dead_links(monkeypatch):
    sb = FakeSupabase(QUOTES, files=[LIVE_V, LIVE_I, FRAME]).install(monkeypatch)
    rep = dead_media.run(SB, "key")
    assert rep["dead_files"] == sorted([PROMO, GONE_I])
    assert rep["cleared"] == 2 and rep["errors"] == 0 and not rep["unchecked"]
    q = {x["id"]: x for x in sb.tables["quotes"]}
    s1, s2 = q["6mhcsgni"]["stones"]
    assert s1["video_url"] == "" and s1["image_url"] == M + LIVE_I and s1["price"] == "900"   # nothing else changed
    assert s2["video_url"] == M + LIVE_V and s2["image_url"] == ""
    assert q["okquote1"]["stones"][0]["image_url"] == M + FRAME                              # frame still exists
    assert {(r["quote"], r["field"], r["file"]) for r in rep["rows"]} == {
        ("6mhcsgni", "video_url", PROMO), ("6mhcsgni", "image_url", GONE_I)}
    # running it again finds nothing
    rep2 = dead_media.run(SB, "key")
    assert rep2["cleared"] == 0 and rep2["dead_files"] == []


def test_blocked_read_is_an_error_not_zero(monkeypatch):
    FakeSupabase(QUOTES, block_reads=True).install(monkeypatch)
    with pytest.raises(dead_media.DeadMediaError, match="HTTP 401"):
        dead_media.run(SB, "key")


def test_empty_quotes_is_an_error(monkeypatch):
    FakeSupabase([], files=[LIVE_V]).install(monkeypatch)
    with pytest.raises(dead_media.DeadMediaError, match="0 rows"):
        dead_media.run(SB, "key")


def test_empty_storage_listing_is_an_error(monkeypatch):
    sb = FakeSupabase(QUOTES, files=[]).install(monkeypatch)
    with pytest.raises(dead_media.DeadMediaError, match="listing came back empty"):
        dead_media.run(SB, "key")
    assert sb.tables["quotes"][0]["stones"][0]["video_url"] == M + PROMO                    # nothing changed


def test_unknown_answers_are_left_alone(monkeypatch):
    sb = FakeSupabase(QUOTES, files=[LIVE_V, LIVE_I, FRAME]).install(monkeypatch)
    real = sb.request

    def flaky(method, url, **kw):
        if method == "HEAD" and PROMO in url:
            from fakesb import Resp
            return Resp(503, {})
        return real(method, url, **kw)
    sb.request = flaky
    rep = dead_media.run(SB, "key")
    assert PROMO in rep["unchecked"] and PROMO not in rep["dead_files"]
    assert sb.tables["quotes"][0]["stones"][0]["video_url"] == M + PROMO
