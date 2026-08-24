#!/usr/bin/env python3
"""
app.py
======
หน้าเว็บทดสอบ Text-to-SQL chatbot (ห่อ text_to_sql_chatbot.py ด้วย Flask)

พิมพ์คำถามภาษาไทย -> เห็น คำตอบ + SQL ที่โมเดลเขียน + ตารางผลลัพธ์ดิบ ในหน้าเดียว

รัน:
  ตั้งค่า .env (ดู .env.example) แล้ว:
    python app.py
  เปิด browser ที่ http://localhost:5000
"""

import os
import time

from flask import Flask, jsonify, render_template, request

from text_to_sql_chatbot import (
    build_column_value_hints,
    build_schema_description,
    build_system_prompt,
    general_answer,
    generate_sql,
    load_dotenv,
    looks_like_chat,
    phrase_answer,
    run_query,
    sanitize_sql,
)
from assist_logic import (
    classify_status,
    compute_gpa,
    is_multicolumn,
    load_grade_points,
    load_thresholds,
    parse_transcript,
)
from google_auth import register_auth

ENV_FILE = os.environ.get("ENV_FILE", ".env")
load_dotenv(ENV_FILE)

DB_PATH = os.environ.get("CHATBOT_DB_PATH", "chatbot_teach_table.db")

app = Flask(__name__)
register_auth(app)  # ผูก Google OAuth (/auth/login, /auth/callback, /auth/logout, /auth/me)

_system_prompt = None


def get_system_prompt() -> str:
    global _system_prompt
    if _system_prompt is None:
        schema_desc = build_schema_description(DB_PATH)
        value_hints = build_column_value_hints(DB_PATH)
        _system_prompt = build_system_prompt(schema_desc, value_hints)
    return _system_prompt


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/ask", methods=["POST"])
def ask():
    try:
        data = request.get_json(force=True, silent=True) or {}
        question = (data.get("question") or "").strip()
        if not question:
            return jsonify({"error": "กรุณาพิมพ์คำถาม"}), 400

        system_prompt = get_system_prompt()

        # จับเวลาแต่ละขั้นด้วย perf_counter() (Phase 0) เพื่อโชว์ latency บนหน้าเว็บ
        t0 = time.perf_counter()

        # ---- ROUTER: ให้โมเดลตัดสินก่อนว่าเป็นคำถามดึงข้อมูล (SQL) หรือคำถามคุย (CHAT) ----
        try:
            routed = generate_sql(question, system_prompt)
        except Exception as e:
            return jsonify({"error": f"เรียก LLM ไม่สำเร็จ: {e}"}), 502
        t_route = time.perf_counter()

        def chat_reply(note=""):
            """โหมดสนทนา — ให้ Qwen ตอบเองแบบ chatbot (ไม่ผูก SQL)"""
            try:
                answer = general_answer(question, note=note)
            except Exception as e:
                return jsonify({"error": f"ตอบแบบสนทนาไม่สำเร็จ: {e}"}), 502
            t_ans = time.perf_counter()
            return jsonify({
                "answer": answer,
                "mode": "chat",
                "timing": {
                    "route": round(t_route - t0, 3),
                    "answer": round(t_ans - t_route, 3),
                    "total": round(t_ans - t0, 3),
                },
            })

        # 1) โมเดลส่งต่อโหมดสนทนา (CHAT / ตอบไม่ใช่ SELECT)
        if looks_like_chat(routed):
            return chat_reply()

        # 2) โหมดข้อมูล: ตรวจ SQL ให้ปลอดภัยก่อนรัน
        sql, err = sanitize_sql(routed)
        if err:
            return chat_reply()  # SQL ไม่ปลอดภัย/ผิดรูป -> ตกไปโหมดสนทนา

        cols, rows, qerr = run_query(DB_PATH, sql)
        if qerr:
            # SQL รันไม่ผ่าน -> ตอบแบบสนทนาแทน (ยังบอก sql ไว้ debug)
            resp = chat_reply(note="ค้นฐานข้อมูลแล้ว SQL มีข้อผิดพลาด เลยตอบจากความรู้ทั่วไปแทน")
            return resp
        t_query = time.perf_counter()

        try:
            answer = phrase_answer(question, sql, cols, rows)
        except Exception as e:
            answer = f"(เรียบเรียงคำตอบไม่สำเร็จ: {e})"
        t_answer = time.perf_counter()

        timing = {
            "sql": round(t_route - t0, 3),           # LLM เขียน SQL (router)
            "query": round(t_query - t_route, 3),    # รัน query บน DB
            "answer": round(t_answer - t_query, 3),  # LLM เรียบเรียงคำตอบ
            "total": round(t_answer - t0, 3),        # รวมทั้งหมด
        }

        return jsonify({
            "sql": sql,
            "columns": cols,
            "rows": rows,
            "answer": answer,
            "timing": timing,
            "mode": "data",
        })
    except Exception as e:
        return jsonify({"error": f"เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}"}), 500


@app.route("/calc-gpa", methods=["POST"])
def calc_gpa():
    """Phase 2: คำนวณ GPA จาก transcript ที่ paste มา หรือจากรายวิชาที่ผู้ใช้แก้ในตาราง
    body: {"transcript": "<ข้อความ>"}  หรือ  {"courses": [ {code,name,credits,grade,year,semester}, ... ]}
    ใช้ grade_scale + rules จาก DB เป็น single source of truth"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        multicolumn = False
        if isinstance(data.get("courses"), list):
            courses = data["courses"]           # แก้จากตารางแล้วส่งกลับมาคำนวณใหม่
        else:
            transcript = data.get("transcript") or ""
            courses = parse_transcript(transcript)
            multicolumn = is_multicolumn(transcript)

        grade_map = load_grade_points(DB_PATH)
        thresholds = load_thresholds(DB_PATH)
        result = compute_gpa(courses, grade_map)
        status = classify_status(result["overall"]["gpa"], thresholds)

        return jsonify({
            "courses": courses,
            "result": result,
            "status": status,
            "thresholds": thresholds,
            "multicolumn": multicolumn,
        })
    except Exception as e:
        return jsonify({"error": f"คำนวณ GPA ไม่สำเร็จ: {e}"}), 500


@app.route("/scholarships")
def scholarships():
    """Phase 3: คืนรายการทุนทั้งหมด (สำหรับหน้า 'ทุนการศึกษา')"""
    try:
        cols, rows, err = run_query(
            DB_PATH,
            "SELECT name, provider, amount, gpa_requirement, eligibility, deadline, description, url "
            "FROM scholarships ORDER BY gpa_requirement IS NULL, gpa_requirement DESC",
        )
        if err:
            return jsonify({"error": err}), 200
        return jsonify({"scholarships": rows})
    except Exception as e:
        return jsonify({"error": f"โหลดทุนไม่สำเร็จ: {e}"}), 500


@app.route("/dashboard-data")
def dashboard_data():
    """Phase 4: ข้อมูลนักศึกษาในความดูแล + สถานะ (คำนวณสดจากเกณฑ์ rules) + GPA รายเทอม"""
    try:
        _, students, err = run_query(
            DB_PATH,
            "SELECT student_id, name, program_code, module, advisor_name, gpax, credits_earned, status "
            "FROM students ORDER BY gpax",
        )
        if err:
            return jsonify({"error": err}), 200

        _, terms, terr = run_query(
            DB_PATH,
            "SELECT student_id, year, semester, gpa FROM student_term_gpa "
            "ORDER BY student_id, year, semester",
        )
        if terr:
            return jsonify({"error": terr}), 200

        thresholds = load_thresholds(DB_PATH)

        # จัดสถานะเสี่ยงให้นักศึกษาแต่ละคนจาก gpax ตามเกณฑ์ (ไม่ hardcode)
        summary = {"risk": 0, "watch": 0, "honors": 0, "normal": 0, "unknown": 0}
        for s in students:
            st = classify_status(s.get("gpax"), thresholds)
            s["risk_status"] = st
            summary[st["level"]] = summary.get(st["level"], 0) + 1

        # จัดกลุ่ม GPA รายเทอมตาม student_id (ไว้ทำกราฟ)
        term_map = {}
        for t in terms:
            term_map.setdefault(t["student_id"], []).append(
                {"year": t["year"], "semester": t["semester"], "gpa": t["gpa"]}
            )

        return jsonify({
            "students": students,
            "term_gpa": term_map,
            "summary": summary,
            "thresholds": thresholds,
        })
    except Exception as e:
        return jsonify({"error": f"โหลด dashboard ไม่สำเร็จ: {e}"}), 500


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"ไม่พบไฟล์ DB: {DB_PATH}")
    app.run(debug=True, port=5000)
