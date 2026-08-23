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
# หัวข้อภาคเรียน เช่น "ภาคการศึกษาที่ 1/2564", "ปีการศึกษา 2564 ภาค 2", "Semester 1/2021"
_TERM_RE = re.compile(
    r"(?:ภาค(?:การศึกษา)?|semester|sem|term)[^\d]{0,6}(\d)\s*[/\-]\s*(\d{2,4})",
    re.IGNORECASE,
)


def _thai_to_arabic(s: str) -> str:
    return (s or "").translate(_THAI_DIGITS)


# ---------------------------------------------------------------------------
# อ่านค่าเกรด + เกณฑ์จาก DB
# ---------------------------------------------------------------------------
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
# Transcript parser (ยืดหยุ่น — ยังไม่มีตัวอย่างจริง ต้อง refine regex ภายหลัง)
# ---------------------------------------------------------------------------
def parse_transcript(text: str) -> list:
    """แยกรายวิชาจากข้อความ transcript ที่ paste มา
    คืน list ของ dict: {code, name, credits, grade, year, semester}

    heuristic ต่อบรรทัด:
      - รหัสวิชา = เลข 8 หลักตัวแรก
      - เกรด = โทเคนเกรดตัวสุดท้ายในบรรทัด
      - หน่วยกิต = เลข 1-2 หลักที่อยู่ก่อนเกรด (หรือเลขเดี่ยวหลังรหัส)
      - ชื่อวิชา = ข้อความระหว่างรหัสกับหน่วยกิต/เกรด
    หัวข้อภาคเรียน (เช่น 'ภาคการศึกษาที่ 1/2564') จะ set เทอมให้วิชาถัดๆ ไป

    ⚠️ ยังไม่ได้ทดสอบกับ transcript จริงของ KMITL — เมื่อมีตัวอย่างจริงให้ปรับ regex ด้านบน
    """
    courses = []
    cur_year, cur_sem = None, None

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue

        # ตรวจหัวข้อภาคเรียนก่อน (บรรทัดนี้อาจไม่มีรหัสวิชา)
        tm = _TERM_RE.search(line)
        if tm and not _CODE_RE.search(line):
            cur_sem = int(tm.group(1))
            yr = tm.group(2)
            cur_year = int(yr) if len(yr) <= 2 else int(yr)  # เก็บปีตามที่พบ (พ.ศ./ค.ศ.)
            continue

        cm = _CODE_RE.search(line)
        if not cm:
            continue
        code = cm.group(1)
        after = line[cm.end():]

        # หาเกรดตัวสุดท้ายในส่วนหลังรหัส
        grades = list(_GRADE_RE.finditer(after))
        if not grades:
            continue
        gm = grades[-1]
        grade = gm.group(1)

        # หน่วยกิต: เลข 1-2 หลักที่อยู่ก่อนเกรด (ใกล้เกรดที่สุด)
        before_grade = after[:gm.start()]
        num_matches = re.findall(r"(\d+(?:\.\d+)?)", before_grade)
        credits = None
        if num_matches:
            try:
                credits = float(num_matches[-1])
                if credits.is_integer():
                    credits = int(credits)
            except ValueError:
                credits = None

        # ชื่อวิชา = ข้อความก่อนหน่วยกิต/เกรด (ตัดตัวเลขท้ายออก)
        name = before_grade
        if num_matches:
            name = before_grade[:before_grade.rfind(num_matches[-1])]
        name = name.strip(" \t.-|")

        courses.append({
            "code": code,
            "name": name,
            "credits": credits,
            "grade": grade,
            "year": cur_year,
            "semester": cur_sem,
        })
    return courses


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
        if gi is None:
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
