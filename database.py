"""
database.py — รองรับ SQLite (local) และ PostgreSQL (Render+Supabase)
ตั้ง DATABASE_URL → PostgreSQL / ไม่ตั้ง → SQLite
"""
import os, sqlite3, re, json
from datetime import datetime

DATABASE_URL = os.environ.get('DATABASE_URL', '')
IS_PG = bool(DATABASE_URL and 'postgres' in DATABASE_URL)

ALL_PERMISSIONS = [
    ('table_open','เปิด / ปิดโต๊ะ','โต๊ะสนุ๊ก'),
    ('table_cancel','ยกเลิกโต๊ะ (คืน stock)','โต๊ะสนุ๊ก'),
    ('table_move','ย้าย / รวมโต๊ะ','โต๊ะสนุ๊ก'),
    ('set_time','ตั้งเวลาปิดโต๊ะ','โต๊ะสนุ๊ก'),
    ('order_remove','ลบ / ลดรายการออเดอร์','โต๊ะสนุ๊ก'),
    ('bill_history','ดูบิลย้อนหลัง','บิล'),
    ('bill_print','พิมพ์ใบเสร็จ','บิล'),
    ('bill_export','Export CSV บิล','บิล'),
    ('inventory_view','ดูรายการสินค้า / สต๊อก','สินค้า'),
    ('inventory_edit','เพิ่ม / แก้ไข / เติมสต๊อก','สินค้า'),
    ('inventory_cat_delete','ลบหมวดหมู่สินค้า','สินค้า'),
    ('expense_add','บันทึกรายจ่าย','การเงิน'),
    ('expense_view','ดูประวัติรายจ่าย','การเงิน'),
    ('exchange','ระบบแลกเงิน','การเงิน'),
    ('report_view','ดูรายงานสรุปกะ','รายงาน'),
    ('report_export','Export รายงาน CSV','รายงาน'),
    ('payroll_manage','จัดการเงินเดือนพนักงาน','พนักงาน'),
    ('employee_manage','จัดการข้อมูลพนักงาน','พนักงาน'),
    ('permissions_manage','จัดการสิทธิ์พนักงาน','พนักงาน'),
    ('settings_manage','ตั้งค่าระบบ (เรท / ตัดกะ)','ระบบ'),
    ('shift_close','ส่งกะ / ปิดจอ','ระบบ'),
]
DEFAULT_STAFF_PERMISSIONS = [
    'table_open','order_remove','bill_history','bill_print',
    'expense_add','expense_view','exchange','shift_close',
]
SUPER_ROLES = ('admin','owner')

# ─── Lightweight row wrapper ──────────────────────────────────
class Row(dict):
    def __getattr__(self, k):
        try: return self[k]
        except KeyError: raise AttributeError(k)
    def __getitem__(self, k):
        if isinstance(k, int): return list(self.values())[k]
        return super().__getitem__(k)

def _row(r): return Row(dict(r)) if r is not None else None
def _rows(rs): return [Row(dict(r)) for r in rs]

# ─── Cursor wrapper ───────────────────────────────────────────
class Cur:
    def __init__(self, cur, is_pg=False):
        self._c = cur; self._pg = is_pg
    def fetchone(self):  return _row(self._c.fetchone())
    def fetchall(self):  return _rows(self._c.fetchall())
    @property
    def lastrowid(self):
        if self._pg:
            self._c.execute("SELECT lastval()")
            return self._c.fetchone()[0]
        return self._c.lastrowid

# ─── Connection wrapper ───────────────────────────────────────
class Conn:
    def __init__(self, raw, is_pg=False):
        self._raw = raw; self._pg = is_pg

    def _fix(self, sql):
        if not self._pg: return sql
        sql = sql.replace('?','%s')
        sql = re.sub(r'(?i)\bINTEGER PRIMARY KEY\b','SERIAL PRIMARY KEY', sql)
        return sql

    def cursor(self):
        if self._pg:
            import psycopg2.extras
            return self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return self._raw.cursor()

    def execute(self, sql, params=()):
        cur = self.cursor()
        orig = sql.upper()
        s = self._fix(sql)
        if self._pg:
            if 'INSERT OR IGNORE' in orig:
                s = re.sub(r'(?i)INSERT OR IGNORE','INSERT',s)
                if 'ON CONFLICT' not in s.upper(): s += ' ON CONFLICT DO NOTHING'
            elif 'INSERT OR REPLACE' in orig:
                s = re.sub(r'(?i)INSERT OR REPLACE','INSERT',s)
        cur.execute(s, params or ())
        return Cur(cur, self._pg)

    def executemany(self, sql, seq):
        cur = self.cursor()
        s = self._fix(sql)
        if self._pg:
            s = re.sub(r'(?i)INSERT OR IGNORE','INSERT',s)
            s = re.sub(r'(?i)INSERT OR REPLACE','INSERT',s)
        cur.executemany(s, seq)

    def commit(self):   self._raw.commit()
    def close(self):    self._raw.close()
    def rollback(self): self._raw.rollback()

# ─── Public: get connection ───────────────────────────────────
def get_db_connection():
    if IS_PG:
        import psycopg2
        raw = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        return Conn(raw, True)
    raw = sqlite3.connect('g2snooker.db')
    raw.row_factory = sqlite3.Row
    return Conn(raw, False)

# ─── init_db ─────────────────────────────────────────────────
def init_db():
    conn = get_db_connection()

    if IS_PG:
        cur = conn.cursor()
        stmts = [
            "CREATE TABLE IF NOT EXISTS employees (id SERIAL PRIMARY KEY, name TEXT, pin TEXT UNIQUE, role TEXT)",
            "CREATE TABLE IF NOT EXISTS tables_config (id SERIAL PRIMARY KEY, name TEXT, type TEXT, rate_1 REAL)",
            "CREATE TABLE IF NOT EXISTS inventory (id SERIAL PRIMARY KEY, product_name TEXT, price REAL, cost REAL, stock_qty INTEGER, category TEXT)",
            "CREATE TABLE IF NOT EXISTS bills (id SERIAL PRIMARY KEY, bill_no TEXT, table_name TEXT, start_time TEXT, end_time TEXT, time_fee REAL, food_fee REAL, total REAL, cashier TEXT, created_at TEXT, status TEXT, payment_method TEXT DEFAULT 'เงินสด', discount REAL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS bill_items (id SERIAL PRIMARY KEY, bill_id INTEGER, name TEXT, qty INTEGER, price REAL, total REAL)",
            "CREATE TABLE IF NOT EXISTS expenses (id SERIAL PRIMARY KEY, category TEXT, amount REAL, note TEXT, created_by TEXT, created_at TEXT)",
            "CREATE TABLE IF NOT EXISTS exchange_history (id SERIAL PRIMARY KEY, total_amount REAL, bill_100_qty INTEGER, bill_20_qty INTEGER, cashier TEXT, created_at TEXT)",
            "CREATE TABLE IF NOT EXISTS rate_settings (id SERIAL PRIMARY KEY, period_name TEXT, start_hour INTEGER, end_hour INTEGER, hourly_rate REAL)",
            "CREATE TABLE IF NOT EXISTS system_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT)",
            """CREATE TABLE IF NOT EXISTS payroll (id SERIAL PRIMARY KEY, emp_name TEXT, month_year TEXT,
               base_salary REAL DEFAULT 0, working_days INTEGER DEFAULT 26, actual_days INTEGER DEFAULT 26,
               daily_rate REAL DEFAULT 0, ot_hours REAL DEFAULT 0, ot_rate REAL DEFAULT 0,
               ot_amount REAL DEFAULT 0, bonus_amount REAL DEFAULT 0, late_count INTEGER DEFAULT 0,
               late_penalty REAL DEFAULT 0, deduct_late REAL DEFAULT 0, deduct_absent REAL DEFAULT 0,
               deduct_other REAL DEFAULT 0, net_salary REAL DEFAULT 0, created_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS payroll_daily (id SERIAL PRIMARY KEY, emp_name TEXT, work_date TEXT,
               status TEXT DEFAULT 'present', is_late INTEGER DEFAULT 0, ot_hours REAL DEFAULT 0,
               note TEXT DEFAULT '', created_at TEXT, UNIQUE(emp_name, work_date))""",
            "CREATE TABLE IF NOT EXISTS payroll_emp_settings (emp_name TEXT PRIMARY KEY, monthly_base REAL DEFAULT 0, working_days INTEGER DEFAULT 26, ot_rate REAL DEFAULT 0, late_penalty REAL DEFAULT 50)",
            "CREATE TABLE IF NOT EXISTS cancel_logs (id SERIAL PRIMARY KEY, log_type TEXT, table_name TEXT, detail TEXT, cashier TEXT, created_at TEXT)",
            "CREATE TABLE IF NOT EXISTS activity_logs (id SERIAL PRIMARY KEY, action_type TEXT, table_name TEXT, detail TEXT, cashier TEXT, created_at TEXT)",
            "CREATE TABLE IF NOT EXISTS print_queue (id SERIAL PRIMARY KEY, bill_no TEXT, payload TEXT, status TEXT DEFAULT 'pending', created_at TEXT)",
            "CREATE TABLE IF NOT EXISTS discount_periods (id SERIAL PRIMARY KEY, period_name TEXT, start_hour INTEGER, end_hour INTEGER, discount_amount REAL DEFAULT 0, is_active INTEGER DEFAULT 1)",
            "CREATE TABLE IF NOT EXISTS work_shifts (id SERIAL PRIMARY KEY, shift_name TEXT, start_time TEXT, end_time TEXT, color TEXT DEFAULT '#6366f1')",
            "CREATE TABLE IF NOT EXISTS work_schedule (id SERIAL PRIMARY KEY, emp_name TEXT, work_date TEXT, shift_id INTEGER, note TEXT DEFAULT '', UNIQUE(emp_name, work_date, shift_id))",
            "CREATE TABLE IF NOT EXISTS special_holidays (id SERIAL PRIMARY KEY, holiday_date TEXT UNIQUE, description TEXT DEFAULT '')",
            "CREATE TABLE IF NOT EXISTS emp_shift_restrictions (id SERIAL PRIMARY KEY, emp_name TEXT, shift_id INTEGER, UNIQUE(emp_name, shift_id))",
            "CREATE TABLE IF NOT EXISTS active_sessions_db (table_id INTEGER PRIMARY KEY, start_time TEXT, orders TEXT DEFAULT '[]', total_food REAL DEFAULT 0, limit_mins INTEGER DEFAULT 0, note TEXT DEFAULT '')",
            "CREATE TABLE IF NOT EXISTS employee_permissions (id SERIAL PRIMARY KEY, emp_id INTEGER NOT NULL, permission_key TEXT NOT NULL, allowed INTEGER DEFAULT 0, UNIQUE(emp_id, permission_key))",
        ]
        for s in stmts: cur.execute(s)
        conn.commit()
    else:
        c = conn
        c.execute('CREATE TABLE IF NOT EXISTS employees (id INTEGER PRIMARY KEY, name TEXT, pin TEXT UNIQUE, role TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS tables_config (id INTEGER PRIMARY KEY, name TEXT, type TEXT, rate_1 REAL)')
        c.execute('CREATE TABLE IF NOT EXISTS inventory (id INTEGER PRIMARY KEY, product_name TEXT, price REAL, cost REAL, stock_qty INTEGER, category TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS bills (id INTEGER PRIMARY KEY, bill_no TEXT, table_name TEXT, start_time TEXT, end_time TEXT, time_fee REAL, food_fee REAL, total REAL, cashier TEXT, created_at TEXT, status TEXT, payment_method TEXT DEFAULT \'เงินสด\', discount REAL DEFAULT 0)')
        c.execute('CREATE TABLE IF NOT EXISTS bill_items (id INTEGER PRIMARY KEY, bill_id INTEGER, name TEXT, qty INTEGER, price REAL, total REAL)')
        c.execute('CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY, category TEXT, amount REAL, note TEXT, created_by TEXT, created_at TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS exchange_history (id INTEGER PRIMARY KEY, total_amount REAL, bill_100_qty INTEGER, bill_20_qty INTEGER, cashier TEXT, created_at TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS rate_settings (id INTEGER PRIMARY KEY, period_name TEXT, start_hour INTEGER, end_hour INTEGER, hourly_rate REAL)')
        c.execute('CREATE TABLE IF NOT EXISTS system_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT)')
        c.execute('''CREATE TABLE IF NOT EXISTS payroll (
            id INTEGER PRIMARY KEY, emp_name TEXT, month_year TEXT,
            base_salary REAL DEFAULT 0, working_days INTEGER DEFAULT 26, actual_days INTEGER DEFAULT 26,
            daily_rate REAL DEFAULT 0, ot_hours REAL DEFAULT 0, ot_rate REAL DEFAULT 0,
            ot_amount REAL DEFAULT 0, bonus_amount REAL DEFAULT 0, late_count INTEGER DEFAULT 0,
            late_penalty REAL DEFAULT 0, deduct_late REAL DEFAULT 0, deduct_absent REAL DEFAULT 0,
            deduct_other REAL DEFAULT 0, net_salary REAL DEFAULT 0, created_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS payroll_daily (
            id INTEGER PRIMARY KEY, emp_name TEXT, work_date TEXT,
            status TEXT DEFAULT 'present', is_late INTEGER DEFAULT 0,
            ot_hours REAL DEFAULT 0, note TEXT DEFAULT '', created_at TEXT,
            UNIQUE(emp_name, work_date))''')
        c.execute('CREATE TABLE IF NOT EXISTS payroll_emp_settings (emp_name TEXT PRIMARY KEY, monthly_base REAL DEFAULT 0, working_days INTEGER DEFAULT 26, ot_rate REAL DEFAULT 0, late_penalty REAL DEFAULT 50)')
        c.execute('''CREATE TABLE IF NOT EXISTS cancel_logs (
            id INTEGER PRIMARY KEY, log_type TEXT,
            table_name TEXT, detail TEXT,
            cashier TEXT, created_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY, action_type TEXT,
            table_name TEXT, detail TEXT,
            cashier TEXT, created_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS print_queue (
            id INTEGER PRIMARY KEY, bill_no TEXT,
            payload TEXT, status TEXT DEFAULT 'pending',
            created_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS discount_periods (
            id INTEGER PRIMARY KEY, period_name TEXT,
            start_hour INTEGER, end_hour INTEGER,
            discount_amount REAL DEFAULT 0, is_active INTEGER DEFAULT 1)''')
        c.execute('''CREATE TABLE IF NOT EXISTS work_shifts (
            id INTEGER PRIMARY KEY, shift_name TEXT,
            start_time TEXT, end_time TEXT, color TEXT DEFAULT '#6366f1')''')
        c.execute('''CREATE TABLE IF NOT EXISTS work_schedule (
            id INTEGER PRIMARY KEY, emp_name TEXT, work_date TEXT,
            shift_id INTEGER, note TEXT DEFAULT '',
            UNIQUE(emp_name, work_date, shift_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS special_holidays (
            id INTEGER PRIMARY KEY, holiday_date TEXT UNIQUE, description TEXT DEFAULT '')''')
        c.execute('''CREATE TABLE IF NOT EXISTS emp_shift_restrictions (
            id INTEGER PRIMARY KEY, emp_name TEXT, shift_id INTEGER, UNIQUE(emp_name, shift_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_sessions_db (
            table_id INTEGER PRIMARY KEY, start_time TEXT,
            orders TEXT DEFAULT '[]', total_food REAL DEFAULT 0, limit_mins INTEGER DEFAULT 0, note TEXT DEFAULT '')''')
        c.execute('''CREATE TABLE IF NOT EXISTS employee_permissions (
            id INTEGER PRIMARY KEY, emp_id INTEGER NOT NULL,
            permission_key TEXT NOT NULL, allowed INTEGER DEFAULT 0,
            UNIQUE(emp_id, permission_key))''')
        # migration
        raw_cur = conn._raw.cursor()
        existing = [r[1] for r in raw_cur.execute("PRAGMA table_info(payroll)").fetchall()]
        for col,typ in [('working_days','INTEGER'),('actual_days','INTEGER'),('daily_rate','REAL'),
                        ('ot_hours','REAL'),('ot_rate','REAL'),('late_count','INTEGER'),
                        ('late_penalty','REAL'),('deduct_absent','REAL')]:
            if col not in existing:
                c.execute(f"ALTER TABLE payroll ADD COLUMN {col} {typ} DEFAULT 0")
        c.commit()

    # ── Seed ────────────────────────────────────────────────────
    def ig(sql, params):
        if IS_PG:
            s = re.sub(r'(?i)INSERT OR IGNORE','INSERT', sql.replace('?','%s'))
            if 'ON CONFLICT' not in s.upper(): s += ' ON CONFLICT DO NOTHING'
            cur = conn.cursor(); cur.execute(s, params)
        else:
            conn.execute(sql, params)

    # Only seed employees if table is completely empty
    emp_count = conn.execute("SELECT COUNT(*) as n FROM employees").fetchone()
    if not emp_count or emp_count['n'] == 0:
        ig("INSERT OR IGNORE INTO employees (name,pin,role) VALUES (?,?,?)", ('Owner (เจ้าของร้าน)','0000','owner'))
        ig("INSERT OR IGNORE INTO employees (name,pin,role) VALUES (?,?,?)", ('ผู้จัดการ','9999','admin'))
        ig("INSERT OR IGNORE INTO employees (name,pin,role) VALUES (?,?,?)", ('น้องสมชาย (แคชเชียร์)','1234','staff'))

    if IS_PG:
        staff = Cur(conn.cursor(), True)
        staff._c.execute("SELECT id FROM employees WHERE pin=%s", ('1234',))
        staff = staff.fetchone()
    else:
        staff = conn.execute("SELECT id FROM employees WHERE pin=?", ('1234',)).fetchone()

    if staff:
        for pk in DEFAULT_STAFF_PERMISSIONS:
            ig("INSERT OR IGNORE INTO employee_permissions (emp_id,permission_key,allowed) VALUES (?,?,1)", (staff['id'], pk))

    for k,v in [('day_cutoff_time','06:00'),('starting_cash','2000'),('late_penalty_default','50')]:
        ig("INSERT OR IGNORE INTO system_settings (setting_key,setting_value) VALUES (?,?)", (k,v))

    if IS_PG:
        cur2 = conn.cursor(); cur2.execute("SELECT COUNT(*) as n FROM rate_settings")
        cnt = Row(cur2.fetchone())
    else:
        cnt = conn.execute("SELECT COUNT(*) as n FROM rate_settings").fetchone()
    if not cnt or cnt['n'] == 0:
        rates = [('ช่วงเช้า (08:00-18:00)',8,18,120.0),('ช่วงค่ำ (18:00-00:00)',18,24,180.0),('รอบดึก (00:00-08:00)',0,8,150.0)]
        conn.executemany("INSERT INTO rate_settings (period_name,start_hour,end_hour,hourly_rate) VALUES (?,?,?,?)", rates)

    if IS_PG:
        cur3 = conn.cursor(); cur3.execute("SELECT COUNT(*) as n FROM tables_config")
        cnt2 = Row(cur3.fetchone())
    else:
        cnt2 = conn.execute("SELECT COUNT(*) as n FROM tables_config").fetchone()
    if not cnt2 or cnt2['n'] == 0:
        for i in range(1,11):
            t = 'snooker' if i<=5 else 'food'
            ig("INSERT OR IGNORE INTO tables_config (name,type,rate_1) VALUES (?,?,?)", (f"โต๊ะ {i}", t, 180.0 if t=='snooker' else 0.0))

    if IS_PG:
        cur4 = conn.cursor(); cur4.execute("SELECT COUNT(*) as n FROM inventory")
        cnt3 = Row(cur4.fetchone())
    else:
        cnt3 = conn.execute("SELECT COUNT(*) as n FROM inventory").fetchone()
    if not cnt3 or cnt3['n'] == 0:
        items = [('น้ำเปล่า',15,7,100,'เครื่องดื่ม'),('เบียร์สิงห์',90,65,48,'เครื่องดื่ม'),('ข้าวผัดหมู',70,35,50,'อาหาร')]
        conn.executemany("INSERT INTO inventory (product_name,price,cost,stock_qty,category) VALUES (?,?,?,?,?)", items)

    conn.commit(); conn.close()
    print(f"✅ [G2SNOOKER] Ready — {'PostgreSQL ☁️' if IS_PG else 'SQLite 💾'}")

if __name__ == '__main__': init_db()