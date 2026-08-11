"""
Extract ALL rules from curriculum documents using OCR + DeepSeek.
Comprehensive extraction: every regulation, criterion, and rule in the documents.
"""

import os, json, re
import pdfplumber
import pytesseract
from pdf2image import convert_from_path
from openai import OpenAI
from json_repair import repair_json

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DATA_DIR = "/Users/nms/Desktop/AI_ASSIST DS/data"
OUTPUT_DIR = "/Users/nms/Desktop/AI_ASSIST DS/output"
MODEL = "deepseek-chat"
OCR_DPI = 250

RULE_FILES = {
    "IT":   ["IT-1.pdf", "IT-2.pdf"],
    "BIT":  ["BIT-1.pdf", "BIT-2.pdf"],
    "DSBA": ["DSBA-1.pdf", "DSBA-2-1.pdf"],
    "AIT":  ["AIT_1.pdf"],
}

SYSTEM_PROMPT = """คุณเป็นผู้เชี่ยวชาญด้านกฎระเบียบมหาวิทยาลัยไทย
ให้ extract กฎระเบียบทุกข้อจากเอกสาร และส่งคืนเป็น JSON ที่สมบูรณ์ถูกต้อง
ตอบเฉพาะ JSON เท่านั้น ไม่มี markdown ไม่มีข้อความอื่น"""

EXTRACTION_PROMPT = """จากข้อความด้านล่าง (อาจมาจาก OCR — ข้อผิดพลาดเล็กน้อยเป็นเรื่องปกติ)
ให้ extract กฎระเบียบ เกณฑ์ และข้อบังคับทุกอย่างที่พบ

ส่งคืนเป็น JSON ตาม schema นี้ (ใส่ข้อมูลทุกอย่างที่มี ถ้าไม่มีให้ใส่ [] หรือ null):
{{
  "graduation_requirements": {{
    "minimum_credits": null,
    "minimum_gpa": null,
    "time_limit_years": null,
    "max_study_years": null,
    "specific_requirements": [],
    "other_conditions": []
  }},
  "honors_criteria": {{
    "first_class_gold_medal": {{
      "name": "เกียรตินิยมอันดับหนึ่งเหรียญทอง",
      "gpa_min": null,
      "conditions": [],
      "disqualifications": []
    }},
    "first_class": {{
      "name": "เกียรตินิยมอันดับหนึ่ง",
      "gpa_min": null,
      "conditions": [],
      "disqualifications": []
    }},
    "second_class": {{
      "name": "เกียรตินิยมอันดับสอง",
      "gpa_min": null,
      "conditions": [],
      "disqualifications": []
    }}
  }},
  "dismissal_criteria": {{
    "gpa_based": [],
    "probation_rules": [],
    "time_based": [],
    "enrollment_based": [],
    "behavioral": [],
    "other": []
  }},
  "probation_rules": [],
  "grading_system": {{
    "grades": [],
    "grade_points": {{}},
    "passing_grade": null,
    "special_grades": [],
    "gpa_types": []
  }},
  "registration_rules": {{
    "credits_per_semester_min": null,
    "credits_per_semester_max": null,
    "rules": []
  }},
  "leave_of_absence_rules": [],
  "withdrawal_rules": [],
  "examination_rules": [],
  "academic_misconduct_rules": [],
  "student_conduct_rules": [],
  "disciplinary_penalties": {{
    "minor": [],
    "major": []
  }},
  "appeal_rules": [],
  "readmission_rules": [],
  "transfer_credit_rules": [],
  "other_regulations": []
}}

ข้อความที่ต้อง extract:
{text}"""


def ocr_page(pdf_path: str, page_index: int) -> str:
    images = convert_from_path(pdf_path, first_page=page_index+1, last_page=page_index+1, dpi=OCR_DPI)
    if not images:
        return ""
    return pytesseract.image_to_string(images[0], lang="tha+eng", config="--psm 3").strip()


def extract_full_text(pdf_path: str) -> str:
    """Extract text — native where possible, OCR for scanned pages."""
    parts = []
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            native = page.extract_text() or ""
            if len(native) > 150:
                parts.append(native)
            else:
                print(f"      OCR p{i+1}/{n}...")
                t = ocr_page(pdf_path, i)
                if t:
                    parts.append(t)
    return "\n\n".join(parts)


def call_deepseek(client: OpenAI, system: str, user: str) -> str:
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.1,
        max_tokens=8192,
    )
    return r.choices[0].message.content.strip()


def parse_json(text: str) -> dict:
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            result = repair_json(text, return_objects=True)
            if isinstance(result, dict) and result:
                print("    [INFO] JSON repaired")
                return result
        except Exception:
            pass
    return {}


def chunk_text(text: str, max_chars: int = 50_000) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    paras = text.split("\n")
    chunks, cur, cur_len = [], [], 0
    for p in paras:
        n = len(p) + 1
        if cur_len + n > max_chars and cur:
            chunks.append("\n".join(cur))
            cur, cur_len = [p], n
        else:
            cur.append(p)
            cur_len += n
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def merge_str_lists(*lists) -> list:
    seen, result = set(), []
    for lst in lists:
        for item in (lst or []):
            s = str(item).strip()
            if s and s not in seen:
                seen.add(s)
                result.append(item)
    return result


def pick(*values):
    for v in values:
        if v not in (None, 0, 0.0, "", [], {}):
            return v
    return None


def deep_merge(dicts: list[dict]) -> dict:
    """Merge list of rule dicts — union lists, pick first non-null scalars."""
    dicts = [d for d in dicts if d and isinstance(d, dict)]
    if not dicts:
        return {}
    if len(dicts) == 1:
        return dicts[0]

    def get_list(key, *sub):
        result = []
        for d in dicts:
            val = d
            for k in [key] + list(sub):
                val = val.get(k, {}) if isinstance(val, dict) else {}
            if isinstance(val, list):
                result.extend(val)
        return merge_str_lists(result)

    def get_val(key, *sub):
        for d in dicts:
            val = d
            for k in [key] + list(sub):
                val = val.get(k) if isinstance(val, dict) else None
            if val not in (None, 0, 0.0, "", [], {}):
                return val
        return None

    def get_dict(key, *sub):
        for d in dicts:
            val = d
            for k in [key] + list(sub):
                val = val.get(k, {}) if isinstance(val, dict) else {}
            if val and isinstance(val, dict):
                return val
        return {}

    return {
        "graduation_requirements": {
            "minimum_credits": get_val("graduation_requirements", "minimum_credits"),
            "minimum_gpa": get_val("graduation_requirements", "minimum_gpa"),
            "time_limit_years": get_val("graduation_requirements", "time_limit_years"),
            "max_study_years": get_val("graduation_requirements", "max_study_years"),
            "specific_requirements": get_list("graduation_requirements", "specific_requirements"),
            "other_conditions": get_list("graduation_requirements", "other_conditions"),
        },
        "honors_criteria": {
            "first_class_gold_medal": {
                "name": "เกียรตินิยมอันดับหนึ่งเหรียญทอง",
                "gpa_min": get_val("honors_criteria", "first_class_gold_medal", "gpa_min"),
                "conditions": get_list("honors_criteria", "first_class_gold_medal", "conditions"),
                "disqualifications": get_list("honors_criteria", "first_class_gold_medal", "disqualifications"),
            },
            "first_class": {
                "name": "เกียรตินิยมอันดับหนึ่ง",
                "gpa_min": get_val("honors_criteria", "first_class", "gpa_min"),
                "conditions": get_list("honors_criteria", "first_class", "conditions"),
                "disqualifications": get_list("honors_criteria", "first_class", "disqualifications"),
            },
            "second_class": {
                "name": "เกียรตินิยมอันดับสอง",
                "gpa_min": get_val("honors_criteria", "second_class", "gpa_min"),
                "conditions": get_list("honors_criteria", "second_class", "conditions"),
                "disqualifications": get_list("honors_criteria", "second_class", "disqualifications"),
            },
        },
        "dismissal_criteria": {
            "gpa_based": get_list("dismissal_criteria", "gpa_based"),
            "probation_rules": get_list("dismissal_criteria", "probation_rules"),
            "time_based": get_list("dismissal_criteria", "time_based"),
            "enrollment_based": get_list("dismissal_criteria", "enrollment_based"),
            "behavioral": get_list("dismissal_criteria", "behavioral"),
            "other": get_list("dismissal_criteria", "other"),
        },
        "probation_rules": get_list("probation_rules"),
        "grading_system": {
            "grades": get_list("grading_system", "grades"),
            "grade_points": get_dict("grading_system", "grade_points"),
            "passing_grade": get_val("grading_system", "passing_grade"),
            "special_grades": get_list("grading_system", "special_grades"),
            "gpa_types": get_list("grading_system", "gpa_types"),
        },
        "registration_rules": {
            "credits_per_semester_min": get_val("registration_rules", "credits_per_semester_min"),
            "credits_per_semester_max": get_val("registration_rules", "credits_per_semester_max"),
            "rules": get_list("registration_rules", "rules"),
        },
        "leave_of_absence_rules": get_list("leave_of_absence_rules"),
        "withdrawal_rules": get_list("withdrawal_rules"),
        "examination_rules": get_list("examination_rules"),
        "academic_misconduct_rules": get_list("academic_misconduct_rules"),
        "student_conduct_rules": get_list("student_conduct_rules"),
        "disciplinary_penalties": {
            "minor": get_list("disciplinary_penalties", "minor"),
            "major": get_list("disciplinary_penalties", "major"),
        },
        "appeal_rules": get_list("appeal_rules"),
        "readmission_rules": get_list("readmission_rules"),
        "transfer_credit_rules": get_list("transfer_credit_rules"),
        "other_regulations": get_list("other_regulations"),
    }


def process_program(client: OpenAI, program: str, pdf_files: list[str]) -> dict:
    print(f"\n{'='*60}\n  {program}\n{'='*60}")

    cache = os.path.join(OUTPUT_DIR, f"_rules_cache_{program}.json")
    if os.path.exists(cache):
        with open(cache) as f:
            d = json.load(f)
        if d and len(str(d)) > 500:
            print(f"  → Using cache")
            return d

    all_results = []
    for pdf_file in pdf_files:
        path = os.path.join(DATA_DIR, pdf_file)
        if not os.path.exists(path):
            continue
        print(f"\n  [{pdf_file}]")
        text = extract_full_text(path)
        print(f"  → {len(text):,} chars")
        if not text.strip():
            continue

        for i, chunk in enumerate(chunk_text(text)):
            print(f"  → Chunk {i+1} → DeepSeek...")
            result = parse_json(call_deepseek(client, SYSTEM_PROMPT,
                                              EXTRACTION_PROMPT.format(text=chunk)))
            if result:
                all_results.append(result)

    merged = deep_merge(all_results)
    if merged:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged


def update_main_json(program: str, rules: dict):
    path = os.path.join(OUTPUT_DIR, f"{program}.json")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        main = json.load(f)
    main.update({k: v for k, v in rules.items()
                 if k not in ("program", "_source")})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(main, f, ensure_ascii=False, indent=2)
    print(f"  → Merged into {program}.json")


def main():
    if not DEEPSEEK_API_KEY:
        print("ERROR: set DEEPSEEK_API_KEY")
        return
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    for program, files in RULE_FILES.items():
        rules = process_program(client, program, files)
        out = os.path.join(OUTPUT_DIR, f"{program}_rules.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
        print(f"\n  Saved → {out}")
        update_main_json(program, rules)

        # Quick summary
        hc = rules.get("honors_criteria", {})
        print(f"  Gold medal GPA: {hc.get('first_class_gold_medal', {}).get('gpa_min')}")
        print(f"  1st class GPA:  {hc.get('first_class', {}).get('gpa_min')}")
        print(f"  2nd class GPA:  {hc.get('second_class', {}).get('gpa_min')}")
        dc = rules.get("dismissal_criteria", {})
        print(f"  Dismissal rules: {sum(len(v) for v in dc.values() if isinstance(v, list))} items")

    print(f"\n{'='*60}\nDONE")


if __name__ == "__main__":
    main()
