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

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


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


def classify_role(email: str) -> str:
    """แยกบทบาทจากอีเมล: 'dev' | 'advisor' | 'student'
    ลำดับความสำคัญ: DEV_EMAILS > ADVISOR/STUDENT override > heuristic
    กติกา (KMITL):
      - DEV_EMAILS (คนพัฒนา) -> dev (เห็นทุกอย่าง + หน้า Dev)
      - อีเมลนักศึกษา = รหัสนักศึกษา (ตัวเลขล้วน) เช่น 66070104@kmitl.ac.th -> student
      - อีเมลชื่อคน เช่น somchai.x@kmitl.ac.th -> advisor/เจ้าหน้าที่"""
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
    return "advisor"


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
            return ("ยังไม่ได้ตั้งค่า GOOGLE_CLIENT_ID/SECRET ใน .env "
                    "(ดูวิธีขอจาก Google Cloud Console ใน README)"), 500
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
        })
        return redirect(f"{_GOOGLE_AUTH_URL}?{params}")

    @app.route("/auth/callback")
    def auth_callback():
        # เทียบ state กัน CSRF — ถ้าไม่ตรง (มัก = session/cookie หลุด) แสดงหน้าให้ลองใหม่ได้เลย
        if not request.args.get("state") or request.args.get("state") != session.pop("oauth_state", None):
            return (
                "<div style='font-family:sans-serif;max-width:520px;margin:80px auto;text-align:center'>"
                "<h3>เข้าสู่ระบบไม่สำเร็จ (session หลุด)</h3>"
                "<p style='color:#555'>มักเกิดจาก cookie เก่าหรือ server เพิ่งรีสตาร์ท "
                "ลองล้าง cookie ของ localhost แล้วเข้าสู่ระบบใหม่</p>"
                "<p><a href='/auth/login' style='background:#E8762C;color:#fff;padding:10px 18px;"
                "border-radius:8px;text-decoration:none'>🔑 ลองเข้าสู่ระบบใหม่</a></p>"
                "<p><a href='/' style='color:#15457A'>กลับหน้าแรก</a></p></div>"
            ), 400
        if request.args.get("error"):
            return f"Google ปฏิเสธการเข้าสู่ระบบ: {request.args.get('error')}", 400
        code = request.args.get("code")
        if not code:
            return "ไม่ได้รับ code จาก Google", 400

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
                return (f"แลก token ไม่สำเร็จ: <b>{err}</b><br>{desc}"
                        + (f"<br><br>💡 {hint}" if hint else "")
                        + "<br><br><a href='/auth/login'>🔑 ลองเข้าสู่ระบบใหม่</a>"), 502
            info = _get_json(_GOOGLE_USERINFO_URL, access_token)
            if info.get("error"):
                print(f"[auth] userinfo failed: {info}")
                return f"ดึงข้อมูลผู้ใช้ไม่สำเร็จ: {info.get('error_description') or info}", 502
        except Exception as e:
            print(f"[auth] callback exception: {type(e).__name__}: {e}")
            return f"เชื่อมต่อ Google ไม่สำเร็จ: {type(e).__name__}: {e}", 502

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
        if u and not u.get("role"):        # backfill role ให้ session เก่าที่ยังไม่มี
            u["role"] = classify_role(u.get("email"))
            session["user"] = u
        return jsonify({"user": u})
