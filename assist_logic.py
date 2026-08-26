"""
assist_logic.py
---------------
ตรรกะที่ใช้ร่วมกันระหว่างหน้า "คำนวณเกรด" (Phase 2) และ "Dashboard อาจารย์" (Phase 4):

  - อ่าน "ค่าเกรด" จากตาราง grade_scale (single source of truth — Phase 1)
  - อ่าน "เกณฑ์ GPA" (รีไทร์/ภาคทัณฑ์/เกียรตินิยม) จากตาราง rules (ไม่ hardcode ตัวเลขมั่ว)
  - parse transcript (ยืดหยุ่น) -> รายวิชา
  - คำนวณ GPA รายเทอม + สะสม (ตัดวิชา is_gpa=0 เช่น S/U/T/I และวิชา W ออก)
  - จัดสถานะจาก GPAX ตามเกณฑ์

ทั้งหมดใช้ DB read-only เท่านั้น
"""

import re
import sqlite3

# เลขไทย -> อารบิก (ข้อความกฎในตาราง rules เก็บตัวเลขเป็นเลขไทย เช่น "ต่ำกว่า ๑.๐๐")
_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

# โทเคนเกรดที่รู้จัก (เรียง B+ ก่อน B เพื่อให้ regex จับ '+' ได้ก่อน)
_GRADE_TOKENS = ["A", "B+", "B", "C+", "C", "D+", "D", "F", "S", "U", "T", "I", "W", "WD", "AU"]
_GRADE_RE = re.compile(r"(?<![A-Za-z+])(" + "|".join(re.escape(g) for g in _GRADE_TOKENS) + r")(?![A-Za-z+])")
_CODE_RE = re.compile(r"\b(\d{8})\b")                 # รหัสวิชา 8 หลัก
# ตัวจุดชนวนว่าบรรทัดนี้ "น่าจะเป็นหัวข้อภาคเรียน"
_TERM_HINT_RE = re.compile(r"semester|ภาค|\(\s*\d\s*/\s*\d{2,4}\s*\)", re.IGNORECASE)
# บรรทัดต่อชื่อวิชา (title ที่ตัดบรรทัด) เช่น 'SKILLS', 'ENGLISH 1', 'TECHNOLOGY 2'
# = อักษรลาติน/ตัวเลข/วงเล็บ ล้วนๆ ไม่มี ':' (กัน 'GPS :', 'Total ...:')
_TITLE_FRAG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 &/().,\-]*$")


def _parse_term(line: str):
    """คืน (semester, year) จากบรรทัดหัวข้อภาคเรียน หรือ None ถ้าไม่ใช่
    รองรับหลายรูปแบบ เช่น
      '1st Semester, Year, 2023-2024 (1/2566)'  -> (1, 2566)
      'ภาคการศึกษาที่ 1/2564'                     -> (1, 2564)
      '2nd Semester ... 2024'                    -> (2, 2024)"""
    if not _TERM_HINT_RE.search(line):
        return None
    # แบบ N/YYYY (มักอยู่ในวงเล็บ เช่น (1/2566)) — แม่นสุด เอาก่อน
    m = re.search(r"(\d)\s*/\s*(\d{2,4})", line)
    if m:
        return int(m.group(1)), int(m.group(2))
    sm = re.search(r"(\d)\s*(?:st|nd|rd|th)?\s*semester", line, re.IGNORECASE)
    ym = re.search(r"(\d{4})", line)
    if sm or ym:
        return (int(sm.group(1)) if sm else None,
                int(ym.group(1)) if ym else None)
    return None


def _thai_to_arabic(s: str) -> str:
    return (s or "").translate(_THAI_DIGITS)


# ---------------------------------------------------------------------------
# อ่านค่าเกรด + เกณฑ์จาก DB
# ---------------------------------------------------------------------------
def build_domain_digest(db_path: str) -> str:
    """สรุป 'ข้อเท็จจริงจริง' จาก DB แบบกระชับ ไว้ยัดเข้า prompt โหมดสนทนา
    เพื่อให้ LLM ตอบโดยยึดข้อมูลของเรา (กัน hallucinate + กันนอกเรื่อง)
    เนื้อหาสั้น สร้างครั้งเดียวแล้ว cache ได้"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        progs = con.execute(
            "SELECT program_code, name_th, total_credits FROM programs ORDER BY program_code"
        ).fetchall()
        mod_rows = con.execute(
            "SELECT p.program_code, GROUP_CONCAT(DISTINCT apc.module) m "
            "FROM academic_plan_courses apc JOIN programs p ON p.program_id = apc.program_id "
            "WHERE apc.module IS NOT NULL GROUP BY p.program_code"
        ).fetchall()
        rule_cats = [r[0] for r in con.execute("SELECT DISTINCT category FROM rules ORDER BY category")]
        schols = [r[0] for r in con.execute(
            "SELECT name FROM scholarships ORDER BY gpa_requirement IS NULL, gpa_requirement DESC")]
        grades = con.execute(
            "SELECT grade, point FROM grade_scale WHERE is_gpa=1 ORDER BY point DESC").fetchall()
    except sqlite3.Error:
        con.close()
        return ""
    con.close()

    lines = ["หลักสูตรทั้งหมด (รหัส = ชื่อ, หน่วยกิตจบ):"]
    for p in progs:
        lines.append(f"  - {p['program_code']} = {p['name_th']} ({p['total_credits']} หน่วยกิต)")
    modmap = {r["program_code"]: r["m"] for r in mod_rows}
    if modmap:
        lines.append("โมดูล/สายเฉพาะทาง (เฉพาะบางหลักสูตร):")
        for code, m in modmap.items():
            lines.append(f"  - {code}: {m}")
    if grades:
        lines.append("ค่าเกรดที่คิด GPA: " + ", ".join(f"{g['grade']}={g['point']}" for g in grades)
                     + "  (S/U/T/I ไม่คิด GPA)")
    if rule_cats:
        lines.append("หมวดกฎ/ระเบียบที่มีในระบบ: " + ", ".join(rule_cats))
    if schols:
        lines.append("ทุนการศึกษาที่มีในระบบ: " + ", ".join(schols))
    return "\n".join(lines)


def load_grade_points(db_path: str) -> dict:
    """คืน dict: grade -> {'point': float|None, 'is_gpa': bool} จากตาราง grade_scale"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT grade, point, is_gpa FROM grade_scale").fetchall()
    finally:
        con.close()
    return {g: {"point": p, "is_gpa": bool(i)} for g, p, i in rows}


def _first_number(text: str):
    """ดึงเลขทศนิยมตัวแรกจากข้อความ (แปลงเลขไทยก่อน) เช่น 'ต่ำกว่า ๑.๐๐' -> 1.0"""
    m = re.search(r"(\d+(?:\.\d+)?)", _thai_to_arabic(text))
    return float(m.group(1)) if m else None


def load_thresholds(db_path: str) -> dict:
    """อ่านเกณฑ์ GPA จากตาราง rules (ไม่ hardcode):
       - retire   : GPAX ต่ำกว่าเท่านี้ = เสี่ยงรีไทร์  (dismissal_criteria.gpa_based)
       - probation: GPAX ต่ำกว่าเท่านี้ = ภาคทัณฑ์/เฝ้าระวัง (probation_rules)
       - honors   : GPAX ไม่ต่ำกว่าเท่านี้ = เข้าเกณฑ์เกียรตินิยม (honors_criteria first_class.gpa_min)
    ถ้าหาไม่เจอในตาราง จะ fallback เป็นค่ามาตรฐานของ KMITL (พร้อม flag source)"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        def q(sql, args=()):
            r = con.execute(sql, args).fetchone()
            return r[0] if r else None

        # รีไทร์: "...เฉลี่ยสะสมต่ำกว่า ๑.๐๐"
        retire_txt = q(
            "SELECT text FROM rules WHERE category='dismissal_criteria' "
            "AND text LIKE '%เฉลี่ยสะสม%ต่ำกว่า%' LIMIT 1"
        )
        # ภาคทัณฑ์: "...เฉลี่ยสะสมต่ำกว่า ๒.๐๐ ต้องถูกภาคทัณฑ์"
        prob_txt = q(
            "SELECT text FROM rules WHERE category='probation_rules' "
            "AND text LIKE '%ต่ำกว่า%' LIMIT 1"
        )
        # เกียรตินิยมอันดับหนึ่ง gpa_min (เก็บเป็นตัวเลขสะอาดในตาราง)
        honors_txt = q(
            "SELECT text FROM rules WHERE category='honors_criteria' "
            "AND subcategory LIKE 'first_class.gpa_min%' LIMIT 1"
        )
    finally:
        con.close()

    retire = _first_number(retire_txt) if retire_txt else None
    probation = _first_number(prob_txt) if prob_txt else None
    honors = _first_number(honors_txt) if honors_txt else None

    return {
        "retire": retire if retire is not None else 1.00,
        "probation": probation if probation is not None else 2.00,
        "honors": honors if honors is not None else 3.50,
        "_source": {
            "retire": "rules" if retire is not None else "fallback",
            "probation": "rules" if probation is not None else "fallback",
            "honors": "rules" if honors is not None else "fallback",
        },
    }


def classify_status(gpax, th: dict) -> dict:
    """คืนสถานะจาก GPAX ตามเกณฑ์ (th มาจาก load_thresholds)"""
    if gpax is None:
        return {"level": "unknown", "label": "ไม่มีข้อมูล", "color": "gray", "emoji": "⚪"}
    if gpax < th["retire"]:
        return {"level": "risk", "label": "เสี่ยงรีไทร์", "color": "red", "emoji": "🔴"}
    if gpax < th["probation"]:
        return {"level": "watch", "label": "เฝ้าระวัง", "color": "amber", "emoji": "🟠"}
    if gpax >= th["honors"]:
        return {"level": "honors", "label": "เข้าเกณฑ์เกียรตินิยม", "color": "green", "emoji": "🟢"}
    return {"level": "normal", "label": "ปกติ", "color": "gray", "emoji": "⚪"}


# ---------------------------------------------------------------------------
# Transcript parser (ปรับจูนกับรูปแบบ transcript จริงของ KMITL ที่ผู้ใช้คัดลอกมาวาง)
# ---------------------------------------------------------------------------
def parse_transcript(text: str) -> list:
    """แยกรายวิชาจากข้อความ transcript ที่ paste มา
    คืน list ของ dict: {code, name, credits, grade, year, semester}

    ปรับจูน + ทดสอบแล้วกับ transcript จริงของ KMITL (คัดลอกข้อความมาวาง) รูปแบบ:
        <รหัส 8 หลัก>  <ชื่อวิชา>  <หน่วยกิต>  <เกรด>
    หัวข้อภาคเรียนแบบ '1st Semester, Year, 2023-2024 (1/2566)' -> set เทอม (1/2566) ให้วิชาถัดๆ ไป

    heuristic ต่อบรรทัด:
      - รหัสวิชา = เลข 8 หลักตัวแรก
      - เกรด = โทเคนเกรดตัวสุดท้ายในบรรทัด (ถ้าไม่มี = วิชากำลังเรียน เก็บ grade=None ไว้แสดง แต่ไม่คิด GPA)
      - หน่วยกิต = เลขตัวสุดท้ายก่อนเกรด
      - ชื่อวิชา = ข้อความระหว่างรหัสกับหน่วยกิต + ต่อบรรทัดถัดไปถ้าชื่อถูกตัดขึ้นบรรทัดใหม่
      - บรรทัดสรุป (GPS/GPA/Total/Cumulative) จะถูกข้าม (ไม่มีรหัสวิชา)
    หมายเหตุ: per-term ที่ได้ = GPS รายภาค, overall = GPAX สะสม (ตรงกับตัวเลขในทรานสคริปต์จริง)
    """
    courses = []
    cur_year, cur_sem = None, None
    cont_idx = None   # index ของวิชาล่าสุด (ไว้ต่อชื่อวิชาที่ตัดบรรทัด)

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue

        # หา "ทุก" รหัสวิชาในบรรทัด (transcript แบบ 2 คอลัมน์ = 1 บรรทัดมีได้หลายวิชา)
        codes = list(_CODE_RE.finditer(line))

        # 1) บรรทัดที่ไม่มีรหัสวิชา -> หัวข้อภาคเรียน / ชื่อวิชาต่อ / บรรทัดสรุป
        if not codes:
            term = _parse_term(line)
            if term is not None:
                cur_sem, cur_year = term
                cont_idx = None
                continue
            # ต่อชื่อวิชาที่ถูกตัดบรรทัด (เช่น 'SKILLS', 'ENGLISH 1') เข้าวิชาก่อนหน้า
            if cont_idx is not None and ":" not in line and _TITLE_FRAG_RE.match(line):
                courses[cont_idx]["name"] = (courses[cont_idx]["name"] + " " + line).strip()
            else:
                cont_idx = None   # บรรทัดสรุป (GPS/Total/…) -> หยุดต่อชื่อ
            continue

        # ข้อความก่อนรหัสแรก อาจเป็นหัวข้อภาคเรียน (คอลัมน์ซ้าย) -> เซ็ตเทอมให้วิชาที่ตามมา
        prefix = line[:codes[0].start()].strip()
        if prefix:
            term = _parse_term(prefix)
            if term is not None:
                cur_sem, cur_year = term

        # 2) แยกแต่ละวิชาในบรรทัด: ช่วง [รหัส_i .. รหัส_i+1)
        for i, cm in enumerate(codes):
            seg_start = cm.end()
            seg_end = codes[i + 1].start() if i + 1 < len(codes) else len(line)
            segment = line[seg_start:seg_end]

            grades = list(_GRADE_RE.finditer(segment))
            gm = grades[-1] if grades else None
            grade = gm.group(1) if gm else None   # None = กำลังเรียน (ยังไม่มีเกรด)

            before_grade = segment[:gm.start()] if gm else segment
            num_matches = re.findall(r"(\d+(?:\.\d+)?)", before_grade)
            credits = None
            if num_matches:
                try:
                    credits = float(num_matches[-1])
                    if credits.is_integer():
                        credits = int(credits)
                except ValueError:
                    credits = None

            name = before_grade
            if num_matches:
                name = before_grade[:before_grade.rfind(num_matches[-1])]
            name = name.strip(" \t.-|")

            courses.append({
                "code": cm.group(1),
                "name": name,
                "credits": credits,
                "grade": grade,
                "year": cur_year,
                "semester": cur_sem,
            })
        # ต่อชื่อวิชาที่ตัดบรรทัดได้เฉพาะกรณีบรรทัดนี้มีวิชาเดียว (กันต่อผิดคอลัมน์)
        cont_idx = len(courses) - 1 if len(codes) == 1 else None
    return courses


def is_multicolumn(text: str) -> bool:
    """True ถ้า transcript ดูเป็นแบบ 2 คอลัมน์ (มีบรรทัดที่มีรหัสวิชา >= 2 รหัส)
    ใช้เตือนผู้ใช้ว่า GPA รายเทอมอาจคลาดเคลื่อน (แต่ GPAX รวมยังถูก)"""
    for raw in (text or "").splitlines():
        if len(_CODE_RE.findall(raw)) >= 2:
            return True
    return False


# ---------------------------------------------------------------------------
# คำนวณ GPA
# ---------------------------------------------------------------------------
def _term_key(c):
    y, s = c.get("year"), c.get("semester")
    if y is None and s is None:
        return None
    return (y, s)


def compute_gpa(courses: list, grade_map: dict) -> dict:
    """คำนวณ GPA จากรายวิชา โดยใช้ grade_map จาก grade_scale (Phase 1)
       GPA = Σ(หน่วยกิต × point) / Σ(หน่วยกิต)  เฉพาะวิชา is_gpa=1 และมีหน่วยกิต
       วิชา S/U/T/I/W และวิชาไม่มีหน่วยกิต -> ตัดออก (แต่รายงานว่าตัดออกไป)
    คืน: overall {gpa, gpa_credits, total_credits, counted, excluded}, terms[]"""
    def blank():
        return {"points": 0.0, "gpa_credits": 0, "counted": [], "excluded": []}

    overall = blank()
    terms = {}   # term_key -> accumulator
    term_order = []

    for c in courses:
        grade = (c.get("grade") or "").strip()
        gi = grade_map.get(grade)
        credits = c.get("credits")
        tk = _term_key(c)
        if tk is not None and tk not in terms:
            terms[tk] = blank()
            term_order.append(tk)

        # เหตุผลที่ตัดออก
        if not grade:
            reason = "ยังไม่มีเกรด (กำลังเรียน)"
        elif gi is None:
            reason = f"เกรด '{grade}' ไม่รู้จัก"
        elif not gi["is_gpa"]:
            reason = f"เกรด {grade} ไม่คิด GPA"
        elif credits is None or credits <= 0:
            reason = "ไม่มีหน่วยกิต"
        else:
            reason = None

        if reason is not None:
            overall["excluded"].append({**c, "reason": reason})
            if tk is not None:
                terms[tk]["excluded"].append({**c, "reason": reason})
            continue

        pts = credits * gi["point"]
        overall["points"] += pts
        overall["gpa_credits"] += credits
        overall["counted"].append(c)
        if tk is not None:
            terms[tk]["points"] += pts
            terms[tk]["gpa_credits"] += credits
            terms[tk]["counted"].append(c)

    def finalize(acc):
        gc = acc["gpa_credits"]
        gpa = round(acc["points"] / gc, 2) if gc else None
        return {
            "gpa": gpa,
            "gpa_credits": gc,
            "counted": len(acc["counted"]),
            "excluded": acc["excluded"],
        }

    term_list = []
    for tk in term_order:
        f = finalize(terms[tk])
        f["year"], f["semester"] = tk
        term_list.append(f)

    ov = finalize(overall)
    ov["total_courses"] = len(courses)
    return {"overall": ov, "terms": term_list}
