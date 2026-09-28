"""Remember-me login: signed expiring cookie token, tied to the app password and a secret."""
import re
import time

import pytest

import login_token as T
from test_app import boot, env, texts  # noqa: F401  (env is a fixture)

SECRET = "s3cret-value-for-tests"


def test_token_roundtrip_and_rejections():
    now = 1_800_000_000
    tok = T.make_token(SECRET, "pw", 30, now=now)
    assert T.valid(tok, SECRET, "pw", now=now + 1)
    assert T.valid(tok, SECRET, "pw", now=now + 30 * 86400 - 5)
    assert not T.valid(tok, SECRET, "pw", now=now + 30 * 86400 + 1)               # expired
    assert not T.valid(tok, "another-secret", "pw", now=now + 1)                  # signed with another secret
    assert not T.valid(tok, SECRET, "new password", now=now + 1)                  # password changed: everyone logged out
    ver, exp, sig = tok.split(".")
    assert not T.valid(f"{ver}.{int(exp) + 86400 * 365}.{sig}", SECRET, "pw", now=now + 1)   # expiry can't be extended
    assert not T.valid(f"{ver}.{exp}.{'0' * len(sig)}", SECRET, "pw", now=now + 1)
    assert not T.valid(f"v2.{exp}.{sig}", SECRET, "pw", now=now + 1)
    for junk in ("", None, "abc", "a.b", "v1.x.y", "v1..", ".".join(["v1", exp, sig, "extra"])):
        assert not T.valid(junk, SECRET, "pw", now=now + 1)
    assert not T.valid(tok, "", "pw", now=now + 1) and not T.valid(tok, SECRET, "", now=now + 1)
    assert re.fullmatch(r"v1\.\d+\.[0-9a-f]{64}", tok)                            # cookie-safe characters only
    assert "pw" not in tok and SECRET not in tok


def test_days_setting():
    assert T.days_from("") == 30 and T.days_from(None) == 30 and T.days_from("abc") == 30
    assert T.days_from("7") == 7 and T.days_from(" 90 ") == 90
    assert T.days_from("0") == 30 and T.days_from("9999") == 30


def _login(at, password="pw"):
    at.text_input[0].input(password)
    at.button[0].click().run()
    return at


def test_remembered_browser_skips_the_password_screen(env, monkeypatch):
    monkeypatch.setenv("APP_SESSION_SECRET", SECRET)
    good = T.make_token(SECRET, "pw", 30)
    monkeypatch.setattr(T, "read_cookie", lambda: good)
    at = boot(authed=False)
    assert not at.exception, at.exception
    assert "Log in" not in texts(at) and "Create quote" in texts(at)


@pytest.mark.parametrize("why,token", [
    ("expired", T.make_token(SECRET, "pw", 30, now=time.time() - 40 * 86400)),
    ("other secret", T.make_token("other", "pw", 30)),
    ("old password", T.make_token(SECRET, "old password", 30)),
    ("tampered", "v1.9999999999." + "a" * 64),
    ("garbage", "hello"),
    ("none", ""),
])
def test_bad_tokens_get_the_password_screen(env, monkeypatch, why, token):
    monkeypatch.setenv("APP_SESSION_SECRET", SECRET)
    monkeypatch.setattr(T, "read_cookie", lambda: token)
    at = boot(authed=False)
    assert "Log in" in texts(at) and "Create quote" not in texts(at), why


def test_without_the_secret_nothing_changes_and_the_log_says_so(env, monkeypatch, capfd):
    monkeypatch.delenv("APP_SESSION_SECRET", raising=False)
    monkeypatch.setattr(T, "_noted", False)
    monkeypatch.setattr(T, "read_cookie", lambda: T.make_token(SECRET, "pw", 30))   # even a "valid-looking" cookie
    at = boot(authed=False)
    assert "Log in" in texts(at)
    at = _login(at)
    assert not at.exception and "Create quote" in texts(at)
    assert "pcg_session" not in texts(at)                                            # no cookie is issued
    out = capfd.readouterr().out
    assert out.count("[login] APP_SESSION_SECRET is not set") == 1                   # once, not on every rerun
    assert "remember-me login is off" in out


def test_login_issues_a_signed_cookie_with_expiry(env, monkeypatch):
    monkeypatch.setenv("APP_SESSION_SECRET", SECRET)
    monkeypatch.setattr(T, "read_cookie", lambda: "")
    at = _login(boot(authed=False))
    assert not at.exception, at.exception
    t = texts(at)
    assert "Create quote" in t
    m = re.search(r"pcg_session=(v1\.\d+\.[0-9a-f]{64})", t)
    assert m and T.valid(m.group(1), SECRET, "pw")
    assert f"Max-Age={30 * 86400}" in t and "SameSite=Lax" in t and "Path=/" in t
    assert "https:" in t and "Secure" in t                     # Secure whenever https
    assert "issue_cookie" not in at.session_state or not at.session_state["issue_cookie"]
    at.run()
    assert "pcg_session=v1." not in texts(at)                                        # written once, not on every rerun
    # a wrong password issues nothing
    at = _login(boot(authed=False), "nope")
    assert "Incorrect password" in texts(at) and "pcg_session=v1." not in texts(at)


def test_cookie_lifetime_is_configurable(env, monkeypatch):
    monkeypatch.setenv("APP_SESSION_SECRET", SECRET)
    monkeypatch.setenv("APP_SESSION_DAYS", "7")
    monkeypatch.setattr(T, "read_cookie", lambda: "")
    at = _login(boot(authed=False))
    t = texts(at)
    assert f"Max-Age={7 * 86400}" in t
    exp = int(re.search(r"pcg_session=v1\.(\d+)\.", t).group(1))
    assert abs(exp - (time.time() + 7 * 86400)) < 120


def test_log_out_clears_the_cookie_and_the_session_stays_logged_out(env, monkeypatch):
    monkeypatch.setenv("APP_SESSION_SECRET", SECRET)
    good = T.make_token(SECRET, "pw", 30)
    monkeypatch.setattr(T, "read_cookie", lambda: good)
    at = boot(authed=False)
    assert "Create quote" in texts(at)                                               # remembered
    at.button(key="logout").click().run()
    t = texts(at)
    assert "Log in" in t and "Create quote" not in t                                 # not logged straight back in
    assert "pcg_session=" in t and "Max-Age=0" in t                                  # the cookie is removed
    at.run()
    assert "Log in" in texts(at)
    at = _login(at)
    assert "Create quote" in texts(at)                                               # can log in again
