"""
google_auth.py
--------------
Google OAuth 2.0 (Sign in with Google) สำหรับ UniAssist (Flask)
พอร์ตมาจากสตาร์ตเตอร์ FastAPI/React -> ปรับให้เข้ากับ Flask + server-side session
(สะอาดกว่าแบบส่ง JWT ผ่าน URL: เก็บ user ไว้ใน session cookie ที่เซ็นแล้ว)

ต้องตั้งค่าใน .env (อ่านจากฝั่ง server เท่านั้น — ห้ามส่ง secret ไป frontend):
    GOOGLE_CLIENT_ID=...
    GOOGLE_CLIENT_SECRET=...
    GOOGLE_REDIRECT_URI=http://localhost:5000/auth/callback
    FLASK_SECRET_KEY=<สุ่มยาวๆ>        # ใช้เซ็น session cookie

Endpoints (register ผ่าน register_auth(app)):
    GET  /auth/login     -> เด้งไปหน้ายินยอมของ Google
    GET  /auth/callback  -> แลก code -> token -> โปรไฟล์ -> เก็บลง session -> กลับหน้าแรก
    POST /auth/logout    -> ล้าง session
    GET  /auth/me        -> คืนข้อมูล user ที่ล็อกอินอยู่ (หรือ null)

ใช้ urllib (stdlib) เรียก Google — ไม่ต้องลง dependency เพิ่ม
"""

import json
import os
import re
import secrets
import urllib.parse
import urllib.request
from functools import wraps

from flask import jsonify, redirect, request, session
from markupsafe import escape

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# โดเมนที่อนุญาตให้ล็อกอิน (ด่านกันโดเมนฝั่ง server) — ตั้งใน .env ได้ถ้าต้องการเปลี่ยน
ALLOWED_DOMAIN = os.environ.get("ALLOWED_DOMAIN", "kmitl.ac.th").strip().lower()


def _auth_page(title, message_html, status=200, show_login=True, tone="info"):
    """หน้า auth (login/error) สไตล์มินิมอลให้เข้าธีมเว็บ UniAssist (KMITL)
    message_html = HTML ที่ปลอดภัยแล้ว (ผู้เรียกต้อง escape ค่าจากผู้ใช้เอง)"""
    accent = "#e0483d" if tone == "error" else "var(--navy)"
    btn = ("<a class='btn' href='/auth/login'>🔑 เข้าสู่ระบบด้วยบัญชี KMITL</a>"
           if show_login else "")
    home = "<a class='home' href='/'>← กลับหน้าแรก</a>" if show_login else ""
    html = f"""<!doctype html>
<html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} — UniAssist</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Thai:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{{--navy:#15457A;--navy-600:#1b5698;--orange:#E8762C;--orange-600:#d3641c;--ink:#1b2430;--muted:#6b7688;--border:#e7ebf1;}}
  *{{box-sizing:border-box;}}
  body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;
    font-family:"IBM Plex Sans Thai","Segoe UI",Tahoma,sans-serif;color:var(--ink);
    background:radial-gradient(1100px 380px at 100% -8%,#eaf1fb,transparent 60%),
      radial-gradient(900px 340px at -10% 0%,#fdeede,transparent 55%),#f5f7fb;
    -webkit-font-smoothing:antialiased;}}
  .card{{background:#fff;border:1px solid var(--border);border-radius:22px;
    box-shadow:0 12px 40px rgba(16,24,40,.13);max-width:430px;width:100%;padding:36px 32px;text-align:center;}}
  .logo{{width:56px;height:56px;border-radius:16px;margin:0 auto 20px;
    background:linear-gradient(135deg,{accent},var(--navy-600));color:#fff;display:flex;
    align-items:center;justify-content:center;font-weight:700;font-size:27px;box-shadow:0 6px 18px rgba(21,69,122,.28);}}
  h1{{font-size:21px;font-weight:700;margin:0 0 12px;letter-spacing:-.2px;}}
  .msg{{color:var(--muted);font-size:14.5px;line-height:1.65;margin:0 0 24px;}}
  .msg b{{color:var(--ink);font-weight:600;}}
  .msg code{{background:rgba(21,69,122,.08);padding:2px 7px;border-radius:6px;font-size:13px;
    font-family:"SF Mono",Consolas,monospace;word-break:break-all;}}
  .btn{{display:inline-block;background:linear-gradient(135deg,var(--orange),var(--orange-600));color:#fff;
    padding:12px 24px;border-radius:13px;text-decoration:none;font-weight:600;font-size:14.5px;
    box-shadow:0 5px 16px rgba(232,118,44,.32);transition:filter .12s,transform .12s;}}
  .btn:hover{{filter:brightness(1.05);transform:translateY(-1px);}}
  .home{{display:block;margin-top:16px;color:var(--navy);font-size:13px;text-decoration:none;}}
  .home:hover{{text-decoration:underline;}}
</style></head>
<body><div class="card">
  <div class="logo">U</div>
  <h1>{escape(title)}</h1>
  <div class="msg">{message_html}</div>
  {btn}{home}
</div></body></html>"""
    return html, status


def _cfg(name, default=None):
    return os.environ.get(name, default)


def _redirect_uri():
    # ค่าเริ่มต้นชี้ callback ของ Flask เอง (ต้องตรงกับที่ตั้งใน Google Cloud Console เป๊ะ)
    return _cfg("GOOGLE_REDIRECT_URI", "http://localhost:5000/auth/callback")


def _post_form(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # Google ส่งรายละเอียด error มาใน body (เช่น invalid_grant) -> อ่านมาคืนให้เห็นสาเหตุจริง
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"error": f"http_{e.code}", "error_description": str(e)}


def _get_json(url, bearer):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"error": f"http_{e.code}", "error_description": str(e)}


def _email_set(env_name: str) -> set:
    return {e.strip().lower() for e in os.environ.get(env_name, "").split(",") if e.strip()}


def _allowed_domains() -> set:
    raw = os.environ.get("ALLOWED_DOMAINS", "kmitl.ac.th")
    return {d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()}


def is_allowed_email(email: str) -> bool:
    """อนุญาตเฉพาะอีเมลในโดเมนที่กำหนด (ค่าเริ่มต้น: kmitl.ac.th)
    ยกเว้นอีเมลที่ถูกระบุ role ไว้ชัดใน DEV/ADVISOR/STUDENT_EMAILS (whitelist รายคน)
    -> กันอีเมลนอกสถาบัน เช่น @gmail.com เข้าระบบ"""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False
    if email in (_email_set("DEV_EMAILS") | _email_set("ADVISOR_EMAILS") | _email_set("STUDENT_EMAILS")):
        return True
    return email.rsplit("@", 1)[-1] in _allowed_domains()


def classify_role(email: str) -> str:
    """แยกบทบาทจากอีเมล: 'dev' | 'advisor' | 'student'
    ลำดับความสำคัญ: DEV_EMAILS > ADVISOR_EMAILS > STUDENT_EMAILS > heuristic > default
    กติกา (KMITL):
      - DEV_EMAILS (คนพัฒนา) -> dev (เห็นทุกอย่าง + หน้า Dev)
      - ADVISOR_EMAILS (allowlist) -> advisor   (advisor มาจาก allowlist นี้เท่านั้น)
      - STUDENT_EMAILS (allowlist) -> student
      - อีเมลนักศึกษา = รหัสนักศึกษา (ตัวเลขล้วน) เช่น 66070104@kmitl.ac.th -> student
      - อื่นๆ ทั้งหมด -> student   (default ปลอดภัย ไม่เดา advisor จากรูปอีเมลอีกต่อไป)"""
    email = (email or "").strip().lower()
    local = email.split("@")[0]
    if email in _email_set("DEV_EMAILS"):
        return "dev"
    if email in _email_set("ADVISOR_EMAILS"):
        return "advisor"
    if email in _email_set("STUDENT_EMAILS"):
        return "student"
    if re.fullmatch(r"\d{6,}", local):   # local-part เป็นตัวเลขล้วน = รหัสนักศึกษา
        return "student"
    return "student"


def current_user():
    """คืน dict ของ user ที่ล็อกอินอยู่ หรือ None"""
    return session.get("user")


def advisor_required(view):
    """เดคอเรเตอร์กันเส้นทางเฉพาะอาจารย์ที่ปรึกษา (dev เข้าได้ด้วย)"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        u = current_user()
        if not u:
            return jsonify({"error": "ต้องเข้าสู่ระบบก่อน"}), 401
        if u.get("role") not in ("advisor", "dev"):
            return jsonify({"error": "เฉพาะอาจารย์ที่ปรึกษาเท่านั้น"}), 403
        return view(*args, **kwargs)
    return wrapped


def dev_required(view):
    """เดคอเรเตอร์กันเส้นทางเฉพาะผู้พัฒนา (role = dev)"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        u = current_user()
        if not u:
            return jsonify({"error": "ต้องเข้าสู่ระบบก่อน"}), 401
        if u.get("role") != "dev":
            return jsonify({"error": "เฉพาะผู้พัฒนาเท่านั้น"}), 403
        return view(*args, **kwargs)
    return wrapped


def login_required(view):
    """เดคอเรเตอร์กันเส้นทางที่ต้องล็อกอินก่อน (ยังไม่ได้บังคับใช้กับหน้าไหนเป็นค่าเริ่มต้น
    — เผื่ออยาก gate เช่น dashboard อาจารย์ ให้เติม @login_required ได้)"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return jsonify({"error": "ต้องเข้าสู่ระบบก่อน"}), 401
        return view(*args, **kwargs)
    return wrapped


def register_auth(app):
    """ผูก session secret + endpoints /auth/* เข้ากับ Flask app"""
    # secret key สำหรับเซ็น session cookie (ต้องตั้งใน .env ตอน production)
    app.secret_key = _cfg("FLASK_SECRET_KEY") or "dev-only-insecure-change-me"

    @app.route("/auth/login")
    def auth_login():
        client_id = _cfg("GOOGLE_CLIENT_ID")
        if not client_id:
            return _auth_page(
                "ยังไม่ได้ตั้งค่า Google Login",
                "ยังไม่ได้ตั้งค่า <code>GOOGLE_CLIENT_ID</code> / <code>GOOGLE_CLIENT_SECRET</code> ใน .env<br>"
                "ดูวิธีขอจาก Google Cloud Console ใน <b>GOOGLE_AUTH_SETUP.md</b>",
                500, show_login=False, tone="error")
        # state กัน CSRF: สุ่มแล้วเก็บใน session ไปเทียบตอน callback
        state = secrets.token_urlsafe(24)
        session["oauth_state"] = state
        params = urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": _redirect_uri(),
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
            "hd": ALLOWED_DOMAIN,   # ให้ Google กรองบัญชีตั้งแต่หน้า consent (ชั้นเสริม ไม่ใช่ชั้นหลัก)
        })
        return redirect(f"{_GOOGLE_AUTH_URL}?{params}")

    @app.route("/auth/callback")
    def auth_callback():
        # เทียบ state กัน CSRF — ถ้าไม่ตรง (มัก = session/cookie หลุด) แสดงหน้าให้ลองใหม่ได้เลย
        if not request.args.get("state") or request.args.get("state") != session.pop("oauth_state", None):
            return _auth_page(
                "เข้าสู่ระบบไม่สำเร็จ",
                "session หลุด (มักเกิดจาก cookie เก่าหรือ server เพิ่งรีสตาร์ท)<br>"
                "ลองล้าง cookie ของ localhost แล้วเข้าสู่ระบบใหม่",
                400, tone="error")
        if request.args.get("error"):
            return _auth_page("เข้าสู่ระบบไม่สำเร็จ",
                              f"Google ปฏิเสธการเข้าสู่ระบบ: <b>{escape(request.args.get('error'))}</b>",
                              400, tone="error")
        code = request.args.get("code")
        if not code:
            return _auth_page("เข้าสู่ระบบไม่สำเร็จ", "ไม่ได้รับ code จาก Google", 400, tone="error")

        try:
            tokens = _post_form(_GOOGLE_TOKEN_URL, {
                "code": code,
                "client_id": _cfg("GOOGLE_CLIENT_ID"),
                "client_secret": _cfg("GOOGLE_CLIENT_SECRET"),
                "redirect_uri": _redirect_uri(),
                "grant_type": "authorization_code",
            })
            access_token = tokens.get("access_token")
            if not access_token:
                # กันเคส callback ยิงซ้ำ (browser prefetch/silent auth) — ถ้าอีก request แลก token
                # สำเร็จและ set session ไปแล้ว ก็ถือว่าเข้าสู่ระบบสำเร็จ ไม่ต้องโชว์ error
                if session.get("user"):
                    return redirect("/")
                # โชว์ error จริงจาก Google (invalid_grant / redirect_uri_mismatch / invalid_client ฯลฯ)
                err = tokens.get("error", "")
                desc = tokens.get("error_description", "") or str(tokens)
                print(f"[auth] token exchange failed: {err} — {desc}  (redirect_uri={_redirect_uri()})")
                hint = {
                    "invalid_grant": "code หมดอายุหรือถูกใช้ไปแล้ว — ลองเข้าสู่ระบบใหม่ (อย่ารีเฟรชหน้า callback)",
                    "redirect_uri_mismatch": f"redirect_uri ไม่ตรงกับที่ตั้งใน Google Cloud — ต้องเป็น {_redirect_uri()} เป๊ะ",
                    "invalid_client": "GOOGLE_CLIENT_ID หรือ GOOGLE_CLIENT_SECRET ไม่ถูกต้อง",
                }.get(err, "")
                msg = f"<b>{escape(err)}</b><br>{escape(desc)}"
                if hint:
                    msg += f"<br><br>💡 {escape(hint)}"
                return _auth_page("แลก token ไม่สำเร็จ", msg, 502, tone="error")
            info = _get_json(_GOOGLE_USERINFO_URL, access_token)
            if info.get("error"):
                print(f"[auth] userinfo failed: {info}")
                return _auth_page("ดึงข้อมูลผู้ใช้ไม่สำเร็จ",
                                  escape(str(info.get("error_description") or info)), 502, tone="error")
        except Exception as e:
            print(f"[auth] callback exception: {type(e).__name__}: {e}")
            return _auth_page("เชื่อมต่อ Google ไม่สำเร็จ",
                              f"{escape(type(e).__name__)}: {escape(str(e))}", 502, tone="error")

        # กันอีเมลนอกสถาบัน — อนุญาตเฉพาะโดเมนที่กำหนด (เช่น @kmitl.ac.th)
        email = info.get("email")
        if not is_allowed_email(email):
            session.pop("user", None)
            print(f"[auth] blocked non-allowed email: {email}")
            doms = ", ".join("@" + d for d in sorted(_allowed_domains()))
            return _auth_page(
                "เข้าสู่ระบบไม่ได้",
                f"ระบบนี้ใช้ได้เฉพาะอีเมลของสถาบัน (<b>{escape(doms)}</b>) เท่านั้น<br>"
                f"อีเมลที่ใช้: <b>{escape(email or '(ไม่ทราบ)')}</b>",
                403, tone="error")

        # ── ด่านกันโดเมน (ฝั่ง server เสมอ — ห้ามพึ่ง hd param อย่างเดียว) ──
        email = (info.get("email") or "").strip().lower()
        verified = info.get("verified_email")
        if verified is False or not email.endswith("@" + ALLOWED_DOMAIN):
            print(f"[auth] domain rejected: email={email!r} verified={verified!r} "
                  f"(allowed=@{ALLOWED_DOMAIN})")
            return (
                "<div style='font-family:sans-serif;max-width:520px;margin:80px auto;text-align:center'>"
                f"<h3>เข้าสู่ระบบไม่สำเร็จ</h3>"
                f"<p style='color:#555'>ระบบนี้ใช้ได้เฉพาะอีเมล <b>@{ALLOWED_DOMAIN}</b> เท่านั้น "
                "กรุณาเข้าสู่ระบบด้วยอีเมลของมหาวิทยาลัย</p>"
                "<p><a href='/auth/login' style='background:#E8762C;color:#fff;padding:10px 18px;"
                "border-radius:8px;text-decoration:none'>🔑 ลองเข้าสู่ระบบใหม่</a></p>"
                "<p><a href='/' style='color:#15457A'>กลับหน้าแรก</a></p></div>"
            ), 403

        # เก็บเฉพาะข้อมูลที่ต้องใช้ลง session (เซ็นด้วย FLASK_SECRET_KEY)
        session["user"] = {
            "id": info.get("id"),
            "email": info.get("email"),
            "name": info.get("name"),
            "picture": info.get("picture"),
            "role": classify_role(info.get("email")),   # student / advisor
        }
        return redirect("/")

    @app.route("/auth/logout", methods=["POST"])
    def auth_logout():
        session.pop("user", None)
        return jsonify({"ok": True})

    @app.route("/auth/me")
    def auth_me():
        u = current_user()
        # กันเหนียว: ถ้า session เก่ามาจากอีเมลนอกโดเมนที่อนุญาต -> ล้างทิ้ง (invalidate)
        if u and not is_allowed_email(u.get("email")):
            session.pop("user", None)
            return jsonify({"user": None})
        if u and not u.get("role"):        # backfill role ให้ session เก่าที่ยังไม่มี
            u["role"] = classify_role(u.get("email"))
            session["user"] = u
        return jsonify({"user": u})
