import io
import json
import zipfile
import datetime
from pathlib import Path
from PIL import Image
import streamlit as st

try:
    import fitz
except ImportError:
    st.error("PyMuPDF is not installed. Run: pip install pymupdf")
    st.stop()

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Certificate Redaction — Pure Carbon Group",
    page_icon="💎",
    layout="centered",
)

# ── Styling ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap');

html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }
.block-container { max-width: 780px; padding-top: 2rem; padding-bottom: 3rem; }

.pcg-header {
    display: flex; align-items: center; gap: 14px;
    padding-bottom: 1.2rem;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    margin-bottom: 1.8rem;
}
.pcg-title { font-size: 1.25rem; font-weight: 600; letter-spacing: -0.01em; }
.pcg-sub { font-size: 0.78rem; opacity: 0.45; letter-spacing: 0.06em; text-transform: uppercase; margin-top: 1px; }

.cert-selector { display: flex; gap: 8px; margin-bottom: 1.4rem; }
.stButton > button { width: 100%; font-family: 'DM Sans', sans-serif !important; }

.section-label {
    font-size: 0.72rem; font-weight: 600; letter-spacing: 0.09em;
    text-transform: uppercase; opacity: 0.45; margin-bottom: 8px;
}
.logo-grid { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 0.6rem; }
.logo-card {
    border: 1.5px solid rgba(255,255,255,0.1);
    border-radius: 10px; padding: 8px 14px;
    cursor: pointer; font-size: 0.8rem; font-weight: 500;
    transition: all 0.15s; background: transparent;
    white-space: nowrap;
}
.logo-card.selected {
    border-color: #c9a84c;
    background: rgba(201,168,76,0.08);
    color: #c9a84c;
}

.history-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 7px 0; border-bottom: 1px solid rgba(255,255,255,0.05);
    font-size: 0.82rem;
}
.history-orig { opacity: 0.45; font-family: 'DM Mono', monospace; font-size: 0.75rem; }
.history-clean { font-family: 'DM Mono', monospace; font-weight: 500; color: #c9a84c; }
.history-type { font-size: 0.68rem; opacity: 0.3; text-transform: uppercase; letter-spacing: 0.06em; }
.history-time { font-size: 0.68rem; opacity: 0.25; }

.divider { border: none; border-top: 1px solid rgba(255,255,255,0.07); margin: 1.4rem 0; }
</style>
""", unsafe_allow_html=True)

# ── Redaction zone definitions ────────────────────────────────────────────────
CERT_ZONES = {
    "GIA": {
        "label": "GIA Natural Diamond",
        "page_w": 792, "page_h": 612,
        "zones": {
            "gia1":        {"x0": 367.7, "y0": 47.0,  "x1": 431.5, "y1": 59.0,  "mask_ratio": 0.60},
            "gia2":        {"x0": 192.8, "y0": 117.9, "x1": 241.6, "y1": 126.9, "mask_ratio": 0.60},
            "inscription": {"x0": 95.1,  "y0": 339.4, "x1": 143.0, "y1": 348.4, "mask_ratio": 0.60},
            "qr":          {"x0": 698.0, "y0": 504.2, "x1": 752.0, "y1": 558.2},
        }
    },
    "GIA Colour": {
        "label": "GIA Natural Coloured Diamond",
        "page_w": 792, "page_h": 612,
        "zones": {
            "gia1":        {"x0": 367.7, "y0": 45.0,  "x1": 431.5, "y1": 57.0,  "mask_ratio": 0.60},
            "gia2":        {"x0": 192.8, "y0": 137.2, "x1": 241.6, "y1": 146.2, "mask_ratio": 0.60},
            "inscription": {"x0": 95.1,  "y0": 446.9, "x1": 143.0, "y1": 455.9, "mask_ratio": 0.60},
            "qr":          {"x0": 698.1, "y0": 507.1, "x1": 752.1, "y1": 561.1},
        }
    },
    "IGI": {
        "label": "IGI Laboratory Grown Diamond",
        "page_w": 1008, "page_h": 612,
        "zones": {
            "num_top_centre":  {"x0": 360.8, "y0": 34.1,  "x1": 420.9, "y1": 43.6,  "mask_ratio": 0.55},
            "num_left":        {"x0": 181.8, "y0": 150.1,  "x1": 232.5, "y1": 158.1, "mask_ratio": 0.55},
            "num_left_insc":   {"x0": 183.7, "y0": 371.5,  "x1": 233.4, "y1": 379.5, "mask_ratio": 0.55},
            "num_right_top":   {"x0": 940.3, "y0": 75.2,   "x1": 981.9, "y1": 81.2,  "mask_ratio": 0.55},
            "num_right_insc":  {"x0": 933.0, "y0": 355.9,  "x1": 981.9, "y1": 361.9, "mask_ratio": 0.55},
            "num_vert_report": {"x0": 810.6, "y0": 535.8,  "x1": 818.6, "y1": 588.6, "mask_ratio": 0.55, "vertical": True},
            "num_vert_insc":   {"x0": 919.5, "y0": 509.4,  "x1": 927.4, "y1": 540.1, "mask_ratio": 0.55, "vertical": True},
            "num_diamond_high":{"x0": 630.0, "y0": 150.0,  "x1": 710.0, "y1": 168.0, "mask_ratio": 0.55},
            "num_diamond_low": {"x0": 622.0, "y0": 308.0,  "x1": 700.0, "y1": 326.0, "mask_ratio": 0.55},
            "qr":              {"x0": 725.1, "y0": 500.9,  "x1": 768.3, "y1": 544.0},
        }
    },
}

PADDING = 1.5
LOGOS_DIR = Path(__file__).parent / "logos"
LOGOS_DIR.mkdir(exist_ok=True)
HISTORY_FILE = Path(__file__).parent / "history.json"

# ── Copy default logo into logos dir if not already there ────────────────────
default_logo = Path(__file__).parent / "logo_pure_carbon.png"
if default_logo.exists() and not (LOGOS_DIR / "Pure Carbon Group.png").exists():
    import shutil
    shutil.copy(default_logo, LOGOS_DIR / "Pure Carbon Group.png")

# ── History helpers ───────────────────────────────────────────────────────────
def load_history():
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            return []
    return []

def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history[-25:], indent=2))

def add_history_entry(original, cleaned, cert_type):
    history = load_history()
    history.append({
        "original": original,
        "cleaned": cleaned,
        "type": cert_type,
        "time": datetime.datetime.now().strftime("%b %d, %H:%M"),
    })
    save_history(history)

# ── Redaction engine ──────────────────────────────────────────────────────────
def redact_pdf(file_bytes: bytes, cert_type: str, logo_img) -> bytes:
    zones = CERT_ZONES[cert_type]["zones"]
    doc = fitz.open(stream=file_bytes, filetype="pdf")

    for page in doc:
        for key, z in zones.items():
            x0 = z["x0"] - PADDING
            y0 = z["y0"] - PADDING
            x1 = z["x1"] + PADDING
            y1 = z["y1"] + PADDING

            if key == "qr":
                rect = fitz.Rect(x0, y0, x1, y1)
                page.add_redact_annot(rect, fill=(1, 1, 1))
                page.apply_redactions()
                if logo_img is not None:
                    buf = io.BytesIO()
                    logo_img.save(buf, format="PNG")
                    buf.seek(0)
                    page.insert_image(rect, stream=buf.read())
            elif z.get("vertical"):
                ratio = z.get("mask_ratio", 0.55)
                mask_rect = fitz.Rect(x0, y0, x1, y0 + (y1 - y0) * ratio)
                page.add_redact_annot(mask_rect, fill=(1, 1, 1))
                page.apply_redactions()
            else:
                ratio = z.get("mask_ratio", 0.60)
                mask_x1 = x0 + (x1 - x0) * ratio
                mask_rect = fitz.Rect(x0, y0, mask_x1, y1)
                page.add_redact_annot(mask_rect, fill=(1, 1, 1))
                page.apply_redactions()

    out = io.BytesIO()
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()
    out.seek(0)
    return out.read()

# ── Session state ─────────────────────────────────────────────────────────────
if "cert_type" not in st.session_state:
    st.session_state.cert_type = "GIA"
if "selected_logo" not in st.session_state:
    st.session_state.selected_logo = "Pure Carbon Group"
if "upload_key" not in st.session_state:
    st.session_state.upload_key = 0
if "results_ready" not in st.session_state:
    st.session_state.results_ready = None

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="pcg-header">
    <div>
        <div class="pcg-title">Certificate Redaction</div>
        <div class="pcg-sub">Pure Carbon Group</div>
    </div>
</div>
""", unsafe_allow_html=True)

# ── Certificate type selector ─────────────────────────────────────────────────
st.markdown('<div class="section-label">Certificate type</div>', unsafe_allow_html=True)
cols = st.columns(3)
for i, (key, cfg) in enumerate(CERT_ZONES.items()):
    with cols[i]:
        if st.button(
            key,
            key=f"cert_{key}",
            type="primary" if st.session_state.cert_type == key else "secondary",
            use_container_width=True,
        ):
            st.session_state.cert_type = key
            st.session_state.results_ready = None
            st.session_state.upload_key += 1
            st.rerun()

cert_type = st.session_state.cert_type
st.caption(f"*{CERT_ZONES[cert_type]['label']}*")

st.markdown('<hr class="divider">', unsafe_allow_html=True)

# ── Logo selector ─────────────────────────────────────────────────────────────
st.markdown('<div class="section-label">Logo for QR replacement</div>', unsafe_allow_html=True)

logo_files = sorted(LOGOS_DIR.glob("*.png")) + sorted(LOGOS_DIR.glob("*.jpg"))
logo_names = [f.stem for f in logo_files]

if logo_names:
    logo_cols = st.columns(min(len(logo_names), 4))
    for i, name in enumerate(logo_names):
        with logo_cols[i % 4]:
            is_selected = st.session_state.selected_logo == name
            if st.button(
                f"{'✓ ' if is_selected else ''}{name}",
                key=f"logo_{name}",
                type="primary" if is_selected else "secondary",
                use_container_width=True,
            ):
                st.session_state.selected_logo = name
                st.rerun()

# Upload new logo
with st.expander("➕  Add a new logo"):
    new_logo_name = st.text_input("Company name", placeholder="e.g. Vendor ABC")
    new_logo_file = st.file_uploader("Logo image (PNG recommended)", type=["png", "jpg", "jpeg"], key="new_logo")
    if st.button("Save logo") and new_logo_name and new_logo_file:
        img = Image.open(new_logo_file).convert("RGBA")
        save_path = LOGOS_DIR / f"{new_logo_name.strip()}.png"
        img.save(save_path)
        st.session_state.selected_logo = new_logo_name.strip()
        st.success(f"Logo saved: {new_logo_name}")
        st.rerun()

# Load selected logo
logo_img = None
selected_path = LOGOS_DIR / f"{st.session_state.selected_logo}.png"
if not selected_path.exists():
    # Try jpg
    selected_path = LOGOS_DIR / f"{st.session_state.selected_logo}.jpg"
if selected_path.exists():
    logo_img = Image.open(selected_path).convert("RGBA")
    col1, col2 = st.columns([1, 5])
    with col1:
        st.image(logo_img, width=52)
    with col2:
        st.caption(f"Using: **{st.session_state.selected_logo}**")

st.markdown('<hr class="divider">', unsafe_allow_html=True)

# ── File uploader ─────────────────────────────────────────────────────────────
st.markdown('<div class="section-label">Upload certificates</div>', unsafe_allow_html=True)
uploaded_files = st.file_uploader(
    f"Drop {cert_type} certificate PDFs here",
    type="pdf",
    accept_multiple_files=True,
    key=f"uploader_{st.session_state.upload_key}",
    label_visibility="collapsed",
)

if uploaded_files:
    st.write(f"**{len(uploaded_files)} file(s) ready**")

    if st.button("▶  Redact all", type="primary", use_container_width=True):
        results = {}
        prog = st.progress(0)
        status = st.empty()

        for i, f in enumerate(uploaded_files):
            status.text(f"Processing {f.name} …")
            try:
                raw = f.read()
                out_bytes = redact_pdf(raw, cert_type, logo_img)
                stem = Path(f.name).stem
                last4 = ''.join(filter(str.isdigit, stem))[-4:]
                out_name = f"{last4}.pdf"
                results[out_name] = out_bytes
                add_history_entry(f.name, out_name, cert_type)
            except Exception as e:
                st.error(f"Error on {f.name}: {e}")
            prog.progress((i + 1) / len(uploaded_files))

        status.empty()
        prog.empty()
        st.session_state.results_ready = results
        # Clear uploader
        st.session_state.upload_key += 1
        st.rerun()

# ── Download area ─────────────────────────────────────────────────────────────
if st.session_state.results_ready:
    results = st.session_state.results_ready
    st.success(f"✅  {len(results)} certificate(s) redacted")

    if len(results) == 1:
        name, data = next(iter(results.items()))
        st.download_button(
            label=f"⬇  Download  {name}",
            data=data,
            file_name=name,
            mime="application/pdf",
            use_container_width=True,
        )
    else:
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in results.items():
                zf.writestr(name, data)
        st.download_button(
            label=f"⬇  Download all ({len(results)} files) as ZIP",
            data=zip_buf.getvalue(),
            file_name="redacted_certificates.zip",
            mime="application/zip",
            use_container_width=True,
        )

    if st.button("Clear & redact more", use_container_width=True):
        st.session_state.results_ready = None
        st.rerun()

st.markdown('<hr class="divider">', unsafe_allow_html=True)

# ── History ───────────────────────────────────────────────────────────────────
st.markdown('<div class="section-label">Recent files</div>', unsafe_allow_html=True)

history = load_history()

if history:
    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("Clear history", use_container_width=True):
            save_history([])
            st.rerun()

    # Show newest first
    for entry in reversed(history[-25:]):
        st.markdown(f"""
        <div class="history-row">
            <div>
                <div class="history-orig">{entry['original']}</div>
                <div class="history-clean">→ {entry['cleaned']}</div>
            </div>
            <div style="text-align:right;">
                <div class="history-type">{entry['type']}</div>
                <div class="history-time">{entry['time']}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
else:
    st.caption("No files processed yet — history will appear here.")
