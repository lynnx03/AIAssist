#!/usr/bin/env python3
"""
Extract curriculum data from PDFs via OpenRouter.
Usage: python3 process.py [model_id]
  Default: google/gemini-2.5-pro
  Example: python3 process.py anthropic/claude-opus-4
"""

import base64
import json
import os
import sys
import time
import requests
import pdfplumber
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("OPENROUTER_API_KEY")
MODEL = sys.argv[1] if len(sys.argv) > 1 else "google/gemini-2.5-pro"
API_URL = "https://openrouter.ai/api/v1/chat/completions"

DATA_DIR = "/Users/nms/Desktop/AI_ASSIST GEM/data"
_model_short = MODEL.split("/")[-1]
OUTPUT_DIR = f"/Users/nms/Desktop/AI_ASSIST GEM/output_{_model_short}"
REF_DIR = "/Users/nms/Desktop/AI_ASSIST DS/output/realone"

PROGRAMS = {
    # IT-2 และ BIT-2 เป็น scanned PDF (ข้อบังคับแนบท้าย) = 3-4M tokens แต่มี text จริงแค่ 570 tokens
    # ตัดออกเพื่อประหยัด cost 99% — rules ดึงจาก IT-1/IT-3/BIT-1/BIT-3 แทน
    "IT":   ["IT-1.pdf", "IT-3.pdf", "แผน_IT.pdf"],
    "BIT":  ["BIT-1.pdf", "BIT-3.pdf", "แผน_BIT.pdf"],
    "DSBA": ["DSBA-1.pdf", "DSBA-2-2.pdf", "DSBA-3.pdf", "แผน_DSBA.pdf"],
    "AIT":  ["AIT_1.pdf", "AIT_4.pdf", "แผน_AIT.pdf"],
}

IS_CLAUDE = MODEL.startswith("anthropic/")


# ─── REFERENCE CACHE ──────────────────────────────────────────────────────────

_ref_cache: dict[str, str] = {}

def load_ref(program: str, suffix: str) -> str:
    key = f"{program}{suffix}"
    if key in _ref_cache:
        return _ref_cache[key]
    path = os.path.join(REF_DIR, f"{key}.json")
    try:
        content = open(path).read()
        _ref_cache[key] = content
        return content
    except Exception:
        _ref_cache[key] = ""
        return ""


def preload_refs():
    print("Preloading reference files...")
    for prog in PROGRAMS:
        for suffix in ["", "_rules", "_prereq"]:
            load_ref(prog, suffix)
    print("  ✓ References cached")


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def encode_pdf(path: str) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def call_api(messages: list, max_tokens: int = 32000) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/local",
        "X-Title": "Curriculum Extractor",
    }
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    for attempt in range(3):
        try:
            resp = requests.post(API_URL, headers=headers, json=payload, timeout=360)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if content:
                return content
            raise ValueError("Empty content in response")
        except Exception as e:
            if attempt == 2:
                raise
            print(f"  Retry {attempt + 1}/3 after error: {e}")
            time.sleep(5)


def extract_json(text: str) -> str:
    if not text:
        raise ValueError("Empty response from API")
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        return text[start:end].strip()
    if "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        return text[start:end].strip()
    for i, ch in enumerate(text):
        if ch in "{[":
            return text[i:]
    return text


def extract_pdf_text(path: str) -> str:
    """Extract text from PDF using pdfplumber."""
    text = ""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text += page.extract_text() or ""
    return text


def build_pdf_content(pdf_files: list[str]) -> list[dict]:
    """Build content blocks for PDFs.
    - PDF ที่ extract text ได้ดี (>5K chars) → ส่งเป็น plain text (ถูกกว่า 100x)
    - PDF ที่เป็น scanned image (<5K chars) → ส่งเป็น multimodal
    """
    content = []
    for fname in pdf_files:
        path = os.path.join(DATA_DIR, fname)
        if not os.path.exists(path):
            print(f"  WARNING: {fname} not found, skipping")
            continue
        size_mb = os.path.getsize(path) / 1024 / 1024

        pdf_text = extract_pdf_text(path)

        if len(pdf_text) > 5000:
            approx_tokens = len(pdf_text) // 4
            print(f"  {fname} ({size_mb:.1f} MB) → text mode (~{approx_tokens:,} tokens)")
            content.append({"type": "text", "text": f"[File: {fname}]\n{pdf_text}"})
        else:
            print(f"  {fname} ({size_mb:.1f} MB) → vision mode (scanned PDF)")
            b64 = encode_pdf(path)
            content.append({"type": "text", "text": f"[File: {fname}]"})
            if IS_CLAUDE:
                content.append({
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": b64,
                    }
                })
            else:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:application/pdf;base64,{b64}"}
                })
    return content


# ─── PROMPT TEMPLATES ──────────────────────────────────────────────────────────

COURSES_PROMPT = """You are extracting course data from a Thai university curriculum PDF for the {program} program.

Your task: Extract EVERY SINGLE course listed in this document — do NOT skip any.

Look through ALL sections:
- หมวดวิชาศึกษาทั่วไป (General Education)
- หมวดวิชาเฉพาะ (Specific/Major courses) — กลุ่มวิชาแกน, กลุ่มวิชาเฉพาะด้าน, กลุ่มวิชาบังคับ, กลุ่มวิชาเลือก
- หมวดวิชาเลือกเสรี (Free Electives)
- รายวิชาสหกิจศึกษา (Co-op if any)

For each course found, use the แผนการศึกษา (academic plan) to determine year and semester.

Return ONLY a valid JSON array — no markdown, no explanation:

[
  {{
    "code": "XXXXXXXX",
    "name_th": "ชื่อวิชาภาษาไทย",
    "name_en": "COURSE NAME IN ENGLISH",
    "credits": "X(X-X-X)",
    "category": "หมวดวิชาศึกษาทั่วไป",
    "subcategory": "กลุ่มวิชาพื้นฐาน",
    "type": "บังคับ",
    "year": 1,
    "semester": 1,
    "prerequisites": [],
    "has_prerequisite": false
  }}
]

STRICT RULES:
- Extract EVERY course — the {program} program should have 50-120 courses total
- year: 1, 2, 3, or 4 (from academic plan)
- semester: 1, 2, or 3 (3 = summer)
- type: "บังคับ" or "เลือก"
- category: exactly one of "หมวดวิชาศึกษาทั่วไป", "หมวดวิชาเฉพาะ", "หมวดวิชาเลือกเสรี"
- prerequisites: list of {{"code":"...", "name_th":"...", "name_en":"..."}} — empty list [] if none
- has_prerequisite: true only if prerequisites is non-empty
- DO NOT stop early — go through every page and every section
- Return complete valid JSON array only
"""

PROGRAM_INFO_PROMPT = """From the provided PDF for the {program} program, extract program metadata and structure.

Return ONLY a valid JSON object:

{{
  "program_info": {{
    "name_th": "หลักสูตร...",
    "name_en": "Bachelor of...",
    "degree_th": "วิทยาศาสตรบัณฑิต (...)",
    "degree_en": "Bachelor of Science (...)",
    "major_th": "...",
    "major_en": "...",
    "total_credits": 0,
    "program_type": "ปริญญาตรี",
    "duration_years": 4,
    "curriculum_year": "2565",
    "institution": "สถาบันเทคโนโลยีพระจอมเกล้าเจ้าคุณทหารลาดกระบัง",
    "faculty": "คณะเทคโนโลยีสารสนเทศ"
  }},
  "curriculum_structure": {{
    "general_education": {{
      "credits": 0,
      "subcategories": [{{"name": "...", "credits": 0}}]
    }},
    "specific_courses": {{
      "credits": 0,
      "subcategories": [{{"name": "...", "credits": 0}}]
    }},
    "elective_courses": {{"credits": null, "description": ""}},
    "free_electives": {{"credits": 0, "description": ""}}
  }},
  "academic_plan": [
    {{
      "year": 1,
      "semester": 1,
      "courses": ["code1", "code2"]
    }}
  ],
  "career_opportunities": ["..."],
  "program_outcomes": ["..."],
  "special_tracks": [],
  "program": "{program}"
}}

Return complete valid JSON only.
"""

RULES_PROMPT = """Extract all academic rules and regulations from the provided PDF for the {program} program.

Return ONLY a valid JSON object:

{{
  "graduation_requirements": {{
    "minimum_credits": 0,
    "minimum_gpa": 2.0,
    "time_limit_years": null,
    "max_study_years": null,
    "specific_requirements": ["..."],
    "other_conditions": ["..."]
  }},
  "honors_criteria": {{
    "first_class_gold_medal": {{
      "name": "เกียรตินิยมอันดับหนึ่งเหรียญทอง",
      "gpa_min": 3.75,
      "conditions": ["..."],
      "disqualifications": ["..."]
    }},
    "first_class": {{
      "name": "เกียรตินิยมอันดับหนึ่ง",
      "gpa_min": 3.5,
      "conditions": ["..."],
      "disqualifications": ["..."]
    }},
    "second_class": {{
      "name": "เกียรตินิยมอันดับสอง",
      "gpa_min": 3.25,
      "conditions": ["..."],
      "disqualifications": ["..."]
    }}
  }},
  "dismissal_criteria": {{
    "gpa_based": ["..."],
    "probation_rules": ["..."],
    "time_based": ["..."],
    "enrollment_based": ["..."]
  }},
  "probation_rules": ["..."],
  "grading_system": {{}},
  "registration_rules": ["..."],
  "leave_of_absence_rules": ["..."],
  "withdrawal_rules": ["..."],
  "examination_rules": ["..."],
  "academic_misconduct_rules": ["..."],
  "student_conduct_rules": ["..."],
  "disciplinary_penalties": ["..."],
  "appeal_rules": ["..."],
  "readmission_rules": ["..."],
  "transfer_credit_rules": ["..."],
  "other_regulations": ["..."]
}}

Extract all rules in Thai as they appear. Return complete valid JSON only.

Reference (IT rules):
{ref_rules}
"""

PREREQ_PROMPT = """From the provided PDF for the {program} program, list ALL courses with their prerequisite (วิชาบังคับก่อน / prerequisite) information.

Return ONLY a valid JSON array — every course must appear:

[
  {{
    "code": "XXXXXXXX",
    "name_th": "...",
    "name_en": "...",
    "credits": "X(X-X-X)",
    "prerequisites": [],
    "has_prerequisite": false
  }}
]

- Include ALL courses (expect 50-120 entries for this program)
- prerequisites: list of {{"code":"...", "name_th":"...", "name_en":"..."}}
- has_prerequisite: true only if prerequisites list is non-empty
- Return complete valid JSON array only
"""


# ─── PROCESS ──────────────────────────────────────────────────────────────────

def process_program(program: str, pdf_files: list[str]):
    print(f"\n{'='*60}")
    print(f"Processing: {program}")
    print(f"{'='*60}")

    ref_rules = load_ref(program, "_rules")

    print("Building PDF content...")
    pdf_content = build_pdf_content(pdf_files)

    if not pdf_content:
        print(f"ERROR: No PDFs loaded for {program}")
        return

    # ── 1. Extract courses (most important — separate call for completeness) ───
    main_out_path = os.path.join(OUTPUT_DIR, f"{program}.json")
    courses_out_path = os.path.join(OUTPUT_DIR, f"{program}_courses_raw.json")

    if os.path.exists(main_out_path):
        print(f"\n[1/4] Skipping main (already exists): {main_out_path}")
        courses_data = json.load(open(main_out_path)).get("courses", [])
    else:
        # Step 1a: Extract courses list
        print(f"\n[1a/4] Extracting ALL courses...")
        prompt_courses = COURSES_PROMPT.format(program=program)
        msg_courses = [{"role": "user", "content": pdf_content + [{"type": "text", "text": prompt_courses}]}]

        courses_data = []
        try:
            raw = call_api(msg_courses, max_tokens=16000)
            json_str = extract_json(raw)
            courses_data = json.loads(json_str)
            with open(courses_out_path, "w", encoding="utf-8") as f:
                json.dump(courses_data, f, ensure_ascii=False, indent=2)
            print(f"  ✓ Extracted {len(courses_data)} courses")
        except Exception as e:
            print(f"  ✗ Error extracting courses: {e}")
            raw_path = os.path.join(OUTPUT_DIR, f"{program}_courses_err.txt")
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write(str(locals().get('raw') or str(e)))

        time.sleep(3)

        # Step 1b: Extract program info & merge with courses
        print(f"\n[1b/4] Extracting program info...")
        prompt_info = PROGRAM_INFO_PROMPT.format(program=program)
        msg_info = [{"role": "user", "content": pdf_content + [{"type": "text", "text": prompt_info}]}]

        try:
            raw = call_api(msg_info, max_tokens=16384)
            json_str = extract_json(raw)
            info_data = json.loads(json_str)
            # Merge courses into program info
            info_data["courses"] = courses_data
            with open(main_out_path, "w", encoding="utf-8") as f:
                json.dump(info_data, f, ensure_ascii=False, indent=2)
            print(f"  ✓ Saved {main_out_path} ({len(courses_data)} courses)")
        except Exception as e:
            print(f"  ✗ Error extracting program info: {e}")
            raw_path = os.path.join(OUTPUT_DIR, f"{program}_info_err.txt")
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write(str(locals().get('raw') or str(e)))

        time.sleep(3)

    # ── 2. Extract rules ───────────────────────────────────────────
    rules_out_path = os.path.join(OUTPUT_DIR, f"{program}_rules.json")
    if os.path.exists(rules_out_path):
        print(f"\n[2/4] Skipping rules (already exists)")
    else:
        print(f"\n[2/4] Extracting rules and regulations...")
        ref_rules_snippet = ref_rules[:3000] if ref_rules else ""
        prompt_rules = RULES_PROMPT.format(program=program, ref_rules=ref_rules_snippet)
        msg_rules = [{"role": "user", "content": pdf_content + [{"type": "text", "text": prompt_rules}]}]

        try:
            raw = call_api(msg_rules, max_tokens=32768)
            json_str = extract_json(raw)
            data = json.loads(json_str)
            with open(rules_out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"  ✓ Saved {rules_out_path}")
        except Exception as e:
            print(f"  ✗ Error extracting rules: {e}")
            raw_path = os.path.join(OUTPUT_DIR, f"{program}_rules_err.txt")
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write(str(locals().get('raw') or str(e)))

        time.sleep(3)

    # ── 3. Extract prerequisites ───────────────────────────────────
    prereq_out_path = os.path.join(OUTPUT_DIR, f"{program}_prereq.json")
    if os.path.exists(prereq_out_path):
        print(f"\n[3/4] Skipping prereq (already exists)")
    else:
        print(f"\n[3/4] Extracting prerequisites...")
        prompt_prereq = PREREQ_PROMPT.format(program=program)
        msg_prereq = [{"role": "user", "content": pdf_content + [{"type": "text", "text": prompt_prereq}]}]

        try:
            raw = call_api(msg_prereq, max_tokens=32768)
            json_str = extract_json(raw)
            data = json.loads(json_str)
            out_path = prereq_out_path
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            n_prereq = sum(1 for c in data if c.get("has_prerequisite"))
            print(f"  ✓ Saved {out_path} ({len(data)} courses, {n_prereq} have prerequisites)")
        except Exception as e:
            print(f"  ✗ Error extracting prerequisites: {e}")
            raw_path = os.path.join(OUTPUT_DIR, f"{program}_prereq_err.txt")
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write(str(locals().get('raw') or str(e)))


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"Model: {MODEL}")
    print(f"Output: {OUTPUT_DIR}")
    print(f"Programs: {list(PROGRAMS.keys())}")

    preload_refs()

    for program, pdf_files in PROGRAMS.items():
        process_program(program, pdf_files)
        print(f"\n✓ Done: {program}")
        time.sleep(5)

    print("\n" + "=" * 60)
    print("All programs processed!")
    print(f"Output files in: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
