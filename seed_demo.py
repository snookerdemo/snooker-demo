# -*- coding: utf-8 -*-
"""
seed_demo.py — รีเซ็ตฐานข้อมูล "เดโม่" ให้กลับเป็นร้านเปล่า พร้อมให้ลูกค้าตั้งค่าเอง

ไม่เติมโต๊ะ/ราคา/เมนู/พนักงานตัวอย่างให้ — เหลือไว้แค่บัญชีเจ้าของร้าน 1 บัญชี
(PIN 0000) เพื่อให้ลูกค้าล็อกอินเข้าไปเพิ่มทุกอย่างเองตั้งแต่ต้น เหมือนเปิดร้านใหม่จริง

⚠️ ห้ามรันสคริปต์นี้กับฐานข้อมูลของร้านจริง (G2 Snooker ตัวจริง) เด็ดขาด
   เพราะจะ "ลบข้อมูลจริงทั้งหมด" แล้วเหลือแต่ร้านเปล่า

วิธีใช้:
  รันครั้งแรกตอน deploy เดโม่:   python3 seed_demo.py
  รีเซ็ตข้อมูลเดโม่ใหม่ทีหลัง:    python3 seed_demo.py --confirm-reset
  (หรือเรียกผ่าน endpoint /api/demo/reset ที่ผูกไว้ใน app.py — ทำงานเฉพาะตอน
   ตั้ง ENV DEMO_MODE=true เท่านั้น กันเผลอรันกับร้านจริง — endpoint นี้ยัง
   ถูกเรียกอัตโนมัติทุก DEMO_AUTO_RESET_HOURS ชั่วโมงจาก app.py ด้วย)
"""
import sys
from database import get_db_connection, init_db, IS_PG

# ตารางทั้งหมดที่ต้องล้าง ให้ตรงกับ schema เต็มของ database.py (โค้ดร้านจริง)
TABLES_TO_WIPE = [
    "bill_items", "bills", "expenses", "exchange_history",
    "cancel_logs", "activity_logs", "print_queue",
    "payroll", "payroll_daily", "payroll_emp_settings",
    "work_schedule", "work_shifts", "special_holidays", "emp_shift_restrictions",
    "employee_permissions",
    "stock_check_items", "stock_checks",
    "active_sessions_db", "employees", "tables_config",
    "inventory", "rate_settings", "discount_periods",
    "system_settings",
]

OWNER_ACCOUNT = ("เจ้าของร้าน (Demo)", "0000", "owner")


def seed(reset=True, verbose=True):
    def log(msg):
        if verbose:
            print(msg)

    conn = get_db_connection()
    init_db()  # ให้แน่ใจว่าตารางทั้งหมดถูกสร้างครบก่อน (เผื่อ DB ใหม่เอี่ยม)
    # หมายเหตุ: init_db() จะเติมค่าเริ่มต้นของร้านจริงไว้ให้ (โต๊ะ/ราคา/พนักงาน default)
    # เราล้างทิ้งเสมอเพื่อให้เดโม่เป็น "ร้านเปล่า" ไม่ใช่ร้านที่ตั้งค่าไว้ล่วงหน้า

    log("🧹 กำลังล้างข้อมูลทั้งหมด ให้กลับเป็นร้านเปล่า...")
    for t in TABLES_TO_WIPE:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception as e:
            log(f"  [ข้าม] {t}: {e}")
    conn.commit()

    log("👤 กำลังเพิ่มบัญชีเจ้าของร้าน (เพื่อให้ล็อกอินเข้าไปตั้งค่าเองได้)...")
    name, pin, role = OWNER_ACCOUNT
    conn.execute("INSERT INTO employees (name,pin,role) VALUES (?,?,?)", (name, pin, role))
    conn.commit()
    conn.close()

    log("✅ รีเซ็ตเรียบร้อย — ร้านว่างเปล่า พร้อมให้ลูกค้าเพิ่มโต๊ะ/ราคา/เมนู/พนักงานเอง")
    log("")
    log(f"PIN สำหรับทดลอง login:  {pin}  → {name} ({role})")


if __name__ == "__main__":
    if "--confirm-reset" in sys.argv:
        seed(reset=True)
    else:
        print("⚠️  สคริปต์นี้จะ 'ล้างข้อมูลทั้งหมด' แล้วเหลือแค่ร้านเปล่า + บัญชีเจ้าของร้าน 1 บัญชี")
        print("    ใช้กับฐานข้อมูลเดโม่เท่านั้น ห้ามใช้กับฐานข้อมูลร้านจริงเด็ดขาด")
        print("    ถ้าแน่ใจแล้ว รันคำสั่ง: python3 seed_demo.py --confirm-reset")
