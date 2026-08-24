# ตั้งค่า Google Login (Sign in with Google) สำหรับ UniAssist

โค้ดฝั่งเว็บเชื่อม Google OAuth ไว้ให้แล้ว ([google_auth.py](google_auth.py) + ปุ่มบน header)
เหลือแค่ **ขอ credentials จาก Google** แล้วใส่ใน `.env` — ขั้นตอนตามนี้ (ทำครั้งเดียว)

> ⚠️ ผมสร้าง credentials ให้ไม่ได้ เพราะต้องใช้บัญชี Google ของคุณล็อกอินเข้า Google Cloud Console
> ทำตามด้านล่างแล้ว copy 2 ค่ามาวางใน `.env` ก็ใช้ได้เลย

## 1) สร้าง OAuth Client ใน Google Cloud Console
1. เข้า https://console.cloud.google.com/ → ล็อกอินด้วยบัญชี Google
2. สร้าง Project ใหม่ (มุมบนซ้าย) เช่นชื่อ `UniAssist` แล้วเลือกเข้า project นั้น
3. ไปที่ **APIs & Services → OAuth consent screen**
   - User type: **External** → Create
   - กรอกชื่อแอป (เช่น UniAssist), email ของคุณ → บันทึกผ่านไปจนจบ
   - ที่หน้า **Audience/Test users** กด **Add users** ใส่อีเมล Google ที่จะใช้ทดสอบ (สำคัญ ถ้าแอปยังเป็น Testing)
4. ไปที่ **APIs & Services → Credentials → + Create Credentials → OAuth client ID**
   - Application type: **Web application**
   - **Authorized redirect URIs** กด Add แล้วใส่ให้ตรงเป๊ะ:
     ```
     http://localhost:5000/auth/callback
     ```
     (ถ้า deploy จริงภายหลัง ให้เพิ่ม URL ของโดเมนจริง เช่น `https://your-domain/auth/callback` ด้วย)
   - กด Create → จะได้ **Client ID** และ **Client secret** → copy ไว้

## 2) ใส่ค่าใน `.env`
เปิดไฟล์ `.env` แล้วเติม:
```
GOOGLE_CLIENT_ID=<Client ID ที่ได้มา>
GOOGLE_CLIENT_SECRET=<Client secret ที่ได้มา>
GOOGLE_REDIRECT_URI=http://localhost:5000/auth/callback
FLASK_SECRET_KEY=<คีย์สุ่มยาวๆ>
```
สร้าง `FLASK_SECRET_KEY` ด้วยคำสั่ง:
```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```
เอาผลลัพธ์มาวางหลัง `FLASK_SECRET_KEY=`

## 3) รันแล้วทดสอบ
```bash
python app.py
```
เปิด http://localhost:5000 → กดปุ่ม **🔑 เข้าสู่ระบบด้วย Google** ที่มุมขวาบน
→ เลือกบัญชี Google → กลับมาหน้าเว็บพร้อมชื่อ/รูปโปรไฟล์ + ปุ่ม "ออกจากระบบ"

## เกิดอะไรขึ้นเบื้องหลัง (flow)
```
ผู้ใช้กดปุ่ม → /auth/login → เด้งไปหน้ายินยอมของ Google
   → Google เด้งกลับ /auth/callback?code=... (+state กัน CSRF)
   → server แลก code เป็น access_token แล้วดึงโปรไฟล์ (email/name/picture)
   → เก็บลง Flask session (cookie ที่เซ็นด้วย FLASK_SECRET_KEY) → กลับหน้าแรก
หน้าเว็บเรียก /auth/me เพื่อรู้ว่าใครล็อกอินอยู่ → แสดงชื่อ/รูป + ปุ่มออกจากระบบ
```
> ต่างจากสตาร์ตเตอร์ FastAPI/React เดิมที่ส่ง JWT ผ่าน URL — เวอร์ชัน Flask นี้ใช้ **server-side session**
> ปลอดภัยกว่า (token ไม่โผล่ใน URL) และไม่ต้องพึ่ง localStorage

## อยากบังคับให้ต้องล็อกอินก่อนใช้บางหน้า?
มี decorator `login_required` ใน [google_auth.py](google_auth.py) ให้แล้ว
เช่นอยาก gate เฉพาะ dashboard อาจารย์ ก็ครอบ route `/dashboard-data`:
```python
from google_auth import login_required

@app.route("/dashboard-data")
@login_required
def dashboard_data():
    ...
```
(ตอนนี้ยังไม่บังคับ gate หน้าไหน — auth เป็นแบบเสริม แสดงสถานะบน header เฉยๆ ตามที่คุยกันไว้ว่า prototype ไม่ต้องซับซ้อน)

## หมายเหตุความปลอดภัย
- `GOOGLE_CLIENT_SECRET`, `FLASK_SECRET_KEY` อ่านฝั่ง server เท่านั้น ไม่เคยส่งไป frontend
- `.env` อยู่ใน `.gitignore` แล้ว — อย่า commit ขึ้น git
- state parameter กัน CSRF ระหว่าง login flow แล้ว
