"""
Parse prerequisite data from course description PDFs using regex on full text.
"""
import os, json, re
import pdfplumber

DATA_DIR   = "/Users/nms/Desktop/AI_ASSIST DS/data"
OUTPUT_DIR = "/Users/nms/Desktop/AI_ASSIST DS/output/realone"

COURSE_DESC_FILES = {
    "IT":   ["IT-3.pdf"],
    "BIT":  ["BIT-3.pdf"],
    "DSBA": ["DSBA-3.pdf"],
    "AIT":  ["AIT_4.pdf"],
}

# Regex: course header line  e.g. "06016401 คณิตศาสตร์สำหรับ... 3(3-0-6)"
HEADER_RE = re.compile(
    r"(\d{8})\s+(.+?)\s+(\d+\(\d+-\d+-\d+\))"
)
# วิชาบังคับก่อน line
TH_PRE_RE = re.compile(r"วิชาบังคับก่อน\s*:\s*(.+)")
# PREREQUISITE line
EN_PRE_RE = re.compile(r"PREREQUISITE\s*:\s*(.+)", re.IGNORECASE)
# Single prereq entry: code + name
SINGLE_PRE_RE = re.compile(r"(\d{8})\s*(.+?)(?=\s*,|\s*และ|$)")


def get_full_text(pdf_path: str) -> str:
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            pages.append(t)
    return "\n".join(pages)


def normalize_th(s: str) -> str:
    """Collapse spaces inside common Thai words broken by PDF extraction."""
    s = re.sub(r"ไม่\s*ม\s*ี", "ไม่มี", s)
    return s.strip()


def parse_prereq_str(s: str) -> list[dict]:
    """Parse 'CODE name, CODE name' or 'ไม่มี' into list of {code, name}."""
    s = normalize_th(s.strip())
    if not s or re.search(r"ไม่มี|^NONE$|^None$|^-$", s, re.IGNORECASE):
        return []
    results = []
    # Split by comma or 'และ'
    for part in re.split(r",\s*|และ\s*", s):
        part = part.strip()
        m = re.match(r"(\d{8})\s*(.*)", part)
        if m:
            results.append({"code": m.group(1), "name": m.group(2).strip()})
        elif part:
            results.append({"code": None, "name": part})
    return results


def parse_courses(text: str) -> list[dict]:
    """
    Split text into course blocks by header pattern, then extract fields.
    """
    # Find all header positions
    headers = list(HEADER_RE.finditer(text))
    if not headers:
        return []

    courses = []
    for i, hm in enumerate(headers):
        code    = hm.group(1)
        name_th = hm.group(2).strip()
        credits = hm.group(3)

        # The block goes from this header to the next header (or end)
        block_start = hm.end()
        block_end   = headers[i+1].start() if i+1 < len(headers) else len(text)
        block = text[block_start:block_end]

        # Extract English name: first non-empty, non-Thai line right after header
        name_en = ""
        for line in block.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Stop at Thai / prerequisite / next course
            if re.search(r"[ก-๙]", line):
                break
            if re.match(r"PREREQUISITE|วิชาบังคับก่อน|\d{8}", line, re.IGNORECASE):
                break
            name_en = (name_en + " " + line).strip()
            # If we got a reasonable length, stop
            if len(name_en) > 10 and not line.endswith((",", "AND", "OR")):
                break

        # Extract Thai prerequisite
        prereqs_th: list[dict] = []
        m_th = TH_PRE_RE.search(block)
        if m_th:
            prereqs_th = parse_prereq_str(m_th.group(1))

        # Extract English prerequisite
        prereqs_en: list[str] = []
        m_en = EN_PRE_RE.search(block)
        if m_en:
            en_val = m_en.group(1).strip()
            # Check if the next line is a continuation (no Thai, no new code)
            after = block[m_en.end():].split("\n")
            for cont in after:
                cont = cont.strip()
                if (cont and not re.search(r"[ก-๙]", cont)
                        and not re.match(r"\d{8}|PREREQUISITE|วิชาบังคับ", cont, re.IGNORECASE)
                        and not re.match(r"\d+\s*มคอ", cont)):
                    en_val += " " + cont
                    break
            # Parse individual EN prereq names (strip codes)
            if not re.search(r"NONE|None", en_val, re.IGNORECASE):
                for part in re.split(r",\s*|AND\s*", en_val, flags=re.IGNORECASE):
                    part = re.sub(r"^\d{8}\s*", "", part).strip()
                    if part:
                        prereqs_en.append(part)

        # Merge th+en
        prerequisites = []
        for idx, p in enumerate(prereqs_th):
            prerequisites.append({
                "code":    p["code"],
                "name_th": p["name"],
                "name_en": prereqs_en[idx] if idx < len(prereqs_en) else None,
            })

        courses.append({
            "code":           code,
            "name_th":        name_th,
            "name_en":        name_en,
            "credits":        credits,
            "prerequisites":  prerequisites,
            "has_prerequisite": len(prerequisites) > 0,
        })

    return courses


def merge_into_main(program: str, prereq_map: dict):
    path = os.path.join(OUTPUT_DIR, f"{program}.json")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    updated = 0
    for course in data.get("courses", []):
        code = course.get("code", "").strip()
        if code in prereq_map:
            course["prerequisites"]    = prereq_map[code]
            course["has_prerequisite"] = len(prereq_map[code]) > 0
            updated += 1
        else:
            if "prerequisites" not in course:
                course["prerequisites"]    = []
                course["has_prerequisite"] = False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  → merged prereqs into {updated} courses in {program}.json")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    for program, pdf_files in COURSE_DESC_FILES.items():
        print(f"\n=== {program} ===")
        all_courses: list[dict] = []

        for pdf_file in pdf_files:
            path = os.path.join(DATA_DIR, pdf_file)
            if not os.path.exists(path):
                print(f"  [SKIP] {pdf_file}")
                continue
            print(f"  Reading {pdf_file}...")
            text = get_full_text(path)
            print(f"  Parsing {len(text):,} chars...")
            courses = parse_courses(text)
            print(f"  → {len(courses)} courses parsed")
            all_courses.extend(courses)

        # Deduplicate
        seen: dict[str, dict] = {}
        for c in all_courses:
            if c["code"] not in seen:
                seen[c["code"]] = c
        unique = list(seen.values())
        with_pre = [c for c in unique if c["has_prerequisite"]]

        print(f"  {len(unique)} unique courses | {len(with_pre)} have prerequisites")
        print("  Samples:")
        for c in with_pre[:5]:
            print(f"    {c['code']} {c['name_th']}")
            for p in c["prerequisites"]:
                print(f"      ← {p['code']} {p['name_th']}")

        # Save prereq JSON
        out = os.path.join(OUTPUT_DIR, f"{program}_prereq.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(unique, f, ensure_ascii=False, indent=2)
        print(f"  Saved → {out}")

        # Merge into main
        prereq_map = {c["code"]: c["prerequisites"] for c in unique}
        merge_into_main(program, prereq_map)

    print("\nDONE")


if __name__ == "__main__":
    main()
