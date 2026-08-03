from flask import Flask, render_template, jsonify, request
from database import get_db_connection, ALL_PERMISSIONS, SUPER_ROLES, init_db, IS_PG
from datetime import datetime, timedelta, timezone
import math, json, os

TZ_OFFSET = timedelta(hours=7)  # UTC+7 Thailand

def round_thb(amount):
    """ปัดเศษสตางค์: >= 0.50 ขึ้น, < 0.50 ตัดทิ้ง"""
    import math
    baht = int(amount)
    satang = amount - baht
    return baht + 1 if satang >= 0.50 else baht

PRICE_MODES = {
    'student': {'label': 'นักศึกษา', 'rate': 80.0},
    'solo':    {'label': 'ซ้อมเดี่ยว', 'rate': 80.0},
}

app = Flask(__name__)

# ── FIRESTORE (สำหรับ Print Agent ที่ร้าน) ─────────────────────
import firebase_admin
from firebase_admin import credentials as fb_credentials, firestore as fb_firestore

_firestore_db = None
def _init_firestore():
    """เชื่อมต่อ Firestore ด้วย Service Account (อ่านจาก env var FIREBASE_SERVICE_ACCOUNT_JSON)"""
    global _firestore_db
    if _firestore_db is not None:
        return _firestore_db
    try:
        raw = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON', '')
        if not raw:
            print("[WARN] FIREBASE_SERVICE_ACCOUNT_JSON ไม่ได้ตั้งค่า — ปิดใช้งาน Firestore print job")
            return None
        cred_dict = json.loads(raw)
        cred = fb_credentials.Certificate(cred_dict)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        _firestore_db = fb_firestore.client()
        print("[OK] Firestore เชื่อมต่อสำเร็จ (print jobs)")
        return _firestore_db
    except Exception as e:
        print(f"[WARN] Firestore init ล้มเหลว: {e}")
        return None

def create_firestore_print_job(bill):
    """สร้าง print job ใหม่ใน Firestore collection 'printJobs' ให้ Agent ที่ร้านหยิบไปพิมพ์"""
    db = _init_firestore()
    if db is None:
        return False, "ยังไม่ได้ตั้งค่า Firebase credentials บนเซิร์ฟเวอร์ (FIREBASE_SERVICE_ACCOUNT_JSON)"
    try:
        doc = {
            "billNo": bill.get("bill_no",""),
            "status": "pending",
            "createdAt": fb_firestore.SERVER_TIMESTAMP,
            "tableName": bill.get("table_name",""),
            "cashier": bill.get("cashier",""),
            "payload": bill,
        }
        db.collection("printJobs").add(doc)
        return True, None
    except Exception as e:
        return False, str(e)

def _get_rate(hr, rates):
    """หาอัตราของชั่วโมงนั้น — รองรับช่วงข้ามเที่ยงคืน"""
    for r in rates:
        sh = r['start_hour']
        eh = r['end_hour']
        if sh < eh:
            if sh <= hr < eh: return r['hourly_rate'], r['period_name']
        elif sh == eh:
            return r['hourly_rate'], r['period_name']
        else:
            if hr >= sh or hr < eh: return r['hourly_rate'], r['period_name']
    return 120.0, 'มาตรฐาน'

def _get_discount(hr, discounts):
    """หาส่วนลดของชั่วโมงนั้น (active เท่านั้น)"""
    for d in discounts:
        if not d['is_active']: continue
        sh = d['start_hour']; eh = d['end_hour']
        if sh < eh:
            if sh <= hr < eh: return d['discount_amount'], d['period_name']
        elif sh == eh:
            return d['discount_amount'], d['period_name']
        else:
            if hr >= sh or hr < eh: return d['discount_amount'], d['period_name']
    return 0.0, ''

def calc_fee(start, end, rates, discounts=[], price_mode=None):
    _, total, _ = calc_fee_breakdown(start, end, rates, discounts, price_mode)
    return total

def calc_fee_breakdown(start, end, rates, discounts=[], price_mode=None):
    if not start or end <= start: return [], 0, 0

    if price_mode and price_mode in PRICE_MODES:
        pm = PRICE_MODES[price_mode]
        mins = (end - start).total_seconds() / 60.0
        fee = round((mins / 60.0) * pm['rate'], 2)
        h = int(mins // 60); m = int(mins % 60)
        breakdown = [{"rate": pm['rate'], "label": f"{pm['label']} {int(pm['rate'])}฿/ชม.",
                      "mins": round(mins,1), "hours_str": f"{h}:{m:02d}",
                      "fee": fee, "is_promo": False, "disc": 0}]
        return breakdown, fee, 0

    hour_blocks = []
    curr = start
    while curr < end:
        next_hour = curr.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        seg_end = min(next_hour, end)
        seg_mins = (seg_end - curr).total_seconds() / 60.0
        thai_hr = (curr + TZ_OFFSET).hour
        hr_rate, pname = _get_rate(thai_hr, rates)
        disc, dname = _get_discount(thai_hr, discounts)
        hour_blocks.append({
            "thai_hr": thai_hr, "rate": hr_rate, "pname": pname,
            "disc": disc, "dname": dname, "mins": seg_mins,
            "block_start": curr
        })
        curr = seg_end

    disc_blocks = {}
    normal_blocks = {}

    for b in hour_blocks:
        rate_key = int(b["rate"])
        if b["disc"] > 0:
            key = (b["dname"], int(b["rate"]), int(b["disc"]))
            if key not in disc_blocks: disc_blocks[key] = 0.0
            disc_blocks[key] += b["mins"]
        else:
            if rate_key not in normal_blocks: normal_blocks[rate_key] = 0.0
            normal_blocks[rate_key] += b["mins"]

    breakdown = []
    total_fee = 0.0
    total_promo_disc = 0.0

    for rate, mins in normal_blocks.items():
        fee = round((mins / 60.0) * rate, 2)
        total_fee += fee
        h = int(mins // 60); m = int(mins % 60)
        breakdown.append({"rate": rate, "label": f"ค่าโต๊ะ {rate}฿/ชม.",
                           "mins": round(mins,1), "hours_str": f"{h}:{m:02d}",
                           "fee": fee, "is_promo": False, "disc": 0})

    for (dname, rate, disc), total_disc_mins in disc_blocks.items():
        full_hours = int(total_disc_mins // 60)
        remainder_mins = total_disc_mins % 60
        if remainder_mins >= 56:
            full_hours += 1
            remainder_mins = 0
        base = round((total_disc_mins / 60.0) * rate, 2)
        promo_amount = round(full_hours * disc, 2)
        total_fee += base
        total_promo_disc += promo_amount
        h = int(total_disc_mins // 60); m = int(total_disc_mins % 60)
        breakdown.append({"rate": rate, "label": f"ค่าโต๊ะ {rate}฿/ชม.",
                           "mins": round(total_disc_mins,1), "hours_str": f"{h}:{m:02d}",
                           "fee": base, "is_promo": True,
                           "disc": promo_amount, "promo_name": dname,
                           "promo_hrs": full_hours})

    net = round(total_fee - total_promo_disc, 2)
    return breakdown, net, total_promo_disc


# ── TELEGRAM NOTIFICATION ─────────────────────────────────────
import urllib.request

def _tg_send_to(token, chat_id, message):
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": message, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[WARN] Telegram send {chat_id}: {e}")

def _get_tg_rooms(event_type):
    try:
        conn = get_db_connection()
        tok = conn.execute("SELECT setting_value FROM system_settings WHERE setting_key='telegram_token'").fetchone()
        rooms_raw = conn.execute("SELECT setting_value FROM system_settings WHERE setting_key='telegram_rooms'").fetchone()
        conn.close()
        token = tok['setting_value'].strip() if tok and tok['setting_value'] else ''
        if not token: return token, []
        rooms = json.loads(rooms_raw['setting_value']) if rooms_raw and rooms_raw['setting_value'] else []
        matched = [r for r in rooms if r.get(event_type)]
        return token, matched
    except Exception as e:
        print(f"[WARN] get_tg_rooms: {e}")
        return '', []

def send_telegram(message, event_type='checkout'):
    token, rooms = _get_tg_rooms(event_type)
    if not token: return
    if not rooms:
        try:
            conn = get_db_connection()
            cid = conn.execute("SELECT setting_value FROM system_settings WHERE setting_key='telegram_chat_id'").fetchone()
            conn.close()
            if cid and cid['setting_value']:
                _tg_send_to(token, cid['setting_value'], message)
        except: pass
        return
    for r in rooms:
        if r.get('chat_id','').strip():
            _tg_send_to(token, r['chat_id'], message)

def send_telegram_cancel(message):
    send_telegram(message, event_type='cancel')

def send_telegram_stock(message):
    send_telegram(message, event_type='stock')

def send_line(message):
    _send_line_raw([{"type":"text","text":message}])

def send_line_cancel(message):
    try:
        conn = get_db_connection()
        settings = {r['setting_key']: r['setting_value'] for r in
                    conn.execute("SELECT setting_key,setting_value FROM system_settings").fetchall()}
        conn.close()
        token = settings.get('line_cancel_token','').strip() or settings.get('line_token','').strip()
        target = settings.get('line_group_id','').strip()
        if not token or not target: return
        url = "https://api.line.me/v2/bot/message/push"
        data = json.dumps({"to": target, "messages": [{"type": "text", "text": message}]}).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        })
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[WARN] LINE cancel notify: {e}")

def send_line_cancel_flex(cancel_type, table_name, detail, cashier, time_str, reason=''):
    icon = "🚫" if cancel_type == "ยกเลิกโต๊ะ" else "🗑️"
    reason_row = [{"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "เหตุผล", "size": "sm", "color": "#ff6b6b", "flex": 2},
                    {"type": "text", "text": reason, "size": "sm", "color": "#ffd166", "flex": 4, "wrap": True}
                ]}] if reason else []
    flex = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#c0392b",
            "contents": [
                {"type": "text", "text": f"{icon} {cancel_type}", "weight": "bold", "size": "lg", "color": "#ffffff"},
                {"type": "text", "text": time_str, "size": "sm", "color": "#ffcccc"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm", "backgroundColor": "#1a0000",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "โต๊ะ", "size": "sm", "color": "#ff6b6b", "flex": 2},
                    {"type": "text", "text": table_name, "size": "sm", "color": "#ffffff", "flex": 4, "weight": "bold"}
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "รายการ", "size": "sm", "color": "#ff6b6b", "flex": 2},
                    {"type": "text", "text": detail, "size": "sm", "color": "#ffffff", "flex": 4, "wrap": True}
                ]},
            ] + reason_row + [
                {"type": "separator", "margin": "sm", "color": "#5a0000"},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "พนักงาน", "size": "sm", "color": "#ff6b6b", "flex": 2},
                    {"type": "text", "text": cashier, "size": "sm", "color": "#ffcccc", "flex": 4}
                ]}
            ]
        }
    }
    try:
        conn = get_db_connection()
        settings = {r['setting_key']: r['setting_value'] for r in
                    conn.execute("SELECT setting_key,setting_value FROM system_settings").fetchall()}
        conn.close()
        token = settings.get('line_cancel_token','').strip() or settings.get('line_token','').strip()
        target = settings.get('line_group_id','').strip()
        if not token or not target: return
        import urllib.request, json
        url = "https://api.line.me/v2/bot/message/push"
        data = json.dumps({"to": target, "messages": [{"type":"flex","altText":f"{icon} {cancel_type} — {table_name}","contents":flex}]}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json","Authorization":f"Bearer {token}"})
        res = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as e:
        print(f"[WARN] LINE cancel flex: {e.code} {e.read().decode()}")
    except Exception as e:
        print(f"[WARN] LINE cancel flex: {e}")

def send_line_bill(bill_data):
    d = bill_data
    pay_icon = "📱" if d.get("payment_method")=="โอน" else "💵"
    order_contents = []
    for o in (d.get("orders") or []):
        order_contents.append({
            "type":"box","layout":"horizontal","contents":[
                {"type":"text","text":o.get("name",""),"size":"sm","color":"#555555","flex":3},
                {"type":"text","text":f"x{o.get('qty',1)}","size":"sm","color":"#aaaaaa","flex":1,"align":"center"},
                {"type":"text","text":f"{o.get('total_price',0):,.0f}฿","size":"sm","color":"#111111","flex":2,"align":"end"}
            ]
        })
    tb_contents = []
    for b in (d.get("time_breakdown") or []):
        tb_contents.append({
            "type":"box","layout":"horizontal","contents":[
                {"type":"text","text":b.get("label","ค่าโต๊ะ"),"size":"sm","color":"#555555","flex":3},
                {"type":"text","text":b.get("hours_str",""),"size":"sm","color":"#aaaaaa","flex":2,"align":"center"},
                {"type":"text","text":f"{b.get('fee',0):,.2f}฿","size":"sm","color":"#111111","flex":2,"align":"end"}
            ]
        })
        if b.get("is_promo") and b.get("disc",0)>0:
            tb_contents.append({
                "type":"box","layout":"horizontal","contents":[
                    {"type":"text","text":b.get("promo_name","โปรโมชั่น"),"size":"xs","color":"#06c755","flex":5},
                    {"type":"text","text":f"-{b.get('disc',0):,.0f}฿","size":"xs","color":"#06c755","flex":2,"align":"end"}
                ]
            })
    all_items = tb_contents + ([{"type":"separator","margin":"sm"}] if (tb_contents and order_contents) else []) + order_contents
    if not all_items:
        all_items = [{"type":"text","text":"ไม่มีรายการ","size":"sm","color":"#aaaaaa"}]
    disc_row = []
    if d.get("discount",0)>0:
        disc_row = [{"type":"box","layout":"horizontal","margin":"sm","contents":[
            {"type":"text","text":"ส่วนลด","size":"sm","color":"#e53e3e","flex":3},
            {"type":"text","text":f"-{d['discount']:,.0f}฿","size":"sm","color":"#e53e3e","flex":2,"align":"end"}
        ]}]
    flex = {
        "type":"bubble",
        "header":{
            "type":"box","layout":"vertical","backgroundColor":"#1a1a2e","contents":[
                {"type":"text","text":"🎱 G2 SNOOKER","weight":"bold","size":"lg","color":"#f6c90e"},
                {"type":"text","text":"ใบเสร็จรับเงิน","size":"sm","color":"#aaaaaa"}
            ]
        },
        "body":{
            "type":"box","layout":"vertical","spacing":"sm","contents":[
                {"type":"box","layout":"horizontal","contents":[
                    {"type":"text","text":"เลขบิล","size":"sm","color":"#aaaaaa","flex":2},
                    {"type":"text","text":d.get("bill_no",""),"size":"sm","color":"#111111","flex":3,"align":"end","weight":"bold"}
                ]},
                {"type":"box","layout":"horizontal","contents":[
                    {"type":"text","text":"โต๊ะ","size":"sm","color":"#aaaaaa","flex":2},
                    {"type":"text","text":d.get("table_name",""),"size":"sm","color":"#111111","flex":3,"align":"end"}
                ]},
                {"type":"box","layout":"horizontal","contents":[
                    {"type":"text","text":"เวลา","size":"sm","color":"#aaaaaa","flex":2},
                    {"type":"text","text":d.get("time_range",""),"size":"sm","color":"#111111","flex":3,"align":"end"}
                ]},
                {"type":"box","layout":"horizontal","contents":[
                    {"type":"text","text":"พนักงาน","size":"sm","color":"#aaaaaa","flex":2},
                    {"type":"text","text":d.get("cashier",""),"size":"sm","color":"#111111","flex":3,"align":"end"}
                ]},
                {"type":"separator","margin":"md"},
                {"type":"box","layout":"vertical","margin":"md","spacing":"xs","contents": all_items},
                {"type":"separator","margin":"md"},
            ]+disc_row+[
                {"type":"box","layout":"horizontal","margin":"md","contents":[
                    {"type":"text","text":"รวมสุทธิ","weight":"bold","size":"lg","flex":3},
                    {"type":"text","text":f"{d.get('total',0):,} ฿","weight":"bold","size":"xl","color":"#f6c90e","flex":3,"align":"end"}
                ]},
                {"type":"box","layout":"horizontal","margin":"sm","contents":[
                    {"type":"text","text":f"{pay_icon} {d.get('payment_method','เงินสด')}","size":"sm","color":"#06c755","flex":5}
                ]}
            ]
        }
    }
    _send_line_raw([{"type":"flex","altText":f"เช็คบิล {d.get('table_name','')} รวม {d.get('total',0):,} ฿","contents":flex}])

def _send_line_raw(messages):
    try:
        conn = get_db_connection()
        s = conn.execute("SELECT setting_value FROM system_settings WHERE setting_key='line_token'").fetchone()
        uid = conn.execute("SELECT setting_value FROM system_settings WHERE setting_key='line_user_id'").fetchone()
        conn.close()
        token = s['setting_value'].strip() if s and s['setting_value'] else ''
        user_id = uid['setting_value'].strip() if uid and uid['setting_value'] else ''
        if not token or not user_id: return
        url = "https://api.line.me/v2/bot/message/push"
        data = json.dumps({"to": user_id, "messages": messages}).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        })
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[LINE ERROR] {e}")

def send_telegram_rooms(message, notify_type='checkout'):
    try:
        conn = get_db_connection()
        tok = conn.execute("SELECT setting_value FROM system_settings WHERE setting_key='telegram_token'").fetchone()
        rooms_raw = conn.execute("SELECT setting_value FROM system_settings WHERE setting_key='telegram_rooms'").fetchone()
        conn.close()
        if not tok or not tok['setting_value']: return
        token = tok['setting_value'].strip()
        rooms = json.loads(rooms_raw['setting_value']) if rooms_raw and rooms_raw['setting_value'] else []
        if not rooms:
            conn2 = get_db_connection()
            cid = conn2.execute("SELECT setting_value FROM system_settings WHERE setting_key='telegram_chat_id'").fetchone()
            conn2.close()
            if cid and cid['setting_value']:
                rooms = [{'chat_id': cid['setting_value'], 'notify_checkout': True, 'notify_cancel': True, 'notify_stock': True}]
        for room in rooms:
            chat_id = room.get('chat_id','').strip()
            if not chat_id: continue
            key_map = {'checkout':'notify_checkout','cancel':'notify_cancel','stock':'notify_stock'}
            if not room.get(key_map.get(notify_type,'notify_checkout'), False): continue
            try:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                data = json.dumps({"chat_id": chat_id, "text": message, "parse_mode": "HTML"}).encode()
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5)
            except Exception as re:
                print(f"[WARN] TG room {chat_id}: {re}")
    except Exception as e:
        print(f"[WARN] send_telegram_rooms: {e}")

def log_activity(action_type, table_name='', detail='', cashier='ไม่ระบุ'):
    try:
        conn = get_db_connection()
        if IS_PG:
            conn.execute("CREATE TABLE IF NOT EXISTS activity_logs (id SERIAL PRIMARY KEY, action_type TEXT, table_name TEXT, detail TEXT, cashier TEXT, created_at TEXT)")
        else:
            conn.execute("CREATE TABLE IF NOT EXISTS activity_logs (id INTEGER PRIMARY KEY, action_type TEXT, table_name TEXT, detail TEXT, cashier TEXT, created_at TEXT)")
        conn.execute("INSERT INTO activity_logs (action_type,table_name,detail,cashier,created_at) VALUES (?,?,?,?,?)",
            (action_type, table_name, detail or '', cashier or 'ไม่ระบุ', datetime.now().isoformat()))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[WARN] log_activity: {e}")

app.secret_key = 'g2snooker_v5_enterprise'

# ── SESSION PERSISTENCE ───────────────────────────────────────
def load_sessions():
    sessions = {}
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM active_sessions_db").fetchall()
    conn.close()
    for r in rows:
        try:
            sessions[r['table_id']] = {
                "active":     True,
                "start":      datetime.fromisoformat(r['start_time']) if r['start_time'] else None,
                "orders":     json.loads(r['orders'] or '[]'),
                "total_food": r['total_food'] or 0,
                "limit_mins": r['limit_mins'] or 0,
                "note":       r['note'] or '',
                "price_mode": (r['price_mode'] if 'price_mode' in r.keys() else '') or '',
            }
        except Exception as e:
            print(f"[WARN] load session table {r['table_id']}: {e}")
    print(f"✅ Loaded {len(sessions)} active table sessions from DB")
    return sessions

def save_session(table_id, sess):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO active_sessions_db (table_id,start_time,orders,total_food,limit_mins,note,price_mode) VALUES (?,?,?,?,?,?,?) ON CONFLICT(table_id) DO UPDATE SET start_time=EXCLUDED.start_time,orders=EXCLUDED.orders,total_food=EXCLUDED.total_food,limit_mins=EXCLUDED.limit_mins,note=EXCLUDED.note,price_mode=EXCLUDED.price_mode",
        (table_id,
         sess['start'].isoformat() if sess.get('start') else None,
         json.dumps(sess.get('orders', []), ensure_ascii=False),
         sess.get('total_food', 0),
         sess.get('limit_mins', 0),
         sess.get('note', ''),
         sess.get('price_mode', ''))
    )
    conn.commit(); conn.close()

def delete_session(table_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM active_sessions_db WHERE table_id=?", (table_id,))
    conn.commit(); conn.close()

active_sessions = {}
_initialized = False

@app.before_request
def startup():
    global active_sessions, _initialized
    if not _initialized:
        try:
            init_db()
            try:
                conn_m = get_db_connection()
                if IS_PG:
                    conn_m.execute("CREATE TABLE IF NOT EXISTS cancel_logs (id SERIAL PRIMARY KEY, log_type TEXT, table_name TEXT, detail TEXT, cashier TEXT, created_at TEXT)")
                else:
                    conn_m.execute("CREATE TABLE IF NOT EXISTS cancel_logs (id INTEGER PRIMARY KEY, log_type TEXT, table_name TEXT, detail TEXT, cashier TEXT, created_at TEXT)")
                conn_m.commit(); conn_m.close()
            except Exception as me:
                print(f"[WARN] cancel_logs migration: {me}")
            try:
                conn_n = get_db_connection()
                if IS_PG:
                    conn_n.execute("ALTER TABLE active_sessions_db ADD COLUMN IF NOT EXISTS note TEXT DEFAULT ''")
                else:
                    existing_cols = [r[1] for r in conn_n._raw.cursor().execute("PRAGMA table_info(active_sessions_db)").fetchall()]
                    if 'note' not in existing_cols:
                        conn_n.execute("ALTER TABLE active_sessions_db ADD COLUMN note TEXT DEFAULT ''")
                conn_n.commit(); conn_n.close()
            except Exception as ne:
                print(f"[WARN] note column migration: {ne}")
            try:
                conn_p = get_db_connection()
                if IS_PG:
                    conn_p.execute("ALTER TABLE bills ADD COLUMN IF NOT EXISTS payment_method TEXT DEFAULT 'เงินสด'")
                else:
                    existing = [r[1] for r in conn_p._raw.cursor().execute("PRAGMA table_info(bills)").fetchall()]
                    if 'payment_method' not in existing:
                        conn_p.execute("ALTER TABLE bills ADD COLUMN payment_method TEXT DEFAULT 'เงินสด'")
                conn_p.commit(); conn_p.close()
            except Exception as pe:
                print(f"[WARN] payment_method migration: {pe}")
            try:
                conn_pm = get_db_connection()
                if IS_PG:
                    conn_pm.execute("ALTER TABLE active_sessions_db ADD COLUMN IF NOT EXISTS price_mode TEXT DEFAULT ''")
                else:
                    existing_pm = [r[1] for r in conn_pm._raw.cursor().execute("PRAGMA table_info(active_sessions_db)").fetchall()]
                    if 'price_mode' not in existing_pm:
                        conn_pm.execute("ALTER TABLE active_sessions_db ADD COLUMN price_mode TEXT DEFAULT ''")
                conn_pm.commit(); conn_pm.close()
            except Exception as pme:
                print(f"[WARN] price_mode column migration: {pme}")
            try:
                conn_pb = get_db_connection()
                if IS_PG:
                    conn_pb.execute("ALTER TABLE bills ADD COLUMN IF NOT EXISTS price_mode TEXT DEFAULT ''")
                else:
                    existing_pb = [r[1] for r in conn_pb._raw.cursor().execute("PRAGMA table_info(bills)").fetchall()]
                    if 'price_mode' not in existing_pb:
                        conn_pb.execute("ALTER TABLE bills ADD COLUMN price_mode TEXT DEFAULT ''")
                conn_pb.commit(); conn_pb.close()
            except Exception as pbe:
                print(f"[WARN] bills price_mode migration: {pbe}")
            try:
                conn_mr = get_db_connection()
                if IS_PG:
                    conn_mr.execute("ALTER TABLE bills ADD COLUMN IF NOT EXISTS member_reward_disc REAL DEFAULT 0")
                else:
                    existing_mr = [r[1] for r in conn_mr._raw.cursor().execute("PRAGMA table_info(bills)").fetchall()]
                    if 'member_reward_disc' not in existing_mr:
                        conn_mr.execute("ALTER TABLE bills ADD COLUMN member_reward_disc REAL DEFAULT 0")
                conn_mr.commit(); conn_mr.close()
            except Exception as mre:
                print(f"[WARN] bills member_reward_disc migration: {mre}")
            try:
                conn_dc = get_db_connection()
                if IS_PG:
                    conn_dc.execute("ALTER TABLE bills ADD COLUMN IF NOT EXISTS discount REAL DEFAULT 0")
                else:
                    existing_dc = [r[1] for r in conn_dc._raw.cursor().execute("PRAGMA table_info(bills)").fetchall()]
                    if 'discount' not in existing_dc:
                        conn_dc.execute("ALTER TABLE bills ADD COLUMN discount REAL DEFAULT 0")
                conn_dc.commit(); conn_dc.close()
            except Exception as dce:
                print(f"[WARN] bills discount migration: {dce}")
            active_sessions = load_sessions()
        except Exception as e:
            print(f"[WARN] DB init failed: {e}")
        _initialized = True

@app.route("/")
def index():
    try: return render_template("index.html")
    except Exception as e: return f"Error: {e}", 500

# ── AUTH ──────────────────────────────────────────────────────
@app.route("/api/login", methods=["POST"])
def login():
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM employees WHERE pin = ?", (request.json.get('pin'),)).fetchone()
    if not user: conn.close(); return jsonify({"status":"error"}), 401
    perms = {}
    if user['role'] not in SUPER_ROLES:
        rows  = conn.execute("SELECT permission_key,allowed FROM employee_permissions WHERE emp_id=?", (user['id'],)).fetchall()
        perms = {r['permission_key']: bool(r['allowed']) for r in rows}
    conn.close()
    return jsonify({"status":"success","name":user['name'],"role":user['role'],"emp_id":user['id'],"permissions":perms})

# ── DEMO (ทำงานเฉพาะตอนตั้ง ENV DEMO_MODE=true บนเซิร์ฟเวอร์เดโม่เท่านั้น) ──
DEMO_MODE = os.environ.get('DEMO_MODE','').lower() in ('1','true','yes')

@app.route("/api/demo/is_demo")
def demo_is_demo():
    return jsonify({"demo_mode": DEMO_MODE})

@app.route("/api/demo/reset", methods=["POST"])
def demo_reset():
    if not DEMO_MODE:
        return jsonify({"status":"error","msg":"ปิดใช้งาน endpoint นี้ (ไม่ใช่โหมดเดโม่)"}), 403
    try:
        import seed_demo
        seed_demo.seed(reset=True, verbose=False)
        return jsonify({"status":"success","msg":"รีเซ็ตข้อมูลเดโม่เรียบร้อย"})
    except Exception as e:
        return jsonify({"status":"error","msg":str(e)}), 500

# ── PERMISSIONS ───────────────────────────────────────────────
@app.route("/api/permissions/list")
def list_permissions():
    return jsonify([{"key":k,"label":l,"group":g} for k,l,g in ALL_PERMISSIONS])

@app.route("/api/permissions", methods=["GET","POST"])
def manage_permissions():
    conn = get_db_connection()
    if request.method == "GET":
        eid  = request.args.get('emp_id')
        rows = conn.execute("SELECT permission_key,allowed FROM employee_permissions WHERE emp_id=?", (eid,)).fetchall()
        conn.close(); return jsonify({r['permission_key']:bool(r['allowed']) for r in rows})
    d = request.json; eid = int(d['emp_id'])
    for k,v in d['permissions'].items():
        conn.execute("INSERT INTO employee_permissions (emp_id,permission_key,allowed) VALUES (?,?,?) ON CONFLICT(emp_id,permission_key) DO UPDATE SET allowed=EXCLUDED.allowed", (eid,k,1 if v else 0))
    conn.commit(); conn.close(); return jsonify({"status":"success"})

# ── TABLES ────────────────────────────────────────────────────
@app.route("/api/tables")
def get_tables():
    try:
        conn  = get_db_connection()
        tabs  = conn.execute("SELECT * FROM tables_config").fetchall()
        rates = conn.execute("SELECT * FROM rate_settings").fetchall()
        try:
            discounts = conn.execute("SELECT * FROM discount_periods ORDER BY id").fetchall()
        except:
            discounts = []
        conn.close()
    except Exception as e:
        print(f"[ERROR] get_tables DB: {e}")
        return jsonify({})
    res = {}
    for t in tabs:
        tid = t['id']
        s   = active_sessions.get(tid, {"active":False,"orders":[],"total_food":0,"start":None,"limit_mins":0,"note":"","price_mode":""})
        fee = 0
        if t['type']=='snooker' and s['active'] and s['start']:
            now=datetime.now()
            if s['limit_mins']>0:
                cap=s['start']+timedelta(minutes=s['limit_mins'])
                if now>cap: now=cap
            fee = calc_fee(s['start'], now, rates, discounts, s.get('price_mode'))
        res[tid]={"id":tid,"name":t['name'],"type":t['type'],"active":s['active'],"orders":s['orders'],
                  "total_food":s.get('total_food',0),
                  "start":s['start'].isoformat() if s['start'] else None,
                  "start_ts":s['start'].timestamp() if s['start'] else None,
                  "limit_mins":s.get('limit_mins',0),"current_time_fee":round(fee,2),"note":s.get('note',''),
                  "price_mode":s.get('price_mode',''),
                  "price_mode_label":PRICE_MODES.get(s.get('price_mode',''),{}).get('label','')}
    return jsonify(res)

@app.route("/api/start/<int:tid>", methods=["POST"])
def start_table(tid):
    d = request.json or {}
    price_mode = d.get('price_mode','') or ''
    if price_mode and price_mode not in PRICE_MODES: price_mode = ''
    cashier = d.get('cashier','ไม่ระบุ')
    sess = {"active":True,"start":datetime.now(),"orders":[],"total_food":0,"limit_mins":0,"note":"","price_mode":price_mode}
    active_sessions[tid] = sess
    save_session(tid, sess)
    send_relay(tid, "on")
    try:
        conn_t = get_db_connection()
        t_row = conn_t.execute("SELECT name FROM tables_config WHERE id=?", (tid,)).fetchone()
        conn_t.close()
        t_name = t_row['name'] if t_row else f"โต๊ะ {tid}"
        pm_label = PRICE_MODES.get(price_mode,{}).get('label','ปกติ') if price_mode else 'ปกติ'
        log_activity('เปิดโต๊ะ', t_name, f"โหมดราคา: {pm_label}", cashier)
    except Exception as le:
        print(f"[WARN] log_activity start_table: {le}")
    return jsonify({"status":"success"})

@app.route("/api/table/set_time", methods=["POST"])
def set_time():
    d=request.json; tid=int(d['table_id'])
    if tid in active_sessions:
        active_sessions[tid]['limit_mins']=int(d['minutes'])
        save_session(tid, active_sessions[tid])
        return jsonify({"status":"success"})
    return jsonify({"status":"error"}),400

@app.route("/api/table/set_price_mode", methods=["POST"])
def set_price_mode():
    d=request.json; tid=int(d['table_id']); mode=d.get('price_mode','') or ''
    if mode and mode not in PRICE_MODES:
        return jsonify({"status":"error","msg":"โหมดราคาไม่ถูกต้อง"}),400
    if tid in active_sessions:
        active_sessions[tid]['price_mode']=mode
        save_session(tid, active_sessions[tid])
        return jsonify({"status":"success","price_mode":mode})
    return jsonify({"status":"error","msg":"ไม่พบ session"}),400

@app.route("/api/table/action", methods=["POST"])
def table_action():
    d=request.json; action=d.get('action'); src=int(d.get('source')); dst=int(d.get('target',0))
    if action=='cancel' and src in active_sessions:
        conn=get_db_connection()
        tab=conn.execute("SELECT name FROM tables_config WHERE id=?",(src,)).fetchone()
        tab_name=tab['name'] if tab else f"โต๊ะ {src}"
        orders_snap=list(active_sessions[src]['orders'])
        for o in orders_snap:
            conn.execute("UPDATE inventory SET stock_qty=stock_qty+? WHERE id=?",(o['qty'],int(o['id'])))
        detail=", ".join([f"{o['name']} x{o['qty']}" for o in orders_snap]) if orders_snap else "ไม่มีออเดอร์"
        cashier=d.get('cashier','ไม่ระบุ')
        reason=d.get('reason','')
        try:
            if IS_PG:
                conn.execute("CREATE TABLE IF NOT EXISTS cancel_logs (id SERIAL PRIMARY KEY, log_type TEXT, table_name TEXT, detail TEXT, cashier TEXT, created_at TEXT)")
            else:
                conn.execute("CREATE TABLE IF NOT EXISTS cancel_logs (id INTEGER PRIMARY KEY, log_type TEXT, table_name TEXT, detail TEXT, cashier TEXT, created_at TEXT)")
            detail_with_reason = f"{detail} | เหตุผล: {reason}" if reason else detail
            conn.execute("INSERT INTO cancel_logs (log_type,table_name,detail,cashier,created_at) VALUES (?,?,?,?,?)",
                ('ยกเลิกโต๊ะ', tab_name, detail_with_reason, cashier, datetime.now().isoformat()))
        except Exception as le:
            print(f"[WARN] cancel_log insert: {le}")
        conn.commit(); conn.close()
        try:
            detail_txt = f"{detail} | เหตุผล: {reason}" if reason else detail
            log_activity('ยกเลิกโต๊ะ', tab_name, detail_txt, cashier)
        except Exception as le2:
            print(f"[WARN] log_activity cancel: {le2}")
        active_sessions.pop(src); delete_session(src)
        send_relay(src, 'off')
        now_th = (datetime.now() + TZ_OFFSET).strftime('%H:%M')
        send_telegram_cancel(f"🚫 <b>ยกเลิกโต๊ะ</b>\nโต๊ะ: {tab_name}\nรายการ: {detail}\nพนักงาน: {cashier}\nเวลา: {now_th}")
        send_line_cancel_flex("ยกเลิกโต๊ะ", tab_name, detail, cashier, now_th, reason)
        return jsonify({"status":"success"})
    elif action=='move' and src in active_sessions and dst not in active_sessions:
        cashier = d.get('cashier','ไม่ระบุ')
        try:
            conn_mv = get_db_connection()
            src_t = conn_mv.execute("SELECT name FROM tables_config WHERE id=?", (src,)).fetchone()
            dst_t = conn_mv.execute("SELECT name FROM tables_config WHERE id=?", (dst,)).fetchone()
            conn_mv.close()
            src_name = src_t['name'] if src_t else f"โต๊ะ {src}"
            dst_name = dst_t['name'] if dst_t else f"โต๊ะ {dst}"
            log_activity('ย้ายโต๊ะ', src_name, f"ย้ายไป {dst_name}", cashier)
        except Exception as le:
            print(f"[WARN] log_activity move: {le}")
        active_sessions[dst]=active_sessions.pop(src)
        delete_session(src); save_session(dst, active_sessions[dst])
        send_relay(src, 'off')
        send_relay(dst, 'on')
        return jsonify({"status":"success"})
    elif action=='merge' and src in active_sessions and dst in active_sessions:
        cashier = d.get('cashier','ไม่ระบุ')
        try:
            conn_mg = get_db_connection()
            src_t = conn_mg.execute("SELECT name FROM tables_config WHERE id=?", (src,)).fetchone()
            dst_t = conn_mg.execute("SELECT name FROM tables_config WHERE id=?", (dst,)).fetchone()
            conn_mg.close()
            src_name = src_t['name'] if src_t else f"โต๊ะ {src}"
            dst_name = dst_t['name'] if dst_t else f"โต๊ะ {dst}"
            log_activity('รวมโต๊ะ', src_name, f"รวมเข้า {dst_name}", cashier)
        except Exception as le:
            print(f"[WARN] log_activity merge: {le}")
        if active_sessions[src]['start'] and active_sessions[dst]['start']:
            active_sessions[dst]['start']-=(datetime.now()-active_sessions[src]['start'])
        for so in active_sessions[src]['orders']:
            fd=next((d for d in active_sessions[dst]['orders'] if int(d['id'])==int(so['id'])),None)
            if fd: fd['qty']+=so['qty']; fd['total_price']+=so['total_price']
            else: active_sessions[dst]['orders'].append(so)
        active_sessions[dst]['total_food']+=active_sessions[src].get('total_food',0)
        active_sessions.pop(src); delete_session(src); save_session(dst, active_sessions[dst])
        send_relay(src, 'off')
        return jsonify({"status":"success"})
    return jsonify({"status":"error"}),400

# ── CHECKOUT ─────────────────────────────────────────────────
@app.route("/api/table/checkout", methods=["POST"])
def checkout():
    try:
        d=request.json; tid=int(d['table_id']); cashier=d['cashier']
        bill_discount=float(d.get('discount',0)); payment_method=d.get('payment_method','เงินสด')
    except Exception as e:
        return jsonify({"status":"error","msg":str(e)}),400
    if tid not in active_sessions: return jsonify({"status":"error","msg":"ไม่พบ session โต๊ะนี้"}),400
    conn=get_db_connection()
    ti=conn.execute("SELECT * FROM tables_config WHERE id=?",(tid,)).fetchone()
    rates=conn.execute("SELECT * FROM rate_settings").fetchall()
    try: discounts=conn.execute("SELECT * FROM discount_periods ORDER BY id").fetchall()
    except Exception: discounts=[]
    sess=active_sessions[tid]; fee=0; end=datetime.now()
    price_mode_in = d.get('price_mode', None)
    price_mode = price_mode_in if price_mode_in is not None else sess.get('price_mode','')
    if price_mode and price_mode not in PRICE_MODES: price_mode = ''
    if sess['limit_mins']>0:
        el=math.ceil((end-sess['start']).total_seconds()/60)
        if el>sess['limit_mins']: end=sess['start']+timedelta(minutes=sess['limit_mins'])
    time_breakdown = []; promo_disc = 0
    if ti['type']=='snooker' and sess['start']:
        time_breakdown, fee, promo_disc = calc_fee_breakdown(sess['start'], end, rates, discounts, price_mode)
    fee=round(fee,2)
    member_reward = bool(d.get('member_reward', False))
    member_reward_disc = 0.0
    if member_reward and fee>0:
        member_reward_disc = round(fee*0.10, 2)
        fee = round(fee-member_reward_disc, 2)
        time_breakdown.append({"rate":0,"label":"🏆 สมาชิกที่ได้รับรางวัล (-10%)","hours_str":"-10%","fee":-member_reward_disc,"is_promo":False,"disc":0})
    subtotal=fee+sess['total_food']; total=round_thb(max(0,subtotal-bill_discount))
    bno=f"B{datetime.now().strftime('%y%m%d%H%M%S')}"
    from database import IS_PG
    if IS_PG:
        import psycopg2.extras
        raw=conn._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        raw.execute("INSERT INTO bills (bill_no,table_name,start_time,end_time,time_fee,food_fee,total,cashier,created_at,status,payment_method,price_mode,member_reward_disc,discount) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'ชำระแล้ว',%s,%s,%s,%s) RETURNING id",
                    (bno,ti['name'],sess['start'].isoformat() if sess['start'] else None,end.isoformat(),fee,sess['total_food'],total,cashier,end.isoformat(),payment_method,price_mode,member_reward_disc,bill_discount))
        bid=raw.fetchone()['id']
        for o in sess['orders']:
            raw.execute("INSERT INTO bill_items (bill_id,name,qty,price,total) VALUES (%s,%s,%s,%s,%s)",(bid,o['name'],o['qty'],o['price'],o['total_price']))
    else:
        raw=conn._raw.cursor()
        raw.execute("INSERT INTO bills (bill_no,table_name,start_time,end_time,time_fee,food_fee,total,cashier,created_at,status,payment_method,price_mode,member_reward_disc,discount) VALUES (?,?,?,?,?,?,?,?,?,'ชำระแล้ว',?,?,?,?)",
                    (bno,ti['name'],sess['start'].isoformat() if sess['start'] else None,end.isoformat(),fee,sess['total_food'],total,cashier,end.isoformat(),payment_method,price_mode,member_reward_disc,bill_discount))
        bid=raw.lastrowid
        for o in sess['orders']:
            raw.execute("INSERT INTO bill_items (bill_id,name,qty,price,total) VALUES (?,?,?,?,?)",(bid,o['name'],o['qty'],o['price'],o['total_price']))
    conn.commit(); conn.close()
    snap=list(sess['orders']); tsnap=sess['start'].isoformat() if sess['start'] else None
    active_sessions.pop(tid); delete_session(tid)
    send_relay(tid, 'off')
    try:
        th_start = (datetime.fromisoformat(tsnap) + TZ_OFFSET).strftime('%H:%M') if tsnap else '-'
        th_end   = (end + TZ_OFFSET).strftime('%H:%M')
        mins_total = int((end - datetime.fromisoformat(tsnap)).total_seconds()/60) if tsnap else 0
        hh, mm = divmod(mins_total, 60)
        pay_icon = '📱' if payment_method == 'โอน' else '💵'
        msg = (f"🎱 <b>G2 SNOOKER — เช็คบิล</b>\n"
               f"โต๊ะ: {ti['name']}\n"
               f"เวลา: {th_start} → {th_end}"
               + (f" ({hh} ชม. {mm} นาที)" if hh > 0 else f" ({mm} นาที)") + "\n"
               f"ค่าโต๊ะ: {fee:,.2f} ฿\n"
               f"ค่าอาหาร: {sess['total_food']:,.2f} ฿\n"
               + (f"ส่วนลด: -{bill_discount:,.2f} ฿\n" if bill_discount > 0 else "")
               + f"{pay_icon} <b>รวมสุทธิ: {total:,} ฿</b>\n"
               f"ช่องทาง: {payment_method}\n"
               f"พนักงาน: {cashier}")
        send_telegram(msg, event_type='checkout')
        send_line_bill({
            "bill_no": bno, "table_name": ti['name'],
            "time_range": f"{th_start} → {th_end}" + (f" ({hh} ชม. {mm} นาที)" if hh > 0 else f" ({mm} นาที)"),
            "cashier": cashier, "payment_method": payment_method,
            "time_fee": fee, "food_fee": sess['total_food'],
            "discount": bill_discount, "total": total,
            "orders": snap, "time_breakdown": time_breakdown
        })
    except Exception as te:
        print(f"[WARN] Telegram notify: {te}")
    try:
        log_activity('ปิดโต๊ะ/เช็คบิล', ti['name'], f"รวม {total:,} ฿ (ค่าโต๊ะ {fee:,.2f} + อาหาร {sess['total_food']:,.2f}) ผ่าน {payment_method}", cashier)
    except Exception as lae:
        print(f"[WARN] log_activity checkout: {lae}")
    return jsonify({"status":"success","bill_no":bno,"bill_id":bid,"table_name":ti['name'],"total":total,
                    "time_fee":fee,"food_fee":sess['total_food'],"discount":bill_discount,"promo_disc":promo_disc,
                    "cashier":cashier,"payment_method":payment_method,
                    "member_reward":member_reward,"member_reward_disc":member_reward_disc,
                    "start_time":tsnap,"end_time":end.isoformat(),"orders":snap,"time_breakdown":time_breakdown})

# ── ORDERS ───────────────────────────────────────────────────
@app.route("/api/menu")
def get_menu():
    return jsonify([dict(i) for i in get_db_connection().execute("SELECT * FROM inventory ORDER BY id").fetchall()])

@app.route("/api/order/add", methods=["POST"])
def add_order():
    d=request.json; tid=int(d['table_id']); iid=int(d['item_id'])
    cashier = d.get('cashier','ไม่ระบุ')
    conn=get_db_connection(); item=conn.execute("SELECT * FROM inventory WHERE id=?",(iid,)).fetchone()
    if item and item['stock_qty']>0:
        conn.execute("UPDATE inventory SET stock_qty=stock_qty-1 WHERE id=?",(iid,)); conn.commit()
        if tid not in active_sessions:
            active_sessions[tid]={"active":True,"start":datetime.now(),"orders":[],"total_food":0,"limit_mins":0,"note":"","price_mode":""}
        orders=active_sessions[tid]['orders']
        fd=next((o for o in orders if int(o['id'])==iid),None)
        if fd: fd['qty']+=1; fd['total_price']=fd['qty']*fd['price']
        else: orders.append({"id":iid,"name":item['product_name'],"price":float(item['price']),"qty":1,"total_price":float(item['price'])})
        active_sessions[tid]['total_food']+=item['price']
        save_session(tid, active_sessions[tid])
        try:
            t_row = conn.execute("SELECT name FROM tables_config WHERE id=?", (tid,)).fetchone()
            t_name = t_row['name'] if t_row else f"โต๊ะ {tid}"
            log_activity('สั่งของ', t_name, f"{item['product_name']} x1 ({item['price']} ฿)", cashier)
        except Exception as le:
            print(f"[WARN] log_activity add_order: {le}")
        conn.close(); return jsonify({"status":"success"})
    conn.close(); return jsonify({"status":"error","msg":"สินค้าหมด"}),400

@app.route("/api/order/remove", methods=["POST"])
def remove_order():
    d=request.json; tid=int(d['table_id']); iid=int(d['item_id'])
    if tid not in active_sessions: return jsonify({"status":"error"}),400
    orders=active_sessions[tid]['orders']
    fd=next((o for o in orders if int(o['id'])==iid),None)
    if not fd: return jsonify({"status":"error"}),400
    conn=get_db_connection()
    conn.execute("UPDATE inventory SET stock_qty=stock_qty+1 WHERE id=?",(iid,))
    tab=conn.execute("SELECT name FROM tables_config WHERE id=?",(tid,)).fetchone()
    tab_name=tab['name'] if tab else f"โต๊ะ {tid}"
    cashier=request.json.get('cashier','ไม่ระบุ')
    try:
        if IS_PG:
            conn.execute("CREATE TABLE IF NOT EXISTS cancel_logs (id SERIAL PRIMARY KEY, log_type TEXT, table_name TEXT, detail TEXT, cashier TEXT, created_at TEXT)")
        else:
            conn.execute("CREATE TABLE IF NOT EXISTS cancel_logs (id INTEGER PRIMARY KEY, log_type TEXT, table_name TEXT, detail TEXT, cashier TEXT, created_at TEXT)")
        conn.execute("INSERT INTO cancel_logs (log_type,table_name,detail,cashier,created_at) VALUES (?,?,?,?,?)",
            ('ลบออเดอร์', tab_name, f"{fd['name']} x1 ({fd['price']} ฿)", cashier, datetime.now().isoformat()))
    except Exception as le:
        print(f"[WARN] cancel_log insert: {le}")
    conn.commit(); conn.close()
    active_sessions[tid]['total_food']=max(0,active_sessions[tid]['total_food']-fd['price'])
    if fd['qty']>1: fd['qty']-=1; fd['total_price']=fd['qty']*fd['price']
    else: orders.remove(fd)
    save_session(tid, active_sessions[tid])
    return jsonify({"status":"success"})

# ── TABLE NOTE ───────────────────────────────────────────────
@app.route("/api/table/note", methods=["POST"])
def set_table_note():
    d = request.json
    tid = int(d['table_id'])
    note = d.get('note', '')
    if tid in active_sessions:
        active_sessions[tid]['note'] = note
        save_session(tid, active_sessions[tid])
        return jsonify({"status":"success"})
    return jsonify({"status":"error", "msg":"ไม่พบ session"}), 400
    try:
        conn = get_db_connection()
        settings = {r['setting_key']: r['setting_value'] for r in
                    conn.execute("SELECT setting_key,setting_value FROM system_settings").fetchall()}
        conn.close()
        token = settings.get('line_token','').strip()
        group_id = settings.get('line_group_id','').strip()
        if not token or not group_id:
            return jsonify({"error":"token หรือ group_id ว่าง","token":bool(token),"group_id":group_id})
        import urllib.error
        url = "https://api.line.me/v2/bot/message/push"
        data = json.dumps({"to": group_id, "messages": [{"type":"text","text":"ทดสอบจาก G2 POS"}]}).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        })
        try:
            res = urllib.request.urlopen(req, timeout=5)
            return jsonify({"status":"success","http":res.status})
        except urllib.error.HTTPError as he:
            body = he.read().decode()
            return jsonify({"status":"error","http":he.code,"body":body})
    except Exception as e:
        return jsonify({"error":str(e)})

@app.route("/api/line/test", methods=["POST"])
def line_test():
    try:
        send_line_bill({"bill_no":"TEST001","table_name":"โต๊ะ 1","time_range":"08:00 → 09:00 (1 ชม. 0 นาที)",
                        "cashier":"ทดสอบ","payment_method":"เงินสด","time_fee":120,"food_fee":0,
                        "discount":0,"total":120,"orders":[],"time_breakdown":[]})
        return jsonify({"status":"success"})
    except Exception as e:
        return jsonify({"status":"error","msg":str(e)}),500

@app.route("/api/telegram/test", methods=["POST"])
def telegram_test():
    try:
        send_telegram("✅ <b>G2 SNOOKER</b> — ทดสอบการแจ้งเตือน Telegram สำเร็จ!")
        return jsonify({"status":"success"})
    except Exception as e:
        return jsonify({"status":"error","msg":str(e)}),500

# ── TABLE MANAGEMENT ──────────────────────────────────────────
@app.route("/api/tables_config", methods=["GET","POST","DELETE"])
def manage_tables_config():
    conn = get_db_connection()
    if request.method == "GET":
        rows = conn.execute("SELECT * FROM tables_config ORDER BY id").fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    if request.method == "POST":
        d = request.json
        name = d.get("name","").strip()
        if not name:
            conn.close(); return jsonify({"status":"error","msg":"กรุณาใส่ชื่อโต๊ะ"}),400
        conn.execute("INSERT INTO tables_config (name,type,rate_1) VALUES (?,?,?)", (name, "food", 0.0))
        conn.commit(); conn.close()
        return jsonify({"status":"success"})
    if request.method == "DELETE":
        tid = int(request.json.get("id"))
        t = conn.execute("SELECT type FROM tables_config WHERE id=?", (tid,)).fetchone()
        if not t:
            conn.close(); return jsonify({"status":"error","msg":"ไม่พบโต๊ะ"}),404
        if t["type"] == "snooker":
            conn.close(); return jsonify({"status":"error","msg":"ไม่สามารถลบโต๊ะสนุ๊กเกอร์ได้"}),400
        conn.execute("DELETE FROM tables_config WHERE id=?", (tid,))
        conn.commit(); conn.close()
        return jsonify({"status":"success"})

# ── WORK SHIFTS ───────────────────────────────────────────────
@app.route("/api/shifts", methods=["GET","POST","DELETE"])
def manage_shifts():
    conn = get_db_connection()
    if IS_PG:
        conn.execute("CREATE TABLE IF NOT EXISTS work_shifts (id SERIAL PRIMARY KEY, shift_name TEXT, start_time TEXT, end_time TEXT, color TEXT DEFAULT '#6366f1')")
    else:
        conn.execute("CREATE TABLE IF NOT EXISTS work_shifts (id INTEGER PRIMARY KEY, shift_name TEXT, start_time TEXT, end_time TEXT, color TEXT DEFAULT '#6366f1')")
    conn.commit()
    if request.method == "POST":
        d = request.json
        if d.get('id'):
            conn.execute("UPDATE work_shifts SET shift_name=?,start_time=?,end_time=?,color=? WHERE id=?",
                (d['shift_name'],d['start_time'],d['end_time'],d.get('color','#6366f1'),int(d['id'])))
        else:
            conn.execute("INSERT INTO work_shifts (shift_name,start_time,end_time,color) VALUES (?,?,?,?)",
                (d['shift_name'],d['start_time'],d['end_time'],d.get('color','#6366f1')))
        conn.commit(); conn.close(); return jsonify({"status":"success"})
    if request.method == "DELETE":
        conn.execute("DELETE FROM work_shifts WHERE id=?", (int(request.json['id']),))
        conn.commit(); conn.close(); return jsonify({"status":"success"})
    rows = conn.execute("SELECT * FROM work_shifts ORDER BY start_time").fetchall()
    conn.close(); return jsonify([dict(r) for r in rows])

# ── WORK SCHEDULE ─────────────────────────────────────────────
@app.route("/api/schedule", methods=["GET","POST","DELETE"])
def manage_schedule():
    conn = get_db_connection()
    if IS_PG:
        conn.execute("CREATE TABLE IF NOT EXISTS work_schedule (id SERIAL PRIMARY KEY, emp_name TEXT, work_date TEXT, shift_id INTEGER, note TEXT DEFAULT '', UNIQUE(emp_name, work_date, shift_id))")
    else:
        conn.execute("CREATE TABLE IF NOT EXISTS work_schedule (id INTEGER PRIMARY KEY, emp_name TEXT, work_date TEXT, shift_id INTEGER, note TEXT DEFAULT '', UNIQUE(emp_name, work_date, shift_id))")
    conn.commit()
    if request.method == "POST":
        d = request.json
        if IS_PG:
            conn.execute("INSERT INTO work_schedule (emp_name,work_date,shift_id,note) VALUES (?,?,?,?) ON CONFLICT(emp_name,work_date,shift_id) DO UPDATE SET note=EXCLUDED.note",
                (d['emp_name'],d['work_date'],int(d['shift_id']),d.get('note','')))
        else:
            conn.execute("INSERT OR REPLACE INTO work_schedule (emp_name,work_date,shift_id,note) VALUES (?,?,?,?)",
                (d['emp_name'],d['work_date'],int(d['shift_id']),d.get('note','')))
        conn.commit(); conn.close(); return jsonify({"status":"success"})
    if request.method == "DELETE":
        d = request.json
        conn.execute("DELETE FROM work_schedule WHERE emp_name=? AND work_date=? AND shift_id=?",
            (d['emp_name'],d['work_date'],int(d['shift_id'])))
        conn.commit(); conn.close(); return jsonify({"status":"success"})
    week = request.args.get('week','')
    end  = request.args.get('end','')
    if week:
        from datetime import datetime as dt2
        we = end if end else (dt2.strptime(week,'%Y-%m-%d')+timedelta(days=29)).strftime('%Y-%m-%d')
        rows = conn.execute("SELECT s.*,sh.shift_name,sh.color,sh.start_time as stime,sh.end_time as etime FROM work_schedule s LEFT JOIN work_shifts sh ON s.shift_id=sh.id WHERE s.work_date>=? AND s.work_date<=? ORDER BY s.work_date,s.emp_name",(week,we)).fetchall()
    else:
        rows = conn.execute("SELECT s.*,sh.shift_name,sh.color,sh.start_time as stime,sh.end_time as etime FROM work_schedule s LEFT JOIN work_shifts sh ON s.shift_id=sh.id ORDER BY s.work_date DESC LIMIT 500").fetchall()
    conn.close(); return jsonify([dict(r) for r in rows])

# ── AUTO SCHEDULE ─────────────────────────────────────────────
# ── AUTO SCHEDULE (วันจันทร์ 10,16,18,02 / ศุกร์-เสาร์เบิ้ล 18) ────────────────────────
@app.route("/api/schedule/auto", methods=["POST"])
def auto_schedule():
    """จัดตาราง 4 คน: ทำ 6 หยุด 1 วันจันทร์บังคับกะ 10,16,18,02 วันศุกร์-เสาร์เบิ้ลกะค่ำ"""
    d = request.json
    week_start = d['week_start']
    
    conn = get_db_connection()
    all_emps = [r['name'] for r in conn.execute("SELECT name FROM employees WHERE role!='owner' ORDER BY id").fetchall()]
    shifts = conn.execute("SELECT * FROM work_shifts").fetchall()
    
    # 1. ค้นหากะจากเวลาเริ่มต้น (10, 16, 18, 02) -> เลิกใช้ 14.00
    s_10 = next((s['id'] for s in shifts if s['start_time'] == '10:00'), None)
    s_16 = next((s['id'] for s in shifts if s['start_time'] == '16:00'), None)
    s_18 = next((s['id'] for s in shifts if s['start_time'] == '18:00'), None)
    s_02 = next((s['id'] for s in shifts if s['start_time'] == '02:00'), None)
    
    if not (s_10 and s_16 and s_18 and s_02):
        conn.close()
        return jsonify({"status":"error", "msg":"กรุณาสร้างกะเวลา 10:00, 16:00, 18:00, 02:00 ให้ครบในเมนูจัดการกะงานก่อนครับ (ส่วนกะ 14:00 กดลบทิ้งได้เลย)"}), 400
        
    if len(all_emps) != 4:
        conn.close()
        return jsonify({"status":"error", "msg":f"ระบบนี้ออกแบบมาสำหรับ 4 คนเป๊ะๆ (ตอนนี้มี {len(all_emps)} คนในระบบ)"}), 400
        
    from datetime import datetime, timedelta
    start_dt = datetime.strptime(week_start, '%Y-%m-%d')
    week_num = start_dt.isocalendar()[1]
    
    # 2. หมุนเวียนบทบาท ทุกคนจะได้สลับกะและสลับวันหยุดในแต่ละสัปดาห์
    offset = week_num % 4
    roles = all_emps[offset:] + all_emps[:offset]
    R1, R2, R3, R4 = roles[0], roles[1], roles[2], roles[3]
    
    # 3. โมเดลตารางงาน (รวม 24 กะ/สัปดาห์)
    # วันหยุด: R1(อังคาร), R2(พุธ), R3(พฤหัส), R4(อาทิตย์)
    template = {
        0: [(R1, s_10), (R2, s_16), (R3, s_18), (R4, s_02)], # จันทร์ (10, 16, 18, 02)
        1: [(R2, s_16), (R3, s_18), (R4, s_02)],             # อังคาร (R1 หยุด)
        2: [(R3, s_16), (R4, s_18), (R1, s_02)],             # พุธ (R2 หยุด)
        3: [(R4, s_16), (R1, s_18), (R2, s_02)],             # พฤหัส (R3 หยุด)
        4: [(R1, s_16), (R2, s_18), (R3, s_18), (R4, s_02)], # ศุกร์ (เสริมคนกะ 18)
        5: [(R2, s_16), (R3, s_18), (R4, s_18), (R1, s_02)], # เสาร์ (เสริมคนกะ 18)
        6: [(R1, s_16), (R2, s_18), (R3, s_02)]              # อาทิตย์ (R4 หยุด)
    }
    
    inserted = 0
    for day_idx in range(7):
        date_str = (start_dt + timedelta(days=day_idx)).strftime('%Y-%m-%d')
        day_shifts = template[day_idx]
        
        for emp_name, shift_id in day_shifts:
            try:
                from database import IS_PG
                if IS_PG:
                    conn.execute("INSERT INTO work_schedule (emp_name,work_date,shift_id,note) VALUES (%s,%s,%s,'auto_v5') ON CONFLICT DO NOTHING", (emp_name, date_str, shift_id))
                else:
                    conn.execute("INSERT OR IGNORE INTO work_schedule (emp_name,work_date,shift_id,note) VALUES (?,?,?,'auto_v5')", (emp_name, date_str, shift_id))
                inserted += 1
            except: pass
            
    conn.commit(); conn.close()
    return jsonify({"status":"success", "inserted":inserted, "summary":f"จัดตารางเรียบร้อย! จันทร์(10,16,18,02) และทุกคนได้หยุด 1 วัน"})

# ── SHOP SCHEDULE (G2snooker rules) ──────────────────────────
@app.route("/api/schedule/shop", methods=["POST"])
def shop_schedule():
    """จัดตารางตามกฎร้าน G2snooker"""
    d = request.json or {}
    week_start   = d.get('week_start')
    days         = int(d.get('days', 30))
    no_overwrite = d.get('no_overwrite', False)

    if not week_start:
        return jsonify({"status":"error","errors":["ไม่ได้ระบุ week_start"]}), 400

    conn = get_db_connection()
    all_shifts = conn.execute("SELECT * FROM work_shifts ORDER BY start_time").fetchall()

    def find_shift(letter):
        letter = letter.upper()
        for s in all_shifts:
            n = s['shift_name'].upper().replace(' ', '')
            if f'กะ{letter}' in n or n.endswith(letter):
                return s
        return None

    sh_a = find_shift('A')
    sh_b = find_shift('B')
    sh_c = find_shift('C')
    sh_d = find_shift('D')
    sh_e = find_shift('E')

    all_emps = conn.execute("SELECT * FROM employees").fetchall()

    import unicodedata as _ud
    def find_emp(*keywords):
        for e in all_emps:
            name_n = _ud.normalize('NFC', e['name'])
            for kw in keywords:
                if kw and _ud.normalize('NFC', kw) in name_n:
                    return e
        return None

    # ดึงชื่อจาก settings ถ้ามี
    try:
        sc_settings = {r['setting_key']: r['setting_value'] for r in
            conn.execute("SELECT setting_key,setting_value FROM system_settings WHERE setting_key LIKE 'sc_emp_%'").fetchall()}
    except:
        sc_settings = {}
    mgr_name   = sc_settings.get('sc_emp_mgr','').strip()
    rot1_name  = sc_settings.get('sc_emp_rot1','').strip()
    rot2_name  = sc_settings.get('sc_emp_rot2','').strip()
    rot3_name  = sc_settings.get('sc_emp_rot3','').strip()
    fixed_name = sc_settings.get('sc_emp_fixed','').strip()
    emp_mgr   = find_emp(mgr_name, 'โต๋', 'ผจก', 'ผู้จัดการ') if mgr_name else find_emp('โต๋', 'ผจก')
    emp_biw   = find_emp(rot1_name, 'บิว') if rot1_name else find_emp('บิว')
    emp_fern  = find_emp(rot2_name, 'เฟิร์น', 'เฟริน์') if rot2_name else find_emp('เฟิร์น', 'เฟริน์')
    emp_nadia = find_emp(rot3_name, 'นาเดียร์') if rot3_name else find_emp('นาเดียร์')
    emp_noy   = find_emp(fixed_name, 'ฟลุ', 'ลุ๊ค') if fixed_name else find_emp('ฟลุ', 'ลุ๊ค')

    errors = []
    for label, obj in [('กะ A',sh_a),('กะ B',sh_b),('กะ C',sh_c),('กะ D',sh_d),('กะ E',sh_e)]:
        if not obj: errors.append(f'ไม่พบ {label} ในระบบ — กรุณาสร้างกะก่อน')
    for label, obj in [('โต๋/ผจก.',emp_mgr),('บิว',emp_biw),(rot2_name or 'เฟิร์น',emp_fern),('นาเดียร์',emp_nadia),(fixed_name or 'ฟลุ๊ค',emp_noy)]:
        if not obj: errors.append(f'ไม่พบพนักงาน "{label}" — ชื่อต้องตรงกัน')
    if errors:
        conn.close()
        return jsonify({"status":"error","errors":errors}), 400

    from datetime import datetime as dt2

    # รอบ D→C→B (3 วันวนซ้ำ)
    # อ้างอิง: จันทร์ 20 เม.ย. 2569 = cycle 0
    REF = dt2(2026, 4, 20)
    ROTATION = [
        (sh_d, sh_c, sh_b),  # cycle 0: บิว=D, เฟริน์=C, นาเดียร์=B
        (sh_c, sh_b, sh_d),  # cycle 1: บิว=C, เฟริน์=B, นาเดียร์=D
        (sh_b, sh_d, sh_c),  # cycle 2: บิว=B, เฟริน์=D, นาเดียร์=C
    ]
    # วันหยุด (weekday 0=จันทร์ ... 6=อาทิตย์)
    OFF = {
        emp_mgr['name']:   0,  # จันทร์
        emp_noy['name']:   3,  # พฤหัส
        emp_fern['name']:  4,  # ศุกร์
        emp_nadia['name']: 5,  # เสาร์
        emp_biw['name']:   6,  # อาทิตย์
    }

    start_dt = dt2.strptime(week_start, '%Y-%m-%d')
    existing = set()
    if no_overwrite:
        end_str = (start_dt + timedelta(days=days-1)).strftime('%Y-%m-%d')
        for r in conn.execute(
            "SELECT emp_name,work_date,shift_id FROM work_schedule WHERE work_date>=? AND work_date<=?",
            (week_start, end_str)
        ).fetchall():
            existing.add((r['emp_name'], r['work_date'], r['shift_id']))

    inserted = 0
    err_log  = []

    def insert(emp_name, date_str, shift_id):
        nonlocal inserted
        if no_overwrite and (emp_name, date_str, shift_id) in existing:
            return
        try:
            if IS_PG:
                conn.execute(
                    "INSERT INTO work_schedule (emp_name,work_date,shift_id,note) VALUES (%s,%s,%s,'shop') ON CONFLICT DO NOTHING",
                    (emp_name, date_str, shift_id))
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO work_schedule (emp_name,work_date,shift_id,note) VALUES (?,?,?,'shop')",
                    (emp_name, date_str, shift_id))
            inserted += 1
        except Exception as e:
            err_log.append(str(e))
            print(f'[INSERT ERR] {e}')

    for i in range(days):
        dt_obj   = start_dt + timedelta(days=i)
        date_str = dt_obj.strftime('%Y-%m-%d')
        dow      = dt_obj.weekday()
        is_mon = (dow == 0)
        is_thu = (dow == 3)
        is_fri = (dow == 4)

        cycle = ((dt_obj - REF).days) % 3
        biw_sh, fern_sh, nadia_sh = ROTATION[cycle]

        # เฟริน์/นาเดียร์ สลับหยุดเสาร์/อาทิตย์
        week_num = ((dt_obj - REF).days) // 7
        if week_num % 2 == 0:
            fern_off_day, nadia_off_day = 5, 6
        else:
            fern_off_day, nadia_off_day = 6, 5

        fern_off  = (dow == fern_off_day)
        nadia_off = (dow == nadia_off_day)

        # วันที่มีคนหยุด 1 คน → ตัดกะ C ออก ใช้ B แทน
        # วันเสาร์/อาทิตย์ มีคนหยุด 1 คน (เฟริน์หรือนาเดียร์)
        is_someone_off = fern_off or nadia_off

        def pick_sh(sh):
            # ถ้าวันนี้มีคนหยุด และกะที่ได้คือ C → เปลี่ยนเป็น B
            # แต่เฉพาะเมื่อบิวไม่ได้ทำ B อยู่แล้ว (ป้องกันกะซ้ำ)
            if is_someone_off and sh['id'] == sh_c['id']:
                if biw_sh['id'] != sh_b['id']:
                    return sh_b
            return sh

        # ── โต๋ กะ A (หยุดจันทร์) ───────────────────────────
        if not is_mon:
            insert(emp_mgr['name'], date_str, sh_a['id'])

        # ── วันจันทร์: บิวเข้ากะ A แทนโต๋ ──────────────────
        if is_mon:
            insert(emp_biw['name'], date_str, sh_a['id'])

        # ── บิว (หยุดศุกร์ ทำ E วันพฤหัส) ──────────────────
        if is_fri:
            pass  # บิวหยุดวันศุกร์
        elif is_thu:
            # พฤหัสบิวทำกะ E (02:00-10:00)
            insert(emp_biw['name'], date_str, sh_e['id'])
        elif not is_mon:
            # วันอื่นทำกะหมุนเวียน
            insert(emp_biw['name'], date_str, biw_sh['id'])  # ไม่ pick_sh ป้องกัน duplicate
        # ── เฟิร์น ────────────────────────────────────────────
        if not fern_off:
            sh = pick_sh(fern_sh)
            if (is_mon and sh['id'] == sh_b['id']) or ((is_thu or is_fri) and sh['id'] == sh_c['id']):
                insert(emp_fern['name'], date_str, biw_sh['id'])  # ย้ายไปกะของบิว
            else:
                insert(emp_fern['name'], date_str, sh['id'])
        # ── นาเดียร์ ──────────────────────────────────────────
        if not nadia_off:
            sh = pick_sh(nadia_sh)
            if (is_mon and sh['id'] == sh_b['id']) or ((is_thu or is_fri) and sh['id'] == sh_c['id']):
                insert(emp_nadia['name'], date_str, biw_sh['id'])  # ย้ายไปกะของบิว
            else:
                insert(emp_nadia['name'], date_str, sh['id'])

        # ── เจมส์ กะ E (หยุดพฤหัสเท่านั้น) ──────────────────
        # ศุกร์บิวทำ E แทน แต่เจมส์ก็ทำ E วันศุกร์ได้ด้วย
        if not is_thu:
            insert(emp_noy['name'], date_str, sh_e['id'])
    conn.commit(); conn.close()
    return jsonify({
        "status":   "success",
        "inserted": inserted,
        "summary":  f"จัด {inserted} slot ใน {days} วัน",
        "errors":   err_log
    })

# ── TABLE NAME EDIT ───────────────────────────────────────────
@app.route("/api/leave_requests", methods=["GET"])
def get_leave_requests():
    conn = get_db_connection()
    try:
        if IS_PG:
            conn.execute("""CREATE TABLE IF NOT EXISTS leave_requests (
                id SERIAL PRIMARY KEY, emp_name TEXT, leave_date TEXT,
                shift_name TEXT, reason TEXT, status TEXT DEFAULT 'pending',
                approved_by TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        else:
            conn.execute("""CREATE TABLE IF NOT EXISTS leave_requests (
                id INTEGER PRIMARY KEY, emp_name TEXT, leave_date TEXT,
                shift_name TEXT, reason TEXT, status TEXT DEFAULT 'pending',
                approved_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        conn.commit()
    except: conn.rollback()
    rows = conn.execute("SELECT * FROM leave_requests ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/leave_requests/<int:lid>", methods=["POST"])
def update_leave_request(lid):
    d = request.json
    status = d.get('status','pending')
    approved_by = d.get('approved_by','')
    conn = get_db_connection()
    try:
        req = conn.execute("SELECT * FROM leave_requests WHERE id=?", (lid,)).fetchone()
        if req and status == 'approved':
            conn.execute("""INSERT INTO payroll_daily (emp_name,work_date,status,is_late,ot_hours,note)
                VALUES (?,?,'leave',0,0,?)
                ON CONFLICT(emp_name,work_date) DO UPDATE SET status='leave',note=EXCLUDED.note""",
                (req['emp_name'], req['leave_date'],
                 f"ลางาน: {req['reason']} (อนุมัติโดย {approved_by})"))
        conn.execute("UPDATE leave_requests SET status=?, approved_by=? WHERE id=?",
            (status, approved_by, lid))
        conn.commit()
        # แจ้งกลุ่ม LINE
        # แจ้งกลุ่ม LINE
        if status in ['approved', 'rejected']:
            try:
                settings = {r["setting_key"]: r["setting_value"] for r in
                    conn.execute("SELECT setting_key,setting_value FROM system_settings").fetchall()}
                tok = settings.get("line_checkin_token","").strip()
                grp = settings.get("line_checkin_group_id","").strip()
                if tok and grp and req:
                    import urllib.request as _ur, json as _js
                    leave_date_fmt = req['leave_date']
                    try:
                        y,m,dd = req['leave_date'].split('-')
                        leave_date_fmt = f"{dd}/{m}/{y}"
                    except: pass
                    if status == 'approved':
                        msg = (f"✅ อนุมัติลางานแล้ว\n{'─'*20}\n"
                               f"👤 {req['emp_name']}\n"
                               f"📅 วันที่: {leave_date_fmt} กะ {req['shift_name']}\n"
                               f"📝 เหตุผล: {req['reason']}\n"
                               f"✍️ อนุมัติโดย: {approved_by}\n\n"
                               f"⚠️ กรุณาเช็คตารางงานของตัวเอง\nอาจมีการเปลี่ยนแปลงครับ")
                    else:
                        msg = (f"❌ คำขอลางานถูกปฏิเสธ\n{'─'*20}\n"
                               f"👤 {req['emp_name']}\n"
                               f"📅 วันที่: {leave_date_fmt} กะ {req['shift_name']}\n"
                               f"📝 เหตุผล: {req['reason']}\n"
                               f"✍️ ปฏิเสธโดย: {approved_by}\n\n"
                               f"กรุณาติดต่อหัวหน้างานครับ")
                    _d = _js.dumps({"to":grp,"messages":[{"type":"text","text":msg}]},
                        ensure_ascii=False).encode("utf-8")
                    _r = _ur.Request("https://api.line.me/v2/bot/message/push", data=_d,
                        headers={"Content-Type":"application/json","Authorization":f"Bearer {tok}"})
                    _ur.urlopen(_r, timeout=5)
            except Exception as e: print(f"[WARN LINE] {e}")
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"status":"error","msg":str(e)}), 500
    conn.close()
    return jsonify({"status":"success"})

@app.route("/api/tables_config/<int:tid>", methods=["PUT"])
def edit_table_name(tid):
    d = request.json
    conn = get_db_connection()
    conn.execute("UPDATE tables_config SET name=? WHERE id=?", (d['name'], tid))
    conn.commit(); conn.close()
    return jsonify({"status":"success"})

# ── RELAY CONTROL (ESP8266) ───────────────────────────────────
import urllib.request as _ureq

def _get_esp_url():
    try:
        conn = get_db_connection()
        r = conn.execute("SELECT setting_value FROM system_settings WHERE setting_key='esp8266_url'").fetchone()
        conn.close()
        return r['setting_value'].strip() if r and r['setting_value'] else None
    except: return None

def send_relay(table_number, state):
    """ส่งคำสั่งเปิด/ปิดไฟโต๊ะผ่าน MQTT (HiveMQ Cloud)"""
    try:
        import paho.mqtt.publish as _mqtt_publish
        import ssl as _ssl
        payload = json.dumps({"table": table_number, "state": state})
        _mqtt_publish.single(
            "g2/relay/cmd",
            payload=payload,
            hostname="1bfcbdfc8ac747fb8e2de47a04bf1d2d.s1.eu.hivemq.cloud",
            port=8883,
            auth={"username": "G2board", "password": "Aa250899"},
            tls={"tls_version": _ssl.PROTOCOL_TLS_CLIENT},
            client_id=f"g2pos-{table_number}",
        )
    except Exception as e:
        print(f"[WARN] Relay MQTT table {table_number}: {e}")

@app.route("/api/relay/test", methods=["POST"])
def relay_test():
    url = _get_esp_url()
    if not url:
        return jsonify({"status":"error","msg":"ยังไม่ได้ตั้งค่า ESP8266 URL"}),400
    try:
        req = _ureq.Request(f"{url}/status")
        res = _ureq.urlopen(req, timeout=3)
        data = json.loads(res.read())
        return jsonify({"status":"success","relay_status":data})
    except Exception as e:
        return jsonify({"status":"error","msg":str(e)}),500

@app.route("/api/relay/control", methods=["POST"])
def relay_control():
    d = request.json
    table = int(d.get("table", 1))
    state = d.get("state", "off")
    send_relay(table, state)
    return jsonify({"status":"success","table":table,"state":state})

# ── SPECIAL HOLIDAYS ─────────────────────────────────────────
@app.route("/api/holidays", methods=["GET","POST","DELETE"])
def manage_holidays():
    conn = get_db_connection()
    if IS_PG:
        conn.execute("CREATE TABLE IF NOT EXISTS special_holidays (id SERIAL PRIMARY KEY, holiday_date TEXT UNIQUE, description TEXT DEFAULT '')")
    else:
        conn.execute("CREATE TABLE IF NOT EXISTS special_holidays (id INTEGER PRIMARY KEY, holiday_date TEXT UNIQUE, description TEXT DEFAULT '')")
    conn.commit()
    if request.method == "POST":
        d = request.json
        if IS_PG:
            conn.execute("INSERT INTO special_holidays (holiday_date,description) VALUES (?,?) ON CONFLICT(holiday_date) DO UPDATE SET description=EXCLUDED.description",
                (d['date'], d.get('description','')))
        else:
            conn.execute("INSERT OR REPLACE INTO special_holidays (holiday_date,description) VALUES (?,?)",
                (d['date'], d.get('description','')))
        conn.commit(); conn.close(); return jsonify({"status":"success"})
    if request.method == "DELETE":
        conn.execute("DELETE FROM special_holidays WHERE holiday_date=?", (request.json['date'],))
        conn.commit(); conn.close(); return jsonify({"status":"success"})
    rows = conn.execute("SELECT * FROM special_holidays ORDER BY holiday_date").fetchall()
    conn.close(); return jsonify([dict(r) for r in rows])

# ── EMP SHIFT RESTRICTIONS ────────────────────────────────────
@app.route("/api/restrictions", methods=["GET","POST","DELETE"])
def manage_restrictions():
    conn = get_db_connection()
    if IS_PG:
        conn.execute("CREATE TABLE IF NOT EXISTS emp_shift_restrictions (id SERIAL PRIMARY KEY, emp_name TEXT, shift_id INTEGER, UNIQUE(emp_name, shift_id))")
    else:
        conn.execute("CREATE TABLE IF NOT EXISTS emp_shift_restrictions (id INTEGER PRIMARY KEY, emp_name TEXT, shift_id INTEGER, UNIQUE(emp_name, shift_id))")
    conn.commit()
    if request.method == "POST":
        d = request.json
        if IS_PG:
            conn.execute("INSERT INTO emp_shift_restrictions (emp_name,shift_id) VALUES (?,?) ON CONFLICT DO NOTHING", (d['emp_name'],int(d['shift_id'])))
        else:
            conn.execute("INSERT OR IGNORE INTO emp_shift_restrictions (emp_name,shift_id) VALUES (?,?)", (d['emp_name'],int(d['shift_id'])))
        conn.commit(); conn.close(); return jsonify({"status":"success"})
    if request.method == "DELETE":
        d = request.json
        conn.execute("DELETE FROM emp_shift_restrictions WHERE emp_name=? AND shift_id=?", (d['emp_name'],int(d['shift_id'])))
        conn.commit(); conn.close(); return jsonify({"status":"success"})
    rows = conn.execute("SELECT * FROM emp_shift_restrictions").fetchall()
    conn.close(); return jsonify([dict(r) for r in rows])

# ── CANCEL LOGS ──────────────────────────────────────────────
@app.route("/api/cancel_logs")
def get_cancel_logs():
    try:
        conn = get_db_connection()
        if IS_PG:
            conn.execute("CREATE TABLE IF NOT EXISTS cancel_logs (id SERIAL PRIMARY KEY, log_type TEXT, table_name TEXT, detail TEXT, cashier TEXT, created_at TEXT)")
        else:
            conn.execute("CREATE TABLE IF NOT EXISTS cancel_logs (id INTEGER PRIMARY KEY, log_type TEXT, table_name TEXT, detail TEXT, cashier TEXT, created_at TEXT)")
        conn.commit()
        df=request.args.get('date','')
        q="SELECT * FROM cancel_logs WHERE 1=1"; p=[]
        if df: q+=" AND created_at LIKE ?"; p.append(f"{df}%")
        q+=" ORDER BY id DESC LIMIT 300"
        rows=[dict(r) for r in conn.execute(q,p).fetchall()]
        conn.close()
        return jsonify(rows)
    except Exception as e:
        print(f"[ERROR] cancel_logs: {e}")
        return jsonify([])

# ── FIRESTORE PRINT (Agent ที่ร้านรับงานผ่าน Firestore realtime) ──
@app.route("/api/print/remote", methods=["POST"])
def print_remote():
    bill = request.json or {}
    ok, err = create_firestore_print_job(bill)
    if ok:
        return jsonify({"status":"success"})
    return jsonify({"status":"error","msg":err}), 500

# ── ACTIVITY LOGS ────────────────────────────────────────────
@app.route("/api/activity_logs")
def get_activity_logs():
    try:
        conn = get_db_connection()
        if IS_PG:
            conn.execute("CREATE TABLE IF NOT EXISTS activity_logs (id SERIAL PRIMARY KEY, action_type TEXT, table_name TEXT, detail TEXT, cashier TEXT, created_at TEXT)")
        else:
            conn.execute("CREATE TABLE IF NOT EXISTS activity_logs (id INTEGER PRIMARY KEY, action_type TEXT, table_name TEXT, detail TEXT, cashier TEXT, created_at TEXT)")
        conn.commit()
        df=request.args.get('date','')
        q="SELECT * FROM activity_logs WHERE 1=1"; p=[]
        if df: q+=" AND created_at LIKE ?"; p.append(f"{df}%")
        q+=" ORDER BY id DESC LIMIT 500"
        rows=[dict(r) for r in conn.execute(q,p).fetchall()]
        conn.close()
        return jsonify(rows)
    except Exception as e:
        print(f"[ERROR] activity_logs: {e}")
        return jsonify([])

# ── INVENTORY ────────────────────────────────────────────────
@app.route("/api/inventory/update", methods=["POST"])
def update_stock():
    d=request.json; conn=get_db_connection()
    cashier = d.get('cashier','ไม่ระบุ')
    item = conn.execute("SELECT product_name FROM inventory WHERE id=?", (int(d['id']),)).fetchone()
    conn.execute("UPDATE inventory SET stock_qty=stock_qty+? WHERE id=?",(int(d['qty']),int(d['id']))); conn.commit(); conn.close()
    try:
        pname = item['product_name'] if item else f"#{d['id']}"
        log_activity('สินค้า/สต็อก', '', f"เติมสต็อก {pname} +{d['qty']} ชิ้น", cashier)
    except Exception as le:
        print(f"[WARN] log_activity update_stock: {le}")
    return jsonify({"status":"success"})

@app.route("/api/inventory/new", methods=["POST"])
def new_product():
    d=request.json; conn=get_db_connection()
    cashier = d.get('cashier','ไม่ระบุ')
    conn.execute("INSERT INTO inventory (product_name,price,cost,stock_qty,category) VALUES (?,?,?,?,?)",
                 (d['name'],float(d['price']),float(d['cost']),int(d['qty']),d['category'])); conn.commit(); conn.close()
    try:
        log_activity('สินค้า/สต็อก', '', f"เพิ่มสินค้าใหม่ {d['name']} ({d['category']}) ราคา {d['price']} ฿ จำนวน {d['qty']}", cashier)
    except Exception as le:
        print(f"[WARN] log_activity new_product: {le}")
    return jsonify({"status":"success"})

@app.route("/api/inventory/<int:item_id>", methods=["DELETE"])
def delete_product(item_id):
    cashier = request.args.get('cashier','ไม่ระบุ')
    conn=get_db_connection()
    item = conn.execute("SELECT product_name FROM inventory WHERE id=?", (item_id,)).fetchone()
    conn.execute("DELETE FROM inventory WHERE id=?",(item_id,)); conn.commit(); conn.close()
    try:
        pname = item['product_name'] if item else f"#{item_id}"
        log_activity('สินค้า/สต็อก', '', f"ลบสินค้า {pname}", cashier)
    except Exception as le:
        print(f"[WARN] log_activity delete_product: {le}")
    return jsonify({"status":"success"})

@app.route("/api/inventory/categories", methods=["GET","DELETE"])
def manage_categories():
    conn=get_db_connection()
    if request.method=="GET":
        rows=conn.execute("SELECT DISTINCT category FROM inventory WHERE category IS NOT NULL AND category!='' ORDER BY category").fetchall()
        conn.close(); return jsonify([r['category'] for r in rows])
    cat=request.json.get('category')
    conn.execute("UPDATE inventory SET category='ทั่วไป' WHERE category=?",(cat,)); conn.commit(); conn.close()
    return jsonify({"status":"success"})

# ── BILLS ────────────────────────────────────────────────────
@app.route("/api/bills")
def get_bills():
    df=request.args.get('date',''); tf=request.args.get('table','')
    q="SELECT * FROM bills WHERE 1=1"; p=[]
    if df: q+=" AND created_at LIKE ?"; p.append(f"{df}%")
    if tf: q+=" AND table_name LIKE ?"; p.append(f"%{tf}%")
    q+=" ORDER BY id DESC LIMIT 200"
    return jsonify([dict(b) for b in get_db_connection().execute(q,p).fetchall()])

@app.route("/api/bills/<int:bid>")
def get_bill_items(bid):
    conn=get_db_connection()
    bill=conn.execute("SELECT * FROM bills WHERE id=?",(bid,)).fetchone()
    items=conn.execute("SELECT * FROM bill_items WHERE bill_id=?",(bid,)).fetchall()
    conn.close(); return jsonify({"bill":dict(bill) if bill else {},"items":[dict(i) for i in items]})

# ── EXPENSES ─────────────────────────────────────────────────
@app.route("/api/expenses", methods=["GET","POST"])
def manage_expenses():
    conn=get_db_connection()
    if request.method=="POST":
        d=request.json
        exp_date=d.get('expense_date','') or datetime.now().strftime('%Y-%m-%d')
        exp_dt=datetime.fromisoformat(exp_date+'T00:00:00') if 'T' not in exp_date else datetime.fromisoformat(exp_date)
        conn.execute("INSERT INTO expenses (category,amount,note,created_by,created_at) VALUES (?,?,?,?,?)",
                     (d['category'],float(d['amount']),d['note'],d['cashier'],exp_dt.isoformat()))
        conn.commit(); conn.close(); return jsonify({"status":"success"})
    # GET: กรองรายการรายจ่ายตาม "รอบกะ" เดียวกับหน้ารายงาน (ตัดยอดตาม day_cutoff_time)
    # แทนที่จะดึง 30 รายการล่าสุดแบบไม่สนวันที่ (ซึ่งทำให้รายการค้ามข้ามกะ)
    r1=conn.execute("SELECT setting_value FROM system_settings WHERE setting_key='day_cutoff_time'").fetchone()
    ct=r1['setting_value'] if r1 else "06:00"
    ch,cm=map(int,ct.split(':'))
    now_th=datetime.now()+TZ_OFFSET
    req_date=request.args.get('date','').strip()
    ss_th=None
    if req_date:
        try:
            base_date=datetime.strptime(req_date,'%Y-%m-%d')
            ss_th=base_date.replace(hour=ch,minute=cm,second=0,microsecond=0)
        except ValueError:
            ss_th=None
    if ss_th is None:
        ctt_th=now_th.replace(hour=ch,minute=cm,second=0,microsecond=0)
        if now_th<ctt_th:
            ss_th=(now_th-timedelta(days=1)).replace(hour=ch,minute=cm,second=0,microsecond=0)
        else:
            ss_th=ctt_th
    se_th=ss_th+timedelta(days=1)
    ss=(ss_th-TZ_OFFSET).strftime('%Y-%m-%d %H:%M:%S')
    se=(se_th-TZ_OFFSET).strftime('%Y-%m-%d %H:%M:%S')
    e=conn.execute("SELECT * FROM expenses WHERE created_at>=? AND created_at<? ORDER BY id DESC",(ss,se)).fetchall()
    conn.close()
    return jsonify([dict(i) for i in e])


# ── STARTING CASH LOG ────────────────────────────────────────
@app.route("/api/starting_cash_log", methods=["GET","POST"])
def starting_cash_log():
    conn = get_db_connection()
    try:
        if IS_PG:
            conn.execute("""CREATE TABLE IF NOT EXISTS starting_cash_log (
                id SERIAL PRIMARY KEY, amount REAL, cashier TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        else:
            conn.execute("""CREATE TABLE IF NOT EXISTS starting_cash_log (
                id INTEGER PRIMARY KEY, amount REAL, cashier TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        conn.commit()
    except: conn.rollback()
    if request.method == "POST":
        d = request.json
        conn.execute("INSERT INTO starting_cash_log (amount,cashier,created_at) VALUES (?,?,?)",
            (float(d.get('amount',0)), d.get('cashier',''), datetime.now().isoformat()))
        conn.commit(); conn.close()
        return jsonify({"status":"success"})
    rows = conn.execute("SELECT * FROM starting_cash_log ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ── EXCHANGE ─────────────────────────────────────────────────
@app.route("/api/exchange", methods=["GET","POST"])
def handle_exchange():
    conn=get_db_connection()
    if request.method=="POST":
        d=request.json
        conn.execute("INSERT INTO exchange_history (total_amount,bill_100_qty,bill_20_qty,cashier,created_at) VALUES (?,?,?,?,?)",
                     (d['amount'],d['qty_100'],d['qty_20'],d['cashier'],datetime.now().isoformat()))
        conn.commit(); conn.close(); return jsonify({"status":"success"})
    h=conn.execute("SELECT * FROM exchange_history ORDER BY id DESC LIMIT 15").fetchall(); conn.close()
    return jsonify([dict(i) for i in h])

# ── DISCOUNT PERIODS ──────────────────────────────────────────
@app.route("/api/discount_periods", methods=["GET","POST"])
def discount_periods():
    conn = get_db_connection()
    if IS_PG:
        conn.execute("CREATE TABLE IF NOT EXISTS discount_periods (id SERIAL PRIMARY KEY, period_name TEXT, start_hour INTEGER, end_hour INTEGER, discount_amount REAL DEFAULT 0, is_active INTEGER DEFAULT 1)")
    else:
        conn.execute("CREATE TABLE IF NOT EXISTS discount_periods (id INTEGER PRIMARY KEY, period_name TEXT, start_hour INTEGER, end_hour INTEGER, discount_amount REAL DEFAULT 0, is_active INTEGER DEFAULT 1)")
    if request.method == "POST":
        rows = request.json
        for r in rows:
            if r.get('id'):
                conn.execute("UPDATE discount_periods SET period_name=?,start_hour=?,end_hour=?,discount_amount=?,is_active=? WHERE id=?",
                    (r['period_name'],int(r['start_hour']),int(r['end_hour']),float(r['discount_amount']),1 if r.get('is_active') else 0,int(r['id'])))
            else:
                conn.execute("INSERT INTO discount_periods (period_name,start_hour,end_hour,discount_amount,is_active) VALUES (?,?,?,?,1)",
                    (r['period_name'],int(r['start_hour']),int(r['end_hour']),float(r['discount_amount'])))
        conn.commit(); conn.close()
        return jsonify({"status":"success"})
    cnt = conn.execute("SELECT COUNT(*) as n FROM discount_periods").fetchone()
    if not cnt or cnt['n']==0:
        conn.execute("INSERT INTO discount_periods (period_name,start_hour,end_hour,discount_amount,is_active) VALUES (?,?,?,?,?)",
            ('ส่วนลดช่วงเช้า (08:00-16:00)',8,16,40.0,1))
        conn.commit()
    rows = conn.execute("SELECT * FROM discount_periods ORDER BY id").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ── RATES ────────────────────────────────────────────────────
@app.route("/api/rates/reset", methods=["POST"])
def reset_rates():
    conn=get_db_connection()
    defaults=[('ช่วงเช้า (08:00-16:00)',8,16,120.0),
              ('ช่วงค่ำ (16:00-02:00)',16,2,120.0),
              ('รอบดึก (02:00-08:00)',2,8,150.0)]
    rows=conn.execute("SELECT id FROM rate_settings ORDER BY id").fetchall()
    for i,row in enumerate(rows):
        if i<len(defaults):
            pname,sh,eh,rate=defaults[i]
            conn.execute("UPDATE rate_settings SET period_name=?,start_hour=?,end_hour=?,hourly_rate=? WHERE id=?",
                         (pname,sh,eh,rate,row['id']))
    conn.commit(); conn.close()
    return jsonify({"status":"success"})

@app.route("/api/rates", methods=["GET","POST"])
def manage_rates():
    conn=get_db_connection()
    if request.method=="POST":
        cashier = request.args.get('cashier','ไม่ระบุ')
        for r in request.json:
            conn.execute("UPDATE rate_settings SET hourly_rate=?,start_hour=?,end_hour=? WHERE id=?",
                         (float(r['rate']),int(r['start_hour']),int(r['end_hour']),int(r['id'])))
        conn.commit(); conn.close()
        try:
            log_activity('ตั้งค่า/ราคา', '', f"แก้ไขเรทราคา {len(request.json)} ช่วงเวลา", cashier)
        except Exception as le:
            print(f"[WARN] log_activity rates: {le}")
        return jsonify({"status":"success"})
    r=conn.execute("SELECT * FROM rate_settings").fetchall(); conn.close()
    return jsonify([dict(i) for i in r])

# ── SETTINGS ─────────────────────────────────────────────────
@app.route("/api/settings", methods=["GET","POST"])
def manage_settings():
    conn=get_db_connection()
    if request.method=="POST":
        cashier = request.args.get('cashier','ไม่ระบุ')
        keys_changed = list(request.json.keys())
        for k,v in request.json.items():
            conn.execute("INSERT INTO system_settings (setting_key,setting_value) VALUES (?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value",(k,str(v)))
        conn.commit(); conn.close()
        try:
            log_activity('ตั้งค่า/ราคา', '', f"แก้ไขตั้งค่า: {', '.join(keys_changed)}", cashier)
        except Exception as le:
            print(f"[WARN] log_activity settings: {le}")
        return jsonify({"status":"success"})
    r=conn.execute("SELECT * FROM system_settings").fetchall(); conn.close()
    return jsonify({i['setting_key']:i['setting_value'] for i in r})

# ── EMPLOYEES ────────────────────────────────────────────────
@app.route("/api/employees", methods=["GET","POST","DELETE"])
def manage_employees():
    conn=get_db_connection()
    if request.method=="GET":
        e=conn.execute("SELECT id,name,pin,role FROM employees ORDER BY id").fetchall(); conn.close()
        return jsonify([dict(i) for i in e])
    if request.method=="POST":
        d=request.json
        try:
            if d.get('id'):
                conn.execute("UPDATE employees SET name=?,pin=?,role=? WHERE id=?",(d['name'],d['pin'],d['role'],int(d['id'])))
            else:
                conn.execute("INSERT INTO employees (name,pin,role) VALUES (?,?,?)",(d['name'],d['pin'],d['role']))
                if d['role']=='staff':
                    from database import DEFAULT_STAFF_PERMISSIONS
                    nid=conn.execute("SELECT id FROM employees WHERE pin=?",(d['pin'],)).fetchone()['id']
                    for pk in DEFAULT_STAFF_PERMISSIONS:
                        conn.execute("INSERT INTO employee_permissions (emp_id,permission_key,allowed) VALUES (?,?,1) ON CONFLICT DO NOTHING",(nid,pk))
            conn.commit(); conn.close()
            return jsonify({"status":"success"})
        except Exception as ex:
            conn.close()
            msg="PIN นี้มีคนใช้แล้ว" if "UNIQUE" in str(ex) else str(ex)
            return jsonify({"status":"error","msg":msg}),400
    if request.method=="DELETE":
        eid=int(request.json['id'])
        conn.execute("DELETE FROM employees WHERE id=?",(eid,))
        conn.execute("DELETE FROM employee_permissions WHERE emp_id=?",(eid,))
        conn.commit(); conn.close(); return jsonify({"status":"success"})

# ── PAYROLL ──────────────────────────────────────────────────
@app.route("/api/payroll/<int:payroll_id>", methods=["PUT","DELETE"])
def edit_delete_payroll(payroll_id):
    conn = get_db_connection()
    if request.method == "DELETE":
        conn.execute("DELETE FROM payroll WHERE id=?", (payroll_id,))
        conn.commit(); conn.close()
        return jsonify({"status":"success"})
    d = request.json
    conn.execute("""UPDATE payroll SET emp_name=?,month_year=?,base_salary=?,working_days=?,actual_days=?,
        daily_rate=?,ot_hours=?,ot_rate=?,ot_amount=?,bonus_amount=?,late_count=?,late_penalty=?,
        deduct_late=?,deduct_absent=?,deduct_other=?,net_salary=? WHERE id=?""",
        (d['emp_name'],d['month_year'],d['base_salary'],d['working_days'],d['actual_days'],
         d['daily_rate'],d['ot_hours'],d['ot_rate'],d['ot_amount'],d['bonus_amount'],
         d['late_count'],d['late_penalty'],d['deduct_late'],d['deduct_absent'],
         d['deduct_other'],d['net_salary'],payroll_id))
    conn.commit(); conn.close()
    return jsonify({"status":"success"})

@app.route("/api/payroll/settings", methods=["GET","POST"])
def payroll_settings():
    conn = get_db_connection()
    if request.method == "POST":
        d = request.json
        conn.execute(
            "INSERT INTO payroll_emp_settings (emp_name,monthly_base,working_days,ot_rate,late_penalty) VALUES (?,?,?,?,?) ON CONFLICT(emp_name) DO UPDATE SET monthly_base=EXCLUDED.monthly_base,working_days=EXCLUDED.working_days,ot_rate=EXCLUDED.ot_rate,late_penalty=EXCLUDED.late_penalty",
            (d["emp_name"], float(d.get("monthly_base",0)), int(d.get("working_days",26)),
             float(d.get("ot_rate",0)), float(d.get("late_penalty",50)))
        )
        conn.commit(); conn.close()
        return jsonify({"status":"success"})
    rows = conn.execute("SELECT * FROM payroll_emp_settings").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/payroll/daily", methods=["GET","POST"])
def payroll_daily():
    conn = get_db_connection()
    if request.method == "POST":
        d = request.json
        conn.execute(
            "INSERT INTO payroll_daily (emp_name,work_date,status,is_late,ot_hours,note,created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(emp_name,work_date) DO UPDATE SET status=EXCLUDED.status,is_late=EXCLUDED.is_late,ot_hours=EXCLUDED.ot_hours,note=EXCLUDED.note,created_at=EXCLUDED.created_at",
            (d["emp_name"], d["work_date"], d.get("status","present"),
             1 if d.get("is_late") else 0, float(d.get("ot_hours",0)),
             d.get("note",""), datetime.now().isoformat())
        )
        conn.commit(); conn.close()
        return jsonify({"status":"success"})
    week_start = request.args.get("week_start", "")
    if week_start:
        week_end = (datetime.strptime(week_start,"%Y-%m-%d") + timedelta(days=6)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM payroll_daily WHERE work_date >= ? AND work_date <= ? ORDER BY work_date,emp_name",
            (week_start, week_end)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM payroll_daily ORDER BY work_date DESC LIMIT 200").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/payroll/weekly_summary", methods=["GET"])
def payroll_weekly_summary():
    week_start = request.args.get("week_start","")
    if not week_start:
        today = datetime.now()
        days_since_mon = today.weekday()
        week_start = (today - timedelta(days=days_since_mon)).strftime("%Y-%m-%d")
    week_end = (datetime.strptime(week_start,"%Y-%m-%d") + timedelta(days=6)).strftime("%Y-%m-%d")
    conn = get_db_connection()
    rows  = conn.execute(
        "SELECT * FROM payroll_daily WHERE work_date >= ? AND work_date <= ?",
        (week_start, week_end)
    ).fetchall()
    settings = {r["emp_name"]: dict(r) for r in conn.execute("SELECT * FROM payroll_emp_settings").fetchall()}
    conn.close()
    emp_data = {}
    for r in rows:
        n = r["emp_name"]
        if n not in emp_data:
            emp_data[n] = {"emp_name":n,"actual_days":0,"absent_days":0,"late_count":0,"ot_hours":0.0,"records":[]}
        if r["status"] == "present":
            emp_data[n]["actual_days"] += 1
        else:
            emp_data[n]["absent_days"] += 1
        if r["is_late"]: emp_data[n]["late_count"] += 1
        emp_data[n]["ot_hours"] += r["ot_hours"] or 0
        emp_data[n]["records"].append(dict(r))
    result = []
    for name, data in emp_data.items():
        s = settings.get(name, {"monthly_base":0,"working_days":26,"ot_rate":0,"late_penalty":50})
        monthly = float(s.get("monthly_base",0) or 0)
        wd      = int(s.get("working_days",26) or 26)
        daily_r = round(monthly / wd, 2) if wd > 0 else 0
        ot_rate = float(s.get("ot_rate",0) or 0)
        late_p  = float(s.get("late_penalty",50) or 50)
        base_pay    = round(data["actual_days"] * daily_r, 2)
        ot_pay      = round(data["ot_hours"] * ot_rate, 2)
        late_deduct = round(data["late_count"] * late_p, 2)
        net = round(base_pay + ot_pay - late_deduct, 2)
        result.append({**data, "daily_rate":daily_r, "base_pay":base_pay,
                       "ot_pay":ot_pay, "late_deduct":late_deduct, "net":net,
                       "monthly_base":monthly, "settings": s})
    return jsonify({"week_start":week_start,"week_end":week_end,"employees":result})

@app.route("/api/payroll/weekly_close", methods=["POST"])
def payroll_weekly_close():
    d = request.json
    conn = get_db_connection()
    week_label = f"สัปดาห์ {d['week_start']} ถึง {d['week_end']}"
    for emp in d.get("employees",[]):
        conn.execute(
            "INSERT INTO payroll (emp_name,month_year,base_salary,working_days,actual_days,daily_rate,"
            "ot_hours,ot_rate,ot_amount,bonus_amount,late_count,late_penalty,"
            "deduct_late,deduct_absent,deduct_other,net_salary,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (emp["emp_name"], week_label,
             emp.get("monthly_base",0), emp.get("working_days",26),
             emp.get("actual_days",0), emp.get("daily_rate",0),
             emp.get("ot_hours",0), emp.get("settings",{}).get("ot_rate",0), emp.get("ot_pay",0),
             0, emp.get("late_count",0), emp.get("settings",{}).get("late_penalty",50),
             emp.get("late_deduct",0), 0, 0, emp.get("net",0),
             datetime.now().isoformat())
        )
    conn.commit(); conn.close()
    return jsonify({"status":"success"})

@app.route("/api/payroll", methods=["GET","POST"])
def manage_payroll():
    conn=get_db_connection()
    if request.method=="POST":
        d=request.json
        base=float(d.get('base_salary',0)); wd=int(d.get('working_days',26)); ad=int(d.get('actual_days',wd))
        dr=round(base/wd,2) if wd>0 else 0
        oth=float(d.get('ot_hours',0)); otr=float(d.get('ot_rate',0)); ota=round(oth*otr,2)
        bon=float(d.get('bonus_amount',0))
        lc=int(d.get('late_count',0)); lp=float(d.get('late_penalty',0)); dl=round(lc*lp,2)
        da=round((wd-ad)*dr,2); doth=float(d.get('deduct_other',0))
        bp=round(ad*dr,2); net=round((bp+ota+bon)-(dl+da+doth),2)
        conn.execute("INSERT INTO payroll (emp_name,month_year,base_salary,working_days,actual_days,daily_rate,"
                     "ot_hours,ot_rate,ot_amount,bonus_amount,late_count,late_penalty,"
                     "deduct_late,deduct_absent,deduct_other,net_salary,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (d['emp_name'],d.get('month_year',''),base,wd,ad,dr,oth,otr,ota,bon,lc,lp,dl,da,doth,net,datetime.now().isoformat()))
        conn.commit(); conn.close()
        return jsonify({"status":"success","net":net,"daily_rate":dr,"deduct_absent":da,"ot_amount":ota,"base_pay":bp})
    rows=conn.execute("SELECT * FROM payroll ORDER BY id DESC LIMIT 100").fetchall(); conn.close()
    return jsonify([dict(r) for r in rows])

# ── REPORT / DASHBOARD ───────────────────────────────────────
@app.route("/api/report/dashboard")
def get_dashboard():
    conn=get_db_connection()
    r1=conn.execute("SELECT setting_value FROM system_settings WHERE setting_key='day_cutoff_time'").fetchone()
    r2=conn.execute("SELECT setting_value FROM system_settings WHERE setting_key='starting_cash'").fetchone()
    ct=r1['setting_value'] if r1 else "06:00"; sc=float(r2['setting_value']) if r2 else 2000.0
    ch,cm=map(int,ct.split(':'))
    # created_at เก็บเป็น UTC (เวลาเซิร์ฟเวอร์) จึงต้องเทียบเวลาตัดยอดด้วย "เวลาไทยปัจจุบัน" (บวก TZ_OFFSET) แล้วแปลงกลับเป็น UTC ก่อน query
    now_th=datetime.now()+TZ_OFFSET
    req_date = request.args.get('date','').strip()
    ss_th = None
    if req_date:
        try:
            base_date = datetime.strptime(req_date, '%Y-%m-%d')
            ss_th = base_date.replace(hour=ch,minute=cm,second=0,microsecond=0)
            sd = base_date.strftime('%d/%m/%Y')
        except ValueError:
            ss_th = None
    if ss_th is None:
        ctt_th=now_th.replace(hour=ch,minute=cm,second=0,microsecond=0)
        if now_th<ctt_th:
            ss_th=(now_th-timedelta(days=1)).replace(hour=ch,minute=cm,second=0,microsecond=0); sd=(now_th-timedelta(days=1)).strftime('%d/%m/%Y')
        else:
            ss_th=ctt_th; sd=now_th.strftime('%d/%m/%Y')
    ss=ss_th-TZ_OFFSET
    se=ss+timedelta(days=1)
    sstr=ss.strftime('%Y-%m-%d %H:%M:%S')
    sestr=se.strftime('%Y-%m-%d %H:%M:%S')
    bills=conn.execute("SELECT * FROM bills WHERE created_at>=? AND created_at<? ORDER BY id DESC",(sstr,sestr)).fetchall()
    sales=sum(b['total'] for b in bills)
    exp=conn.execute("SELECT SUM(amount) FROM expenses WHERE created_at>=? AND created_at<?",(sstr,sestr)).fetchone()[0] or 0
    conn.close()
    cash_sales = sum(b['total'] for b in bills if (b.get('payment_method') or 'เงินสด')=='เงินสด')
    transfer_sales = sum(b['total'] for b in bills if (b.get('payment_method') or 'เงินสด')!='เงินสด')
    return jsonify({"sales":round(sales,2),"expenses":round(float(exp),2),"net":round(sales-float(exp),2),
                    "cash_sales":round(cash_sales,2),"transfer_sales":round(transfer_sales,2),
                    "shift_date":sd,"starting_cash":sc,"daily_bills":[dict(b) for b in bills]})
    # ── LINE WEBHOOK (ระบบเช็คอินด้วยรูปภาพ) ──────────────────────────────────

# ── DAILY SALES REPORT (CRON 08:00, 3 SHIFTS) ────────────────
@app.route("/api/cron/daily-report", methods=["GET", "POST"])
def cron_daily_report():
    secret = request.args.get("key", "")
    if secret != "g2_cron_2026":
        return jsonify({"error": "unauthorized"}), 403
    try:
        from datetime import datetime, timedelta
        conn = get_db_connection()
        # created_at เก็บเป็น UTC จึงต้องคำนวณเวลาตัดยอดด้วย "เวลาไทยปัจจุบัน" (บวก TZ_OFFSET) แล้วแปลงกลับเป็น UTC ก่อน query
        r_ct = conn.execute("SELECT setting_value FROM system_settings WHERE setting_key='day_cutoff_time'").fetchone()
        ct_str = r_ct['setting_value'] if r_ct and r_ct['setting_value'] else "06:00"
        ch, cm = map(int, ct_str.split(':'))
        now_th = datetime.now() + TZ_OFFSET
        ctt_th = now_th.replace(hour=ch, minute=cm, second=0, microsecond=0)
        cur_shift_start_th = (now_th - timedelta(days=1)).replace(hour=ch, minute=cm, second=0, microsecond=0) if now_th < ctt_th else ctt_th
        period_start_th = cur_shift_start_th - timedelta(days=1)
        period_end_th   = cur_shift_start_th
        period_start = period_start_th - TZ_OFFSET
        period_end   = period_end_th - TZ_OFFSET
        bills = conn.execute(
            "SELECT total, time_fee, food_fee, created_at FROM bills WHERE created_at >= %s AND created_at < %s AND status = %s",
            (period_start.isoformat(), period_end.isoformat(), "ชำระแล้ว")
        ).fetchall()

        shifts = {"A": [0,0.0,0.0,0.0], "B": [0,0.0,0.0,0.0], "C": [0,0.0,0.0,0.0]}
        for b in bills:
            try:
                hh = (datetime.fromisoformat(b["created_at"]) + TZ_OFFSET).hour
            except Exception:
                hh = 0
            k = "A" if hh < 8 else ("B" if hh < 16 else "C")
            shifts[k][0] += 1
            shifts[k][1] += float(b["total"] or 0)
            shifts[k][2] += float(b["time_fee"] or 0)
            shifts[k][3] += float(b["food_fee"] or 0)

        yd = period_start_th.strftime("%d/%m/%Y")
        labels = [("A","🌙 กะดึก 00:00–08:00"),("B","☀️ กะเช้า 08:00–16:00"),("C","🌆 กะเย็น 16:00–00:00")]
        gt = 0.0; gb = 0
        msg = f"📊 G2 SNOOKER — สรุปยอดขาย\n📅 {yd}\n━━━━━━━━━━━━━━\n"
        for k, lab in labels:
            bc, ts, tt, tf = shifts[k]
            gt += ts; gb += bc
            msg += f"{lab}\n"
            if bc > 0:
                msg += f"  ยอดขาย: {ts:,.0f} ฿ ({bc} บิล)\n  🎱 {tt:,.0f} ฿  🍔 {tf:,.0f} ฿\n"
            else:
                msg += "  — ไม่มียอด —\n"
            msg += "━━━━━━━━━━━━━━\n"
        msg += f"💰 รวมทั้งวัน: {gt:,.0f} ฿\n🧾 รวม {gb} บิล"

        # ── ยอดค้าง: โต๊ะที่ยังเล่นอยู่ (ยังไม่เช็คบิล) ──
        try:
            import json as _pj
            rates_p = conn.execute("SELECT * FROM rate_settings").fetchall()
            sess_rows = conn.execute(
                "SELECT a.table_id, a.start_time, a.total_food, t.name AS tname, t.type AS ttype "
                "FROM active_sessions_db a LEFT JOIN tables_config t ON t.id = a.table_id"
            ).fetchall()
            pend_count = 0
            pend_fee = 0.0
            pend_food = 0.0
            for s in sess_rows:
                pend_count += 1
                pend_food += float(s["total_food"] or 0)
                if s["ttype"] == "snooker" and s["start_time"]:
                    try:
                        stt = datetime.fromisoformat(s["start_time"])
                        pend_fee += float(calc_fee(stt, datetime.now(), rates_p) or 0)
                    except Exception as fe:
                        print(f"[WARN] pending fee table {s['table_id']}: {fe}")
            if pend_count > 0:
                msg += f"\n⏳ ยอดค้าง (ยังเล่นอยู่)\n"
                msg += f"  🎱 {pend_count} โต๊ะ\n"
                msg += f"  ค่าโต๊ะประมาณ: {pend_fee:,.0f} ฿\n"
                if pend_food > 0:
                    msg += f"  ค่าอาหารค้าง: {pend_food:,.0f} ฿\n"
                msg += f"  (ยังไม่รวมในยอดด้านบน — จะนับเมื่อเช็คบิล)"
        except Exception as pe:
            print(f"[WARN] pending section: {pe}")

        st = {r["setting_key"]: r["setting_value"] for r in
              conn.execute("SELECT setting_key,setting_value FROM system_settings").fetchall()}
        conn.close()
        tok = (st.get("line_checkin_token") or "").strip() or (st.get("line_token") or "").strip()
        gid = (st.get("line_report_group_id") or "").strip()
        if tok and gid:
            import json as _json, urllib.request as _ur
            try:
                data = _json.dumps({"to": gid, "messages": [{"type":"text","text":msg}]}).encode()
                req = _ur.Request("https://api.line.me/v2/bot/message/push", data=data,
                    headers={"Content-Type":"application/json","Authorization":f"Bearer {tok}"})
                _ur.urlopen(req, timeout=5)
            except Exception as le:
                print(f"[WARN] report push: {le}")
                return jsonify({"ok": False, "error": str(le), "sent": msg}), 200
        else:
            return jsonify({"ok": False, "error": "missing token or group_id"}), 200
        return jsonify({"ok": True, "sent": msg, "group": gid})
    except Exception as e:
        print(f"[ERROR] daily report: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/line/webhook", methods=["POST"])
def line_webhook():
    import json, urllib.request, re as _re
    from datetime import datetime, timedelta
    conn = get_db_connection()
    try:
        conn.execute("ALTER TABLE employees ADD COLUMN line_user_id TEXT")
        conn.commit()
    except: conn.rollback()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS line_conv_state (
            user_id TEXT PRIMARY KEY, state TEXT, data TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS leave_requests (
            id INTEGER PRIMARY KEY, emp_name TEXT, leave_date TEXT,
            shift_name TEXT, reason TEXT, status TEXT DEFAULT 'pending',
            approved_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        conn.commit()
    except: conn.rollback()
    body = request.get_data(as_text=True)
    try:
        events = json.loads(body).get("events", [])
    except:
        conn.close()
        return "OK", 200
    settings = {r["setting_key"]: r["setting_value"] for r in
                conn.execute("SELECT setting_key,setting_value FROM system_settings").fetchall()}
    line_token = settings.get("line_checkin_token","").strip() or settings.get("line_token","").strip()
    for event in events:
        if event.get("type") != "message":
            continue
        reply_token = event.get("replyToken","")
        user_id = event.get("source",{}).get("userId","")
        group_id = event.get("source",{}).get("groupId","")
        if group_id: print(f"[GROUP ID] {group_id}")
        global _REPORT_GRP
        _REPORT_GRP = bool(group_id and group_id == settings.get("line_report_group_id","").strip())
        msg_type = event.get("message",{}).get("type","")
        if reply_token in ["00000000000000000000000000000000","ffffffffffffffffffffffffffffffff"]:
            continue
        now = datetime.utcnow() + timedelta(hours=7)
        today_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")
        if msg_type == "text":
            text = event["message"].get("text","").strip()
            try:
                conv = conn.execute("SELECT state,data FROM line_conv_state WHERE user_id=?", (user_id,)).fetchone()
            except: conv = None
            skip_kw = ["ลางาน","ลงทะเบียน","เลิกงาน","ตารางงาน","คำสั่ง","ขอไอดีกลุ่ม","เช็คอิน","อนุมัติ","ยกเลิก","พนักงานวันนี้","ยอดวันนี้"]
            if conv and conv["state"] and not any(text==k or text.startswith(k+" ") for k in skip_kw):
                import json as _js
                state = conv["state"]
                data = _js.loads(conv["data"] or "{}")
                emp = conn.execute("SELECT name FROM employees WHERE line_user_id=?", (user_id,)).fetchone()
                emp_name = emp["name"] if emp else ""
                if text == "ยกเลิก":
                    try: conn.execute("DELETE FROM line_conv_state WHERE user_id=?", (user_id,)); conn.commit()
                    except: pass
                    reply_msg(reply_token, line_token, "❌ ยกเลิกแล้วครับ")
                    continue
                if state == "leave_step1":
                    try:
                        parts = text.strip().split("/")
                        if len(parts) == 3:
                            d,m,y = parts
                            leave_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
                            leave_date_fmt = f"{d.zfill(2)}/{m.zfill(2)}/{y}"
                            sc = conn.execute("""SELECT s.shift_name,s.start_time,s.end_time
                                FROM work_schedule w JOIN work_shifts s ON w.shift_id=s.id
                                WHERE w.emp_name=? AND w.work_date=?""", (emp_name,leave_date)).fetchone()
                            data = {"leave_date":leave_date,"leave_date_fmt":leave_date_fmt,
                                    "shift_name":sc["shift_name"] if sc else "ไม่มีกะ",
                                    "start_time":sc["start_time"] if sc else "-",
                                    "end_time":sc["end_time"] if sc else "-"}
                            conn.execute("INSERT INTO line_conv_state (user_id,state,data) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET state=EXCLUDED.state,data=EXCLUDED.data",
                                (user_id,"leave_step2",_js.dumps(data))); conn.commit()
                            shift_info = f"⏰ กะ: {data['shift_name']} ({data['start_time']}-{data['end_time']})" if sc else "⚠️ ไม่พบกะงานวันนั้น"
                            reply_msg(reply_token, line_token,
                                f"📅 วันที่: {leave_date_fmt}\n{shift_info}\n\n📝 กรุณาระบุเหตุผลการลา\nเช่น: ลาป่วย / ลากิจ / ธุระส่วนตัว", show_menu=False)
                        else:
                            reply_msg(reply_token, line_token, "❌ รูปแบบไม่ถูกต้อง\nกรุณาใส่: วว/ดด/ปปปป\nเช่น: 20/04/2026", show_menu=False)
                    except: reply_msg(reply_token, line_token, "❌ รูปแบบวันที่ผิดครับ\nเช่น: 20/04/2026", show_menu=False)
                    continue
                elif state == "leave_step2":
                    data["reason"] = text
                    conn.execute("INSERT INTO line_conv_state (user_id,state,data) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET state=EXCLUDED.state,data=EXCLUDED.data",
                        (user_id,"leave_confirm",_js.dumps(data))); conn.commit()
                    reply_msg(reply_token, line_token,
                        f"📋 สรุปคำขอลางาน\n{'─'*20}\n"
                        f"👤 {emp_name}\n📅 วันที่: {data['leave_date_fmt']}\n"
                        f"⏰ กะ: {data['shift_name']} ({data['start_time']}-{data['end_time']})\n"
                        f"📝 เหตุผล: {text}\n{'─'*20}\n"
                        f"ยืนยันส่งคำขอไหม?\nพิมพ์: ยืนยัน หรือ ยกเลิก", show_menu=False)
                    continue
                elif state == "leave_confirm":
                    if text in ["ยืนยัน","ยืนยันครับ","ยืนยันค่ะ"]:
                        try:
                            conn.execute("INSERT INTO leave_requests (emp_name,leave_date,shift_name,reason,status) VALUES (?,?,?,?,'pending')",
                                (emp_name,data["leave_date"],data["shift_name"],data.get("reason",""))); conn.commit()
                        except: conn.rollback()
                        try: conn.execute("DELETE FROM line_conv_state WHERE user_id=?", (user_id,)); conn.commit()
                        except: pass
                        reply_msg(reply_token, line_token,
                            f"✅ ส่งคำขอลางานเรียบร้อย!\n👤 {emp_name}\n📅 {data['leave_date_fmt']}\n📝 {data.get('reason','')}\n\n⏳ กรุณารอการยืนยันจากหัวหน้างานครับ")
                        try:
                            mgr_token = settings.get("line_cancel_token","").strip() or line_token
                            mgr_group = settings.get("line_group_id","").strip()
                            if mgr_token and mgr_group:
                                msg = (f"📋 คำขอลางานใหม่\n{'─'*20}\n👤 {emp_name}\n📅 {data['leave_date_fmt']}\n"
                                       f"⏰ กะ: {data['shift_name']}\n📝 {data.get('reason','')}\n{'─'*20}\n"
                                       f"พิมพ์: อนุมัติ {emp_name} {data['leave_date_fmt']}")
                                _d = _js.dumps({"to":mgr_group,"messages":[{"type":"text","text":msg}]},ensure_ascii=False).encode("utf-8")
                                _r = urllib.request.Request("https://api.line.me/v2/bot/message/push",data=_d,
                                    headers={"Content-Type":"application/json","Authorization":f"Bearer {mgr_token}"})
                                urllib.request.urlopen(_r, timeout=5)
                        except Exception as e: print(f"[WARN] {e}")
                    else:
                        reply_msg(reply_token, line_token, "กรุณาพิมพ์: ยืนยัน หรือ ยกเลิก ครับ", show_menu=False)
                    continue
            if text == "ขอไอดีกลุ่ม":
                reply_msg(reply_token, line_token, f"✅ Group ID:\n{group_id}" if group_id else "❌ ใช้ได้เฉพาะในกลุ่มครับ")
                continue
            if text == "ยอดวันนี้":
                rep_grp = group_id and group_id == settings.get("line_report_group_id","").strip()
                bills_t = conn.execute(
                    "SELECT total, time_fee, food_fee, created_at FROM bills WHERE created_at >= ? AND created_at < ? AND status = ?",
                    (today_str + "T00:00:00", today_str + "T23:59:59", "ชำระแล้ว")
                ).fetchall()
                shf = {"A":[0,0.0], "B":[0,0.0], "C":[0,0.0]}
                for b in bills_t:
                    try: hh = int(b["created_at"][11:13])
                    except: hh = 0
                    kk = "A" if hh < 8 else ("B" if hh < 16 else "C")
                    shf[kk][0] += 1
                    shf[kk][1] += float(b["total"] or 0)
                labs = [("A","🌙 ดึก 00-08"),("B","☀️ เช้า 08-16"),("C","🌆 เย็น 16-00")]
                gt2 = sum(v[1] for v in shf.values()); gb2 = sum(v[0] for v in shf.values())
                ml = [f"💰 ยอดวันนี้ ({today_str})", "─"*16]
                for kk, lab in labs:
                    bc2, ts2 = shf[kk]
                    ml.append(f"{lab}: {ts2:,.0f} ฿ ({bc2} บิล)")
                ml.append("─"*16)
                ml.append(f"รวม: {gt2:,.0f} ฿ ({gb2} บิล)")
                ml.append("(ยอด ณ ตอนนี้ ยังไม่รวมโต๊ะที่ยังเล่นอยู่)")
                reply_msg(reply_token, line_token, "\n".join(ml), show_menu=False)
                continue
            if text == "พนักงานวันนี้":
                rows = conn.execute("""
                    SELECT w.emp_name, s.shift_name, s.start_time, s.end_time
                    FROM work_schedule w
                    JOIN work_shifts s ON w.shift_id = s.id
                    WHERE w.work_date = ?
                    ORDER BY s.start_time
                """, (today_str,)).fetchall()
                # กฎ: ถ้ามีคนอื่นอยู่กะ A (10:00) ด้วย → ตัด CEO โต๋ ออก (โต๋หยุด)
                a_others = [r for r in rows if r["start_time"] == "10:00" and r["emp_name"] != "CEO โต๋"]
                if a_others:
                    rows = [r for r in rows if not (r["start_time"] == "10:00" and r["emp_name"] == "CEO โต๋")]
                if rows:
                    sep = '─'*20
                    msg_lines = [f"👥 พนักงานวันนี้ ({today_str})\n{sep}"]
                    for r in rows:
                        msg_lines.append(f"👤 {r['emp_name']}\n   📋 {r['shift_name']} ({r['start_time']} - {r['end_time']})")
                    reply_msg(reply_token, line_token, "\n".join(msg_lines))
                else:
                    reply_msg(reply_token, line_token, f"❌ ไม่มีตารางงานวันนี้ครับ ({today_str})")
                continue
            if text in ["คำสั่ง","ช่วยด้วย","help"]:
                reply_msg(reply_token, line_token,
                    "📋 คำสั่งทั้งหมด\n─────────────────\n"
                    "👤 ลงทะเบียน [ชื่อ] → ลงทะเบียนครั้งแรก\n"
                    "📸 ส่งรูปภาพ → เช็คอินเข้างาน\n"
                    "📅 ตารางงาน → ดูตาราง 7 วัน\n"
                    "🏁 เลิกงาน → บันทึกเวลาออก\n"
                    "🌴 ลางาน → แจ้งลา (รอหัวหน้าอนุมัติ)\n"
                    "👥 พนักงานวันนี้ → ดูรายชื่อพนักงานวันนี้\n"
                    "🆔 ขอไอดีกลุ่ม → ดู Group ID")
                continue
            if text in ["เช็คอิน","checkin"]:
                emp = conn.execute("SELECT name FROM employees WHERE line_user_id=?", (user_id,)).fetchone()
                if not emp:
                    reply_msg(reply_token, line_token, "❌ ยังไม่ได้ลงทะเบียน\nพิมพ์: ลงทะเบียน [ชื่อ]")
                else:
                    reply_msg(reply_token, line_token, f"📸 {emp['name']} กรุณาถ่ายรูป/ส่งรูปภาพเพื่อเช็คอินครับ", show_menu=False)
                continue
            if text == "เลิกงาน":
                emp = conn.execute("SELECT name,role FROM employees WHERE line_user_id=?", (user_id,)).fetchone()
                if not emp:
                    reply_msg(reply_token, line_token, "❌ ยังไม่ได้ลงทะเบียน\nพิมพ์: ลงทะเบียน [ชื่อ]")
                else:
                    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                    is_owner = emp["role"] in ["owner","admin"]
                    # หากะจากตาราง (วันนี้ก่อน ถ้าไม่เจอให้ลองเมื่อวาน)
                    sc = conn.execute("""SELECT s.start_time, w.work_date FROM work_schedule w
                        JOIN work_shifts s ON w.shift_id=s.id
                        WHERE w.emp_name=? AND w.work_date=?""", (emp["name"],today_str)).fetchone()
                    checkin_date = today_str
                    if not sc:
                        sc = conn.execute("""SELECT s.start_time, w.work_date FROM work_schedule w
                            JOIN work_shifts s ON w.shift_id=s.id
                            WHERE w.emp_name=? AND w.work_date=?""", (emp["name"],yesterday_str)).fetchone()
                        if sc: checkin_date = yesterday_str
                    if not sc and not is_owner:
                        reply_msg(reply_token, line_token, f"❌ {emp['name']} ไม่พบตารางกะวันนี้ครับ")
                        continue
                    # ดึงเวลาเช็คอินจริงจาก payroll_daily ก่อน
                    existing_pre = conn.execute(
                        "SELECT note FROM payroll_daily WHERE emp_name=? AND work_date=?",
                        (emp["name"],checkin_date)).fetchone()
                    if not existing_pre:
                        existing_pre = conn.execute(
                            "SELECT note FROM payroll_daily WHERE emp_name=? AND work_date=?",
                            (emp["name"],yesterday_str)).fetchone()
                    shift_start_str = sc["start_time"] if sc else time_str
                    if existing_pre and existing_pre["note"]:
                        import re as _re
                        m = _re.search(r'เช็คอิน (\d{2}:\d{2})', existing_pre["note"])
                        if m: shift_start_str = m.group(1)
                    cin_dt = datetime.strptime(f"{checkin_date} {shift_start_str}", "%Y-%m-%d %H:%M")
                    if cin_dt > now: cin_dt -= timedelta(days=1)
                    if (now - cin_dt).total_seconds() > 86400: cin_dt += timedelta(days=1)
                    worked_mins = int((now-cin_dt).total_seconds()/60)
                    wh,wm = worked_mins//60, worked_mins%60
                    if worked_mins < 480: status = "⚠️ ออกก่อนเวลา"
                    elif worked_mins <= 510: status = "✅ ออกงานตรงเวลา"
                    else:
                        oth,otm = (worked_mins-480)//60,(worked_mins-480)%60
                        status = f"⏰ โอที {oth} ชม. {otm} นาที\nกรุณาแจ้ง CEO โต๋"
                    existing = conn.execute("SELECT id,note FROM payroll_daily WHERE emp_name=? AND work_date=?", (emp["name"],checkin_date)).fetchone()
                    if existing:
                        conn.execute("UPDATE payroll_daily SET note=? WHERE id=?", ((existing["note"] or "")+f" | เลิกงาน {time_str} น.",existing["id"]))
                    else:
                        conn.execute("INSERT INTO payroll_daily (emp_name,work_date,status,is_late,ot_hours,note) VALUES (?,?,'present',0,0,?)",
                            (emp["name"],checkin_date,f"เลิกงาน {time_str} น."))
                    reply_msg(reply_token, line_token,
                        f"🏁 บันทึกเลิกงานสำเร็จ!\n👤 {emp['name']}\n🕒 {time_str} น.\n⏱ ทำงาน: {wh} ชม. {wm} นาที\n📌 {status}")
                continue
            if text == "ตารางงาน":
                emp = conn.execute("SELECT name FROM employees WHERE line_user_id=?", (user_id,)).fetchone()
                if not emp:
                    reply_msg(reply_token, line_token, "❌ ยังไม่ได้ลงทะเบียน\nพิมพ์: ลงทะเบียน [ชื่อ]")
                else:
                    d7 = (now+timedelta(days=6)).strftime("%Y-%m-%d")
                    rows = conn.execute("""SELECT w.work_date,s.shift_name,s.start_time,s.end_time,s.color
                        FROM work_schedule w JOIN work_shifts s ON w.shift_id=s.id
                        WHERE w.emp_name=? AND w.work_date>=? AND w.work_date<=?
                        ORDER BY w.work_date""", (emp["name"],today_str,d7)).fetchall()
                    day_s = ["จ.","อ.","พ.","พฤ.","ศ.","ส.","อา."]
                    if rows:
                        contents = []
                        for r in rows:
                            d = datetime.strptime(r["work_date"],"%Y-%m-%d")
                            is_today = r["work_date"]==today_str
                            color = r["color"] or "#6366f1"
                            contents.append({"type":"box","layout":"horizontal",
                                "backgroundColor":"#1a2035" if is_today else "#0d1520",
                                "cornerRadius":"8px","margin":"sm","paddingAll":"10px",
                                "borderWidth":"2px" if is_today else "1px",
                                "borderColor":color if is_today else "#333333",
                                "contents":[
                                    {"type":"box","layout":"vertical","width":"60px","contents":[
                                        {"type":"text","text":day_s[d.weekday()],"size":"sm","color":"#aaaaaa","align":"center"},
                                        {"type":"text","text":d.strftime("%d/%m"),"size":"lg","weight":"bold",
                                         "color":"#ffffff" if is_today else "#cccccc","align":"center"}]},
                                    {"type":"separator","color":"#333333"},
                                    {"type":"box","layout":"vertical","flex":1,"paddingStart":"12px","contents":[
                                        {"type":"text","text":r["shift_name"],"weight":"bold","color":color,"size":"md"},
                                        {"type":"text","text":f"{r['start_time']} — {r['end_time']}","size":"sm","color":"#aaaaaa"}]}]})
                        flex = {"type":"flex","altText":f"ตารางงาน {emp['name']}",
                            "contents":{"type":"bubble","size":"giga",
                                "header":{"type":"box","layout":"vertical","backgroundColor":"#0a1628","paddingAll":"16px",
                                    "contents":[
                                        {"type":"text","text":"📅 ตารางงาน","color":"#38bdf8","weight":"bold","size":"lg"},
                                        {"type":"text","text":emp["name"],"color":"#ffffff","weight":"bold","size":"xxl"},
                                        {"type":"text","text":"7 วันข้างหน้า","color":"#666666","size":"sm"}]},
                                "body":{"type":"box","layout":"vertical","backgroundColor":"#0d1520","paddingAll":"8px","contents":contents},
                                "footer":{"type":"box","layout":"vertical","backgroundColor":"#0a1628","paddingAll":"12px",
                                    "contents":[{"type":"text","text":"G2 SNOOKER — Jarvis","color":"#444444","size":"xs","align":"center"}]}}}
                    else:
                        flex = {"type":"flex","altText":"ไม่มีตารางงาน",
                            "contents":{"type":"bubble","body":{"type":"box","layout":"vertical","contents":[
                                {"type":"text","text":"📅 ตารางงาน","color":"#38bdf8","weight":"bold"},
                                {"type":"text","text":emp["name"],"color":"#ffffff","weight":"bold","size":"xl"},
                                {"type":"text","text":"ไม่มีตารางงานใน 7 วันข้างหน้า","color":"#aaaaaa","margin":"md"}]}}}
                    try:
                        _d = json.dumps({"replyToken":reply_token,"messages":[flex]},ensure_ascii=False).encode("utf-8")
                        _r = urllib.request.Request("https://api.line.me/v2/bot/message/reply",data=_d,
                            headers={"Content-Type":"application/json","Authorization":f"Bearer {line_token}"})
                        urllib.request.urlopen(_r, timeout=5)
                    except Exception as e:
                        print(f"[FLEX ERR] {e}")
                        reply_msg(reply_token, line_token, f"📅 ตารางงาน {emp['name']}\nไม่สามารถแสดงได้ครับ")
                continue
            if text in ["ลางาน","day off"]:
                emp = conn.execute("SELECT name FROM employees WHERE line_user_id=?", (user_id,)).fetchone()
                if not emp:
                    reply_msg(reply_token, line_token, "❌ ยังไม่ได้ลงทะเบียน\nพิมพ์: ลงทะเบียน [ชื่อ]")
                else:
                    try:
                        conn.execute("INSERT INTO line_conv_state (user_id,state,data) VALUES (?,?,'{}') ON CONFLICT(user_id) DO UPDATE SET state=EXCLUDED.state,data=EXCLUDED.data",
                            (user_id,"leave_step1")); conn.commit()
                    except: conn.rollback()
                    reply_msg(reply_token, line_token,
                        f"🌴 แจ้งลางาน — {emp['name']}\n{'─'*20}\n"
                        f"📅 ระบุวันที่ต้องการลา\nรูปแบบ: วว/ดด/ปปปป\nเช่น: 20/04/2026\n\nพิมพ์ 'ยกเลิก' เพื่อออก", show_menu=False)
                continue
            if text.startswith("อนุมัติ "):
                parts = text.split()
                if len(parts) >= 3:
                    req_name,req_date_raw = parts[1],parts[2]
                    try:
                        d,m,y = req_date_raw.split("/")
                        req_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
                    except: req_date = req_date_raw
                    approver = conn.execute("SELECT name,role FROM employees WHERE line_user_id=?", (user_id,)).fetchone()
                    if not approver or approver["role"] not in ["admin","owner"]:
                        reply_msg(reply_token, line_token, "❌ คุณไม่มีสิทธิ์อนุมัติลางานครับ")
                    else:
                        req = conn.execute("SELECT * FROM leave_requests WHERE emp_name=? AND leave_date=? AND status='pending'",
                            (req_name,req_date)).fetchone()
                        if not req:
                            reply_msg(reply_token, line_token, f"❌ ไม่พบคำขอลาของ {req_name} วันที่ {req_date_raw}")
                        else:
                            conn.execute("UPDATE leave_requests SET status='approved',approved_by=? WHERE id=?",
                                (approver["name"],req["id"]))
                            conn.execute("INSERT INTO payroll_daily (emp_name,work_date,status,is_late,ot_hours,note) VALUES (?,?,'leave',0,0,?) ON CONFLICT(emp_name,work_date) DO UPDATE SET status='leave',note=EXCLUDED.note",
                                (req_name,req_date,f"ลางาน: {req['reason']} (อนุมัติโดย {approver['name']})"))
                            conn.commit()
                            try:
                                tok = settings.get("line_checkin_token","").strip() or line_token
                                grp = settings.get("line_checkin_group_id","").strip() or group_id
                                if tok and grp:
                                    gm = (f"📢 แจ้งเตือนตารางงาน\n{'─'*20}\n"
                                          f"👤 {req_name} ได้รับอนุมัติลางาน\n"
                                          f"📅 วันที่: {req_date_raw} กะ {req['shift_name']}\n\n"
                                          f"⚠️ กรุณาเช็คตารางงานของตัวเอง\nอาจมีการเปลี่ยนแปลงครับ")
                                    _d = json.dumps({"to":grp,"messages":[{"type":"text","text":gm}]},ensure_ascii=False).encode("utf-8")
                                    _r = urllib.request.Request("https://api.line.me/v2/bot/message/push",data=_d,
                                        headers={"Content-Type":"application/json","Authorization":f"Bearer {tok}"})
                                    urllib.request.urlopen(_r, timeout=5)
                            except Exception as e: print(f"[WARN] {e}")
                            reply_msg(reply_token, line_token, f"✅ อนุมัติลางาน {req_name} วันที่ {req_date_raw} สำเร็จครับ")
                else:
                    reply_msg(reply_token, line_token, "รูปแบบ: อนุมัติ [ชื่อ] [วว/ดด/ปปปป]")
                continue
            if text.startswith("ลงทะเบียน "):
                emp_name = text.replace("ลงทะเบียน ","").strip()
                conn.execute("UPDATE employees SET line_user_id=? WHERE name=?", (user_id,emp_name)); conn.commit()
                check = conn.execute("SELECT name FROM employees WHERE line_user_id=?", (user_id,)).fetchone()
                if check:
                    reply_msg(reply_token, line_token, f"✅ ลงทะเบียนสำเร็จ!\n👤 คุณคือ '{check['name']}'\n\n📸 ส่งรูปภาพเพื่อเช็คอินได้เลยครับ")
                else:
                    reply_msg(reply_token, line_token, f"❌ ไม่พบชื่อ '{emp_name}' ในระบบครับ")
                continue
            known = ["ลงทะเบียน","ตารางงาน","เลิกงาน","ลางาน","คำสั่ง","ช่วยด้วย","help","ขอไอดีกลุ่ม","เช็คอิน","อนุมัติ","พนักงานวันนี้","ยอดวันนี้"]
            if not any(text==k or text.startswith(k+" ") for k in known):
                if not (group_id and group_id == settings.get("line_report_group_id","").strip()):
                    reply_msg(reply_token, line_token, "⚠️ ไม่รู้จักคำสั่งนี้ครับ\nพิมพ์ 'คำสั่ง' เพื่อดูรายการ")
            continue
        elif msg_type == "image":
            # กลุ่มรายงาน: ไม่ประมวลผลรูป (ไม่ใช่ห้องเช็คอิน) → เงียบ
            if group_id and group_id == settings.get("line_report_group_id","").strip():
                continue
            emp = conn.execute("SELECT name,role FROM employees WHERE line_user_id=?", (user_id,)).fetchone()
            if not emp:
                reply_msg(reply_token, line_token, "❌ ยังไม่ได้ลงทะเบียน\nพิมพ์: ลงทะเบียน [ชื่อ]")
                continue
            emp_name = emp["name"]
            is_owner = emp["role"] in ["owner","admin"]
            yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            # หากะวันนี้ก่อน ถ้าไม่เจอให้ลองเมื่อวาน (กะข้ามคืน)
            sc = conn.execute("""SELECT s.start_time FROM work_schedule w
                JOIN work_shifts s ON w.shift_id=s.id
                WHERE w.emp_name=? AND w.work_date=?""", (emp_name,today_str)).fetchone()
            checkin_date = today_str
            if not sc:
                tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                sc_tmr = conn.execute("""SELECT s.start_time FROM work_schedule w
                    JOIN work_shifts s ON w.shift_id=s.id
                    WHERE w.emp_name=? AND w.work_date=?""", (emp_name,tomorrow_str)).fetchone()
                if sc_tmr:
                    sh_h = int(sc_tmr['start_time'].split(':')[0])
                    if sh_h <= 5 and now.hour <= 5:
                        sc = sc_tmr
                        checkin_date = tomorrow_str
            if not sc:
                sc = conn.execute("""SELECT s.start_time FROM work_schedule w
                    JOIN work_shifts s ON w.shift_id=s.id
                    WHERE w.emp_name=? AND w.work_date=?""", (emp_name,yesterday_str)).fetchone()
                if sc:
                    sh_h = int(sc['start_time'].split(':')[0])
                    shift_ref = datetime.strptime(f"{yesterday_str} {sc['start_time']}", "%Y-%m-%d %H:%M")
                    hours_since = (now - shift_ref).total_seconds() / 3600
                    if hours_since <= 12:
                        checkin_date = yesterday_str
                    else:
                        sc = None
            if not sc and not is_owner:
                reply_msg(reply_token, line_token, f"❌ {emp_name} วันนี้ไม่มีตารางงานครับ")
                continue
            if not sc: sc = {"start_time":time_str}
            has_checked = conn.execute("SELECT id,note FROM payroll_daily WHERE emp_name=? AND work_date=?",
                (emp_name,checkin_date)).fetchone()
            if has_checked:
                if has_checked["note"] and "เลิกงาน" in has_checked["note"]:
                    if "เช็คอินกะ2" in has_checked["note"]:
                        reply_msg(reply_token, line_token, f"⚠️ {emp_name} เช็คอินกะ 2 ไปแล้วครับ")
                        continue
                    conn.execute("UPDATE payroll_daily SET note=? WHERE id=?",
                        ((has_checked["note"] or "")+f" | เช็คอินกะ2 {time_str} น.",has_checked["id"]))
                    conn.commit()
                    reply_msg(reply_token, line_token, f"📸 เช็คอินกะ 2 สำเร็จ!\n👤 {emp_name}\n🕒 {time_str} น.")
                else:
                    reply_msg(reply_token, line_token, f"⚠️ {emp_name} เช็คอินแล้ววันนี้\nหากทำ 2 กะ พิมพ์ 'เลิกงาน' ก่อนครับ")
                continue
            shift_dt = datetime.strptime(f"{checkin_date} {sc['start_time']}", "%Y-%m-%d %H:%M")
            if shift_dt > now + timedelta(hours=6): shift_dt -= timedelta(days=1)
            early_mins = int((shift_dt-now).total_seconds()/60)
            is_early = early_mins > 15
            is_late = now > shift_dt + timedelta(minutes=15)
            if is_early: status_msg = f"🌟 มาก่อนเวลา {early_mins} นาที"
            elif is_late:
                late_mins = int((now - (shift_dt + timedelta(minutes=15))).total_seconds()/60) + 15
                status_msg = f"⏰ มาสาย {late_mins} นาที"
            else: status_msg = "✅ ตรงเวลา"
            try:
                conn.execute("INSERT INTO payroll_daily (emp_name,work_date,status,is_late,ot_hours,note) VALUES (?,?,'present',?,0,?)",
                    (emp_name,checkin_date,int(is_late),f"เช็คอิน {time_str} น."))
                conn.commit()
            except: conn.rollback()
            extra = ""
            if is_early:
                extra = f"\n\n🎉 ว้าว! มาก่อนเวลา {early_mins} นาที\nหากยังไม่ได้กินข้าวหรือแต่งหน้า\nทำให้เสร็จก่อนถึงเวลาเข้างานนะครับ 😊"
            reply_msg(reply_token, line_token,
                f"📸 เช็คอินสำเร็จ!\n👤 {emp_name}\n🕒 {time_str} น.\n📌 {status_msg}{extra}")
            continue
    return "OK", 200


_REPORT_GRP = False

def reply_msg(reply_token, token, text, show_menu=True, custom_menu=None):
    import json, urllib.request
    url = "https://api.line.me/v2/bot/message/reply"
    msg = {"type": "text", "text": str(text)}
    if _REPORT_GRP and not custom_menu:
        custom_menu = [("💰 ยอดวันนี้","ยอดวันนี้"),("👥 พนักงานวันนี้","พนักงานวันนี้")]
    if custom_menu:
        msg["quickReply"] = {"items": [
            {"type":"action","action":{"type":"message","label":lbl,"text":txt}}
            for lbl, txt in custom_menu
        ]}
    elif show_menu:
        msg["quickReply"] = {
            "items": [
                {"type":"action","action":{"type":"message","label":"เช็คอิน","text":"เช็คอิน"}},
                {"type":"action","action":{"type":"message","label":"เลิกงาน","text":"เลิกงาน"}},
                {"type":"action","action":{"type":"message","label":"ตารางงาน","text":"ตารางงาน"}},
                {"type":"action","action":{"type":"message","label":"ลางาน","text":"ลางาน"}},
                {"type":"action","action":{"type":"message","label":"คำสั่ง","text":"คำสั่ง"}},
                {"type":"action","action":{"type":"message","label":"พนักงานวันนี้","text":"พนักงานวันนี้"}}
            ]
        }
    payload = json.dumps({"replyToken": reply_token, "messages": [msg]}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {token}"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[REPLY ERR] {e}")

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

