"""Remember-me login: a signed, expiring token kept in a cookie so a known browser skips the password screen.

The token is "v1.<expiry as unix seconds>.<HMAC-SHA256>". The signature covers the expiry and a fingerprint of
the CURRENT app password, keyed with APP_SESSION_SECRET, so:
  - it can't be forged or extended without the secret;
  - it stops working at its expiry (default 30 days, APP_SESSION_DAYS);
  - changing the app password (or the secret) invalidates every token, on every device.
Nothing is stored server-side. Without APP_SESSION_SECRET the feature is off.

The cookie is written from the page (Streamlit has no server-side cookie API), so it can't be HttpOnly;
it is SameSite=Lax, and Secure whenever the page is served over https.
"""
import hashlib
import hmac
import json
import time

COOKIE = "pcg_session"
DEFAULT_DAYS = 30
VERSION = "v1"


def _sig(secret, password, exp):
    pw_fp = hmac.new(secret.encode(), b"pw|" + password.encode(), hashlib.sha256).hexdigest()
    return hmac.new(secret.encode(), f"{VERSION}|{int(exp)}|{pw_fp}".encode(), hashlib.sha256).hexdigest()


def make_token(secret, password, days=DEFAULT_DAYS, now=None):
    exp = int((time.time() if now is None else now) + days * 86400)
    return f"{VERSION}.{exp}.{_sig(secret, password, exp)}"


def valid(token, secret, password, now=None):
    """True only for a well-formed, unexpired token signed with this secret for this password."""
    if not (token and secret and password):
        return False
    try:
        ver, exp, sig = str(token).split(".")
        exp = int(exp)
    except ValueError:
        return False
    if ver != VERSION or exp <= (time.time() if now is None else now):
        return False
    return hmac.compare_digest(sig, _sig(secret, password, exp))


def days_from(value):
    """APP_SESSION_DAYS -> days (1-365); anything else is the default."""
    try:
        d = float(str(value).strip())
    except ValueError:
        return DEFAULT_DAYS
    return d if 1 <= d <= 365 else DEFAULT_DAYS


def read_cookie():
    """The cookie sent with this page load ('' if none / not available)."""
    try:
        import streamlit as st
        return str(st.context.cookies.get(COOKIE, "") or "")
    except Exception:
        return ""


def set_cookie_html(token, days):
    """A tiny script (no visible output) that stores the token: SameSite=Lax, Secure on https."""
    return ("<script>try{var p=window.parent;var s=p.location.protocol==='https:'?'; Secure':'';"
            f"p.document.cookie={json.dumps(COOKIE + '=' + token)}+'; Max-Age={int(days * 86400)}; Path=/; SameSite=Lax'+s;"
            "}catch(e){}</script>")


def clear_cookie_html():
    return ("<script>try{var p=window.parent;var s=p.location.protocol==='https:'?'; Secure':'';"
            f"p.document.cookie={json.dumps(COOKIE + '=')}+'; Max-Age=0; Path=/; SameSite=Lax'+s;"
            "}catch(e){}</script>")


_noted = False


def note_secret(secret):
    """Once per server process: say in the log that remember-me is off when there is no secret."""
    global _noted
    if not secret and not _noted:
        _noted = True
        print("[login] APP_SESSION_SECRET is not set: remember-me login is off "
              "(the password is asked in every new browser session)", flush=True)


def run_script(html):
    """Puts a script-only snippet on the page (no visible output). Uses st.iframe where this Streamlit
    has it (components.html is being retired), else components.html."""
    import streamlit as st
    if hasattr(st, "iframe"):
        st.iframe(html, height=1)
    else:
        import streamlit.components.v1 as components
        components.html(html, height=0)
