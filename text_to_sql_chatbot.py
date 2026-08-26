#!/usr/bin/env python3
"""
text_to_sql_chatbot.py
======================
Text-to-SQL chatbot prototype for UniAssist.

ให้ LLM เขียน SQL จากคำถามภาษาไทย แล้วรันบน qwen_teach_table.db (read-only)
เป้าหมายเฟสนี้: เช็คว่า agent ดึงข้อมูลจาก relational DB ได้ "ตรง" ไหม
เลยออกแบบให้ *เห็น SQL ที่ LLM เขียน* เสมอ จะได้เอาไปเทียบกับ ground truth ได้

ความปลอดภัย (สำคัญเพราะ นศ. เป็นคนพิมพ์คำถาม):
  - ต่อ DB แบบ read-only จริง (file:...?mode=ro) → LLM เขียน DELETE/UPDATE ไม่ได้เด็ดขาด
  - ตรวจ SQL ให้เป็น SELECT/WITH เดี่ยวๆ, บล็อกคำสั่งเขียน, กัน multiple statements
  - เติม LIMIT อัตโนมัติถ้าไม่มี → กันดึงทั้งตาราง

การตั้งค่า LLM (OpenAI-compatible — ใช้ได้กับ Qwen/DeepSeek/Typhoon/Gemini):
  วิธีที่ 1 (แนะนำ): สร้างไฟล์ .env ไว้ในโฟลเดอร์เดียวกัน (ก๊อปจาก .env.example) ใส่ค่า:
      LLM_BASE_URL=https://openrouter.ai/api/v1
      LLM_API_KEY=sk-...
      LLM_MODEL=qwen/qwen-2.5-72b-instruct
    สคริปต์จะอ่าน .env ให้อัตโนมัติ (ไม่ต้อง pip install อะไรเพิ่ม)
  วิธีที่ 2: ตั้ง environment variable เอง
      Windows PowerShell:  $env:LLM_API_KEY="sk-..."
      macOS/Linux:         export LLM_API_KEY="sk-..."

วิธีใช้:
  python3 text_to_sql_chatbot.py                          # โหมดคุยโต้ตอบ (พิมพ์คำถามภาษาไทย)
  python3 text_to_sql_chatbot.py -q "หลักสูตร IT จบกี่หน่วยกิต"
  python3 text_to_sql_chatbot.py -q "..." --raw          # เอาแค่ตาราง ไม่ต้องเรียบเรียงคำตอบ
  python3 text_to_sql_chatbot.py --sql "SELECT ..."      # รัน SQL ตรงๆ (ไว้เทสต์/ทำ eval)
  python3 text_to_sql_chatbot.py --db path/to.db
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import textwrap

DEFAULT_DB = "qwen_teach_table.db"


def load_dotenv(path: str = ".env") -> None:
    """โหลดค่า KEY=VALUE จากไฟล์ .env เข้า environment (ถ้ามีไฟล์)
    ลองใช้ python-dotenv ก่อน ถ้าไม่มี package ก็ parse เองแบบง่ายๆ
    ค่าที่ตั้งไว้ใน environment จริงอยู่แล้วจะไม่ถูกทับ (env จริงชนะ .env)"""
    try:
        from dotenv import load_dotenv as _ld  # python-dotenv ถ้ามี
        _ld(path, override=False)
        return
    except ImportError:
        pass
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:  # ไม่ทับค่าที่ตั้งไว้แล้ว
                os.environ[key] = val

# ---------------------------------------------------------------------------
# 1) Schema introspection — สร้างคำอธิบาย schema จากไฟล์ DB จริง (กัน prompt ล้าสมัย)
# ---------------------------------------------------------------------------

SCHEMA_NOTES = """\
หมายเหตุสำคัญเกี่ยวกับข้อมูล (ต้องคำนึงตอนเขียน SQL):
- มี 4 หลักสูตร ใน programs.program_code = 'IT','DSBA','BIT','AIT'
  หน่วยกิตจบ: IT=129, DSBA=132, BIT=126, AIT=120 (เก็บใน programs.total_credits)
- courses.code ไม่ unique ทั่วทั้งตาราง (AIT มีรหัส placeholder เช่น '9064xxxx','xxxxxxxx')
  ถ้าจะอ้างถึงวิชาให้ filter ด้วย program_id ประกอบด้วยเสมอ
- courses.type = 'บังคับ' หรือ 'เลือก'
- credits เก็บ 2 แบบ: credits_raw = string เช่น '3(3-0-6)', credits_total = จำนวนหน่วยกิต (int)
  ถ้าจะรวม/นับหน่วยกิตให้ใช้ credits_total (บางแถวเป็น NULL ถ้า parse ไม่ได้)
- course_prerequisites.prerequisite_code เป็นรหัสวิชา (text) ที่ต้องเรียนก่อน
- แผนการเรียนอยู่ในตาราง academic_plan_courses (1 แถว = 1 วิชาในแผน):
    * plan_type = 'coop' (แผนสหกิจ) หรือ 'no_coop' (แผนปกติ) สำหรับ IT, DSBA, BIT ที่มีให้เลือก
    * AIT ใช้ plan_type = 'standard' (มีแผนเดียว สหกิจบังคับที่ปี 4 เทอม 2 ไม่มีตัวเลือก)
    * year/semester = 0 หมายถึงวิชาเลือกแบบยืดหยุ่น (ดูคอลัมน์ flexible_year_semester ประกอบ
      เช่น '3/2, 4/1, 4/2' = เลือกลงได้หลายเทอม)
    * ถ้าคำถามพูดถึง "สหกิจ/coop" ของ IT/DSBA/BIT ให้ใช้ plan_type='coop'; ถ้าพูดถึง "แผนปกติ" ให้ใช้ 'no_coop'
    * AIT ทุกคำถามใช้ plan_type='standard' (มีแผนเดียว)
    * ถ้าคำถามเกี่ยวกับ IT/DSBA/BIT แต่ไม่ระบุแผน ให้ default เป็น plan_type='no_coop'
    * คอลัมน์ module = สาย/โมดูลเฉพาะทาง (NULL = วิชา core เรียนทุกโมดูล):
        - IT: 'software' (วิศวกรรมซอฟต์แวร์/Full-Stack), 'network' (เครือข่าย/ระบบ), 'game' (เกม/สื่อประสม)
        - DSBA: 'data_science' (วิทยาการข้อมูล), 'data_engineering' (วิศวกรรมข้อมูล), 'statistical_analysis' (การวิเคราะห์เชิงสถิติ)
        - BIT, AIT: ไม่มี module (เป็น NULL หมด)
      ถ้านักศึกษาพูดถึงสาย/โมดูล (เช่น "สายเกม"→game, "โมดูล SE/ซอฟต์แวร์"→software, "สายเน็ตเวิร์ก"→network)
      ให้ filter ด้วย module ที่ตรง; ถ้าถามวิชาของโมดูลใดโมดูลหนึ่งให้รวมวิชา core (module IS NULL) ด้วยเสมอ
    * **คอลัมน์ module มีอยู่เฉพาะในตาราง academic_plan_courses เท่านั้น** — courses, course_prerequisites,
      programs ไม่มีคอลัมน์นี้เด็ดขาด ห้ามอ้างอิง courses.module หรือ c.module เป็นอันขาด (จะ error: no such column)
      ถ้าคำถามถามถึงวิชาที่ระบุรหัส/ชื่อวิชาชัดเจนแล้ว (เช่น "วิชา 06016418 ต้องเรียนอะไรก่อน") ไม่ต้องเติม filter
      module ใดๆ เลย แม้คำถามจะพูดถึงชื่อสาย/โมดูลประกอบก็ตาม (เป็นแค่บริบท ไม่ใช่เงื่อนไข) เพราะรหัสวิชา + program_id
      ก็ระบุวิชานั้นได้ชัดเจนอยู่แล้ว
    * **ห้ามเขียน `module IN ('xxx', NULL)` เพื่อรวมวิชา core** เพราะ SQL ไม่ถือว่า NULL อยู่ใน IN list
      (ไม่ error แต่ได้ผลลัพธ์ผิด/ไม่ครบ) ให้เขียนเป็น `(module = 'xxx' OR module IS NULL)` แทนเสมอ
    * เวลา filter ด้วย module บน academic_plan_courses อย่าลืมใส่ plan_type ควบคู่กันไปด้วยเสมอ (ดูกฎ plan_type
      ด้านบน) — ทั้งสองเงื่อนไขต้องมาพร้อมกัน ไม่ใช่เลือกใส่แค่อันใดอันหนึ่ง
    * เรื่องสหกิจศึกษา (coop) มีแถวจริงอยู่ใน academic_plan_courses เสมอ (ชื่อวิชามีคำว่า "สหกิจ" หรือ "coop"
      อยู่ใน name_th/name_en) ห้ามตอบจากความจำ/หมายเหตุด้านบนตรงๆ ให้ query หา year/semester ของแถวนั้นจริง
      เช่น `WHERE name_th LIKE '%สหกิจ%' OR name_en LIKE '%coop%'`
- **ห้าม hardcode คำตอบเป็นค่าคงที่** (เช่น `SELECT '4' AS note` หรือ `SELECT 'ปี 4 เทอม 2' AS note`) แม้จะรู้คำตอบ
  จากคำอธิบาย schema ด้านบนก็ตาม ทุกคำตอบต้องมาจากการ query ตารางจริงเท่านั้น เพราะเป้าหมายของระบบนี้คือตรวจสอบว่า
  ดึงข้อมูลจาก DB ได้ถูกต้องจริง — ถ้าหาข้อมูลจริงมาตอบไม่ได้ ให้ตอบ SELECT 'NO_ANSWER' AS note; แทนการเดา
- **สำคัญ**: คอลัมน์ชื่อซ้ำหลายตาราง (เช่น name_th, code, year, semester, category, type อยู่ทั้งใน
  programs/courses/academic_plan_courses) → เวลา JOIN ต้องใส่ชื่อตาราง/alias นำหน้าเสมอ
  (เช่น apc.name_th ไม่ใช่ name_th เฉยๆ) มิฉะนั้นจะ error ambiguous column
- ภาษากฎระเบียบไทย "ไม่ต่ำกว่า X" หมายถึง >= X (ไม่ใช่ >)
- **กฎ/ระเบียบ/เกณฑ์** (รีไทร์, ภาคทัณฑ์, เกียรตินิยม, ลงทะเบียน, ลาพัก ฯลฯ) อยู่ในตาราง rules(category, subcategory, text)
  ค้นด้วย LIKE บน category และ/หรือ text; ตัวเลขในกฎมักเป็น "เลขไทย" (เช่น "ต่ำกว่า ๑.๐๐" ไม่ใช่ 1.00) จึงควร LIKE ด้วยคำไทย
  เช่น รีไทร์ -> WHERE category='dismissal_criteria' AND text LIKE '%เฉลี่ยสะสม%'; เกียรตินิยม -> text LIKE '%เกียรตินิยม%'
- **ค่าเกรด** อยู่ในตาราง grade_scale(grade, point, is_gpa): A=4.0,B+=3.5,...,F=0.0 (is_gpa=1); I,S,U,T -> point NULL, is_gpa=0
- **ทุนการศึกษา** อยู่ในตาราง scholarships(name, provider, amount, gpa_requirement, eligibility, deadline, ...) ค้นเมื่อถามเรื่องทุน
"""


# คอลัมน์ประเภทหมวดหมู่ (category) ที่ควรบอก "ค่าที่เป็นไปได้จริง" ให้โมเดล
# เพื่อกันโมเดลเดาค่าผิด (เช่น เดา category='ศึกษาทั่วไป' ทั้งที่จริงเก็บเป็น 'general_education')
ENUM_MAX_DISTINCT = 25   # เอาเฉพาะคอลัมน์ที่ค่าไม่กี่แบบ (เป็น category จริงๆ)
ENUM_MAX_LEN = 80        # ข้ามค่าที่ยาวเกิน (พวก free text)
# คอลัมน์ที่รู้อยู่แล้วว่าเป็น free text / รหัส ไม่ต้องเอามาทำ enum
ENUM_SKIP_COLS = {
    "name_th", "name_en", "description", "career_text", "subcategory_text",
    "track_text", "code", "prerequisite_code", "credits_raw", "source_files",
    "degree_th", "degree_en", "institution", "faculty", "major_th", "major_en",
}


def build_schema_description(db_path: str) -> str:
    """อ่านโครงสร้างจริงจาก DB แล้วสร้างข้อความ schema สำหรับใส่ใน prompt."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.cursor()
    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    lines = []
    for t in tables:
        cols = cur.execute(f"PRAGMA table_info({t})").fetchall()  # cid,name,type,notnull,dflt,pk
        col_strs = []
        for c in cols:
            tag = " PK" if c[5] else ""
            col_strs.append(f"{c[1]} {c[2]}{tag}")
        lines.append(f"TABLE {t} ({', '.join(col_strs)})")
        fks = cur.execute(f"PRAGMA foreign_key_list({t})").fetchall()
        for fk in fks:  # id,seq,table,from,to,...
            lines.append(f"    FK {t}.{fk[3]} -> {fk[2]}.{fk[4]}")
    con.close()
    return "\n".join(lines)


def build_column_value_hints(db_path: str) -> str:
    """ดึงค่า distinct ของคอลัมน์ประเภทหมวดหมู่มาบอกโมเดล ('คอลัมน์นี้มีค่าได้แค่พวกนี้')
    ทำอัตโนมัติจากข้อมูลจริง จะได้ไม่ต้องมานั่งเขียน enum เองทีละอัน และอัปเดตตามข้อมูลเสมอ."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.cursor()
    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    lines = []
    for t in tables:
        for c in cur.execute(f"PRAGMA table_info({t})").fetchall():
            name, ctype = c[1], (c[2] or "").upper()
            if "TEXT" not in ctype or name in ENUM_SKIP_COLS:
                continue
            try:
                dc = cur.execute(
                    f'SELECT COUNT(DISTINCT "{name}") FROM "{t}" WHERE "{name}" IS NOT NULL'
                ).fetchone()[0]
            except sqlite3.Error:
                continue
            if dc == 0 or dc > ENUM_MAX_DISTINCT:
                continue
            vals = [r[0] for r in cur.execute(
                f'SELECT DISTINCT "{name}" FROM "{t}" WHERE "{name}" IS NOT NULL ORDER BY "{name}"'
            )]
            if any(len(str(v)) > ENUM_MAX_LEN for v in vals):
                continue
            vals_str = ", ".join(repr(v) for v in vals)
            lines.append(f"- {t}.{name} มีค่าได้แค่: {vals_str}")
    con.close()
    if not lines:
        return ""
    return ("ค่าที่เป็นไปได้ของคอลัมน์ประเภทหมวดหมู่ "
            "(ต้อง match ให้ตรงเป๊ะ ห้ามเดาเป็นคำอื่น):\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# 2) Few-shot examples — คู่ตัวอย่าง คำถามไทย -> SQL ช่วยให้ LLM เขียนถูก
# ---------------------------------------------------------------------------

FEWSHOT = [
    ("หลักสูตร IT ต้องเรียนกี่หน่วยกิตถึงจะจบ",
     "SELECT total_credits FROM programs WHERE program_code = 'IT';"),
    ("วิชา 06066102 ของ IT ต้องผ่านวิชาอะไรก่อน",
     "SELECT cp.prerequisite_code FROM courses c "
     "JOIN programs p ON p.program_id = c.program_id "
     "JOIN course_prerequisites cp ON cp.course_id = c.course_id "
     "WHERE p.program_code = 'IT' AND c.code = '06066102';"),
    ("เทอม 1 ปี 1 ของ DSBA ลงเรียนวิชาอะไรบ้าง",
     "SELECT apc.code, apc.name_en, apc.credits_raw FROM programs p "
     "JOIN academic_plans ap ON ap.program_id = p.program_id "
     "JOIN academic_plan_courses apc ON apc.plan_id = ap.plan_id "
     "WHERE p.program_code = 'DSBA' AND ap.year = 1 AND ap.semester = 1;"),
    ("หลักสูตร BIT มีวิชาบังคับกี่วิชา",
     "SELECT COUNT(*) AS n FROM courses c "
     "JOIN programs p ON p.program_id = c.program_id "
     "WHERE p.program_code = 'BIT' AND c.type = 'บังคับ';"),
    ("หมวดวิชาศึกษาทั่วไปของ BIT กี่หน่วยกิต",
     "SELECT cs.credits FROM curriculum_structure cs "
     "JOIN programs p ON p.program_id = cs.program_id "
     "WHERE p.program_code = 'BIT' AND cs.category = 'general_education';"),
    ("แผนสหกิจของ IT ปี 4 เทอม 2 เรียนวิชาอะไรบ้าง",
     "SELECT apc.code, apc.name_th, apc.credits_raw FROM academic_plan_courses apc "
     "JOIN programs p ON p.program_id = apc.program_id "
     "WHERE p.program_code = 'IT' AND apc.plan_type = 'coop' "
     "AND apc.year = 4 AND apc.semester = 2;"),
    ("IT สายเกม ปี 3 เทอม 1 เรียนอะไรบ้าง",
     "SELECT apc.code, apc.name_th, apc.credits_raw FROM academic_plan_courses apc "
     "JOIN programs p ON p.program_id = apc.program_id "
     "WHERE p.program_code = 'IT' AND apc.plan_type = 'no_coop' "
     "AND (apc.module = 'game' OR apc.module IS NULL) "
     "AND apc.year = 3 AND apc.semester = 1;"),
    ("วิชา 06016418 ของ IT สายเกม ต้องเรียนอะไรก่อน",
     "SELECT cp.prerequisite_code FROM courses c "
     "JOIN programs p ON p.program_id = c.program_id "
     "JOIN course_prerequisites cp ON cp.course_id = c.course_id "
     "WHERE p.program_code = 'IT' AND c.code = '06016418';"),
    ("AIT ไปสหกิจปีไหน",
     "SELECT apc.year, apc.semester FROM academic_plan_courses apc "
     "JOIN programs p ON p.program_id = apc.program_id "
     "WHERE p.program_code = 'AIT' "
     "AND (apc.name_th LIKE '%สหกิจ%' OR apc.name_en LIKE '%coop%');"),
    ("เกรด B+ มีค่าเท่าไหร่",
     "SELECT grade, point FROM grade_scale WHERE grade = 'B+';"),
    ("GPA เท่าไหร่ถึงจะโดนรีไทร์",
     "SELECT text FROM rules WHERE category = 'dismissal_criteria' AND text LIKE '%เฉลี่ยสะสม%';"),
    ("เกียรตินิยมอันดับหนึ่งต้องได้ GPA เท่าไหร่",
     "SELECT subcategory, text FROM rules "
     "WHERE category = 'honors_criteria' AND text LIKE '%เกียรตินิยมอันดับหนึ่ง%';"),
    ("มีทุนอะไรบ้าง",
     "SELECT name, amount, gpa_requirement, deadline FROM scholarships;"),
]


def build_system_prompt(schema_desc: str, value_hints: str = "") -> str:
    ex = "\n\n".join(f"คำถาม: {q}\nSQL: {s}" for q, s in FEWSHOT)
    hints_block = f"\n{value_hints}\n" if value_hints else ""
    return textwrap.dedent(f"""\
    คุณคือผู้ช่วยแปลงคำถามภาษาไทยเป็นคำสั่ง SQLite (SQL) สำหรับฐานข้อมูลหลักสูตรมหาวิทยาลัย
    (ฐานข้อมูลนี้ครอบคลุมทั้ง หลักสูตร/แผนการเรียน, กฎระเบียบ (ตาราง rules), ค่าเกรด (grade_scale), และทุนการศึกษา (scholarships))

    กติกา:
    - ตอบกลับเป็น SQL เพียงคำสั่งเดียวเท่านั้น ห้ามมีคำอธิบาย ห้ามมี markdown ห้ามมี ```
    - ใช้ได้แค่ SELECT (หรือ WITH ... SELECT) ห้าม INSERT/UPDATE/DELETE/DROP เด็ดขาด
    - ใช้เฉพาะตารางและคอลัมน์ที่มีใน schema ข้างล่างเท่านั้น
    - ค่าในคอลัมน์ประเภทหมวดหมู่ต้องใช้ให้ตรงกับรายการ "ค่าที่เป็นไปได้" ด้านล่าง ห้ามแปล/เดาเป็นคำอื่น
    - ถ้าคำถามกว้าง ให้ใส่ LIMIT ที่เหมาะสม
    - ถ้าคำถาม "ไม่ใช่การดึงข้อมูลจากฐานข้อมูล" — เช่น ทักทาย/คุยเล่น, ขอคำแนะนำหรือความคิดเห็น, ให้ช่วยตัดสินใจ,
      อธิบายแนวคิด/นิยามทั่วไป, หรือเป็นคำถามที่ตารางในฐานข้อมูลนี้ตอบไม่ได้ — ให้ตอบกลับเป็นคำเดียวว่า: CHAT
      (ระบบจะส่งต่อให้โหมดสนทนาไปตอบเอง) ห้ามพยายามเดา SQL สำหรับคำถามประเภทนี้

    Schema:
    {schema_desc}

    {SCHEMA_NOTES}{hints_block}
    ตัวอย่าง:
    {ex}
    """)


# ---------------------------------------------------------------------------
# 3) SQL safety guard
# ---------------------------------------------------------------------------

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
    r"pragma|vacuum|reindex|truncate)\b", re.IGNORECASE)


def sanitize_sql(sql: str, default_limit: int = 200):
    """คืน (clean_sql, error). ถ้า error != None คือไม่ปลอดภัย/ผิดรูปแบบ."""
    if not sql:
        return None, "empty SQL"
    # ตัด markdown fences ถ้ามี
    sql = re.sub(r"```[a-zA-Z]*", "", sql).replace("```", "").strip()
    # เอาเฉพาะคำสั่งแรก (กัน multiple statements)
    if ";" in sql.rstrip().rstrip(";"):
        # มี ; อยู่ตรงกลาง = หลายคำสั่ง
        sql = sql.split(";")[0]
    sql = sql.strip().rstrip(";").strip()
    low = sql.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return None, "อนุญาตเฉพาะ SELECT/WITH"
    if _FORBIDDEN.search(sql):
        return None, "พบคำสั่งที่ไม่อนุญาต (เขียน/แก้ข้อมูล)"
    # เติม LIMIT ถ้ายังไม่มี (เฉพาะกรณีไม่ใช่ aggregate ล้วน ก็ใส่ไปเลยกันเหนียว)
    if not re.search(r"\blimit\b", low):
        sql = f"{sql}\nLIMIT {default_limit}"
    return sql, None


# ---------------------------------------------------------------------------
# 4) Run query (read-only)
# ---------------------------------------------------------------------------

def run_query(db_path: str, sql: str):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]
        cols = [d[0] for d in cur.description] if cur.description else []
        return cols, rows, None
    except sqlite3.Error as e:
        return [], [], str(e)
    finally:
        con.close()


def format_table(cols, rows, max_rows=30):
    if not rows:
        return "(ไม่มีผลลัพธ์)"
    shown = rows[:max_rows]
    widths = {c: max(len(str(c)), *(len(str(r.get(c, ""))) for r in shown)) for c in cols}
    line = " | ".join(str(c).ljust(widths[c]) for c in cols)
    sep = "-+-".join("-" * widths[c] for c in cols)
    out = [line, sep]
    for r in shown:
        out.append(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))
    if len(rows) > max_rows:
        out.append(f"... ({len(rows)} แถวทั้งหมด, แสดง {max_rows})")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 5) LLM calls (OpenAI-compatible)
# ---------------------------------------------------------------------------

def _client():
    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("ต้องติดตั้ง openai ก่อน:  pip install openai")
    base_url = os.environ.get("LLM_BASE_URL")
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        sys.exit("ยังไม่ได้ตั้งค่า LLM_API_KEY — สร้างไฟล์ .env (ดู .env.example) "
                 "หรือตั้ง environment variable เอง")
    return OpenAI(base_url=base_url, api_key=api_key)


def _extra_body() -> dict:
    """คุมการ routing ของ OpenRouter เพื่อให้ผลลัพธ์ "นิ่ง" (deterministic):
    - LLM_PROVIDER : ล็อกให้ใช้ provider เดียว (เช่น 'Novita' หรือ 'DeepInfra')
      ตั้ง allow_fallbacks=false เพื่อไม่ให้สลับไป provider อื่น
      (สาเหตุที่คำตอบเคยไม่นิ่ง: OpenRouter สลับระหว่าง provider ที่ quantization ต่างกัน fp8/bf16)
    - LLM_SEED     : ใส่ seed (ถ้า provider รองรับ) ช่วยให้สุ่มซ้ำแบบเดิม
    ถ้าไม่ตั้ง env เหล่านี้ ก็ทำงานเหมือนเดิม (ไม่ล็อก)"""
    body = {}
    provider = os.environ.get("LLM_PROVIDER", "").strip()
    if provider:
        body["provider"] = {"order": [provider], "allow_fallbacks": False}
    seed = os.environ.get("LLM_SEED", "").strip()
    if seed:
        try:
            body["seed"] = int(seed)
        except ValueError:
            pass
    return body


def generate_sql(question: str, system_prompt: str, force: bool = False) -> str:
    """เขียน SQL จากคำถาม.
    force=True ใช้ตอน "ลองใหม่" เมื่อรอบแรกตอบ NO_ANSWER แบบพลาดๆ (เพราะ LLM ฝั่ง
    provider มี nondeterminism ตอบ NO_ANSWER มั่วเป็นบางครั้ง แม้คำถามตอบได้จริง) —
    จะย้ำว่าห้าม NO_ANSWER + เพิ่ม temperature เล็กน้อยให้ลองเส้นทางอื่น"""
    client = _client()
    model = os.environ.get("LLM_MODEL", "qwen/qwen-2.5-72b-instruct")
    user = f"คำถาม: {question}\nSQL:"
    if force:
        user = (f"คำถาม: {question}\n"
                "คำถามนี้ตอบได้จากตารางในฐานข้อมูลแน่นอน ห้ามตอบ SELECT 'NO_ANSWER' "
                "ให้เลือกตารางที่เกี่ยวข้อง (programs/courses/academic_plan_courses/rules/"
                "grade_scale/scholarships) แล้วเขียน SELECT ที่เหมาะสม\nSQL:")
    resp = client.chat.completions.create(
        model=model,
        temperature=0.2 if force else 0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user},
        ],
        extra_body=_extra_body(),
    )
    return resp.choices[0].message.content.strip()


def phrase_answer(question: str, sql: str, cols, rows) -> str:
    client = _client()
    model = os.environ.get("LLM_MODEL", "qwen/qwen-2.5-72b-instruct")
    data = json.dumps(rows[:50], ensure_ascii=False)
    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[
            {"role": "system", "content":
                "คุณคือผู้ช่วยตอบคำถามนักศึกษาเรื่องหลักสูตร ตอบเป็นภาษาไทยสั้นกระชับ "
                "อ้างอิงจากข้อมูลผลลัพธ์เท่านั้น ถ้าไม่มีข้อมูลให้บอกตรงๆ ว่าไม่พบ ห้ามเดา"},
            {"role": "user", "content":
                f"คำถาม: {question}\nSQL ที่ใช้: {sql}\nผลลัพธ์ (JSON): {data}\n\nช่วยเรียบเรียงคำตอบ:"},
        ],
        extra_body=_extra_body(),
    )
    return resp.choices[0].message.content.strip()


_CHAT_SYSTEM = (
    "คุณคือ 'UniAssist' ผู้ช่วยนักศึกษาของคณะเทคโนโลยีสารสนเทศ สจล. (KMITL) ที่เป็นกันเอง สุภาพ ช่วยเหลือดี "
    "ตอบเป็นภาษาไทย กระชับ อ่านง่าย เป็นธรรมชาติแบบแชท\n\n"
    "ขอบเขต (สำคัญ): ตอบเฉพาะเรื่องที่เกี่ยวกับ 'การเรียน/หลักสูตร/กฎระเบียบ/ทุน/การวางแผนเรียน/ชีวิตนักศึกษา' "
    "ของคณะ IT KMITL เท่านั้น\n"
    "- ถ้าถูกถามนอกเรื่องเหล่านี้ (เช่น การเมือง ดูดวง เขียนโค้ดที่ไม่เกี่ยวข้อง เรื่องส่วนตัว) ให้ปฏิเสธสุภาพสั้นๆ "
    "แล้วชวนกลับมาเรื่องการเรียน\n\n"
    "กันข้อมูลผิด (สำคัญมาก):\n"
    "- ให้ยึด 'ข้อเท็จจริงจริงของระบบ' ที่ให้ไว้ด้านล่างเป็นหลัก ห้ามตอบขัดแย้งกับข้อมูลนั้น\n"
    "- ถ้าผู้ใช้ถามรายละเอียด 'เจาะจง' ที่ไม่มีในข้อเท็จจริงด้านล่าง (เช่น วิชาในแผนแต่ละเทอม, รหัสวิชา, "
    "ตัวเลข/ข้อความกฎเป๊ะๆ, จำนวนเงินทุน) — ห้ามเดา/แต่งขึ้น ให้บอกตรงๆ ว่าไม่มีข้อมูลตรงนั้น "
    "แล้วชวนผู้ใช้ 'ถามแบบเจาะจง' (ระบุหลักสูตร/ชั้นปี/เทอม/ชื่อทุน) เพื่อให้ระบบดึงจากฐานข้อมูลจริงมาให้\n"
    "- คำแนะนำ/ความเห็นทั่วไป (เช่น เทคนิคการเรียน วิธีวางแผน) ตอบตามความรู้ทั่วไปได้ แต่ถ้าอ้างตัวเลข/ข้อเท็จจริง "
    "ของหลักสูตรต้องมาจากข้อเท็จจริงด้านล่างเท่านั้น"
)


_domain_digest_cache = None


def general_answer(question: str, note: str = "", digest: str = "") -> str:
    """โหมดสนทนา: ให้ LLM ตอบเองแบบ chatbot (ไม่ผูกกับ SQL) แต่ยึดข้อเท็จจริงของเรา
    digest = สรุปข้อเท็จจริงจริงจาก DB (จาก assist_logic.build_domain_digest) ไว้ ground คำตอบ
    note   = บริบทเสริม เช่น '(ค้นฐานข้อมูลแล้วไม่พบข้อมูลตรงคำถามนี้)'"""
    client = _client()
    model = os.environ.get("LLM_MODEL", "qwen/qwen-2.5-72b-instruct")
    system = _CHAT_SYSTEM
    if digest:
        system += "\n\n=== ข้อเท็จจริงจริงของระบบ (ยึดตามนี้) ===\n" + digest
    user = question if not note else f"{question}\n\n[หมายเหตุระบบ: {note}]"
    resp = client.chat.completions.create(
        model=model,
        temperature=0.4,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        extra_body=_extra_body(),
    )
    return resp.choices[0].message.content.strip()


def looks_like_chat(raw: str) -> bool:
    """ตรวจว่า output จาก generate_sql เป็นการส่งต่อโหมดสนทนา (CHAT/NO_ANSWER)
    หรือไม่ใช่ SELECT/WITH (ตอบเพี้ยน) -> ให้ไปโหมดสนทนา"""
    if not raw:
        return True
    s = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "").strip().rstrip(";").strip()
    up = s.upper()
    if up == "CHAT" or "NO_ANSWER" in up:
        return True
    return not (up.startswith("SELECT") or up.startswith("WITH"))


# ---------------------------------------------------------------------------
# 6) Orchestration
# ---------------------------------------------------------------------------

def answer_question(db_path, system_prompt, question, raw=False, verbose=True):
    raw_sql = generate_sql(question, system_prompt)
    sql, err = sanitize_sql(raw_sql)
    if err:
        print(f"[SQL ไม่ปลอดภัย/ผิดรูป] {err}\nโมเดลตอบมา: {raw_sql}")
        return
    if verbose:
        print(f"\n--- SQL ที่โมเดลเขียน ---\n{sql}\n")
    cols, rows, qerr = run_query(db_path, sql)
    if qerr:
        print(f"[รัน SQL ไม่ผ่าน] {qerr}")
        return
    print("--- ผลลัพธ์จากฐานข้อมูล ---")
    print(format_table(cols, rows))
    if not raw:
        print("\n--- คำตอบ ---")
        print(phrase_answer(question, sql, cols, rows))


def main():
    ap = argparse.ArgumentParser(description="Text-to-SQL chatbot (UniAssist)")
    ap.add_argument("--db", default=DEFAULT_DB, help="path ไปยังไฟล์ .db")
    ap.add_argument("-q", "--question", help="ถามคำถามเดียวแล้วจบ")
    ap.add_argument("--sql", help="รัน SQL ตรงๆ (ข้าม LLM, ไว้เทสต์/eval)")
    ap.add_argument("--raw", action="store_true", help="แสดงแค่ตาราง ไม่เรียบเรียงคำตอบ")
    ap.add_argument("--show-schema", action="store_true", help="พิมพ์ schema ที่ส่งให้โมเดลแล้วออก")
    ap.add_argument("--env-file", default=".env", help="path ไปยังไฟล์ .env (ค่าเริ่มต้น: .env)")
    args = ap.parse_args()

    load_dotenv(args.env_file)  # อ่านค่า LLM_* จาก .env อัตโนมัติ (ถ้ามีไฟล์)

    if not os.path.exists(args.db):
        sys.exit(f"ไม่พบไฟล์ DB: {args.db}")

    schema_desc = build_schema_description(args.db)
    value_hints = build_column_value_hints(args.db)
    system_prompt = build_system_prompt(schema_desc, value_hints)

    if args.show_schema:
        print(system_prompt)
        return

    if args.sql:  # โหมดรัน SQL ตรงๆ (ไม่ต้องเรียก LLM)
        sql, err = sanitize_sql(args.sql)
        if err:
            sys.exit(f"[SQL ไม่ปลอดภัย] {err}")
        cols, rows, qerr = run_query(args.db, sql)
        if qerr:
            sys.exit(f"[รัน SQL ไม่ผ่าน] {qerr}")
        print(format_table(cols, rows))
        return

    if args.question:
        answer_question(args.db, system_prompt, args.question, raw=args.raw)
        return

    # โหมดคุยโต้ตอบ
    print("UniAssist Text-to-SQL chatbot — พิมพ์คำถามภาษาไทย (พิมพ์ 'exit' เพื่อออก)")
    while True:
        try:
            q = input("\nคำถาม> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if q.lower() in ("exit", "quit", "ออก", ""):
            break
        answer_question(args.db, system_prompt, q, raw=args.raw)


if __name__ == "__main__":
    main()