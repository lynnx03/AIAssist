# UniAssist — ผู้ช่วยวิชาการนักศึกษา (Text-to-SQL Chatbot)

ระบบผู้ช่วยด้านวิชาการสำหรับนักศึกษา/อาจารย์ที่ปรึกษา KMITL สร้างด้วย **Flask + LLM**
ผู้ใช้พิมพ์คำถามภาษาไทย ระบบให้ LLM เขียน SQL รันบนฐานข้อมูลหลักสูตร (read-only)
แล้วเรียบเรียงคำตอบกลับมา พร้อมโหมดสนทนาทั่วไป, คำนวณ GPA, รายการทุน และแดชบอร์ดอาจารย์

## ฟีเจอร์หลัก

- **ถาม-ตอบข้อมูลหลักสูตร (Text-to-SQL)** — LLM ตัดสินเองว่าเป็นคำถามดึงข้อมูล (SQL) หรือคำถามคุยเล่น (CHAT); เห็น SQL ที่โมเดลเขียน + ตารางผลลัพธ์ดิบ + latency แต่ละขั้น
- **โหมดสนทนา** — ตอบแบบ chatbot โดย ground ด้วยข้อเท็จจริงจาก DB (ลด hallucination) และใช้เป็น fallback เมื่อ SQL ผิดพลาด
- **คำนวณ GPA** — paste transcript หรือแก้รายวิชาในตาราง แล้วคำนวณ GPA รายเทอม/สะสม + จัดสถานะ (เสี่ยงรีไทร์/ภาคทัณฑ์/เกียรตินิยม) โดยอิงค่าเกรดและเกณฑ์จาก DB ไม่ hardcode
- **รายการทุนการศึกษา**
- **แดชบอร์ดอาจารย์ที่ปรึกษา** — ดูนักศึกษาในความดูแล + GPA รายเทอม + สรุปความเสี่ยง
- **Google OAuth** — ล็อกอินด้วยบัญชี Google จำกัดโดเมน แบ่ง role (student / advisor / dev)

## ความปลอดภัย

เนื่องจากนักศึกษาเป็นคนพิมพ์คำถามเอง การรัน SQL จึงถูกจำกัดหลายชั้น:

- ต่อ DB แบบ **read-only** จริง (`file:...?mode=ro`) — LLM เขียน `DELETE`/`UPDATE` ไม่ได้
- ตรวจ SQL ให้เป็น `SELECT`/`WITH` เดี่ยวๆ, บล็อกคำสั่งเขียน, กัน multiple statements
- เติม `LIMIT` อัตโนมัติถ้าไม่มี — กันดึงทั้งตาราง
- Google Client Secret อ่านฝั่ง server เท่านั้น, เก็บ user ใน session cookie ที่เซ็นแล้ว

## โครงสร้างไฟล์

| ไฟล์ | หน้าที่ |
|---|---|
| `app.py` | เว็บ Flask — routes `/`, `/ask`, `/calc-gpa`, `/scholarships`, `/dashboard-data`, `/dev-info` |
| `text_to_sql_chatbot.py` | แกน Text-to-SQL: สร้าง prompt, เรียก LLM, ตรวจ/รัน SQL, เรียบเรียงคำตอบ (รันเป็น CLI ได้ด้วย) |
| `assist_logic.py` | ตรรกะร่วม: parse transcript, คำนวณ GPA, จัดสถานะจากเกณฑ์ใน DB |
| `google_auth.py` | Google OAuth 2.0 + decorators คุม role (`login_required`, `advisor_required`, `dev_required`) |
| `build_qwen_db.py` | สร้าง DB จาก JSON ที่ Qwen สกัดจากตารางหลักสูตร |
| `build_chatbot_db.py` | ประกอบ DB ของ chatbot (แผนการเรียนจาก Ground Truth + ส่วนที่เหลือจาก Qwen) |
| `build_extra_tables.py` | เพิ่มตารางเสริม: `grade_scale`, `rules`, `scholarships`, `students`, `student_term_gpa` (idempotent) |
| `templates/` | `login.html`, `index.html` |
| `data/` | ข้อมูลต้นทาง (GT, ผลสกัด, ตารางหลักสูตร) |
| `chatbot_teach_table.db` | ฐานข้อมูลหลักที่แอปใช้งาน |

## การติดตั้ง

ต้องมี Python 3.10+

```bash
pip install -r requirements.txt
```

## ตั้งค่า `.env`

สร้างไฟล์ `.env` ในโฟลเดอร์เดียวกันกับ `app.py`:

```ini
# LLM (OpenAI-compatible — ใช้ได้กับ Qwen / DeepSeek / Typhoon / Gemini ผ่าน OpenRouter ฯลฯ)
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-...
LLM_MODEL=qwen/qwen3-235b-a22b
LLM_PROVIDER=            # เว้นว่างได้ (auto)

# ฐานข้อมูล
DB_PATH=chatbot_teach_table.db

# Google OAuth (ดูขั้นตอนละเอียดใน GOOGLE_AUTH_SETUP.md)
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=http://localhost:5000/auth/callback
FLASK_SECRET_KEY=<สุ่มยาวๆ>

# สิทธิ์การเข้าใช้ (คั่นด้วยจุลภาค)
ALLOWED_DOMAINS=kmitl.ac.th
DEV_EMAILS=you@kmitl.ac.th
ADVISOR_EMAILS=advisor@kmitl.ac.th
STUDENT_EMAILS=
```

> การตั้งค่า Google OAuth แบบละเอียด (สร้าง credential, redirect URI) ดูที่ [GOOGLE_AUTH_SETUP.md](GOOGLE_AUTH_SETUP.md)

## การรัน

```bash
python app.py
```

เปิดเบราว์เซอร์ที่ http://localhost:5000 (ต้องล็อกอินด้วย Google ก่อนถึงจะใช้งานได้)

### รันแกน chatbot แบบ CLI (ไว้เทส/ทำ eval)

```bash
python text_to_sql_chatbot.py                                  # โหมดโต้ตอบ
python text_to_sql_chatbot.py -q "หลักสูตร IT จบกี่หน่วยกิต"
python text_to_sql_chatbot.py --sql "SELECT ..."               # รัน SQL ตรงๆ
```

## สร้างฐานข้อมูลใหม่ (ถ้าต้องการ build เอง)

```bash
python build_qwen_db.py ./data/extracted_teach_table ./qwen_teach_table.db
python build_chatbot_db.py       # ประกอบ chatbot_teach_table.db
python build_extra_tables.py     # เพิ่ม grade_scale / rules / scholarships / students
```

## หมายเหตุ

- ตาราง `students` / `student_term_gpa` ปัจจุบันเป็นข้อมูล mock เตรียมไว้ต่อกับ registrar API ภายหลัง (ดู flow การดึงข้อมูลจริงใน [README-2.md](README-2.md))
- บาง provider ของ LLM ตอบไม่นิ่ง (คืน `NO_ANSWER` เป็นครั้งคราวตามช่วงเวลา) — ระบบมี retry-on-NO_ANSWER รองรับแล้ว
