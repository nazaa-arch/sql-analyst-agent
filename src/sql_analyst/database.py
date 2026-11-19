"""Deterministic Indonesian e-commerce & logistics SQLite database and schema introspector."""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sql_analyst.schemas import ColumnSchema, DatabaseSchemaResponse, TableSchema

SCHEMA_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS warehouses (
    warehouse_id INTEGER PRIMARY KEY,
    warehouse_code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    province TEXT NOT NULL,
    region TEXT NOT NULL,
    capacity_units INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id INTEGER PRIMARY KEY,
    customer_name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    city TEXT NOT NULL,
    province TEXT NOT NULL,
    segment TEXT NOT NULL CHECK(segment IN ('Retail', 'SMB', 'Enterprise')),
    signup_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY,
    sku TEXT UNIQUE NOT NULL,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('Electronics', 'Fashion', 'Home', 'Groceries', 'Health')),
    unit_cost REAL NOT NULL,
    unit_price REAL NOT NULL,
    stock_qty INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(warehouse_id),
    order_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('completed', 'shipped', 'cancelled', 'returned')),
    payment_method TEXT NOT NULL CHECK(payment_method IN ('Bank Transfer', 'QRIS', 'Virtual Account', 'Credit Card', 'COD')),
    discount_amount REAL NOT NULL DEFAULT 0,
    total_amount REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(order_id),
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    line_total REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id INTEGER PRIMARY KEY,
    order_id INTEGER UNIQUE NOT NULL REFERENCES orders(order_id),
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(warehouse_id),
    carrier TEXT NOT NULL CHECK(carrier IN ('JNE', 'J&T Express', 'SiCepat', 'AnterAja', 'Pos Indonesia')),
    shipped_date TEXT NOT NULL,
    shipping_cost REAL NOT NULL,
    delivery_days INTEGER NOT NULL,
    is_delayed INTEGER NOT NULL CHECK(is_delayed IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_warehouse ON orders(warehouse_id);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product ON order_items(product_id);
CREATE INDEX IF NOT EXISTS idx_shipments_carrier ON shipments(carrier);
CREATE INDEX IF NOT EXISTS idx_shipments_warehouse ON shipments(warehouse_id);
"""

WAREHOUSE_SEED: list[tuple[int, str, str, str, str, str, int]] = [
    (1, "WH-SBY", "Surabaya Hub", "Surabaya", "Jawa Timur", "Java", 45000),
    (2, "WH-JKT", "Jakarta Hub", "Jakarta", "DKI Jakarta", "Java", 72000),
    (3, "WH-BDG", "Bandung Hub", "Bandung", "Jawa Barat", "Java", 38000),
    (4, "WH-SMG", "Semarang Hub", "Semarang", "Jawa Tengah", "Java", 31000),
    (5, "WH-MKS", "Makassar Hub", "Makassar", "Sulawesi Selatan", "Sulawesi", 26000),
]

INDONESIAN_FIRST_NAMES = [
    "Budi", "Siti", "Agus", "Dewi", "Rizky", "Putri", "Andi", "Nadia", "Fajar", "Intan",
    "Dimas", "Ayu", "Hendra", "Rina", "Yoga", "Citra", "Wahyu", "Lestari", "Bayu", "Sari",
    "Arif", "Fitri", "Dian", "Eko", "Maya", "Reza", "Wulan", "Taufik", "Ratna", "Ilham",
]

INDONESIAN_LAST_NAMES = [
    "Santoso", "Wijaya", "Pratama", "Kusuma", "Hidayat", "Saputra", "Permata", "Nugroho",
    "Setiawan", "Rahmawati", "Utomo", "Hartono", "Maulana", "Puspita", "Gunawan", "Kurniawan",
    "Laksana", "Syahputra", "Wibowo", "Anggraini",
]

COMPANY_PREFIXES = [
    "PT Nusantara", "CV Mitra", "PT Karya", "CV Sinar", "PT Global", "UD Mandiri",
    "PT Sejahtera", "CV Prima", "PT Cipta", "PT Graha",
]

COMPANY_SUFFIXES = [
    "Logistik", "Abadi", "Niaga", "Sentosa", "Teknologi", "Distribusi", "Ritel", "Makmur",
]

CITY_PROVINCES = [
    ("Surabaya", "Jawa Timur"),
    ("Malang", "Jawa Timur"),
    ("Sidoarjo", "Jawa Timur"),
    ("Jakarta Selatan", "DKI Jakarta"),
    ("Jakarta Barat", "DKI Jakarta"),
    ("Bandung", "Jawa Barat"),
    ("Bekasi", "Jawa Barat"),
    ("Depok", "Jawa Barat"),
    ("Semarang", "Jawa Tengah"),
    ("Solo", "Jawa Tengah"),
    ("Yogyakarta", "DI Yogyakarta"),
    ("Makassar", "Sulawesi Selatan"),
    ("Medan", "Sumatera Utara"),
    ("Denpasar", "Bali"),
    ("Balikpapan", "Kalimantan Timur"),
]

# 40 realistic Indonesian e-commerce products across 5 categories (cost & price in IDR)
PRODUCT_CATALOG: list[tuple[str, str, str, float, float, int]] = [
    # Electronics (8)
    ("ELC-001", "Mechanical Keyboard RGB 75%", "Electronics", 420000, 680000, 85),
    ("ELC-002", "USB-C GaN Fast Charger 65W", "Electronics", 165000, 295000, 140),
    ("ELC-003", "Wireless ANC Earbuds Pro", "Electronics", 310000, 549000, 62),
    ("ELC-004", "27-Inch IPS Monitor QHD", "Electronics", 2150000, 3150000, 24),
    ("ELC-005", "Ergonomic Vertical Mouse", "Electronics", 190000, 340000, 110),
    ("ELC-006", "Portable SSD NVMe 1TB", "Electronics", 890000, 1290000, 48),
    ("ELC-007", "Smart Wi-Fi CCTV 2K", "Electronics", 220000, 385000, 95),
    ("ELC-008", "Powerbank 20000mAh PD", "Electronics", 210000, 369000, 18),
    # Fashion (8)
    ("FSH-001", "Batik Slim-Fit Kemeja Solo", "Fashion", 135000, 265000, 120),
    ("FSH-002", "Jaket Parka Anti-Air Urban", "Fashion", 210000, 399000, 74),
    ("FSH-003", "Sepatu Lari Knit Nusantara", "Fashion", 280000, 520000, 55),
    ("FSH-004", "Tas Ransel Laptop 16-Inch", "Fashion", 175000, 345000, 90),
    ("FSH-005", "Kaos Katun Combed 30s Pack", "Fashion", 78000, 165000, 210),
    ("FSH-006", "Celana Chino Stretch Navy", "Fashion", 115000, 239000, 130),
    ("FSH-007", "Hijab Voal Premium Motif", "Fashion", 48000, 115000, 185),
    ("FSH-008", "Dompet Kulit Asli Garut", "Fashion", 95000, 219000, 14),
    # Home (8)
    ("HOM-001", "Air Fryer Digital 4.5L", "Home", 540000, 899000, 42),
    ("HOM-002", "Vacuum Cleaner Cordless", "Home", 680000, 1150000, 33),
    ("HOM-003", "Rice Cooker Low-Carbo 1.8L", "Home", 360000, 625000, 64),
    ("HOM-004", "Sprei Katun Jepang King", "Home", 195000, 379000, 88),
    ("HOM-005", "Lampu Meja LED Dimmer", "Home", 92000, 185000, 115),
    ("HOM-006", "Kursi Kerja Ergonomis Mesh", "Home", 790000, 1350000, 22),
    ("HOM-007", "Tumbler Stainless Vacuum 750ml", "Home", 72000, 159000, 160),
    ("HOM-008", "Blender Kaca Heavy Duty", "Home", 270000, 465000, 16),
    # Groceries (8)
    ("GRC-001", "Kopi Arabika Gayo 500g", "Groceries", 82000, 145000, 190),
    ("GRC-002", "Madu Hutan Sumbawa 650ml", "Groceries", 95000, 175000, 145),
    ("GRC-003", "Beras Pandan Wangi Organik 5kg", "Groceries", 78000, 118000, 230),
    ("GRC-004", "Minyak Zaitun Extra Virgin 500ml", "Groceries", 105000, 179000, 98),
    ("GRC-005", "Teh Hijau Premium Ciwidey 250g", "Groceries", 45000, 89000, 175),
    ("GRC-006", "Granola Cokelat Almond 400g", "Groceries", 48000, 88000, 150),
    ("GRC-007", "Kecap Manis Organik Paket 3", "Groceries", 42000, 74000, 210),
    ("GRC-008", "Susu Almond Unsweetened 1L x3", "Groceries", 85000, 139000, 19),
    # Health (8)
    ("HLT-001", "Multivitamin Imun Kompleks 60s", "Health", 98000, 195000, 165),
    ("HLT-002", "Omega-3 Fish Oil 1000mg", "Health", 125000, 245000, 112),
    ("HLT-003", "Tensimeter Digital Lengan", "Health", 230000, 415000, 49),
    ("HLT-004", "Termometer Inframerah Medis", "Health", 110000, 215000, 77),
    ("HLT-005", "Whey Protein Isolate 900g", "Health", 340000, 565000, 58),
    ("HLT-006", "Matras Yoga TPE Anti-Slip", "Health", 115000, 235000, 92),
    ("HLT-007", "Essential Oil Eucalyptus 30ml", "Health", 54000, 119000, 140),
    ("HLT-008", "Timbangan Badan Smart BMI", "Health", 155000, 289000, 12),
]

PAYMENT_METHODS = ["Bank Transfer", "QRIS", "Virtual Account", "Credit Card", "COD"]
PAYMENT_WEIGHTS = [0.24, 0.28, 0.23, 0.14, 0.11]

CARRIERS = ["JNE", "J&T Express", "SiCepat", "AnterAja", "Pos Indonesia"]
CARRIER_WEIGHTS = [0.28, 0.26, 0.22, 0.14, 0.10]

# Base delivery days and shipping cost profile per carrier
CARRIER_PROFILES: dict[str, tuple[float, float, float]] = {
    # carrier: (mean_days, base_cost_idr, delay_bias)
    "SiCepat": (2.4, 24000.0, 0.08),
    "J&T Express": (2.9, 22000.0, 0.14),
    "JNE": (3.2, 25000.0, 0.17),
    "AnterAja": (3.5, 21000.0, 0.22),
    "Pos Indonesia": (4.4, 18500.0, 0.36),
}

# Regional hub delay modifier (Makassar has longer inter-island transit)
WAREHOUSE_DELAY_BONUS: dict[int, float] = {
    1: 0.0,   # Surabaya
    2: -0.3,  # Jakarta
    3: 0.1,   # Bandung
    4: 0.2,   # Semarang
    5: 1.2,   # Makassar
}


def seed_database(db_path: str | Path, force: bool = False, seed: int = 42) -> Path:
    """Create and seed the deterministic enterprise analytics SQLite database."""
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() and not force:
        with sqlite3.connect(target) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='orders'"
            ).fetchone()
            if row and row[0] == 1:
                cnt = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
                if cnt >= 600:
                    return target

    if target.exists() and force:
        target.unlink()

    rng = random.Random(seed)
    conn = sqlite3.connect(target)
    try:
        conn.executescript(SCHEMA_DDL)

        # 1. Warehouses (5)
        conn.executemany(
            """
            INSERT INTO warehouses
                (warehouse_id, warehouse_code, name, city, province, region, capacity_units)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            WAREHOUSE_SEED,
        )

        # 2. Customers (120)
        customers_rows: list[tuple[Any, ...]] = []
        segment_pool = ["Retail"] * 72 + ["SMB"] * 32 + ["Enterprise"] * 16
        rng.shuffle(segment_pool)
        signup_start = date(2024, 1, 10)
        for cid in range(1, 121):
            segment = segment_pool[cid - 1]
            city, province = rng.choice(CITY_PROVINCES)
            if segment == "Enterprise":
                name = (
                    f"{rng.choice(COMPANY_PREFIXES)} "
                    f"{rng.choice(COMPANY_SUFFIXES)} {cid:02d}"
                )
            elif segment == "SMB":
                name = (
                    f"Toko {rng.choice(INDONESIAN_FIRST_NAMES)} "
                    f"{rng.choice(INDONESIAN_LAST_NAMES)}"
                )
            else:
                name = (
                    f"{rng.choice(INDONESIAN_FIRST_NAMES)} "
                    f"{rng.choice(INDONESIAN_LAST_NAMES)}"
                )
            slug = name.lower().replace(" ", ".").replace("-", "")
            email = f"{slug}.{cid}@example.co.id"
            signup = (signup_start + timedelta(days=rng.randint(0, 420))).isoformat()
            customers_rows.append((cid, name, email, city, province, segment, signup))

        conn.executemany(
            """
            INSERT INTO customers
                (customer_id, customer_name, email, city, province, segment, signup_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            customers_rows,
        )
        customer_segments = {r[0]: r[5] for r in customers_rows}

        # 3. Products (40)
        product_rows: list[tuple[Any, ...]] = []
        for pid, (sku, pname, cat, cost, price, stock) in enumerate(PRODUCT_CATALOG, start=1):
            product_rows.append((pid, sku, pname, cat, float(cost), float(price), stock))

        conn.executemany(
            """
            INSERT INTO products
                (product_id, sku, product_name, category, unit_cost, unit_price, stock_qty)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            product_rows,
        )
        product_prices = {r[0]: r[5] for r in product_rows}

        # 4. Orders (600 across 2025-01-01 .. 2026-06-30), Order Items (~1,400), Shipments (~500)
        order_start = date(2025, 1, 1)
        total_days = (date(2026, 6, 30) - order_start).days
        warehouse_weights = [0.24, 0.32, 0.18, 0.14, 0.12]
        warehouse_ids = [1, 2, 3, 4, 5]

        orders_rows: list[tuple[Any, ...]] = []
        items_rows: list[tuple[Any, ...]] = []
        shipments_rows: list[tuple[Any, ...]] = []

        item_id = 1
        shipment_id = 1

        for oid in range(1, 601):
            cid = rng.randint(1, 120)
            seg = customer_segments[cid]
            wid = rng.choices(warehouse_ids, weights=warehouse_weights, k=1)[0]

            # Add realistic slight Q4 & 2026 growth tilt
            day_offset = rng.randint(0, total_days)
            odate = order_start + timedelta(days=day_offset)

            # ~70% completed, ~13.5% shipped, ~11% cancelled, ~5.5% returned
            # That yields ~534 non-cancelled -> wait: let's target ~505 shipments
            # (84% non-cancelled: 68% completed, 11% shipped, 5% returned, 16% cancelled -> ~504 shipments)
            status = rng.choices(
                ["completed", "shipped", "returned", "cancelled"],
                weights=[0.68, 0.11, 0.05, 0.16],
                k=1,
            )[0]

            payment = rng.choices(PAYMENT_METHODS, weights=PAYMENT_WEIGHTS, k=1)[0]

            # Number of line items: Enterprise tends to buy more items & higher qty
            if seg == "Enterprise":
                num_items = rng.choices([2, 3, 4], weights=[0.25, 0.45, 0.30], k=1)[0]
                qty_range = (2, 6)
            elif seg == "SMB":
                num_items = rng.choices([1, 2, 3, 4], weights=[0.15, 0.35, 0.35, 0.15], k=1)[0]
                qty_range = (1, 4)
            else:
                num_items = rng.choices([1, 2, 3], weights=[0.35, 0.45, 0.20], k=1)[0]
                qty_range = (1, 3)

            chosen_pids = rng.sample(range(1, 41), k=num_items)
            gross_total = 0.0
            for pid in chosen_pids:
                qty = rng.randint(qty_range[0], qty_range[1])
                uprice = product_prices[pid]
                line_total = round(qty * uprice, 2)
                gross_total += line_total
                items_rows.append((item_id, oid, pid, qty, uprice, line_total))
                item_id += 1

            # Discount: 40% of orders have 5%-15% voucher discount
            if rng.random() < 0.40:
                disc_rate = rng.choice([0.05, 0.08, 0.10, 0.12, 0.15])
                discount = round(gross_total * disc_rate, 2)
            else:
                discount = 0.0

            total_amount = round(max(0.0, gross_total - discount), 2)
            orders_rows.append(
                (oid, cid, wid, odate.isoformat(), status, payment, discount, total_amount)
            )

            # Create shipment for every non-cancelled order (~504 shipments)
            if status != "cancelled":
                carrier = rng.choices(CARRIERS, weights=CARRIER_WEIGHTS, k=1)[0]
                mean_days, base_cost, _ = CARRIER_PROFILES[carrier]
                wh_bonus = WAREHOUSE_DELAY_BONUS[wid]
                raw_days = mean_days + wh_bonus + rng.gauss(0.0, 1.1)
                delivery_days = max(1, min(10, int(round(raw_days))))
                is_delayed = 1 if delivery_days > 4 else 0
                ship_date = (odate + timedelta(days=rng.randint(0, 2))).isoformat()
                shipping_cost = round(
                    base_cost * (1.0 + 0.15 * num_items) + rng.randint(-3000, 9000),
                    2,
                )
                shipments_rows.append(
                    (
                        shipment_id,
                        oid,
                        wid,
                        carrier,
                        ship_date,
                        max(12000.0, shipping_cost),
                        delivery_days,
                        is_delayed,
                    )
                )
                shipment_id += 1

        conn.executemany(
            """
            INSERT INTO orders
                (order_id, customer_id, warehouse_id, order_date, status,
                 payment_method, discount_amount, total_amount)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            orders_rows,
        )
        conn.executemany(
            """
            INSERT INTO order_items
                (order_item_id, order_id, product_id, quantity, unit_price, line_total)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            items_rows,
        )
        conn.executemany(
            """
            INSERT INTO shipments
                (shipment_id, order_id, warehouse_id, carrier, shipped_date,
                 shipping_cost, delivery_days, is_delayed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            shipments_rows,
        )

        conn.commit()
    finally:
        conn.close()

    return target


def ensure_database(db_path: str | Path) -> Path:
    """Ensure the SQLite analytics database exists and is seeded."""
    return seed_database(db_path, force=False)


def get_schema_info(db_path: str | Path, sample_limit: int = 3) -> DatabaseSchemaResponse:
    """Introspect tables, columns, foreign keys, row counts, DDL, and sample rows."""
    target = ensure_database(db_path).resolve()
    conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        table_rows = conn.execute(
            """
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

        tables: list[TableSchema] = []
        total_rows = 0

        for trow in table_rows:
            tname = trow["name"]
            ddl = trow["sql"] or ""
            row_count = int(conn.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()[0])
            total_rows += row_count

            fk_map: dict[str, str] = {}
            for fk in conn.execute(f'PRAGMA foreign_key_list("{tname}")').fetchall():
                fk_map[fk["from"]] = f"{fk['table']}.{fk['to']}"

            columns: list[ColumnSchema] = []
            for col in conn.execute(f'PRAGMA table_info("{tname}")').fetchall():
                cname = col["name"]
                columns.append(
                    ColumnSchema(
                        name=cname,
                        data_type=col["type"] or "TEXT",
                        nullable=not bool(col["notnull"]),
                        primary_key=bool(col["pk"]),
                        foreign_key=fk_map.get(cname),
                    )
                )

            samples = [
                dict(r)
                for r in conn.execute(
                    f'SELECT * FROM "{tname}" LIMIT ?', (sample_limit,)
                ).fetchall()
            ]

            tables.append(
                TableSchema(
                    name=tname,
                    row_count=row_count,
                    columns=columns,
                    ddl=ddl.strip(),
                    sample_rows=samples,
                )
            )

        return DatabaseSchemaResponse(
            db_path=str(target),
            table_count=len(tables),
            total_rows=total_rows,
            tables=tables,
        )
    finally:
        conn.close()


def get_schema_context(
    db_path: str | Path,
    selected_tables: list[str] | None = None,
) -> str:
    """Render concise DDL, FK metadata, business definitions, and sample values for prompting."""
    info = get_schema_info(db_path, sample_limit=2)
    chosen = set(selected_tables) if selected_tables else None
    lines: list[str] = [
        "-- SQLite Enterprise E-Commerce & Logistics Schema",
        "-- Business Definitions:",
        "--   Recognized Revenue / Valid Sales: orders.status IN ('completed', 'shipped')",
        "--   Order Gross Line Total: order_items.line_total = quantity * unit_price",
        "--   Order Net Total: orders.total_amount = MAX(0, SUM(line_total) - discount_amount)",
        "--   Delayed Shipment Flag: shipments.is_delayed = 1 when delivery_days > 4",
        "",
    ]
    for table in info.tables:
        if chosen is not None and table.name not in chosen:
            continue
        lines.append(f"-- Table: {table.name} ({table.row_count} rows)")
        lines.append(table.ddl + ";")
        fks = [f"{c.name} -> {c.foreign_key}" for c in table.columns if c.foreign_key]
        if fks:
            lines.append(f"-- Foreign Keys: {', '.join(fks)}")
        if table.sample_rows:
            lines.append(f"-- Sample Row: {table.sample_rows[0]}")
        lines.append("")
    return "\n".join(lines).strip()
