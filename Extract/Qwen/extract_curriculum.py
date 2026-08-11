"""
extract_curriculum.py
UniAssist AI — ดึงข้อมูลหลักสูตรจากเอกสาร PDF/text ด้วย OpenRouter API (Qwen3)
รัน: python extract_curriculum.py
"""

import os
import json
import re
import time
import pathlib
import logging
from dataclasses import dataclass, field
from typing import Optional

import requests
from dotenv import load_dotenv

# ── PDF extraction ──────────────────────────────────────────────────────────
try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

try:
    import pypdf
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

# ── Setup ────────────────────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
if not OPENROUTER_API_KEY:
    raise EnvironmentError("OPENROUTER_API_KEY not found in .env")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PRIMARY_MODEL  = "qwen/qwen3.6-plus"
FALLBACK_MODEL = "qwen/qwen3.6-flash"

# Qwen3 Plus pricing (USD per 1M tokens)
PRICE_INPUT  = 0.325 / 1_000_000
PRICE_OUTPUT = 1.95  / 1_000_000

# ── Prompts (ห้ามแก้ไขเนื้อหา) ──────────────────────────────────────────────
SYSTEM_PROMPT_PLAN = """คุณเป็นผู้เชี่ยวชาญด้านการวิเคราะห์เอกสารหลักสูตรมหาวิทยาลัยไทย (มคอ.2)
งานของคุณคือ extract ข้อมูลจากเอกสารหลักสูตรและส่งคืนเป็น JSON ที่มีโครงสร้างชัดเจน
ตอบเฉพาะ JSON เท่านั้น ไม่มีข้อความอื่น ไม่มี markdown code block
สำคัญ: ตอบให้ครบ อย่าตัดข้อความกลางคัน"""

EXTRACTION_PROMPT_PLAN = """จากเนื้อหาเอกสารหลักสูตรต่อไปนี้ ให้ extract ข้อมูลทั้งหมดที่มีและส่งคืนเป็น JSON ที่สมบูรณ์
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
      "semester": 0,
      "prerequisites": ["รหัสวิชาที่ต้องเรียนก่อน เช่น 06016001"]
    }}
  ],
  "academic_plan": [
    {{
      "year": 1,
      "semester": 1,
      "plan_type": "ปกติ/สหกิจ",
      "courses": [{{"code": "", "name_en": "", "credits": ""}}],
      "total_credits": 0
    }}
  ],
  "career_opportunities": [],
  "program_outcomes": [],
  "special_tracks": []
}}
หมายเหตุ prerequisites: ให้ดึงรหัสวิชา (course code) ที่ระบุว่าต้องเรียนก่อน (บางเอกสารเรียกว่า "วิชาบังคับก่อน" หรือ "prerequisite") ถ้าไม่มีให้ใส่ []
ถ้าข้อมูลไม่มีใน document ให้ใส่ null หรือ [] หรือ {{}}
รักษาข้อมูลภาษาไทยให้ครบถ้วน ตอบ JSON ที่สมบูรณ์เท่านั้น
เนื้อหาเอกสาร:
{text}"""

SYSTEM_PROMPT_RULES = """คุณเป็นผู้เชี่ยวชาญด้านการวิเคราะห์เอกสารหลักสูตรมหาวิทยาลัยไทย (มคอ.2)
งานของคุณคือ extract ข้อมูลจากเอกสารหลักสูตรและส่งคืนเป็น JSON ที่มีโครงสร้างชัดเจน
ตอบเฉพาะ JSON เท่านั้น ไม่มีข้อความอื่น ไม่มี markdown code block
สำคัญ: ตอบให้ครบ อย่าตัดข้อความกลางคัน"""

EXTRACTION_PROMPT_RULES = """จากเนื้อหาเอกสารหลักสูตรต่อไปนี้ ให้ extract ข้อมูลเกี่ยวกับกฎ ระเบียบ และเกณฑ์ต่างๆ ทั้งหมดให้ครบถ้วนที่สุด แล้วส่งคืนเป็น JSON ที่สมบูรณ์
Schema:
{{
  "graduation_requirements": {{
    "minimum_credits": 0,
    "minimum_gpa": 0.0,
    "time_limit_years": null,
    "max_study_years": null,
    "specific_requirements": [],
    "other_conditions": []
  }},
  "honors_criteria": {{
    "first_class_gold_medal": {{
      "name": "เกียรตินิยมอันดับหนึ่งเหรียญทอง",
      "gpa_min": 0.0,
      "conditions": [],
      "disqualifications": []
    }},
    "first_class": {{
      "name": "เกียรตินิยมอันดับหนึ่ง",
      "gpa_min": 0.0,
      "conditions": [],
      "disqualifications": []
    }},
    "second_class": {{
      "name": "เกียรตินิยมอันดับสอง",
      "gpa_min": 0.0,
      "conditions": [],
      "disqualifications": []
    }}
  }},
  "dismissal_criteria": {{
    "gpa_based": [],
    "time_based": [],
    "enrollment_based": [],
    "other_criteria": []
  }},
  "probation_rules": [],
  "grading_system": {{
    "A": "4.00 (ดีเลิศ)",
    "B+": "3.50 (ดีมาก)",
    "B": "3.00 (ดี)",
    "C+": "2.50 (ดีพอใช้)",
    "C": "2.00 (พอใช้)",
    "D+": "1.50 (อ่อน)",
    "D": "1.00 (อ่อนมาก)",
    "F": "0 (ตก)",
    "I": "ไม่สมบูรณ์",
    "S": "พอใจ",
    "U": "ไม่พอใจ",
    "T": "รับโอน"
  }},
  "registration_rules": [],
  "leave_of_absence_rules": [],
  "withdrawal_rules": [],
  "examination_rules": [],
  "academic_misconduct_rules": [],
  "student_conduct_rules": [],
  "disciplinary_penalties": [],
  "appeal_rules": [],
  "readmission_rules": [],
  "transfer_credit_rules": [],
  "other_regulations": []
}}
ถ้าข้อมูลไม่มีใน document ให้ใส่ null หรือ [] หรือ {{}}
รักษาข้อมูลภาษาไทยให้ครบถ้วน ตอบ JSON ที่สมบูรณ์เท่านั้น
เนื้อหาเอกสาร:
{text}"""

# ── Dataclasses ───────────────────────────────────────────────────────────────
@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        return (self.prompt_tokens * PRICE_INPUT) + (self.completion_tokens * PRICE_OUTPUT)


@dataclass
class ProgramConfig:
    name: str                    # ชื่อย่อ เช่น IT, DSBA
    files: list[str]             # path ไฟล์ PDF/text ทั้งหมดของหลักสูตร


# ── ระบุหลักสูตรและไฟล์ ──────────────────────────────────────────────────────
DATA_DIR = pathlib.Path(__file__).parent / "data"
OUTPUT_DIR = pathlib.Path(__file__).parent / "output"

PROGRAMS: list[ProgramConfig] = [
    ProgramConfig("IT", [
        str(DATA_DIR / "IT-1.pdf"),
        str(DATA_DIR / "IT-2.pdf"),
        str(DATA_DIR / "IT-3.pdf"),
        str(DATA_DIR / "แผน_IT.pdf"),
    ]),
    ProgramConfig("DSBA", [
        str(DATA_DIR / "DSBA-1.pdf"),
        str(DATA_DIR / "DSBA-2-1.pdf"),
        str(DATA_DIR / "DSBA-2-2.pdf"),
        str(DATA_DIR / "DSBA-2-3-1.pdf"),
        str(DATA_DIR / "DSBA-2-3-2.pdf"),
        str(DATA_DIR / "DSBA-2-4.pdf"),
        str(DATA_DIR / "DSBA-2-5.pdf"),
        str(DATA_DIR / "DSBA-3.pdf"),
        str(DATA_DIR / "แผน_DSBA.pdf"),
    ]),
    ProgramConfig("BIT", [
        str(DATA_DIR / "BIT-1.pdf"),
        str(DATA_DIR / "BIT-2.pdf"),
        str(DATA_DIR / "BIT-3.pdf"),
        str(DATA_DIR / "แผน_BIT.pdf"),
    ]),
    ProgramConfig("AIT", [
        str(DATA_DIR / "AIT_1.pdf"),
        str(DATA_DIR / "AIT_2.pdf"),
        str(DATA_DIR / "AIT_3.pdf"),
        str(DATA_DIR / "AIT_4.pdf"),
        str(DATA_DIR / "แผน_AIT.pdf"),
    ]),
]

# ── Utilities ────────────────────────────────────────────────────────────────

def extract_text_from_pdf(path: str) -> str:
    """ดึงข้อความจาก PDF ใช้ pdfplumber ก่อน ถ้าไม่มีจึงใช้ pypdf"""
    if HAS_PDFPLUMBER:
        try:
            with pdfplumber.open(path) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages]
            return "\n".join(pages)
        except Exception as e:
            log.warning("pdfplumber failed on %s: %s — trying pypdf", path, e)

    if HAS_PYPDF:
        try:
            reader = pypdf.PdfReader(path)
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n".join(pages)
        except Exception as e:
            log.error("pypdf also failed on %s: %s", path, e)
            return ""

    raise ImportError("ต้องติดตั้ง pdfplumber หรือ pypdf: pip install pdfplumber pypdf")


def load_document_text(paths: list[str]) -> str:
    """รวมข้อความจากหลายไฟล์เข้าด้วยกัน"""
    parts = []
    for path in paths:
        p = pathlib.Path(path)
        if not p.exists():
            log.warning("ไม่พบไฟล์: %s — ข้าม", path)
            continue
        if p.suffix.lower() == ".pdf":
            text = extract_text_from_pdf(path)
        else:
            text = p.read_text(encoding="utf-8", errors="replace")
        if text.strip():
            parts.append(f"=== {p.name} ===\n{text}")
            log.info("  โหลด %s (%.1f KB)", p.name, len(text) / 1024)
        else:
            log.warning("  ไม่มีข้อความใน %s", p.name)
    return "\n\n".join(parts)


def strip_code_fence(text: str) -> str:
    """ลบ ```json ... ``` หรือ ``` ... ``` ออก"""
    text = text.strip()
    # ลบ fence ที่ขึ้นต้นด้วย ```json หรือ ```
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def call_openrouter(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    model: str = PRIMARY_MODEL,
) -> tuple[str, TokenUsage]:
    """เรียก OpenRouter API และคืน (raw_content, usage)"""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/uniassist-ai",
        "X-Title": "UniAssist AI Curriculum Extractor",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=300)
    resp.raise_for_status()
    data = resp.json()

    choice  = data["choices"][0]
    content = choice["message"]["content"]
    finish  = choice.get("finish_reason", "")

    raw_usage = data.get("usage", {})
    usage = TokenUsage(
        prompt_tokens=raw_usage.get("prompt_tokens", 0),
        completion_tokens=raw_usage.get("completion_tokens", 0),
    )
    return content, finish, usage


def extract_with_retry(
    system_prompt: str,
    user_prompt: str,
    initial_max_tokens: int,
    part_label: str,
) -> tuple[Optional[dict], TokenUsage]:
    """
    เรียก API พร้อม retry logic:
    - ถ้า JSON parse ล้มเหลว หรือ finish_reason == 'length' → เพิ่ม max_tokens แล้ว retry
    - ลอง PRIMARY_MODEL ก่อน ถ้า error ให้ fallback FALLBACK_MODEL
    """
    max_tokens    = initial_max_tokens
    total_usage   = TokenUsage()
    max_retries   = 3
    retry_boost   = 4000

    for attempt in range(1, max_retries + 1):
        model = PRIMARY_MODEL if attempt <= 2 else FALLBACK_MODEL
        log.info("  [%s] attempt %d/%d  model=%s  max_tokens=%d",
                 part_label, attempt, max_retries, model, max_tokens)
        try:
            raw, finish, usage = call_openrouter(
                system_prompt, user_prompt, max_tokens, model
            )
        except requests.HTTPError as e:
            log.warning("  [%s] HTTP error: %s — ลอง fallback model", part_label, e)
            if model == PRIMARY_MODEL:
                try:
                    raw, finish, usage = call_openrouter(
                        system_prompt, user_prompt, max_tokens, FALLBACK_MODEL
                    )
                except Exception as e2:
                    log.error("  [%s] fallback ก็ล้มเหลว: %s", part_label, e2)
                    if attempt < max_retries:
                        time.sleep(5)
                        continue
                    return None, total_usage
            else:
                if attempt < max_retries:
                    time.sleep(5)
                    continue
                return None, total_usage

        total_usage.prompt_tokens     += usage.prompt_tokens
        total_usage.completion_tokens += usage.completion_tokens

        log.info("  [%s] finish_reason=%s  tokens: in=%d out=%d  cost=$%.6f",
                 part_label, finish,
                 usage.prompt_tokens, usage.completion_tokens, usage.cost_usd)

        if finish == "length":
            log.warning("  [%s] ถูกตัดกลางคัน (finish_reason=length) → เพิ่ม max_tokens +%d",
                        part_label, retry_boost)
            max_tokens += retry_boost
            time.sleep(2)
            continue

        # พยายาม parse JSON
        cleaned = strip_code_fence(raw)
        try:
            result = json.loads(cleaned)
            log.info("  [%s] parse JSON สำเร็จ", part_label)
            return result, total_usage
        except json.JSONDecodeError as e:
            log.warning("  [%s] JSON parse ล้มเหลว: %s", part_label, e)
            log.debug("  raw output (500 chars): %s", cleaned[:500])
            if attempt < max_retries:
                max_tokens += retry_boost
                time.sleep(2)
                continue

    log.error("  [%s] หมด retry แล้ว ข้ามส่วนนี้", part_label)
    return None, total_usage


def merge_results(part1: Optional[dict], part2: Optional[dict]) -> dict:
    """รวม JSON จาก Part 1 และ Part 2 เป็น object เดียว"""
    merged = {}
    if part1:
        merged.update(part1)
    if part2:
        merged.update(part2)
    return merged


def format_cost(usage: TokenUsage) -> str:
    return (f"in={usage.prompt_tokens:,}  out={usage.completion_tokens:,}  "
            f"cost=${usage.cost_usd:.6f}")


# ── Main ─────────────────────────────────────────────────────────────────────

def process_program(prog: ProgramConfig) -> TokenUsage:
    """
    ประมวลผล 1 หลักสูตร:
    1. โหลด text จากไฟล์ทั้งหมด
    2. Call 1: Part plan
    3. Call 2: Part rules
    4. Merge + save
    คืน TokenUsage รวม
    """
    log.info("=" * 60)
    log.info("หลักสูตร: %s", prog.name)
    log.info("=" * 60)

    total_usage = TokenUsage()

    # 1. โหลด text
    doc_text = load_document_text(prog.files)
    if not doc_text.strip():
        log.error("[%s] ไม่มีข้อความในเอกสาร — ข้ามหลักสูตรนี้", prog.name)
        return total_usage

    log.info("รวมข้อความทั้งหมด: %.1f KB", len(doc_text) / 1024)

    # 2. Call 1 — Part Plan
    log.info("--- Call 1: academic_plan ---")
    part1_prompt = EXTRACTION_PROMPT_PLAN.format(text=doc_text)
    part1_result, usage1 = extract_with_retry(
        system_prompt=SYSTEM_PROMPT_PLAN,
        user_prompt=part1_prompt,
        initial_max_tokens=10000,
        part_label=f"{prog.name}/Part1",
    )
    total_usage.prompt_tokens     += usage1.prompt_tokens
    total_usage.completion_tokens += usage1.completion_tokens
    log.info("  [%s/Part1] รวม %s", prog.name, format_cost(usage1))

    # 3. Call 2 — Part Rules
    log.info("--- Call 2: rules & requirements ---")
    part2_prompt = EXTRACTION_PROMPT_RULES.format(text=doc_text)
    part2_result, usage2 = extract_with_retry(
        system_prompt=SYSTEM_PROMPT_RULES,
        user_prompt=part2_prompt,
        initial_max_tokens=4000,
        part_label=f"{prog.name}/Part2",
    )
    total_usage.prompt_tokens     += usage2.prompt_tokens
    total_usage.completion_tokens += usage2.completion_tokens
    log.info("  [%s/Part2] รวม %s", prog.name, format_cost(usage2))

    # 4. Merge + Save
    if part1_result is None and part2_result is None:
        log.error("[%s] ทั้ง 2 part ล้มเหลว — ไม่บันทึกไฟล์", prog.name)
        return total_usage

    merged = merge_results(part1_result, part2_result)

    # บันทึก metadata การ extract
    merged["_extract_meta"] = {
        "program": prog.name,
        "part1_success": part1_result is not None,
        "part2_success": part2_result is not None,
        "source_files": [pathlib.Path(f).name for f in prog.files],
        "usage_part1": {
            "prompt_tokens": usage1.prompt_tokens,
            "completion_tokens": usage1.completion_tokens,
            "cost_usd": round(usage1.cost_usd, 8),
        },
        "usage_part2": {
            "prompt_tokens": usage2.prompt_tokens,
            "completion_tokens": usage2.completion_tokens,
            "cost_usd": round(usage2.cost_usd, 8),
        },
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{prog.name}.json"
    out_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("[%s] บันทึกผลลัพธ์ → %s", prog.name, out_path)

    if part1_result is None:
        log.warning("[%s] Part 1 (academic_plan) ล้มเหลว — บันทึกเฉพาะ Part 2", prog.name)
    if part2_result is None:
        log.warning("[%s] Part 2 (rules) ล้มเหลว — บันทึกเฉพาะ Part 1", prog.name)

    return total_usage


def main():
    grand_total = TokenUsage()
    program_summaries: list[tuple[str, TokenUsage]] = []

    for prog in PROGRAMS:
        try:
            usage = process_program(prog)
            program_summaries.append((prog.name, usage))
            grand_total.prompt_tokens     += usage.prompt_tokens
            grand_total.completion_tokens += usage.completion_tokens
        except Exception as e:
            log.exception("เกิดข้อผิดพลาดที่หลักสูตร %s: %s", prog.name, e)
        time.sleep(2)  # pause เล็กน้อยระหว่างหลักสูตร

    # ── สรุปค่าใช้จ่าย ────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("สรุปค่าใช้จ่าย (Token Usage Summary)")
    print("=" * 60)
    print(f"{'หลักสูตร':<10}  {'Input':>10}  {'Output':>10}  {'Cost (USD)':>12}")
    print("-" * 50)
    for name, u in program_summaries:
        print(f"{name:<10}  {u.prompt_tokens:>10,}  {u.completion_tokens:>10,}  ${u.cost_usd:>11.6f}")
    print("-" * 50)
    print(f"{'รวมทั้งหมด':<10}  {grand_total.prompt_tokens:>10,}  {grand_total.completion_tokens:>10,}  ${grand_total.cost_usd:>11.6f}")
    print("=" * 60)
    print(f"\nModel ราคา: input=${PRICE_INPUT*1e6:.3f}/1M  output=${PRICE_OUTPUT*1e6:.3f}/1M  ({PRIMARY_MODEL})")
    print(f"ไฟล์ผลลัพธ์อยู่ใน: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
