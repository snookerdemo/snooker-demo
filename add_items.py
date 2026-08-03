from database import get_db_connection

def add_starter_items():
    conn = get_db_connection()
    items = [
        ('น้ำเปล่า', 10, 5, 50),
        ('เบียร์สิงห์', 80, 60, 24),
        ('ข้าวผัดหมู', 60, 30, 99),
        ('เอสโคล่า', 20, 12, 36)
    ]
    conn.executemany("INSERT INTO inventory (product_name, price, cost, stock_qty) VALUES (?, ?, ?, ?)", items)
    conn.commit()
    conn.close()
    print("✅ เพิ่มสินค้าตัวอย่างเรียบร้อย!")

if __name__ == '__main__':
    add_starter_items()