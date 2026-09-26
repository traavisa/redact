"""Part 4: hide stones with no video/360 or no image."""
from fakeapi import stone

import live_search as ls
import stone_source


def S(**kw):
    base = {"sid": "x", "video": "", "image": "", "media": {}}
    base.update(kw)
    return base


def test_definitions():
    assert ls.has_video_360(S(video="https://h/v360/1/view.html"))                      # viewer URL
    assert ls.has_video_360(S(media={"certificate": {"v360": {"url": "https://h/f/", "frame_count": 100}}}))
    assert ls.has_video_360(S(media={"certificate": {"product_videos": [{"url": "https://h/a.mp4"}]}}))
    assert ls.has_video_360(S(media={"video_file": "https://h/a.mp4"}))
    assert not ls.has_video_360(S(media={"certificate": {"v360": {"url": "", "frame_count": 0}}}))
    assert not ls.has_video_360(S())
    assert ls.has_image(S(image="https://h/a.jpg"))
    assert ls.has_image(S(media={"certificate": {"image": "https://h/still.jpg"}}))
    assert not ls.has_image(S(image="not a link"))


def test_media_keep_counts():
    rows = [S(sid="1", video="https://h/v", image="https://h/i"), S(sid="2", image="https://h/i"),
            S(sid="3", video="https://h/v"), S(sid="4")]
    kept, rem = ls.media_keep(rows, True, True)
    assert [r["sid"] for r in kept] == ["1"] and rem == {"no_video_360": 2, "no_image": 1}
    kept, rem = ls.media_keep(rows, False, False)
    assert len(kept) == 4 and rem == {"no_video_360": 0, "no_image": 0}
    kept, rem = ls.media_keep(rows, False, True)
    assert [r["sid"] for r in kept] == ["1", "2"]


def test_media_plan_server_and_client():
    sch = {"filters": {"has_image": {"kind": "bool"}, "has_v360": {"kind": "bool"}, "has_video": {"kind": "bool"}}}
    assert ls.media_plan(sch, True, True) == {"image": "has_image", "video": ["has_v360", "has_video"]}
    assert ls.media_plan(sch, False, False) == {"image": None, "video": None}
    only360 = {"filters": {"has_v360": {"kind": "bool"}}}
    assert ls.media_plan(only360, True, True) == {"image": None, "video": None}      # client-side only


def _crit(**kw):
    c = {"lab_grown": False, "shapes": [], "ct_min": None, "ct_max": None, "fancy": False, "col": ("D", "Z"),
         "fancy_col": "Yellow", "fancy_int": [], "cla": ("FL", "I3"), "cut": "Any", "pol": "Any", "sym": "Any",
         "flo": [], "labs": [], "pr_min": None, "pr_max": None, "pr_client": False, "ratio": (None, None),
         "depth": (None, None), "table": (None, None), "as_grown": False, "hide_vid": True, "hide_img": True}
    c.update(kw)
    return c


FX = {"col": False, "cla": False, "ct": False, "budget": False, "lab": False, "flo": False, "ct_tol": 0.05}


def test_server_query_sends_image_flag_and_plans_video(fake_api):
    api, client = fake_api([])
    q, plan = ls.server_query(_crit(), FX, client.schema(), 1.37, 0, 100)
    assert q.get("has_image") is True and "has_v360" not in q and "has_video" not in q
    kinds = {p[0]: p[1] for p in plan}
    assert kinds["Media: image"] == "server" and kinds["Media: video/360"] == "server"
    q, plan = ls.server_query(_crit(hide_vid=False, hide_img=False), FX, client.schema(), 1.37, 0, 100)
    assert "has_image" not in q and not any(p[0].startswith("Media") for p in plan)


def test_client_side_when_schema_has_no_flags(fake_api):
    api, client = fake_api([], media_flags=())
    q, plan = ls.server_query(_crit(), FX, client.schema(), 1.37, 0, 100)
    assert "has_image" not in q
    kinds = {p[0]: p[1] for p in plan}
    assert kinds["Media: image"] == "client" and kinds["Media: video/360"] == "client"


def test_hidden_stones_never_outside_criteria():
    rows = [dict(stone_source.whitelist(stone(f"n{i}", f"10000000{i}", "GIA", 100000 + i, lg=False,
                                              video=i % 2 == 0, color="H"))) for i in range(6)]
    crit = _crit(col=("D", "G"))
    fx = dict(FX, col=True)                          # H is outside D–G by one grade: "Outside your criteria"
    shown, removed = ls.media_keep(rows, True, True)
    stats = {}
    ex, fl, _ = ls.bucket(shown, crit, fx, 1.37, 0, 100, stats)
    assert removed["no_video_360"] == 3
    assert len(fl) == 3 and stats["flexed"] == 3    # only stones WITH media are counted as outside criteria


def test_count_query(fake_api):
    stones = [stone(f"z{i}", f"20000000{i}", "GIA", 1000 + i, lg=False, image=i < 3) for i in range(5)]
    api, client = fake_api(stones)
    assert client.count({"labgrown": False}) == 5
    assert client.count({"labgrown": False, "has_image": True}) == 3
