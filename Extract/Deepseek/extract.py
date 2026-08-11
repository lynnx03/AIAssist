"""
Extract curriculum data from PDFs using DeepSeek API.
Outputs JSON files per program: IT, BIT, DSBA, AIT.
- Uses json_repair for truncated responses
- Merges results in Python (no DeepSeek merge step)
- Caches per-file results to avoid re-processing
"""

import os
import json
import re
import pdfplumber
from openai import OpenAI
from json_repair import repair_json

# ─── Configuration ────────────────────────────────────────────────────────────

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DATA_DIR = "/Users/nms/Desktop/AI_ASSIST DS/data"
OUTPUT_DIR = "/Users/nms/Desktop/AI_ASSIST DS/output"
MODEL = "deepseek-chat"
MAX_CHARS_PER_CHUNK = 55_000  # conservative to leave room for output tokens

PROGRAMS = {
    "IT": ["IT-1.pdf", "IT-2.pdf", "IT-3.pdf", "แผน_IT.pdf"],
    "BIT": ["BIT-1.pdf", "BIT-2.pdf", "BIT-3.pdf", "แผน_BIT.pdf"],
    "DSBA": [
        "DSBA-1.pdf", "DSBA-2-1.pdf", "DSBA-2-2.pdf",
        "DSBA-2-3-1.pdf", "DSBA-2-3-2.pdf", "DSBA-2-4.pdf",
        "DSBA-2-5.pdf", "DSBA-3.pdf", "แผน_DSBA.pdf",
    ],
    "AIT": ["AIT_1.pdf", "AIT_2.pdf", "AIT_3.pdf", "AIT_4.pdf", "แผน_AIT.pdf"],
}

# ─── Prompts ──────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """คุณเป็นผู้เชี่ยวชาญด้านการวิเคราะห์เอกสารหลักสูตรมหาวิทยาลัยไทย (มคอ.2)
งานของคุณคือ extract ข้อมูลจากเอกสารหลักสูตรและส่งคืนเป็น JSON ที่มีโครงสร้างชัดเจน
ตอบเฉพาะ JSON เท่านั้น ไม่มีข้อความอื่น ไม่มี markdown code block
สำคัญ: ตอบให้ครบ อย่าตัดข้อความกลางคัน"""

EXTRACTION_PROMPT = """จากเนื้อหาเอกสารหลักสูตรต่อไปนี้ ให้ extract ข้อมูลทั้งหมดที่มีและส่งคืนเป็น JSON ที่สมบูรณ์

Schema:
{{
  "program_info": {{
    "name_th": "ชื่อหลักสูตรภาษาไทย",
    "name_en": "ชื่อหลักสูตรภาษาอังกฤษ",
    "degree_th": "ชื่อปริญญาภาษาไทย",
    "degree_en": "ชื่อปริญญาภาษาอังกฤษ",
    "major_th": "สาขาวิชาภาษาไทย",
    "major_en": "สาขาวิชาภาษาอังกฤษ",
    "total_credits": 0,
    "program_type": "ปริญญาตรี/โท/เอก",
    "duration_years": 0,
    "curriculum_year": "พ.ศ.",
    "institution": "ชื่อสถาบัน",
    "faculty": "ชื่อคณะ"
  }},
  "curriculum_structure": {{
    "general_education": {{ "credits": 0, "subcategories": [] }},
    "specific_courses": {{ "credits": 0, "subcategories": [] }},
    "elective_courses": {{ "credits": 0, "description": "" }},
    "free_electives": {{ "credits": 0, "description": "" }}
  }},
  "courses": [
    {{
      "code": "รหัสวิชา",
      "name_th": "ชื่อวิชาไทย",
      "name_en": "ชื่อวิชาอังกฤษ",
      "credits": "เช่น 3(3-0-6)",
      "category": "หมวดหมู่",
      "type": "บังคับ/เลือก",
      "year": 0,
      "semester": 0
    }}
  ],
  "academic_plan": [
    {{
      "year": 1,
      "semester": 1,
      "plan_type": "ปกติ/สหกิจ",
      "courses": [{{"code": "", "name_th": "", "credits": ""}}],
      "total_credits": 0
    }}
  ],
  "graduation_requirements": {{
    "minimum_credits": 0,
    "minimum_gpa": 0.0,
    "specific_requirements": [],
    "other_conditions": []
  }},
  "dismissal_criteria": {{
    "academic_dismissal": [],
    "other_criteria": []
  }},
  "rules_and_regulations": {{
    "enrollment_rules": [],
    "examination_rules": [],
    "grade_rules": [],
    "other_rules": []
  }},
  "career_opportunities": [],
  "program_outcomes": [],
  "special_tracks": []
}}

ถ้าข้อมูลไม่มีใน document ให้ใส่ null หรือ [] หรือ {{}}
รักษาข้อมูลภาษาไทยให้ครบถ้วน ตอบ JSON ที่สมบูรณ์เท่านั้น

เนื้อหาเอกสาร:
{text}"""

# ─── Helpers ──────────────────────────────────────────────────────────────────

def extract_text_from_pdf(pdf_path: str) -> str:
    texts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                texts.append(text)
    return "\n".join(texts)


def chunk_text(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks = []
    paragraphs = text.split("\n")
    current_chunk, current_len = [], 0
    for para in paragraphs:
        para_len = len(para) + 1
        if current_len + para_len > max_chars and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk, current_len = [para], para_len
        else:
            current_chunk.append(para)
            current_len += para_len
    if current_chunk:
        chunks.append("\n".join(current_chunk))
    return chunks


def call_deepseek(client: OpenAI, system: str, user: str) -> str:
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
        max_tokens=8192,
    )
    return response.choices[0].message.content.strip()


def parse_json_response(text: str) -> dict:
    """Parse JSON with automatic repair for truncated responses."""
    # Strip markdown fences
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Use json_repair to fix truncated/malformed JSON
    try:
        repaired = repair_json(text, return_objects=True)
        if isinstance(repaired, dict) and repaired:
            print(f"    [INFO] JSON repaired successfully")
            return repaired
    except Exception:
        pass

    print(f"    [WARN] Could not parse JSON, returning empty dict")
    return {}


# ─── Python-based merge (no API calls needed) ─────────────────────────────────

def merge_list_dedup(lists: list[list], key: str) -> list:
    """Merge multiple lists, deduplicating by a key field."""
    seen = {}
    for lst in lists:
        for item in lst:
            if isinstance(item, dict):
                k = item.get(key, "")
                if k and k not in seen:
                    seen[k] = item
                elif not k:
                    seen[id(item)] = item  # keep items without key
    return list(seen.values())


def merge_list_unique(lists: list[list]) -> list:
    """Merge lists of strings, removing duplicates."""
    seen = set()
    result = []
    for lst in lists:
        for item in lst:
            s = str(item)
            if s not in seen:
                seen.add(s)
                result.append(item)
    return result


def pick_best(values: list, prefer_non_empty=True):
    """Pick the best non-null, non-empty value from a list."""
    for v in values:
        if v is None:
            continue
        if prefer_non_empty and v in ("", 0, [], {}):
            continue
        return v
    return values[0] if values else None


def merge_program_info(infos: list[dict]) -> dict:
    infos = [i for i in infos if i]
    if not infos:
        return {}
    result = {}
    for field in ["name_th", "name_en", "degree_th", "degree_en", "major_th", "major_en",
                  "total_credits", "program_type", "duration_years", "curriculum_year",
                  "institution", "faculty"]:
        values = [i.get(field) for i in infos if i.get(field)]
        result[field] = values[0] if values else None
    return result


def merge_curriculum_structure(structs: list[dict]) -> dict:
    structs = [s for s in structs if s]
    if not structs:
        return {}
    result = {}
    for key in ["general_education", "specific_courses", "elective_courses", "free_electives"]:
        candidates = [s.get(key) for s in structs if s.get(key)]
        if candidates:
            best = max(candidates, key=lambda x: len(str(x)))
            result[key] = best
        else:
            result[key] = {}
    return result


def merge_graduation_requirements(reqs: list[dict]) -> dict:
    reqs = [r for r in reqs if r]
    if not reqs:
        return {}
    credits = [r.get("minimum_credits") for r in reqs if r.get("minimum_credits")]
    gpa = [r.get("minimum_gpa") for r in reqs if r.get("minimum_gpa")]
    specific = merge_list_unique([r.get("specific_requirements", []) for r in reqs])
    other = merge_list_unique([r.get("other_conditions", []) for r in reqs])
    return {
        "minimum_credits": credits[0] if credits else None,
        "minimum_gpa": gpa[0] if gpa else None,
        "specific_requirements": specific,
        "other_conditions": other,
    }


def merge_dismissal_criteria(crits: list[dict]) -> dict:
    crits = [c for c in crits if c]
    if not crits:
        return {}
    return {
        "academic_dismissal": merge_list_unique([c.get("academic_dismissal", []) for c in crits]),
        "other_criteria": merge_list_unique([c.get("other_criteria", []) for c in crits]),
    }


def merge_rules(rules_list: list[dict]) -> dict:
    rules_list = [r for r in rules_list if r]
    if not rules_list:
        return {}
    return {
        "enrollment_rules": merge_list_unique([r.get("enrollment_rules", []) for r in rules_list]),
        "examination_rules": merge_list_unique([r.get("examination_rules", []) for r in rules_list]),
        "grade_rules": merge_list_unique([r.get("grade_rules", []) for r in rules_list]),
        "other_rules": merge_list_unique([r.get("other_rules", []) for r in rules_list]),
    }


def merge_academic_plan(plans: list[list]) -> list:
    """Merge academic plans, grouping by year+semester+plan_type."""
    combined = {}
    for plan_list in plans:
        for entry in plan_list:
            if not isinstance(entry, dict):
                continue
            key = (entry.get("year"), entry.get("semester"), entry.get("plan_type", "ปกติ"))
            if key not in combined:
                combined[key] = entry.copy()
                combined[key]["courses"] = list(entry.get("courses", []))
            else:
                # Merge courses for same year/semester
                existing_codes = {c.get("code") for c in combined[key]["courses"] if isinstance(c, dict)}
                for course in entry.get("courses", []):
                    if isinstance(course, dict) and course.get("code") not in existing_codes:
                        combined[key]["courses"].append(course)
                        existing_codes.add(course.get("code"))
    return sorted(combined.values(), key=lambda x: (x.get("year", 0) or 0, x.get("semester", 0) or 0))


def python_merge(extractions: list[dict]) -> dict:
    """Merge all extractions into a single dict using Python logic."""
    extractions = [e for e in extractions if e and isinstance(e, dict)]
    if not extractions:
        return {}
    if len(extractions) == 1:
        return extractions[0]

    result = {
        "program_info": merge_program_info([e.get("program_info", {}) for e in extractions]),
        "curriculum_structure": merge_curriculum_structure([e.get("curriculum_structure", {}) for e in extractions]),
        "courses": merge_list_dedup([e.get("courses", []) for e in extractions], key="code"),
        "academic_plan": merge_academic_plan([e.get("academic_plan", []) for e in extractions]),
        "graduation_requirements": merge_graduation_requirements([e.get("graduation_requirements", {}) for e in extractions]),
        "dismissal_criteria": merge_dismissal_criteria([e.get("dismissal_criteria", {}) for e in extractions]),
        "rules_and_regulations": merge_rules([e.get("rules_and_regulations", {}) for e in extractions]),
        "career_opportunities": merge_list_unique([e.get("career_opportunities", []) for e in extractions]),
        "program_outcomes": merge_list_unique([e.get("program_outcomes", []) for e in extractions]),
        "special_tracks": merge_list_unique([e.get("special_tracks", []) for e in extractions]),
    }
    return result


# ─── Core processing ──────────────────────────────────────────────────────────

def extract_from_chunk(client: OpenAI, text_chunk: str, source_file: str) -> dict:
    prompt = EXTRACTION_PROMPT.format(text=text_chunk)
    print(f"    → Calling DeepSeek ({len(text_chunk):,} chars)...")
    response = call_deepseek(client, SYSTEM_PROMPT, prompt)
    result = parse_json_response(response)
    result["_source"] = source_file
    return result


def process_pdf(client: OpenAI, program_name: str, pdf_file: str) -> dict:
    """Process a single PDF, returning merged extraction (with caching)."""
    pdf_path = os.path.join(DATA_DIR, pdf_file)
    if not os.path.exists(pdf_path):
        print(f"    [SKIP] file not found")
        return {}

    cache_path = os.path.join(OUTPUT_DIR, f"_cache_{program_name}_{pdf_file}.json")
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if cached and len(cached) > 2:  # valid cache
            print(f"    → Using cache ({os.path.getsize(cache_path):,} bytes)")
            return cached
        else:
            print(f"    → Cache invalid, re-processing")
            os.remove(cache_path)

    print(f"    → Extracting PDF text...")
    text = extract_text_from_pdf(pdf_path)
    print(f"    → {len(text):,} characters extracted")

    if not text.strip():
        print(f"    [WARN] No text (scanned PDF?)")
        return {}

    chunks = chunk_text(text)
    print(f"    → {len(chunks)} chunk(s)")

    chunk_results = []
    for i, chunk in enumerate(chunks):
        print(f"    → Chunk {i+1}/{len(chunks)}")
        result = extract_from_chunk(client, chunk, pdf_file)
        if result:
            chunk_results.append(result)

    file_result = python_merge(chunk_results) if chunk_results else {}

    # Cache result
    if file_result:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(file_result, f, ensure_ascii=False, indent=2)
        print(f"    → Cached ({os.path.getsize(cache_path):,} bytes)")

    return file_result


def process_program(client: OpenAI, program_name: str, pdf_files: list[str]) -> dict:
    print(f"\n{'='*60}")
    print(f"  Program: {program_name}")
    print(f"{'='*60}")

    all_extractions = []
    for pdf_file in pdf_files:
        print(f"\n  [{pdf_file}]")
        result = process_pdf(client, program_name, pdf_file)
        if result:
            all_extractions.append(result)

    print(f"\n  → Python-merging {len(all_extractions)} file result(s)...")
    final = python_merge(all_extractions)
    final["program"] = program_name
    return final


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not DEEPSEEK_API_KEY:
        print("ERROR: DEEPSEEK_API_KEY not set")
        return

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    summary = {}

    for program_name, pdf_files in PROGRAMS.items():
        result = process_program(client, program_name, pdf_files)

        if result:
            out_path = os.path.join(OUTPUT_DIR, f"{program_name}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

            courses = result.get("courses", [])
            plan = result.get("academic_plan", [])
            info = result.get("program_info", {})
            summary[program_name] = {
                "output_file": out_path,
                "program_name_th": info.get("name_th", ""),
                "total_credits": info.get("total_credits"),
                "total_courses": len(courses),
                "academic_plan_entries": len(plan),
            }
            print(f"\n  Saved → {out_path}")
            print(f"  Courses: {len(courses)}, Plan entries: {len(plan)}")

    summary_path = os.path.join(OUTPUT_DIR, "_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print("DONE!")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
