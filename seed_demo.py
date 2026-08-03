# -*- coding: utf-8 -*-
"""
seed_demo.py — เติม/รีเซ็ตข้อมูลตัวอย่างสำหรับ "เดโม่" ให้ลูกค้าทดลองเล่น

⚠️ ห้ามรันสคริปต์นี้กับฐานข้อมูลของร้านจริง (G2 Snooker ตัวจริง) เด็ดขาด
   เพราะจะ "ลบข้อมูลจริงทั้งหมด" แล้วแทนที่ด้วยข้อมูลตัวอย่าง

วิธีใช้:
  รันครั้งแรกตอน deploy เดโม่:   python3 seed_demo.py
  รีเซ็ตข้อมูลเดโม่ใหม่ทีหลัง:    python3 seed_demo.py --confirm-reset
  (หรือเรียกผ่าน endpoint /api/demo/reset ที่ผูกไว้ใน app.py — ทำงานเฉพาะตอน
   ตั้ง ENV DEMO_MODE=true เท่านั้น กันเผลอรันกับร้านจริง)
"""
import os
import sys
import random
from datetime import datetime, timedelta

from database import get_db_connection, init_db, IS_PG

TABLES_TO_WIPE = [
    "bill_items", "bills", "expenses", "exchange_history",
    "cancel_logs", "activity_logs", "print_queue",
    "work_schedule", "employee_permissions",
    "active_sessions_db", "employees", "tables_config",
    "inventory", "rate_settings", "discount_periods",
    "work_shifts", "system_settings",
]

DEMO_EMPLOYEES = [
    # (name, pin, role)
    ("เจ้าของร้าน (Demo)", "0000", "owner"),
    ("แคชเชียร์ Demo",     "1111", "cashier"),
    ("พนักงาน Demo",       "2222", "staff"),
]

DEMO_TABLES = [
    ("โต๊ะ 1", "snooker", 120),
    ("โต๊ะ 2", "snooker", 120),
    ("โต๊ะ 3", "snooker", 150),
    ("โต๊ะ 4", "snooker", 150),
    ("โต๊ะ VIP", "snooker", 200),
]

DEMO_RATES = [
    ("ช่วงเช้า (10:00-17:00)", 10, 17, 100),
    ("ช่วงเย็น (17:00-24:00)", 17, 24, 130),
]

DEMO_MENU = [
    # (product_name, price, cost, stock_qty, category)
    ("น้ำเปล่า", 10, 5, 100, "เครื่องดื่ม"),
    ("น้ำอัดลม", 15, 8, 80, "เครื่องดื่ม"),
    ("โซดา", 15, 7, 80, "เครื่องดื่ม"),
    ("น้ำแข็ง", 10, 3, 100, "เครื่องดื่ม"),
    ("กาแฟเย็น", 25, 12, 50, "เครื่องดื่ม"),
    ("ชาไทย", 25, 12, 50, "เครื่องดื่ม"),
    ("เบียร์สิงห์", 65, 45, 40, "แอลกอฮอล์"),
    ("เบียร์ช้าง", 60, 42, 40, "แอลกอฮอล์"),
    ("มาม่าผัด", 30, 15, 30, "อาหาร"),
    ("ข้าวไข่เจียว", 35, 18, 30, "อาหาร"),
    ("เฟรนช์ฟรายส์", 40, 20, 25, "อาหาร"),
    ("ปีกไก่ทอด", 50, 28, 25, "อาหาร"),
]


def _now():
    return datetime.now()


def seed(reset=True, verbose=True):
    def log(msg):
        if verbose:
            print(msg)

    conn = get_db_connection()
    init_db()  # ให้แน่ใจว่าตารางทั้งหมดถูกสร้างครบก่อน (เผื่อ DB ใหม่เอี่ยม)
    # หมายเหตุ: init_db() จะเติมพนักงาน default ไว้ให้ (PIN 0000/9999/1234)
    # เราล้างทิ้งเสมอก่อนเติมข้อมูลเดโม่ กันชนกับ PIN ตัวอย่างของเราเอง

    log("🧹 กำลังล้างข้อมูลเดโม่เดิม...")
    for t in TABLES_TO_WIPE:
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception as e:
            log(f"  [ข้าม] {t}: {e}")
    conn.commit()

    log("👤 กำลังเพิ่มพนักงานตัวอย่าง...")
    emp_ids = {}
    for name, pin, role in DEMO_EMPLOYEES:
        cur = conn.execute(
            "INSERT INTO employees (name,pin,role) VALUES (?,?,?)",
            (name, pin, role),
        )
        try:
            emp_ids[name] = cur.lastrowid
        except Exception:
            pass
    conn.commit()

    log("🎱 กำลังเพิ่มโต๊ะตัวอย่าง...")
    table_ids = []
    for name, ttype, rate in DEMO_TABLES:
        cur = conn.execute(
            "INSERT INTO tables_config (name,type,rate_1) VALUES (?,?,?)",
            (name, ttype, rate),
        )
        try:
            table_ids.append((name, cur.lastrowid))
        except Exception:
            table_ids.append((name, None))
    conn.commit()

    log("💰 กำลังตั้งค่าเรทเวลา...")
    for period_name, sh, eh, rate in DEMO_RATES:
        conn.execute(
            "INSERT INTO rate_settings (period_name,start_hour,end_hour,hourly_rate) VALUES (?,?,?,?)",
            (period_name, sh, eh, rate),
        )
    conn.commit()

    log("🍔 กำลังเพิ่มเมนูตัวอย่าง...")
    for name, price, cost, stock, cat in DEMO_MENU:
        conn.execute(
            "INSERT INTO inventory (product_name,price,cost,stock_qty,category) VALUES (?,?,?,?,?)",
            (name, price, cost, stock, cat),
        )
    conn.commit()

    log("⚙️  กำลังตั้งค่าระบบ (ชื่อร้านเดโม่)...")
    demo_settings = {
        "shop_name": "สนุ๊กเกอร์ ตัวอย่าง (DEMO)",
        "shop_subtitle": "ระบบ POS เดโม่ — ทดลองใช้งานได้ทุกฟังก์ชัน",
        "day_cutoff_time": "06:00",
    }
    for k, v in demo_settings.items():
        conn.execute(
            "INSERT INTO system_settings (setting_key,setting_value) VALUES (?,?) "
            "ON CONFLICT(setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value"
            if IS_PG else
            "INSERT OR REPLACE INTO system_settings (setting_key,setting_value) VALUES (?,?)",
            (k, v),
        )
    conn.commit()

    log("🧾 กำลังสร้างประวัติบิลตัวอย่าง (ย้อนหลัง 3 วัน)...")
    cashier_names = [e[0] for e in DEMO_EMPLOYEES]
    menu_names = [(m[0], m[1]) for m in DEMO_MENU]
    table_names = [t[0] for t in DEMO_TABLES]
    n = 0
    for day_offset in range(3, 0, -1):
        day = _now() - timedelta(days=day_offset)
        num_bills = random.randint(6, 12)
        for _ in range(num_bills):
            hour = random.randint(11, 23)
            minute = random.randint(0, 59)
            start = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            play_minutes = random.choice([30, 45, 60, 90, 120])
            end = start + timedelta(minutes=play_minutes)
            table = random.choice(table_names)
            rate = random.choice([100, 130])
            time_fee = round(play_minutes / 60 * rate, 2)
            food_items = random.sample(menu_names, k=random.randint(0, 3))
            food_fee = sum(p for _, p in food_items)
            total = time_fee + food_fee
            bill_no = f"DEMO{start.strftime('%y%m%d%H%M%S')}{n}"
            cashier = random.choice(cashier_names)
            cur = conn.execute(
                "INSERT INTO bills (bill_no,table_name,start_time,end_time,time_fee,food_fee,total,"
                "cashier,created_at,status,payment_method) VALUES (?,?,?,?,?,?,?,?,?,'ชำระแล้ว',?)",
                (bill_no, table, start.isoformat(), end.isoformat(), time_fee, food_fee, total,
                 cashier, end.isoformat(), random.choice(["เงินสด", "โอนเงิน"])),
            )
            try:
                bid = cur.lastrowid
                for name, price in food_items:
                    conn.execute(
                        "INSERT INTO bill_items (bill_id,name,qty,price,total) VALUES (?,?,?,?,?)",
                        (bid, name, 1, price, price),
                    )
            except Exception:
                pass
            n += 1
    conn.commit()
    log(f"  → สร้างบิลตัวอย่างทั้งหมด {n} รายการ")

    conn.close()
    log("✅ เติมข้อมูลเดโม่เสร็จสมบูรณ์")
    log("")
    log("PIN ตัวอย่างสำหรับทดลอง login:")
    for name, pin, role in DEMO_EMPLOYEES:
        log(f"  {pin}  → {name} ({role})")


if __name__ == "__main__":
    if "--confirm-reset" in sys.argv:
        seed(reset=True)
    else:
        print("⚠️  สคริปต์นี้จะ 'ล้างข้อมูลทั้งหมด' แล้วแทนที่ด้วยข้อมูลตัวอย่าง")
        print("    ใช้กับฐานข้อมูลเดโม่เท่านั้น ห้ามใช้กับฐานข้อมูลร้านจริงเด็ดขาด")
        print("    ถ้าแน่ใจแล้ว รันคำสั่ง: python3 seed_demo.py --confirm-reset")
