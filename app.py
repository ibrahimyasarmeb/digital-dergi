import os
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (
    Flask, request, redirect, url_for, session,
    send_from_directory, abort, Response
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ---------------- CONFIG ----------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "app.db")
UPLOAD_DIR = os.path.join(APP_DIR, "uploads")
THUMB_DIR = os.path.join(APP_DIR, "thumbs")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = "CHANGE_ME_TO_A_LONG_RANDOM_SECRET"

# PDF cover rendering
try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except Exception:
    HAS_FITZ = False

# ✅ 100 puan üzerinden rubrik (her kriterin max puanı)
RUBRIC = [
    ("icerik", "İçerik Kalitesi", 25),
    ("dil", "Dil ve Anlatım", 15),
    ("tasarim", "Tasarım Düzeni", 20),
    ("yaraticilik", "Yaratıcılık", 15),
    ("gorsel", "Görsel Kullanımı", 10),
    ("tamlik", "Bölümlerin Tamlığı", 10),
    ("ekip", "Ekip Çalışması", 5),
]
RUBRIC_MAX = sum(w for _, _, w in RUBRIC)  # 100


# ---------------- DB ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('admin','judge','student')),
      class_name TEXT,
      group_no INTEGER,
      created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_student_class_group
    ON users(class_name, group_no)
    WHERE role='student'
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS submissions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      student_user_id INTEGER NOT NULL,
      title TEXT NOT NULL,
      filename TEXT NOT NULL,
      uploaded_at TEXT NOT NULL,
      FOREIGN KEY(student_user_id) REFERENCES users(id)
    )
    """)

    # ✅ scores: her kriter 0..max puan
    cur.execute("""
    CREATE TABLE IF NOT EXISTS scores (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      submission_id INTEGER NOT NULL,
      judge_user_id INTEGER NOT NULL,
      icerik INTEGER NOT NULL,
      dil INTEGER NOT NULL,
      tasarim INTEGER NOT NULL,
      yaraticilik INTEGER NOT NULL,
      gorsel INTEGER NOT NULL,
      tamlik INTEGER NOT NULL,
      ekip INTEGER NOT NULL,
      note TEXT,
      created_at TEXT NOT NULL,
      UNIQUE(submission_id, judge_user_id),
      FOREIGN KEY(submission_id) REFERENCES submissions(id),
      FOREIGN KEY(judge_user_id) REFERENCES users(id)
    )
    """)
    conn.commit()

    # defaults
    cur.execute("SELECT id FROM users WHERE username=?", ("admin",))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO users(username,password_hash,role,class_name,group_no,created_at) VALUES(?,?,?,?,?,?)",
            ("admin", generate_password_hash("admin123"), "admin", None, None, datetime.utcnow().isoformat())
        )
        conn.commit()

    cur.execute("SELECT id FROM users WHERE username=?", ("judge",))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO users(username,password_hash,role,class_name,group_no,created_at) VALUES(?,?,?,?,?,?)",
            ("judge", generate_password_hash("judge123"), "judge", None, None, datetime.utcnow().isoformat())
        )
        conn.commit()

    conn.close()


init_db()


# ---------------- HELPERS ----------------
def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            if role and session.get("role") != role:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return deco


def fmt_dt(iso_text: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_text.replace("Z", ""))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso_text


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def score_total(row) -> int:
    return int(row["icerik"] + row["dil"] + row["tasarim"] + row["yaraticilik"] +
               row["gorsel"] + row["tamlik"] + row["ekip"])


def delete_submission_assets(filename: str):
    """Remove uploaded PDF and cached thumb if exists."""
    if filename:
        try:
            os.remove(os.path.join(UPLOAD_DIR, filename))
        except Exception:
            pass
        # thumb cache name = secure_filename(filename) + ".png"
        try:
            safe = secure_filename(filename) + ".png"
            os.remove(os.path.join(THUMB_DIR, safe))
        except Exception:
            pass


def page(title: str, body_html: str):
    logged = "user_id" in session
    role = session.get("role")

    nav = ["<a class='navlink' href='/'>Ana Sayfa</a>"]
    if logged and role == "student":
        nav.append("<a class='navlink' href='/student'>Öğrenci</a>")
    if logged and role == "judge":
        nav.append("<a class='navlink' href='/judge'>Jüri</a>")
        nav.append("<a class='navlink' href='/results'>Sonuçlar</a>")
    if logged and role == "admin":
        nav.append("<a class='navlink' href='/admin'>Yönetim</a>")
        nav.append("<a class='navlink' href='/results'>Sonuçlar</a>")

    auth = "<a class='btn btn-ghost' href='/logout'>Çıkış</a>" if logged else "<a class='btn' href='/login'>Giriş</a>"

    return f"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<style>
  :root {{
    --bg1:#0b1020;
    --bg2:#081226;
    --card: rgba(255,255,255,.10);
    --card2: rgba(255,255,255,.06);
    --stroke: rgba(255,255,255,.14);
    --text: rgba(255,255,255,.92);
    --muted: rgba(255,255,255,.72);

    --brand:#4f8cff;
    --mint:#2fe6c6;
    --pink:#ff4fd8;
    --sun:#ffd25a;

    --wood1:#91602f;
    --wood2:#6f4320;
  }}

  body {{
    margin:0;
    font-family: system-ui,-apple-system,Segoe UI,Roboto,Arial;
    color: var(--text);
    background:
      radial-gradient(900px 520px at 15% 0%, rgba(79,140,255,.42), transparent 58%),
      radial-gradient(820px 520px at 85% 8%, rgba(47,230,198,.25), transparent 60%),
      radial-gradient(900px 620px at 55% 110%, rgba(255,79,216,.12), transparent 60%),
      linear-gradient(180deg, var(--bg1), var(--bg2));
  }}

  header {{
    position: sticky; top: 0; z-index: 50;
    backdrop-filter: blur(10px);
    background: rgba(6,10,18,.55);
    border-bottom: 1px solid rgba(255,255,255,.10);
  }}
  .wrap {{ max-width: 1220px; margin: 0 auto; padding: 14px 16px; }}
  .topbar {{ display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }}

  .brand {{ display:flex; gap:10px; align-items:center; }}
  .brand .logo {{
    width:38px;height:38px;border-radius:14px;
    display:flex;align-items:center;justify-content:center;
    background: linear-gradient(135deg, rgba(79,140,255,.35), rgba(47,230,198,.22));
    border: 1px solid rgba(255,255,255,.14);
    box-shadow: 0 14px 35px rgba(0,0,0,.25);
    font-size:18px;
  }}
  .brand b {{ font-size: 15px; letter-spacing:.2px; }}
  .brand span {{ font-size: 12px; color: var(--muted); }}

  .nav {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
  .navlink {{
    color: var(--text);
    text-decoration:none;
    padding: 8px 10px;
    border-radius: 12px;
    opacity:.90;
  }}
  .navlink:hover {{ background: rgba(255,255,255,.08); opacity:1; }}

  .btn {{
    display:inline-flex; gap:8px; align-items:center; justify-content:center;
    padding: 10px 12px;
    border-radius: 12px;
    font-weight: 900;
    text-decoration:none;
    color: #061018;
    background: linear-gradient(135deg, rgba(79,140,255,1), rgba(47,230,198,1));
    box-shadow: 0 16px 35px rgba(0,0,0,.25);
  }}
  .btn:hover {{ filter: brightness(.97); }}
  .btn-ghost {{
    color: var(--text);
    background: rgba(255,255,255,.10);
    border: 1px solid rgba(255,255,255,.16);
    box-shadow: none;
  }}
  .btn-danger {{
    color: #fff;
    background: linear-gradient(135deg, #ff3b3b, #ff7a7a);
  }}
  .btn-lite {{
    display:inline-flex; gap:8px; align-items:center; justify-content:center;
    padding: 10px 12px; border-radius: 12px; font-weight: 900; text-decoration:none;
    color: var(--text);
    background: rgba(255,255,255,.10);
    border: 1px solid rgba(255,255,255,.16);
  }}
  .btn-lite:hover {{ background: rgba(255,255,255,.14); }}

  main {{ max-width: 1220px; margin: 18px auto; padding: 0 16px 28px; }}

  .panelrow {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom: 14px; }}
  .panel {{
    flex:1; min-width: 280px;
    background: var(--card2);
    border: 1px solid var(--stroke);
    border-radius: 20px;
    padding: 14px;
    box-shadow: 0 28px 65px rgba(0,0,0,.35);
  }}
  .panel b {{ font-size: 14px; }}
  .panel .small {{ color: var(--muted); font-size: 12px; line-height: 1.5; margin-top: 6px; }}

  /* --- Shelf scene --- */
  .scene {{
    border-radius: 22px;
    overflow: hidden;
    border: 1px solid rgba(255,255,255,.12);
    background:
      radial-gradient(900px 520px at 22% 18%, rgba(255,255,255,.10), transparent 60%),
      radial-gradient(900px 520px at 86% 26%, rgba(79,140,255,.14), transparent 62%),
      linear-gradient(180deg, rgba(255,255,255,.05), rgba(0,0,0,.20));
    box-shadow: 0 34px 85px rgba(0,0,0,.42);
    padding: 18px;
  }}

  .scenehead {{
    display:flex; justify-content:space-between; align-items:flex-start;
    gap:12px; flex-wrap:wrap; margin-bottom: 12px;
  }}
  .scenehead h1 {{ margin:0; font-size: 18px; letter-spacing:.2px; }}
  .scenehead p {{ margin: 6px 0 0 0; color: var(--muted); max-width: 860px; line-height: 1.5; }}

  .badge {{
    font-size: 12px; color: var(--muted);
  }}
  .badge code {{
    background: rgba(255,255,255,.10);
    border: 1px solid rgba(255,255,255,.14);
    padding: 2px 8px;
    border-radius: 999px;
  }}

  .shelf {{ position:relative; margin-top: 14px; padding-bottom: 18px; }}
  .wood {{
    position:absolute; left:0; right:0; bottom: 6px;
    height: 24px;
    border-radius: 14px;
    background: linear-gradient(90deg, var(--wood2), var(--wood1), var(--wood2));
    border: 1px solid rgba(255,255,255,.10);
    box-shadow: 0 14px 30px rgba(0,0,0,.40);
  }}
  .books {{
    display:flex;
    gap: 18px;
    align-items: flex-end;
    flex-wrap: wrap;
    padding: 10px 6px 32px;
  }}

  .book {{
    width: 178px;
    text-decoration: none;
    color: var(--text);
  }}

  .cover {{
    width: 178px;
    aspect-ratio: 3 / 4;
    border-radius: 18px;
    overflow: hidden;
    border: 1px solid rgba(255,255,255,.14);
    background: rgba(255,255,255,.06);
    box-shadow: 0 20px 50px rgba(0,0,0,.50);
    transform-origin: 50% 100%;
    transition: transform .16s ease, box-shadow .16s ease, filter .16s ease;
  }}
  .cover img {{ width:100%; height:100%; object-fit: cover; display:block; }}

  /* ✅ Hover büyüme animasyonu */
  .book:hover .cover {{
    transform: scale(1.07) rotate(-1deg);
    box-shadow: 0 30px 65px rgba(0,0,0,.60);
    filter: saturate(1.07) contrast(1.02);
  }}

  .meta {{ margin-top: 10px; }}
  .pill {{
    display:inline-flex; align-items:center; gap:6px;
    background: rgba(255,255,255,.10);
    border: 1px solid rgba(255,255,255,.14);
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 12px;
  }}
  .title {{
    margin: 8px 0 0 0;
    font-size: 15px;
    line-height: 1.25;
    font-weight: 900;
  }}
  .sub {{ margin: 6px 0 0 0; font-size: 12px; color: var(--muted); }}

  .scorechip {{
    display:inline-flex; gap:8px; align-items:center;
    margin-top: 8px;
    padding: 6px 10px;
    border-radius: 14px;
    border: 1px solid rgba(255,255,255,.14);
    background: rgba(255,255,255,.08);
    font-size: 12px;
    color: var(--text);
  }}
  .dot {{
    width:10px; height:10px; border-radius: 999px;
    background: var(--mint);
    box-shadow: 0 0 0 4px rgba(47,230,198,.15);
  }}

  .empty {{
    background: rgba(255,255,255,.07);
    border: 1px solid rgba(255,255,255,.12);
    border-radius: 18px;
    padding: 18px;
    color: var(--text);
  }}

  /* Forms */
  .formcard {{
    max-width: 980px;
    margin: 0 auto;
    background: rgba(255,255,255,.96);
    color:#0f172a;
    border-radius: 20px;
    border: 1px solid #e5e7eb;
    box-shadow: 0 28px 75px rgba(0,0,0,.40);
    padding: 16px;
  }}
  label {{ font-size: 12px; color: #64748b; font-weight: 900; }}
  input, select, textarea {{
    width: 100%;
    padding: 10px 12px;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    box-sizing: border-box;
    font-family: inherit;
  }}
  textarea {{ resize: vertical; }}

  button {{
    border:0;
    border-radius: 12px;
    padding: 10px 12px;
    font-weight: 900;
    cursor: pointer;
    color:#061018;
    background: linear-gradient(135deg, rgba(79,140,255,1), rgba(47,230,198,1));
  }}
  button:hover {{ filter: brightness(.97); }}

  .alert-ok {{
    margin-top:12px;
    background:#ecfff1;
    border:1px solid #bbf7d0;
    color:#166534;
    border-radius:12px;
    padding:10px 12px;
    font-weight: 800;
  }}
  .alert-bad {{
    margin-top:12px;
    background:#ffecec;
    border:1px solid #fecaca;
    color:#b91c1c;
    border-radius:12px;
    padding:10px 12px;
    font-weight: 800;
  }}

  .viewer {{
    width: 100%;
    height: 78vh;
    border-radius: 20px;
    overflow: hidden;
    border: 1px solid rgba(255,255,255,.14);
    box-shadow: 0 26px 70px rgba(0,0,0,.55);
    background: rgba(255,255,255,.05);
  }}

  table {{ width:100%; border-collapse: collapse; }}
  th, td {{ padding: 10px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }}
  th {{ text-align: left; color: #64748b; font-size: 12px; }}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <div class="topbar">
      <div class="brand">
        <div class="logo">📖</div>
        <div>
          <b>Dijital Dergi Yarışması</b><br/>
          <span>vitrin + jüri</span>
        </div>
      </div>
      <div class="nav">
        {"".join(nav)}
        {auth}
      </div>
    </div>
  </div>
</header>
<main>
{body_html}
</main>
</body>
</html>"""


# ---------------- THUMB: PDF first page -> cover image ----------------
@app.get("/thumb/<path:pdf_filename>.png")
def thumb(pdf_filename):
    pdf_path = os.path.join(UPLOAD_DIR, pdf_filename)
    if not os.path.exists(pdf_path):
        abort(404)

    safe = secure_filename(pdf_filename)
    out_name = safe + ".png"
    out_path = os.path.join(THUMB_DIR, out_name)

    if os.path.exists(out_path):
        return send_from_directory(THUMB_DIR, out_name)

    if not HAS_FITZ:
        svg = """<svg xmlns="http://www.w3.org/2000/svg" width="600" height="800">
          <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#4f8cff"/><stop offset="1" stop-color="#2fe6c6"/></linearGradient></defs>
          <rect width="100%" height="100%" fill="url(#g)"/>
          <rect x="75" y="95" width="450" height="610" rx="26" fill="rgba(255,255,255,.18)" stroke="rgba(255,255,255,.35)" stroke-width="3"/>
          <text x="50%" y="48%" text-anchor="middle" font-family="Arial" font-size="38" fill="#0b1020">PDF</text>
          <text x="50%" y="55%" text-anchor="middle" font-family="Arial" font-size="18" fill="rgba(11,16,32,.9)">Kapak için pymupdf</text>
        </svg>"""
        return Response(svg, mimetype="image/svg+xml")

    try:
        doc = fitz.open(pdf_path)
        page0 = doc.load_page(0)
        mat = fitz.Matrix(2.2, 2.2)
        pix = page0.get_pixmap(matrix=mat, alpha=False)
        pix.save(out_path)
        doc.close()
    except Exception:
        svg = """<svg xmlns="http://www.w3.org/2000/svg" width="600" height="800">
          <rect width="100%" height="100%" fill="#7f1d1d"/>
          <text x="50%" y="50%" text-anchor="middle" font-family="Arial" font-size="22" fill="#ffffff">Kapak üretilemedi</text>
        </svg>"""
        return Response(svg, mimetype="image/svg+xml")

    return send_from_directory(THUMB_DIR, out_name)


# ---------------- HOME ----------------
@app.get("/")
def home():
    logged = "user_id" in session
    role = session.get("role")

    conn = db()
    cur = conn.cursor()
    cur.execute("""
      SELECT s.id AS sid, s.title, s.filename, s.uploaded_at,
             u.class_name, u.group_no
      FROM submissions s
      JOIN users u ON u.id=s.student_user_id
      ORDER BY s.uploaded_at DESC
      LIMIT 240
    """)
    rows = cur.fetchall()

    stats = {}
    if logged:
        cur.execute("""
          SELECT s.id AS sid,
                 COUNT(sc.id) AS judge_count,
                 AVG(sc.icerik + sc.dil + sc.tasarim + sc.yaraticilik + sc.gorsel + sc.tamlik + sc.ekip) AS avg_total
          FROM submissions s
          LEFT JOIN scores sc ON sc.submission_id=s.id
          GROUP BY s.id
        """)
        for r in cur.fetchall():
            stats[int(r["sid"])] = {
                "judge_count": int(r["judge_count"] or 0),
                "avg_total": None if r["avg_total"] is None else float(r["avg_total"]),
            }
    conn.close()

    top_panel = ""
    if logged:
        if role == "student":
            btn = "<a class='btn' href='/student'>Öğrenci Paneli</a>"
        elif role == "judge":
            btn = "<a class='btn' href='/judge'>Jüri Paneli</a>"
        elif role == "admin":
            btn = "<a class='btn' href='/admin'>Yönetim</a>"
        else:
            btn = ""
        top_panel = f"""
        <div class="panelrow">
          <div class="panel">
            <b>Hoş geldin!</b>
            <div class="small">Kapaklara tıklayınca sunum açılır. Giriş yapanlar ortalama puanları görebilir.</div>
            <div style="margin-top:10px">{btn}</div>
          </div>
        </div>
        """

    if not rows:
        body = top_panel + """
        <div class="scene">
          <div class="empty"><b>Henüz yüklenen dergi yok.</b><div style="margin-top:8px;color:var(--muted)">İlk yükleme yapıldığında raflarda görünecek.</div></div>
        </div>
        """
        return page("Ana Sayfa", body)

    half = (len(rows) + 1) // 2
    shelf1, shelf2 = rows[:half], rows[half:]

    def render_shelf(items):
        out = ["<div class='books'>"]
        for r in items:
            sid = int(r["sid"])
            cover_url = f"/thumb/{r['filename']}.png"

            score_html = ""
            if logged:
                st = stats.get(sid, {"judge_count": 0, "avg_total": None})
                avg = st["avg_total"]
                jc = st["judge_count"]
                if avg is None:
                    score_html = f"<div class='scorechip'><span class='dot' style='background:var(--sun);box-shadow:0 0 0 4px rgba(255,210,90,.15)'></span> Ortalama: - / {RUBRIC_MAX} · Jüri: {jc}</div>"
                else:
                    score_html = f"<div class='scorechip'><span class='dot'></span> Ortalama: {avg:.2f} / {RUBRIC_MAX} · Jüri: {jc}</div>"

            out.append(f"""
            <a class="book" href="/present/{sid}">
              <div class="cover"><img src="{cover_url}" alt="Kapak"></div>
              <div class="meta">
                <div class="pill">🏷️ {r['class_name']} Grup {int(r['group_no'])}</div>
                <div class="title">{r['title']}</div>
                <div class="sub">🕒 {fmt_dt(r['uploaded_at'])}</div>
                {score_html}
              </div>
            </a>
            """)
        out.append("</div><div class='wood'></div>")
        return "\n".join(out)

    body = f"""
    {top_panel}
    <div class="scene">
      <div class="scenehead">
        <div>
          <h1>Yayın Vitrini</h1>
          <p>Kapaklara tıklayınca sunum açılır. Misafirler puanları görmez.</p>
        </div>
        <div class="badge">Kapak motoru: <code>{"pymupdf ✅" if HAS_FITZ else "pymupdf ❌"}</code></div>
      </div>

      <div class="shelf">{render_shelf(shelf1)}</div>
      <div class="shelf">{render_shelf(shelf2)}</div>
    </div>
    """
    return page("Ana Sayfa", body)


# ---------------- PRESENT ----------------
@app.get("/present/<int:submission_id>")
def present(submission_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
      SELECT s.id, s.title, s.filename, s.uploaded_at,
             u.class_name, u.group_no
      FROM submissions s
      JOIN users u ON u.id=s.student_user_id
      WHERE s.id=?
    """, (submission_id,))
    s = cur.fetchone()
    if not s:
        conn.close()
        abort(404)

    avg_txt = "-"
    jc = 0
    if "user_id" in session:
        cur.execute("""
          SELECT COUNT(*) AS judge_count,
                 AVG(icerik + dil + tasarim + yaraticilik + gorsel + tamlik + ekip) AS avg_total
          FROM scores
          WHERE submission_id=?
        """, (submission_id,))
        st = cur.fetchone()
        jc = int(st["judge_count"] or 0)
        if st["avg_total"] is not None:
            avg_txt = f"{float(st['avg_total']):.2f}"

    my_score_html = ""
    judge_btn = ""
    if session.get("role") == "judge":
        judge_btn = f"<a class='btn' href='/judge/score/{submission_id}'>🧑‍⚖️ Puanla / Düzenle</a>"
        cur.execute("SELECT * FROM scores WHERE submission_id=? AND judge_user_id=?", (submission_id, session["user_id"]))
        mine = cur.fetchone()
        if mine:
            my_score_html = f"<div class='scorechip' style='margin-top:10px;'><span class='dot'></span> Benim puanım: <b>{score_total(mine)} / {RUBRIC_MAX}</b></div>"
        else:
            my_score_html = f"<div class='scorechip' style='margin-top:10px;'><span class='dot' style='background:var(--pink);box-shadow:0 0 0 4px rgba(255,79,216,.12)'></span> Henüz puanlamadın</div>"

    conn.close()

    score_bar = ""
    if "user_id" in session:
        score_bar = f"<div class='scorechip' style='margin-top:10px;'><span class='dot'></span> Ortalama: <b>{avg_txt} / {RUBRIC_MAX}</b> · Jüri: <b>{jc}</b></div>"

    body = f"""
    <div class="scene" style="padding:16px;">
      <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:flex-start;">
        <div>
          <div class="pill">🏷️ {s['class_name']} Grup {int(s['group_no'])}</div>
          <h1 style="margin:10px 0 6px 0;font-size:20px;">{s['title']}</h1>
          <div class="sub">🕒 {fmt_dt(s['uploaded_at'])}</div>
          {score_bar}
          {my_score_html}
        </div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
          <a class="btn-lite" href="/">⬅ Galeri</a>
          {judge_btn}
        </div>
      </div>

      <div style="margin-top:14px" class="viewer">
        <embed src="/file/{s['filename']}#toolbar=1&navpanes=0&scrollbar=1"
               type="application/pdf" width="100%" height="100%"/>
      </div>
    </div>
    """
    return page("Sunum", body)


# ---------------- AUTH ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username=?", (username,))
        u = cur.fetchone()
        conn.close()

        if u and check_password_hash(u["password_hash"], password):
            session["user_id"] = u["id"]
            session["role"] = u["role"]
            session["username"] = u["username"]
            session["class_name"] = u["class_name"]
            session["group_no"] = u["group_no"]
            return redirect(url_for("home"))

        err = "Hatalı kullanıcı adı veya şifre."

    body = f"""
    <div class="formcard" style="max-width:560px;">
      <h2 style="margin:0 0 10px 0;">Giriş</h2>
      {f"<div class='alert-bad'>{err}</div>" if err else ""}
      <form method="post" style="display:flex;flex-direction:column;gap:10px;margin-top:10px;">
        <div>
          <label>Kullanıcı adı</label>
          <input name="username" required>
        </div>
        <div>
          <label>Şifre</label>
          <input name="password" type="password" required>
        </div>
        <button type="submit">Giriş Yap</button>
      </form>
      <div style="margin-top:10px;font-size:12px;color:#64748b;">
        Kapak için: <b>py -m pip install pymupdf</b><br/>
        Varsayılan admin: <b>admin / admin123</b> · Varsayılan jüri: <b>judge / judge123</b>
      </div>
    </div>
    """
    return page("Giriş", body)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.get("/file/<path:filename>")
def file(filename):
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)


# ---------------- STUDENT ----------------
@app.route("/student", methods=["GET", "POST"])
@login_required("student")
def student():
    msg = ""
    ok = True

    conn = db()
    cur = conn.cursor()

    # current submission + avg
    cur.execute("""
      SELECT s.id AS sid, s.title, s.filename, s.uploaded_at
      FROM submissions s
      WHERE s.student_user_id=?
      ORDER BY s.uploaded_at DESC
      LIMIT 1
    """, (session["user_id"],))
    current = cur.fetchone()

    avg_txt = "-"
    jc = 0
    if current:
        cur.execute("""
          SELECT COUNT(*) AS judge_count,
                 AVG(icerik + dil + tasarim + yaraticilik + gorsel + tamlik + ekip) AS avg_total
          FROM scores WHERE submission_id=?
        """, (int(current["sid"]),))
        st = cur.fetchone()
        jc = int(st["judge_count"] or 0)
        if st["avg_total"] is not None:
            avg_txt = f"{float(st['avg_total']):.2f}"

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        f = request.files.get("file")

        if not title:
            msg = "Dergi adı gerekli."
            ok = False
        elif not f or not f.filename.lower().endswith(".pdf"):
            msg = "Sadece PDF yüklenebilir."
            ok = False
        else:
            uid = session["user_id"]
            safe = secure_filename(f.filename)
            stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            stored = f"u{uid}_{stamp}_{safe}"
            f.save(os.path.join(UPLOAD_DIR, stored))

            # single submission: delete old (files+scores+thumb)
            cur.execute("SELECT id, filename FROM submissions WHERE student_user_id=? ORDER BY uploaded_at DESC LIMIT 1", (uid,))
            old = cur.fetchone()
            if old:
                cur.execute("DELETE FROM scores WHERE submission_id=?", (old["id"],))
                cur.execute("DELETE FROM submissions WHERE id=?", (old["id"],))
                conn.commit()
                delete_submission_assets(old["filename"])

            cur.execute(
                "INSERT INTO submissions(student_user_id,title,filename,uploaded_at) VALUES(?,?,?,?)",
                (uid, title, stored, datetime.utcnow().isoformat())
            )
            conn.commit()
            msg = "Yükleme tamamlandı."
            ok = True

            # refresh current
            cur.execute("""
              SELECT s.id AS sid, s.title, s.filename, s.uploaded_at
              FROM submissions s
              WHERE s.student_user_id=?
              ORDER BY s.uploaded_at DESC
              LIMIT 1
            """, (uid,))
            current = cur.fetchone()
            avg_txt = "-"
            jc = 0

    conn.close()

    info = ""
    if current:
        info = f"""
        <div class="panel" style="margin-top:12px;">
          <b>Mevcut Gönderin</b>
          <div class="small" style="margin-top:6px;">
            <div><b>{current['title']}</b></div>
            <div>🕒 {fmt_dt(current['uploaded_at'])}</div>
            <div style="margin-top:8px;">
              <span class="scorechip"><span class="dot"></span> Ortalama: <b>{avg_txt} / {RUBRIC_MAX}</b> · Jüri: <b>{jc}</b></span>
            </div>
            <div style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap;">
              <a class="btn-lite" href="/present/{int(current['sid'])}">Sunuma Git</a>

              <!-- ✅ İÇERİK KALDIR -->
              <form method="post" action="/student/delete" onsubmit="return confirm('Gönderini ve jüri puanlarını SİLMEK istediğine emin misin?');">
                <button class="btn btn-danger" type="submit">🗑️ İçeriği Kaldır</button>
              </form>
            </div>
          </div>
        </div>
        """

    body = f"""
    <div class="formcard" style="max-width:820px;">
      <h2 style="margin:0 0 8px 0;">Öğrenci Paneli</h2>
      <div style="color:#64748b;font-size:12px;">
        Bu hesap bir grubu temsil eder: <b>{(session.get("class_name") or "")} Grup {session.get("group_no")}</b>
      </div>

      {f"<div class='{'alert-ok' if ok else 'alert-bad'}'>{msg}</div>" if msg else ""}

      <form method="post" enctype="multipart/form-data" style="margin-top:12px;display:flex;flex-direction:column;gap:10px;">
        <div>
          <label>Dergi adı</label>
          <input name="title" required placeholder="örn: Bilim ve Teknoloji">
        </div>
        <div>
          <label>PDF dosyası</label>
          <input name="file" type="file" accept=".pdf" required>
        </div>
        <button type="submit">Yükle</button>
      </form>

      <div style="margin-top:10px;color:#64748b;font-size:12px;">
        Not: Yeniden yüklersen eski gönderin ve jüri puanları silinir (tek gönderi).
      </div>

      {info}
    </div>
    """
    return page("Öğrenci", body)


# ✅ Student delete route
@app.post("/student/delete")
@login_required("student")
def student_delete():
    uid = session["user_id"]
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT id, filename FROM submissions WHERE student_user_id=? ORDER BY uploaded_at DESC LIMIT 1", (uid,))
    s = cur.fetchone()
    if not s:
        conn.close()
        return redirect(url_for("student"))

    # delete scores then submission
    cur.execute("DELETE FROM scores WHERE submission_id=?", (s["id"],))
    cur.execute("DELETE FROM submissions WHERE id=?", (s["id"],))
    conn.commit()
    conn.close()

    delete_submission_assets(s["filename"])
    return redirect(url_for("student"))


# ---------------- JUDGE ----------------
@app.get("/judge")
@login_required("judge")
def judge():
    body = """
    <div class="panelrow">
      <div class="panel">
        <b>Jüri Paneli</b>
        <div class="small">Vitrinden bir dergi aç → sunum sayfasında “Puanla / Düzenle” var.</div>
        <div style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap;">
          <a class="btn-lite" href="/">Vitrine Git</a>
          <a class="btn-lite" href="/results">Sonuçlar</a>
        </div>
      </div>
    </div>
    """
    return page("Jüri", body)


@app.route("/judge/score/<int:submission_id>", methods=["GET", "POST"])
@login_required("judge")
def judge_score(submission_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
      SELECT s.id, s.title, s.uploaded_at,
             u.class_name, u.group_no
      FROM submissions s
      JOIN users u ON u.id=s.student_user_id
      WHERE s.id=?
    """, (submission_id,))
    sub = cur.fetchone()
    if not sub:
        conn.close()
        abort(404)

    judge_id = session["user_id"]
    cur.execute("SELECT * FROM scores WHERE submission_id=? AND judge_user_id=?", (submission_id, judge_id))
    existing = cur.fetchone()

    msg = ""
    ok = True

    if request.method == "POST":
        vals = {}
        for key, _, maxp in RUBRIC:
            try:
                v = int(request.form.get(key, "0"))
            except Exception:
                v = 0
            vals[key] = clamp(v, 0, maxp)

        note = (request.form.get("note") or "").strip()

        if existing:
            cur.execute("""
              UPDATE scores
              SET icerik=?, dil=?, tasarim=?, yaraticilik=?, gorsel=?, tamlik=?, ekip=?, note=?, created_at=?
              WHERE submission_id=? AND judge_user_id=?
            """, (
                vals["icerik"], vals["dil"], vals["tasarim"], vals["yaraticilik"],
                vals["gorsel"], vals["tamlik"], vals["ekip"],
                note, datetime.utcnow().isoformat(),
                submission_id, judge_id
            ))
        else:
            cur.execute("""
              INSERT INTO scores(submission_id, judge_user_id, icerik, dil, tasarim, yaraticilik, gorsel, tamlik, ekip, note, created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (
                submission_id, judge_id,
                vals["icerik"], vals["dil"], vals["tasarim"], vals["yaraticilik"],
                vals["gorsel"], vals["tamlik"], vals["ekip"],
                note, datetime.utcnow().isoformat()
            ))

        conn.commit()
        msg = "Puan kaydedildi."
        ok = True

        cur.execute("SELECT * FROM scores WHERE submission_id=? AND judge_user_id=?", (submission_id, judge_id))
        existing = cur.fetchone()

    def getv(k):
        return int(existing[k]) if existing else 0

    saved_total = score_total(existing) if existing else 0

    rows = []
    for key, label, maxp in RUBRIC:
        rows.append(f"""
        <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;margin:10px 0;padding:10px;border-radius:14px;background:#f8fafc;border:1px solid #e5e7eb;">
          <div style="min-width:260px;">
            <b>{label}</b>
            <div style="color:#64748b;font-size:12px;">Maks: {maxp} puan</div>
          </div>
          <div style="min-width:190px;flex:1;">
            <input type="number" name="{key}" min="0" max="{maxp}" value="{getv(key)}" oninput="recalc()" />
          </div>
        </div>
        """)

    msg_box = f"<div class='{'alert-ok' if ok else 'alert-bad'}'>{msg}</div>" if msg else ""

    body = f"""
    <div class="formcard" style="max-width:920px;">
      <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:flex-start;">
        <div>
          <span style="background:#eef3ff;color:#1e66d0;border-radius:999px;padding:4px 10px;font-size:12px;">
            {sub['class_name']} Grup {int(sub['group_no'])}
          </span>
          <h2 style="margin:10px 0 6px 0;">{sub['title']}</h2>
          <div style="color:#64748b;font-size:12px;">🕒 {fmt_dt(sub['uploaded_at'])}</div>
        </div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;">
          <a class="btn btn-ghost" href="/present/{submission_id}">⬅ Sunuma Dön</a>
          <a class="btn btn-ghost" href="/results">Sonuçlar</a>
        </div>
      </div>

      {msg_box}

      <div style="margin-top:12px;background:#eef3ff;border:1px solid #dbeafe;color:#1e3a8a;border-radius:12px;padding:10px 12px;font-weight:900;">
        Toplam (anlık): <span id="liveTotal">{saved_total}</span> / {RUBRIC_MAX}
      </div>

      <form method="post" style="margin-top:12px;">
        {"".join(rows)}
        <div style="margin-top:12px;">
          <label>Not (opsiyonel)</label>
          <textarea name="note" rows="3">{(existing["note"] if existing and existing["note"] else "")}</textarea>
        </div>
        <div style="margin-top:12px;">
          <button type="submit">Kaydet</button>
        </div>
      </form>
    </div>

    <script>
      function recalc(){{
        let total = 0;
        const fields = {{ {",".join([f"'{k}':{m}" for k,_,m in RUBRIC])} }};
        for (const k in fields){{
          const el = document.querySelector("input[name='"+k+"']");
          if(!el) continue;
          let v = parseInt(el.value || "0", 10);
          if (isNaN(v)) v = 0;
          if (v < 0) v = 0;
          if (v > fields[k]) v = fields[k];
          el.value = v;
          total += v;
        }}
        document.getElementById("liveTotal").innerText = total;
      }}
      recalc();
    </script>
    """
    conn.close()
    return page("Puanlama", body)


# ---------------- RESULTS ----------------
@app.get("/results")
def results():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = db()
    cur = conn.cursor()
    cur.execute("""
      SELECT s.id AS sid, s.title, s.uploaded_at, s.filename,
             u.class_name, u.group_no,
             COUNT(sc.id) AS judge_count,
             AVG(sc.icerik + sc.dil + sc.tasarim + sc.yaraticilik + sc.gorsel + sc.tamlik + sc.ekip) AS avg_total
      FROM submissions s
      JOIN users u ON u.id=s.student_user_id
      LEFT JOIN scores sc ON sc.submission_id=s.id
      GROUP BY s.id
      ORDER BY (avg_total IS NULL) ASC, avg_total DESC, s.uploaded_at DESC
      LIMIT 400
    """)
    rows = cur.fetchall()
    conn.close()

    tr = []
    rank = 0
    for r in rows:
        rank += 1
        avg = "-" if r["avg_total"] is None else f"{float(r['avg_total']):.2f}"
        tr.append(f"""
        <tr>
          <td>{rank}</td>
          <td><b>{r['class_name']} Grup {int(r['group_no'])}</b><div style="color:#64748b;font-size:12px;">{r['title']}</div></td>
          <td>{avg} / {RUBRIC_MAX}</td>
          <td>{int(r['judge_count'] or 0)}</td>
          <td><a href="/present/{int(r['sid'])}">Sunum</a></td>
        </tr>
        """)

    body = f"""
    <div class="formcard">
      <h2 style="margin:0 0 10px 0;">Sonuçlar</h2>
      <div style="color:#64748b;font-size:12px;margin-bottom:10px;">Sıralama: Ortalama puana göre (100 üzerinden).</div>
      <table>
        <tr>
          <th>#</th><th>Grup / Dergi</th><th>Ortalama</th><th>Jüri</th><th>Aç</th>
        </tr>
        {''.join(tr) if tr else "<tr><td colspan='5' style='color:#64748b;'>Henüz sonuç yok.</td></tr>"}
      </table>
    </div>
    """
    return page("Sonuçlar", body)


# ---------------- ADMIN ----------------
@app.route("/admin", methods=["GET", "POST"])
@login_required("admin")
def admin():
    msg = ""
    ok = True

    # create user
    if request.method == "POST" and request.form.get("action") == "create_user":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        role = (request.form.get("role") or "student").strip()
        class_name = (request.form.get("class_name") or "").strip().upper()
        group_no = request.form.get("group_no") or ""

        if role not in ("admin", "judge", "student"):
            role = "student"

        gno = None
        if role == "student":
            try:
                gno = int(group_no)
            except Exception:
                gno = None
            if not class_name or not gno:
                msg = "Student için sınıf ve grup no gerekli (örn: 9A / 1)."
                ok = False

        if not username or not password:
            msg = "Kullanıcı adı ve şifre gerekli."
            ok = False

        if ok:
            conn = db()
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO users(username,password_hash,role,class_name,group_no,created_at) VALUES(?,?,?,?,?,?)",
                    (username, generate_password_hash(password), role,
                     class_name if role == "student" else None,
                     gno if role == "student" else None,
                     datetime.utcnow().isoformat())
                )
                conn.commit()
                msg = "Kullanıcı oluşturuldu."
                ok = True
            except sqlite3.IntegrityError:
                msg = "Bu kullanıcı adı veya sınıf+grup no zaten var."
                ok = False
            conn.close()

    # delete submission (admin)
    if request.method == "POST" and request.form.get("action") == "delete_submission":
        sid = request.form.get("sid")
        try:
            sid = int(sid)
        except Exception:
            sid = None

        if sid:
            conn = db()
            cur = conn.cursor()
            cur.execute("SELECT id, filename FROM submissions WHERE id=?", (sid,))
            s = cur.fetchone()
            if s:
                cur.execute("DELETE FROM scores WHERE submission_id=?", (sid,))
                cur.execute("DELETE FROM submissions WHERE id=?", (sid,))
                conn.commit()
                delete_submission_assets(s["filename"])
                msg = f"Gönderi silindi (ID: {sid})."
                ok = True
            else:
                msg = "Gönderi bulunamadı."
                ok = False
            conn.close()

    # list users
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT username, role, class_name, group_no FROM users ORDER BY role, class_name, group_no, username")
    users = cur.fetchall()

    # list submissions for admin controls
    cur.execute("""
      SELECT s.id AS sid, s.title, s.uploaded_at, u.class_name, u.group_no
      FROM submissions s
      JOIN users u ON u.id=s.student_user_id
      ORDER BY s.uploaded_at DESC
      LIMIT 200
    """)
    subs = cur.fetchall()
    conn.close()

    user_rows = ""
    for u in users:
        grp = f"{u['class_name']} Grup {int(u['group_no'])}" if u["role"] == "student" else "-"
        user_rows += f"<tr><td>{u['username']}</td><td>{u['role']}</td><td>{grp}</td></tr>"

    sub_rows = ""
    for s in subs:
        sub_rows += f"""
        <tr>
          <td>{int(s['sid'])}</td>
          <td><b>{s['class_name']} Grup {int(s['group_no'])}</b><div style="color:#64748b;font-size:12px;">{s['title']}</div></td>
          <td>{fmt_dt(s['uploaded_at'])}</td>
          <td>
            <a href="/present/{int(s['sid'])}">Sunum</a>
            <form method="post" style="display:inline-block;margin-left:8px;"
                  onsubmit="return confirm('Bu gönderiyi ve puanlarını SİLMEK istiyor musun?');">
              <input type="hidden" name="action" value="delete_submission">
              <input type="hidden" name="sid" value="{int(s['sid'])}">
              <button type="submit" style="background:#ef4444;color:#fff">Sil</button>
            </form>
          </td>
        </tr>
        """

    body = f"""
    <div class="formcard">
      <h2 style="margin:0 0 10px 0;">Yönetim</h2>
      {f"<div class='{'alert-ok' if ok else 'alert-bad'}'>{msg}</div>" if msg else ""}

      <div style="display:flex;gap:14px;flex-wrap:wrap;">
        <div style="flex:1;min-width:320px;">
          <h3 style="margin:0 0 10px 0;">Kullanıcı Oluştur</h3>
          <form method="post" style="display:flex;flex-direction:column;gap:10px;">
            <input type="hidden" name="action" value="create_user">
            <div>
              <label>Kullanıcı adı</label>
              <input name="username" required placeholder="örn: 9A_G1">
            </div>
            <div>
              <label>Şifre</label>
              <input name="password" required>
            </div>
            <div>
              <label>Rol</label>
              <select name="role">
                <option value="student">student</option>
                <option value="judge">judge</option>
                <option value="admin">admin</option>
              </select>
            </div>

            <div style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:14px;padding:12px;">
              <div style="color:#64748b;font-size:12px;"><b>Student seçtiysen:</b> sınıf + grup no gir</div>
              <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px;">
                <div style="flex:1;min-width:200px;">
                  <label>Sınıf</label>
                  <input name="class_name" placeholder="9A">
                </div>
                <div style="flex:1;min-width:200px;">
                  <label>Grup No</label>
                  <input name="group_no" placeholder="1">
                </div>
              </div>
            </div>

            <button type="submit">Oluştur</button>
          </form>
        </div>

        <div style="flex:1;min-width:320px;">
          <h3 style="margin:0 0 10px 0;">Kullanıcılar</h3>
          <table>
            <tr><th>Kullanıcı</th><th>Rol</th><th>Grup</th></tr>
            {user_rows if user_rows else "<tr><td colspan='3' style='color:#64748b'>Yok</td></tr>"}
          </table>
        </div>
      </div>

      <h3 style="margin:18px 0 10px 0;">Gönderiler (Silme)</h3>
      <table>
        <tr><th>ID</th><th>Grup / Dergi</th><th>Tarih</th><th>İşlem</th></tr>
        {sub_rows if sub_rows else "<tr><td colspan='4' style='color:#64748b'>Gönderi yok</td></tr>"}
      </table>
    </div>
    """
    return page("Yönetim", body)

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


