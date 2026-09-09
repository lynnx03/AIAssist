"""
build_extra_tables.py
---------------------
เพิ่มตาราง "เสริม" เข้า DB ของ chatbot (ต่อยอดจาก build_chatbot_db.py) แบบ idempotent
รันซ้ำได้เรื่อยๆ — ทุกตารางจะ DROP แล้ว CREATE ใหม่ทุกครั้ง (ไม่กระทบตารางหลักสูตรเดิม)

ครอบคลุม:
  Phase 1 : grade_scale, rules        <- มาจาก data/extracted_rules/rules_qwen.json
  Phase 3 : scholarships              <- ข้อมูลจริงจากเว็บคณะ IT (it.kmitl.ac.th/th/scholarship)
  Phase 4 : students, student_term_gpa<- mock (ต่อ registrar API ภายหลัง)

Usage:
    python build_extra_tables.py [db_path] [rules_json]
    # ค่าเริ่มต้น: chatbot_teach_table.db  data/extracted_rules/rules_qwen.json
"""

import json
import sqlite3
import sys
from pathlib import Path

# บน Windows คอนโซลมักเป็น cp1252 -> print ภาษาไทยแล้ว error; บังคับ stdout เป็น utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

DEFAULT_DB = "chatbot_teach_table.db"
DEFAULT_RULES = "data/extracted_rules/rules_qwen.json"

# เกรดที่คิด GPA (มีค่าตัวเลข) — ใช้ตัดสิน is_gpa เวลาตัวเลขในไฟล์อาจ parse ยาก
GPA_GRADES = {"A", "B+", "B", "C+", "C", "D+", "D", "F"}


# ---------------------------------------------------------------------------
# Phase 1: grade_scale
# ---------------------------------------------------------------------------
def build_grade_scale(cur, rules):
    """สร้างตาราง grade_scale จาก grading_system.scale
    เกรดตัวเลข (A..F) -> point = ค่าจริง, is_gpa=1
    เกรดพิเศษ (I,S,U,T) -> point=NULL, is_gpa=0 (ไม่คิด GPA)"""
    cur.execute("DROP TABLE IF EXISTS grade_scale")
    cur.execute("""
        CREATE TABLE grade_scale (
            grade  TEXT PRIMARY KEY,
            point  REAL,        -- ค่าระดับคะแนน (NULL = ไม่คิด GPA)
            is_gpa INTEGER      -- 1 = นำไปคำนวณ GPA, 0 = ไม่คิด
        )
    """)
    scale = (rules.get("grading_system") or {}).get("scale", {})
    n = 0
    for grade, val in scale.items():
        # ค่าที่เป็นตัวเลขจริง (float/int) = เกรดคิด GPA; ค่าที่เป็น string คำอธิบาย = ไม่คิด
        if isinstance(val, (int, float)):
            point, is_gpa = float(val), 1
        else:
            point, is_gpa = None, 0
        cur.execute(
            "INSERT INTO grade_scale (grade, point, is_gpa) VALUES (?,?,?)",
            (grade, point, is_gpa),
        )
        n += 1
    return n


# ---------------------------------------------------------------------------
# Phase 1: rules (flatten หมวดกฎทั้งหมด ยกเว้น grading_system.scale ที่เป็นตัวเลขล้วน)
# ---------------------------------------------------------------------------
def _flatten(category, obj, out, subcategory=None):
    """แปลง nested structure ให้เป็นแถวๆ (category, subcategory, text)
    - list ของ string  -> แต่ละ item เป็น 1 แถว
    - dict             -> ลงลึกต่อ โดยใช้ key เป็น subcategory
    - string/number    -> 1 แถว
    ข้าม value ที่เป็น None/[] เปล่า"""
    if obj is None:
        return
    if isinstance(obj, str):
        text = obj.strip()
        if text:
            out.append((category, subcategory, text))
    elif isinstance(obj, (int, float)):
        out.append((category, subcategory, str(obj)))
    elif isinstance(obj, list):
        for item in obj:
            _flatten(category, item, out, subcategory)
    elif isinstance(obj, dict):
        for key, val in obj.items():
            # ต่อชื่อ subcategory ไปเรื่อยๆ (เช่น honors_criteria.first_class.gpa_min)
            sub = key if subcategory is None else f"{subcategory}.{key}"
            _flatten(category, val, out, sub)


def build_rules(cur, rules):
    """flatten ทุกหมวดกฎ (nested) -> ตาราง rules(category, subcategory, text)
    เพื่อให้ Text-to-SQL ค้นด้วย LIKE ได้ (prose ยาวๆ)
    grading_system.scale เป็นตัวเลขล้วน (มี grade_scale แยกแล้ว) จึงข้าม"""
    cur.execute("DROP TABLE IF EXISTS rules")
    cur.execute("""
        CREATE TABLE rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT NOT NULL,
            subcategory TEXT,
            text        TEXT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX idx_rules_category ON rules(category)")

    out = []
    for category, obj in rules.items():
        if category == "grading_system":
            # เก็บทุกอย่างในหมวดเกรด ยกเว้น scale (ตัวเลขล้วน -> อยู่ใน grade_scale แล้ว)
            gs = dict(obj)
            gs.pop("scale", None)
            _flatten(category, gs, out)
        else:
            _flatten(category, obj, out)

    cur.executemany(
        "INSERT INTO rules (category, subcategory, text) VALUES (?,?,?)",
        out,
    )
    return len(out)


# ---------------------------------------------------------------------------
# Phase 3: scholarships (mock — placeholder ให้แทนที่ด้วยข้อมูลจริงจากเว็บคณะภายหลัง)
# ---------------------------------------------------------------------------
# NOTE: ข้อมูลทุนจริง ดึงจากเว็บคณะ IT (it.kmitl.ac.th/th/scholarship) เมื่อ 2026-08-23
#       รายละเอียด/กำหนดการอาจเปลี่ยน — ตรวจสอบหน้าเว็บทางการอีกครั้งก่อนใช้จริง
#       gpa_requirement เก็บเป็นเกณฑ์ GPAX ระดับมหาวิทยาลัย (NULL = ไม่กำหนด/ใช้เกณฑ์อื่น)
_IT = "คณะเทคโนโลยีสารสนเทศ (IT KMITL)"
_INST = "สถาบัน (KMITL)"
_BASE = "https://www.it.kmitl.ac.th/th/scholarship/"
SCHOLARSHIPS = [
    ("ทุนนักเรียนที่มีคะแนนสอบเข้าดีเด่น (แรกเข้า)", _IT, "ไม่ระบุ", 3.50,
     "นักเรียนแรกเข้า ป.ตรี, GPAX ม.ปลาย ไม่น้อยกว่า 3.50", "1 มิ.ย. 2569",
     "ทุนแรกเข้าสำหรับผู้มีคะแนนสอบเข้าดีเด่น (สัมภาษณ์ 16 มิ.ย., ประกาศผล 19 มิ.ย. 2569)",
     _BASE + "scholarships-for-first-time-bachelors-degree-students"),
    ("ทุนนักเรียนที่มีผลงานนวัตกรรมดีเด่น (แรกเข้า)", _IT, "ไม่ระบุ", None,
     "นักเรียนแรกเข้า ป.ตรี, มีผลงานนวัตกรรม/รางวัลแข่งขัน (โอลิมปิก สอวน. Hackathon) หรือสิทธิบัตร/ซอฟต์แวร์", "1 มิ.ย. 2569",
     "ทุนแรกเข้าสำหรับผู้มีผลงานนวัตกรรม/รางวัลเชิงวิชาการดีเด่น",
     _BASE + "scholarships-for-first-time-bachelors-degree-students"),
    ("ทุนโควตาโครงการนักเรียนโอลิมปิกวิชาการ (แรกเข้า)", _IT, "ไม่ระบุ", None,
     "นักเรียนแรกเข้า ป.ตรี ที่เป็นนักเรียนโครงการโอลิมปิกวิชาการ", "1 มิ.ย. 2569",
     "ทุนแรกเข้าโควตาสำหรับนักเรียนโอลิมปิกวิชาการ",
     _BASE + "scholarships-for-first-time-bachelors-degree-students"),
    ("ทุนเรียนดีอันดับหนึ่ง", _IT, "ยกเว้นค่าธรรมเนียมการศึกษาแบบเหมาจ่าย (1 ภาคการศึกษา)", None,
     "ป.ตรี ทุกชั้นปี (ปี1 เทอม2 ถึง ปี4 เทอม1) ที่ได้คะแนนเฉลี่ยภาคที่แล้วสูงสุดอันดับ 1 ของหลักสูตร/ชั้นปี, ไม่ลาพัก ไม่เคยสอบตก ประพฤติดี", "ไม่ระบุ",
     "ยกเว้นค่าเทอมให้ผู้ที่ได้เกรดเฉลี่ยสูงสุดอันดับ 1 ของแต่ละหลักสูตร/ชั้นปี",
     _BASE + "top-academic-excellence-scholarship"),
    ("ทุนเรียนดีและขาดแคลนทุนทรัพย์", _IT, "ยกเว้นค่าธรรมเนียมการศึกษาแบบเหมาจ่าย", 3.00,
     "GPAX ไม่ต่ำกว่า 3.00 (ม.6 ≥ 3.25), รายได้ครอบครัว ≤ 360,000 บาท/ปี, ปฏิบัติงานให้คณะ ≥ 25 ชม./ภาคเรียน, ไม่ลาพัก ประพฤติดี", "3 ต.ค. 2568",
     "ยกเว้นค่าเทอมสำหรับผู้เรียนดีแต่ขาดแคลนทุนทรัพย์ (ประกาศผล 14 พ.ย. 2568)",
     _BASE + "academic-excellence-and-financial-need"),
    ("ทุนช่วยเหลือค่าใช้จ่ายทางการศึกษาแบบฉุกเฉิน", _IT, "ไม่เกิน 30,000 บาท/ทุน", 2.25,
     "GPAX ไม่ต่ำกว่า 2.25, ป.ตรีของคณะที่ประสบปัญหาค่าใช้จ่ายฉุกเฉิน, ปฏิบัติงานให้คณะ ≥ 25 ชม./ภาคเรียน, ประพฤติดี", "3 ต.ค. 2568",
     "ทุนฉุกเฉินช่วยค่าเทอม/ค่าครองชีพ ระยะเวลา 1 ภาคการศึกษา",
     _BASE + "Emergency-Educational-Assistance-Grant"),
    ("ทุนน้องใหม่ลูกพระจอม", _INST, "10,000 บาท", None,
     "นักศึกษาปี 1 ทุกสาขา, รายได้ครอบครัว ≤ 400,000 บาท/ปี, เข้าร่วมกิจกรรม 30 ชม.", "ไม่ระบุ",
     "ทุนระดับสถาบันสำหรับน้องใหม่ปี 1 (สมัคร scholarship.kmitl.ac.th)",
     _BASE + "thunkansueksaradabsthaban"),
    ("ทุนอุดหนุนการศึกษาประเภท ก", _INST, "ยกเว้นค่าธรรมเนียม + ค่าใช้จ่ายรายเดือน", 3.00,
     "GPAX ไม่ต่ำกว่า 3.00, ป.ตรี ปี 1 ขาดแคลนทุนทรัพย์ (รายได้ ≤ 360,000 บาท/ปี), กิจกรรม 60 ชม., ได้รับต่อเนื่องจนจบ", "ไม่ระบุ",
     "ทุนระดับสถาบันแบบต่อเนื่องสำหรับผู้ขาดแคลน (สมัคร scholarship.kmitl.ac.th)",
     _BASE + "thunkansueksaradabsthaban"),
    ("ทุนผู้ทำคุณประโยชน์ให้แก่สถาบัน", _INST, "ไม่ระบุ", 2.00,
     "GPAX ไม่ต่ำกว่า 2.00, ป.ตรี ทุกชั้นปี ที่ทำคุณประโยชน์ด้านกิจกรรม/จิตอาสา/วิชาการ", "ไม่ระบุ",
     "ทุนระดับสถาบันสำหรับผู้ทำคุณประโยชน์ (สมัคร scholarship.kmitl.ac.th)",
     _BASE + "thunkansueksaradabsthaban"),
    ("ทุนอุดหนุนการศึกษาประเภท ข", _INST, "ไม่ระบุ", 2.00,
     "GPAX ไม่ต่ำกว่า 2.00, ปี 1 (รายได้ ≤ 600,000) หรือ ปี 2-5 (รายได้ 360,001-600,000 บาท/ปี), กิจกรรม 45 ชม.", "ไม่ระบุ",
     "ทุนระดับสถาบันสำหรับผู้ขาดแคลน (สมัคร scholarship.kmitl.ac.th)",
     _BASE + "thunkansueksaradabsthaban"),
    ("ทุนโครงการ BKI Scholarship", "บริษัท กรุงเทพประกันภัย จำกัด (มหาชน)", "ทุนให้เปล่า (ไม่ระบุจำนวน)", 3.00,
     "นักศึกษาปี 2 ทุกสาขา, GPAX ตั้งแต่ 3.00 ขึ้นไป, ไม่รับทุนอื่นพร้อมกัน (ยกเว้น กยศ./กรอ.), ประพฤติดี", "14 ส.ค. 2569",
     "ทุนให้เปล่าจากกรุงเทพประกันภัย ต่อเนื่องตลอดการศึกษา (สัมภาษณ์ ส.ค. 2569)",
     _BASE + "BKI-Scholarship"),
]


def build_scholarships(cur):
    cur.execute("DROP TABLE IF EXISTS scholarships")
    cur.execute("""
        CREATE TABLE scholarships (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            provider        TEXT,
            amount          TEXT,        -- เก็บเป็น text เพราะรูปแบบหลากหลาย ('20,000 บาท/ปี')
            gpa_requirement REAL,        -- NULL = ไม่กำหนดเกณฑ์ GPA
            eligibility     TEXT,
            deadline        TEXT,
            description     TEXT,
            url             TEXT
        )
    """)
    cur.executemany(
        "INSERT INTO scholarships (name,provider,amount,gpa_requirement,eligibility,deadline,description,url) "
        "VALUES (?,?,?,?,?,?,?,?)",
        SCHOLARSHIPS,
    )
    return len(SCHOLARSHIPS)


# ---------------------------------------------------------------------------
# Phase 4: students + student_term_gpa (mock — ต่อ registrar API จริงภายหลัง)
# ---------------------------------------------------------------------------
# NOTE: ข้อมูลนักศึกษาทั้งหมดเป็น mock สำหรับเดโม Dashboard อาจารย์ที่ปรึกษา
#       โครงสร้างออกแบบให้ map กับข้อมูลจาก registrar ได้ตรงๆ ภายหลัง
# fields: (student_id, name, program_code, module, admission_year(พ.ศ.), advisor_name, gpax, credits_earned, status)
# หมายเหตุ: 2 ตัวแรกของรหัส นศ. = ปีที่เข้า (พ.ศ. ย่อ) เช่น 64xxxxxx = เข้าปี 2564
MOCK_STUDENTS = [
    # รุ่นเข้าปี 2564
    ("64070001", "ธนกร ใจดี",        "IT",   "software",             2564, "ผศ.ดร. สมชาย", 3.82, 96,  "กำลังศึกษา"),
    ("64070002", "ปิยะพร แสงเพชร",   "IT",   "game",                 2564, "ผศ.ดร. สมชาย", 3.15, 90,  "กำลังศึกษา"),
    ("64070003", "อนุชา มั่นคง",     "IT",   "network",              2564, "ผศ.ดร. สมชาย", 1.78, 78,  "กำลังศึกษา"),
    ("64070004", "กมลชนก ศรีสุข",    "DSBA", "data_science",         2564, "ผศ.ดร. สมชาย", 3.91, 99,  "กำลังศึกษา"),
    ("64070005", "ณัฐวุฒิ พงษ์ไพร",  "DSBA", "data_engineering",     2564, "ผศ.ดร. สมชาย", 0.87, 42,  "กำลังศึกษา"),
    ("64070006", "สุพรรณี วงศ์ทอง",  "BIT",  None,                   2564, "ผศ.ดร. สมชาย", 2.64, 84,  "กำลังศึกษา"),
    ("64070007", "จิรายุ ตั้งใจ",    "BIT",  None,                   2564, "ผศ.ดร. สมชาย", 3.55, 93,  "กำลังศึกษา"),
    ("64070008", "วรรณพร ทองมา",     "AIT",  None,                   2564, "ผศ.ดร. สมชาย", 1.42, 60,  "ภาคทัณฑ์"),
    ("64070009", "ภาณุพงศ์ เกิดผล",  "AIT",  None,                   2564, "ผศ.ดร. สมชาย", 2.98, 87,  "กำลังศึกษา"),
    ("64070010", "ศิริพร ดวงแก้ว",   "DSBA", "statistical_analysis", 2564, "ผศ.ดร. สมชาย", 3.68, 96,  "กำลังศึกษา"),
    # รุ่นเข้าปี 2565
    ("65070011", "ธีรภัทร คำแก้ว",   "IT",   "software",             2565, "ผศ.ดร. สมชาย", 2.10, 60,  "กำลังศึกษา"),
    ("65070012", "อารยา ชูใจ",       "DSBA", "data_science",         2565, "ผศ.ดร. สมชาย", 0.95, 33,  "ภาคทัณฑ์"),
    ("65070013", "กิตติพงศ์ แซ่ลี้", "AIT",  None,                   2565, "ผศ.ดร. สมชาย", 1.85, 51,  "กำลังศึกษา"),
    ("65070014", "ชนิกานต์ ใจงาม",   "BIT",  None,                   2565, "ผศ.ดร. สมชาย", 3.40, 63,  "กำลังศึกษา"),
    # รุ่นเข้าปี 2566
    ("66070015", "ปุณยวีร์ สุขสันต์", "IT",   "game",                2566, "ผศ.ดร. สมชาย", 3.05, 36,  "กำลังศึกษา"),
    ("66070016", "รวิสรา แก้วมณี",    "AIT",  None,                   2566, "ผศ.ดร. สมชาย", 1.20, 30,  "ภาคทัณฑ์"),
    ("66070017", "ศุภกร ทองดี",      "DSBA", "statistical_analysis", 2566, "ผศ.ดร. สมชาย", 2.80, 36,  "กำลังศึกษา"),
]

# (student_id, year, semester, gpa) — ไว้ทำกราฟ GPA รายเทอม (ปีที่นี่หมายถึงชั้นปี 1..3)
MOCK_TERM_GPA = [
    ("64070001",1,1,3.70),("64070001",1,2,3.85),("64070001",2,1,3.80),("64070001",2,2,3.90),("64070001",3,1,3.86),
    ("64070003",1,1,2.10),("64070003",1,2,1.90),("64070003",2,1,1.65),("64070003",2,2,1.70),("64070003",3,1,1.55),
    ("64070005",1,1,1.20),("64070005",1,2,0.95),("64070005",2,1,0.80),("64070005",2,2,0.75),
    ("64070008",1,1,1.80),("64070008",1,2,1.55),("64070008",2,1,1.30),("64070008",2,2,1.25),
    ("64070004",1,1,3.85),("64070004",1,2,3.92),("64070004",2,1,3.95),("64070004",2,2,3.88),("64070004",3,1,3.94),
    ("64070006",1,1,2.40),("64070006",1,2,2.55),("64070006",2,1,2.70),("64070006",2,2,2.75),("64070006",3,1,2.72),
]


def build_students(cur):
    cur.execute("DROP TABLE IF EXISTS students")
    cur.execute("""
        CREATE TABLE students (
            student_id     TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            program_code   TEXT,       -- IT / DSBA / BIT / AIT
            module         TEXT,       -- สาย/โมดูล (NULL = ไม่มี)
            admission_year INTEGER,    -- ปีที่เข้าเรียน (พ.ศ.) เช่น 2564
            advisor_name   TEXT,
            gpax           REAL,
            credits_earned INTEGER,
            status         TEXT        -- สถานะการลงทะเบียน (mock) — สถานะเสี่ยงคำนวณสดจาก gpax
        )
    """)
    cur.executemany(
        "INSERT INTO students (student_id,name,program_code,module,admission_year,advisor_name,gpax,credits_earned,status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        MOCK_STUDENTS,
    )

    cur.execute("DROP TABLE IF EXISTS student_term_gpa")
    cur.execute("""
        CREATE TABLE student_term_gpa (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL REFERENCES students(student_id),
            year       INTEGER,     -- ชั้นปี
            semester   INTEGER,
            gpa        REAL
        )
    """)
    cur.executemany(
        "INSERT INTO student_term_gpa (student_id,year,semester,gpa) VALUES (?,?,?,?)",
        MOCK_TERM_GPA,
    )
    return len(MOCK_STUDENTS), len(MOCK_TERM_GPA)


# ---------------------------------------------------------------------------
def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(DEFAULT_DB)
    rules_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(DEFAULT_RULES)

    if not db_path.exists():
        sys.exit(f"ไม่พบไฟล์ DB: {db_path} (รัน build_chatbot_db.py ก่อน)")
    if not rules_path.exists():
        sys.exit(f"ไม่พบไฟล์กฎ: {rules_path}")

    rules = json.loads(rules_path.read_text(encoding="utf-8"))

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    n_grade = build_grade_scale(cur, rules)
    n_rules = build_rules(cur, rules)
    n_schol = build_scholarships(cur)
    n_stu, n_term = build_students(cur)

    conn.commit()

    print(f"[Phase 1] grade_scale : {n_grade} เกรด")
    print(f"[Phase 1] rules       : {n_rules} แถว")
    print(f"[Phase 3] scholarships: {n_schol} ทุน (จริง จากเว็บคณะ IT)")
    print(f"[Phase 4] students    : {n_stu} คน, student_term_gpa: {n_term} แถว (mock)")
    print("\n--- ตัวอย่าง grade_scale ---")
    for r in cur.execute("SELECT grade, point, is_gpa FROM grade_scale ORDER BY is_gpa DESC, point DESC"):
        print(f"  {r[0]:3} point={r[1]}  is_gpa={r[2]}")
    print("\n--- rules แยกตามหมวด ---")
    for r in cur.execute("SELECT category, COUNT(*) FROM rules GROUP BY category ORDER BY category"):
        print(f"  {r[0]:28} {r[1]} แถว")

    conn.close()
    print(f"\nDone -> {db_path}")


if __name__ == "__main__":
    main()
