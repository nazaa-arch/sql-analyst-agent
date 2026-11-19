"""Multi-provider SQL Planner & Self-Corrector (Local bilingual + Ollama/Gemini/OpenAI)."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from sql_analyst.config import Settings
from sql_analyst.schemas import ChartType, LanguageCode, SQLPlan, TableSchema

INDONESIAN_MARKERS: frozenset[str] = frozenset(
    {
        "berapa", "total", "penjualan", "pendapatan", "omzet", "produk", "pelanggan",
        "gudang", "pengiriman", "terlambat", "tertinggi", "terbanyak", "terlaris",
        "bulanan", "kuartal", "kategori", "rata-rata", "pembatalan", "pengembalian",
        "keuntungan", "laba", "kurir", "ekspedisi", "provinsi", "segmen", "pembayaran",
        "metode", "tampilkan", "daftar", "bandingkan", "siapa", "mana", "dari", "yang",
        "untuk", "tahun", "bulan", "pesanan", "diskon", "stok", "hapus", "ubah",
        "semua", "tercepat", "terendah", "paling", "berdasarkan", "setiap", "tiap",
        "kinerja", "performa", "ringkasan", "hari", "biaya", "persentase", "tingkat",
    }
)

RAW_UNSAFE_SQL_RE = re.compile(
    r"\b(DROP\s+TABLE|DROP\s+DATABASE|DELETE\s+FROM|UPDATE\s+[A-Za-z_]+\s+SET|"
    r"INSERT\s+INTO|ALTER\s+TABLE|TRUNCATE\s+TABLE|CREATE\s+TABLE|ATTACH\s+DATABASE|"
    r"DETACH\s+DATABASE|PRAGMA\s+|VACUUM\b|REINDEX\b|LOAD_EXTENSION\s*\(|REPLACE\s+INTO)\b",
    re.IGNORECASE,
)

DESTRUCTIVE_NL_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"\b(hapus|delete|remove|wipe|clear|kosongkan)\b.*\b(order_items?)\b", re.I),
        "DELETE FROM order_items",
        "Destructive request attempting to delete rows from order_items.",
    ),
    (
        re.compile(r"\b(hapus|delete|remove|wipe|clear|kosongkan)\b.*\b(orders?|pesanan)\b", re.I),
        "DELETE FROM orders",
        "Destructive request attempting to delete rows from orders.",
    ),
    (
        re.compile(r"\b(hapus|delete|remove|wipe|clear|kosongkan)\b.*\b(customers?|pelanggan)\b", re.I),
        "DELETE FROM customers",
        "Destructive request attempting to delete rows from customers.",
    ),
    (
        re.compile(r"\b(hapus|delete|remove|wipe|clear|kosongkan)\b.*\b(products?|produk)\b", re.I),
        "DELETE FROM products",
        "Destructive request attempting to delete rows from products.",
    ),
    (
        re.compile(r"\b(hapus|delete|drop|hancurkan|destroy)\b.*\b(table|tabel|database|data|semua|all)\b", re.I),
        "DROP TABLE orders",
        "Destructive request attempting to drop or delete database records.",
    ),
    (
        re.compile(r"\b(ubah|update|modify|modifikasi|ganti)\b.*\b(status|harga|price|orders?|pesanan|data)\b", re.I),
        "UPDATE orders SET status = 'completed'",
        "Write/mutation request attempting to update database records.",
    ),
    (
        re.compile(r"\b(tambah|insert|masukkan|create)\b.*\b(row|baris|record|tabel|table)\b", re.I),
        "INSERT INTO orders (order_id) VALUES (99999)",
        "Write request attempting to insert or create database objects.",
    ),
]

SYSTEM_SQL_PROMPT = """You are a Staff Data & SQL Analyst Agent for a SQLite e-commerce & logistics database.
You MUST follow these rules strictly:
1. Generate ONLY a single read-only SQLite SELECT (or WITH ... SELECT) query. Never generate DDL/DML/PRAGMA/stacked queries.
2. Use ONLY tables and columns defined in the provided schema.
3. Unless the user specifies a particular order status, recognized revenue/sales uses orders.status IN ('completed', 'shipped').
4. Respond ONLY with a valid JSON object matching this exact schema:
{
  "reasoning": "concise analytical explanation",
  "sql": "SELECT ...",
  "chart_type": "bar" | "line" | "doughnut" | "table",
  "selected_tables": ["orders", "..."],
  "language": "en" | "id"
}
"""


def detect_language(text: str) -> LanguageCode:
    """Detect whether a query is primarily Indonesian ('id') or English ('en')."""
    tokens = re.findall(r"[a-zA-Z\-]+", text.lower())
    id_hits = sum(1 for t in tokens if t in INDONESIAN_MARKERS)
    if id_hits >= 2:
        return "id"
    strong_id = {
        "berapa", "tampilkan", "penjualan", "pendapatan", "pelanggan", "gudang",
        "pengiriman", "terlambat", "terlaris", "hapus", "pembatalan", "kurir",
        "bulanan", "kuartal", "keuntungan", "pesanan", "kategori", "provinsi",
    }
    if any(t in strong_id for t in tokens):
        return "id"
    return "en"


def _extract_top_n(q: str, default: int = 5) -> int:
    patterns = [
        r"\btop\s+(\d+)\b",
        r"\b(\d+)\s+(?:teratas|tertinggi|terbanyak|terlaris|terbesar|terendah|besar|produk|pelanggan|provinsi)\b",
        r"\blimit\s+(\d+)\b",
        r"\bbest\s+(\d+)\b",
        r"\bfirst\s+(\d+)\b",
    ]
    for pat in patterns:
        m = re.search(pat, q, re.IGNORECASE)
        if m:
            val = int(m.group(1))
            if 1 <= val <= 100:
                return val
    return default


def _extract_year(q: str) -> str | None:
    m = re.search(r"\b(2025|2026)\b", q)
    return m.group(1) if m else None


def _extract_category(q: str) -> str | None:
    mapping = {
        "electronics": "Electronics",
        "elektronik": "Electronics",
        "fashion": "Fashion",
        "home": "Home",
        "rumah": "Home",
        "groceries": "Groceries",
        "sembako": "Groceries",
        "health": "Health",
        "kesehatan": "Health",
    }
    low = q.lower()
    for key, canonical in mapping.items():
        if re.search(rf"\b{key}\b", low):
            return canonical
    return None


def _extract_segment(q: str) -> str | None:
    low = q.lower()
    if re.search(r"\benterprise\b", low):
        return "Enterprise"
    if re.search(r"\bsmb\b", low):
        return "SMB"
    if re.search(r"\b(retail|ritel)\b", low):
        return "Retail"
    return None


def _extract_warehouse_city(q: str) -> str | None:
    low = q.lower()
    for city in ("Surabaya", "Jakarta", "Bandung", "Semarang", "Makassar"):
        if re.search(rf"\b{city.lower()}\b", low):
            return city
    return None


def _extract_explicit_status(q: str) -> str | None:
    low = q.lower()
    has_completed = bool(re.search(r"\b(completed|selesai)\b", low))
    has_shipped = bool(re.search(r"\b(shipped|sedang dikirim)\b", low))
    if has_completed and has_shipped:
        return None
    if has_completed:
        return "completed"
    if re.search(r"\b(cancelled|canceled|dibatalkan)\b", low):
        return "cancelled"
    if re.search(r"\b(returned|dikembalikan|retur)\b", low):
        return "returned"
    if has_shipped:
        return "shipped"
    return None


def _parse_json_payload(raw_text: str) -> dict[str, Any]:
    """Parse JSON from an LLM response, stripping optional markdown fences."""
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1)
    else:
        brace_start = cleaned.find("{")
        brace_end = cleaned.rfind("}")
        if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
            cleaned = cleaned[brace_start : brace_end + 1]
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("Planner JSON payload must be an object.")
    return data


class BaseSQLPlanner(ABC):
    """Abstract interface for SQL planning and error-driven self-correction."""

    provider_name: str = "base"

    @abstractmethod
    async def plan(
        self,
        question: str,
        schema_context: str,
        tables: list[TableSchema],
    ) -> SQLPlan:
        """Generate a structured SQLPlan from a user question and schema context."""

    @abstractmethod
    async def repair(
        self,
        question: str,
        failed_sql: str,
        error_message: str,
        schema_context: str,
        tables: list[TableSchema],
    ) -> SQLPlan:
        """Repair a SQL query given the exact SQLite error message."""


class LocalSQLPlanner(BaseSQLPlanner):
    """Deterministic, zero-dependency bilingual (ID/EN) SQL Planner & Self-Corrector."""

    provider_name: str = "local"

    async def plan(
        self,
        question: str,
        schema_context: str,
        tables: list[TableSchema],
    ) -> SQLPlan:
        del schema_context, tables
        q_raw = question.strip()
        q = q_raw.lower()
        lang = detect_language(q_raw)

        # 1. Check raw dangerous SQL or stacked injection attempts first
        if RAW_UNSAFE_SQL_RE.search(q_raw) or (";" in q_raw and re.search(r";\s*\w+", q_raw)):
            return SQLPlan(
                reasoning="Detected potentially unsafe or multi-statement SQL in user input; forwarding to strict sandbox validator for enforcement.",
                sql=q_raw,
                chart_type="table",
                selected_tables=[],
                language=lang,
                intent="unsafe_sql",
                is_unsafe_request=True,
                unsafe_reason="Raw destructive, administrative, or stacked SQL detected in prompt.",
            )

        # 2. Check natural-language destructive/write requests (ID & EN)
        for pattern, synth_sql, reason in DESTRUCTIVE_NL_PATTERNS:
            if pattern.search(q_raw):
                return SQLPlan(
                    reasoning=f"Detected write/destructive intent: {reason}",
                    sql=synth_sql,
                    chart_type="table",
                    selected_tables=[],
                    language=lang,
                    intent="unsafe_nl",
                    is_unsafe_request=True,
                    unsafe_reason=reason,
                )

        # 3. Direct read-only SQL pass-through (if user typed a SELECT / WITH query directly)
        if re.match(r"^\s*(SELECT|WITH)\b", q_raw, re.IGNORECASE):
            return SQLPlan(
                reasoning="Direct read-only SQL query provided by analyst; routing through security sandbox.",
                sql=q_raw,
                chart_type="table",
                selected_tables=[],
                language=lang,
                intent="direct_sql",
            )

        # Extract common query modifiers
        top_n = _extract_top_n(q, default=5)
        year = _extract_year(q)
        category = _extract_category(q)
        segment = _extract_segment(q)
        warehouse_city = _extract_warehouse_city(q)
        explicit_status = _extract_explicit_status(q)
        asc = bool(
            re.search(
                r"\b(lowest|terendah|fastest|tercepat|paling cepat|least|terkecil|paling sedikit|minimum)\b",
                q,
            )
        )
        sort_dir = "ASC" if asc else "DESC"

        def _revenue_where(alias: str = "o") -> list[str]:
            conds: list[str] = []
            if explicit_status:
                conds.append(f"{alias}.status = '{explicit_status}'")
            else:
                conds.append(f"{alias}.status IN ('completed', 'shipped')")
            if year:
                conds.append(f"strftime('%Y', {alias}.order_date) = '{year}'")
            return conds

        # 4. Quarterly revenue & order trend
        if re.search(r"\b(quarter|quarterly|kuartal|triwulan)\b", q):
            conds = _revenue_where("o")
            where_sql = "WHERE " + " AND ".join(conds)
            sql = f"""
SELECT
    strftime('%Y', o.order_date) || '-Q' || ((CAST(strftime('%m', o.order_date) AS INTEGER) - 1) / 3 + 1) AS quarter,
    COUNT(o.order_id) AS total_orders,
    ROUND(SUM(o.total_amount), 2) AS total_revenue,
    ROUND(AVG(o.total_amount), 2) AS avg_order_value
FROM orders o
{where_sql}
GROUP BY quarter
ORDER BY quarter ASC
""".strip()
            return SQLPlan(
                reasoning="Aggregating recognized orders and revenue by calendar quarter to reveal quarterly growth trends.",
                sql=sql,
                chart_type="line",
                selected_tables=["orders"],
                language=lang,
                intent="quarterly_trend",
            )

        # 5. Monthly revenue & order trend
        if re.search(r"\b(monthly|bulanan|per bulan|by month|tiap bulan|setiap bulan|trend|tren)\b", q):
            conds = _revenue_where("o")
            where_sql = "WHERE " + " AND ".join(conds)
            sql = f"""
SELECT
    strftime('%Y-%m', o.order_date) AS month,
    COUNT(o.order_id) AS total_orders,
    ROUND(SUM(o.total_amount), 2) AS total_revenue,
    ROUND(AVG(o.total_amount), 2) AS avg_order_value
FROM orders o
{where_sql}
GROUP BY month
ORDER BY month ASC
""".strip()
            return SQLPlan(
                reasoning="Aggregating recognized monthly order volume and revenue chronologically.",
                sql=sql,
                chart_type="line",
                selected_tables=["orders"],
                language=lang,
                intent="monthly_trend",
            )

        # 6. Cancellation / Return rate analysis
        if re.search(r"\b(cancel|cancelled|canceled|cancellation|batal|pembatalan|return rate|returned|pengembalian|retur)\b", q):
            if re.search(r"\b(warehouses?|gudang|hubs?)\b", q):
                sql = """
SELECT
    w.name AS warehouse_name,
    COUNT(o.order_id) AS total_orders,
    SUM(CASE WHEN o.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_orders,
    SUM(CASE WHEN o.status = 'returned' THEN 1 ELSE 0 END) AS returned_orders,
    ROUND(100.0 * SUM(CASE WHEN o.status = 'cancelled' THEN 1 ELSE 0 END) / COUNT(o.order_id), 2) AS cancellation_rate_pct,
    ROUND(100.0 * SUM(CASE WHEN o.status = 'returned' THEN 1 ELSE 0 END) / COUNT(o.order_id), 2) AS return_rate_pct
FROM orders o
JOIN warehouses w ON w.warehouse_id = o.warehouse_id
GROUP BY w.warehouse_id, w.name
ORDER BY cancellation_rate_pct DESC, w.name ASC
""".strip()
                return SQLPlan(
                    reasoning="Joining orders with warehouses to compare cancellation and return rates across logistics hubs.",
                    sql=sql,
                    chart_type="bar",
                    selected_tables=["orders", "warehouses"],
                    language=lang,
                    intent="cancellation_by_warehouse",
                )

            if re.search(r"\b(category|kategori)\b", q):
                sql = """
SELECT
    p.category,
    COUNT(DISTINCT o.order_id) AS total_orders,
    COUNT(DISTINCT CASE WHEN o.status = 'cancelled' THEN o.order_id END) AS cancelled_orders,
    COUNT(DISTINCT CASE WHEN o.status = 'returned' THEN o.order_id END) AS returned_orders,
    ROUND(100.0 * COUNT(DISTINCT CASE WHEN o.status = 'returned' THEN o.order_id END) / COUNT(DISTINCT o.order_id), 2) AS return_rate_pct
FROM orders o
JOIN order_items oi ON oi.order_id = o.order_id
JOIN products p ON p.product_id = oi.product_id
GROUP BY p.category
ORDER BY return_rate_pct DESC, p.category ASC
""".strip()
                return SQLPlan(
                    reasoning="Joining orders, order_items, and products to compute return and cancellation rates per category.",
                    sql=sql,
                    chart_type="bar",
                    selected_tables=["orders", "order_items", "products"],
                    language=lang,
                    intent="return_by_category",
                )

            sql = """
SELECT
    o.status,
    COUNT(o.order_id) AS order_count,
    ROUND(100.0 * COUNT(o.order_id) / (SELECT COUNT(*) FROM orders), 2) AS pct_of_orders,
    ROUND(SUM(o.total_amount), 2) AS total_amount
FROM orders o
GROUP BY o.status
ORDER BY order_count DESC
""".strip()
            return SQLPlan(
                reasoning="Computing order counts, percentage shares (including cancellation and return rates), and value across all order statuses.",
                sql=sql,
                chart_type="doughnut",
                selected_tables=["orders"],
                language=lang,
                intent="order_status_rates",
            )

        # 7. Order status breakdown
        if re.search(r"\b(status pesanan|order status|status order|breakdown status)\b", q):
            sql = """
SELECT
    o.status,
    COUNT(o.order_id) AS order_count,
    ROUND(100.0 * COUNT(o.order_id) / (SELECT COUNT(*) FROM orders), 2) AS pct_of_orders,
    ROUND(SUM(o.total_amount), 2) AS total_amount
FROM orders o
GROUP BY o.status
ORDER BY order_count DESC
""".strip()
            return SQLPlan(
                reasoning="Breaking down total orders and percentage distribution by order status.",
                sql=sql,
                chart_type="doughnut",
                selected_tables=["orders"],
                language=lang,
                intent="order_status_breakdown",
            )

        # 8. Shipping / Delivery / Delay Performance (by warehouse or carrier)
        if re.search(
            r"\b(carriers?|kurir|ekspedisi|delivery|pengiriman|shipments?|delayed|terlambat|keterlambatan|shipping cost|ongkir|biaya kirim)\b",
            q,
        ):
            if re.search(r"\b(warehouses?|gudang|hubs?)\b", q) and not re.search(r"\b(carriers?|kurir|ekspedisi)\b", q):
                sql = f"""
SELECT
    w.name AS warehouse_name,
    w.city,
    COUNT(s.shipment_id) AS total_shipments,
    SUM(s.is_delayed) AS delayed_shipments,
    ROUND(100.0 * SUM(s.is_delayed) / COUNT(s.shipment_id), 2) AS delay_rate_pct,
    ROUND(AVG(s.delivery_days), 2) AS avg_delivery_days,
    ROUND(AVG(s.shipping_cost), 2) AS avg_shipping_cost
FROM shipments s
JOIN warehouses w ON w.warehouse_id = s.warehouse_id
GROUP BY w.warehouse_id, w.name, w.city
ORDER BY delay_rate_pct {sort_dir}, w.name ASC
""".strip()
                return SQLPlan(
                    reasoning="Joining shipments with warehouses to evaluate delivery speed and delay rates per logistics hub.",
                    sql=sql,
                    chart_type="bar",
                    selected_tables=["shipments", "warehouses"],
                    language=lang,
                    intent="warehouse_delivery_performance",
                )

            order_col = "avg_delivery_days" if re.search(r"\b(day|hari|fast|cepat|speed|lama|durasi|rata-rata)\b", q) else "delay_rate_pct"
            if re.search(r"\b(cost|biaya|ongkir)\b", q):
                order_col = "avg_shipping_cost"
            sql = f"""
SELECT
    s.carrier,
    COUNT(s.shipment_id) AS total_shipments,
    ROUND(AVG(s.delivery_days), 2) AS avg_delivery_days,
    SUM(s.is_delayed) AS delayed_shipments,
    ROUND(100.0 * SUM(s.is_delayed) / COUNT(s.shipment_id), 2) AS delay_rate_pct,
    ROUND(AVG(s.shipping_cost), 2) AS avg_shipping_cost
FROM shipments s
GROUP BY s.carrier
ORDER BY {order_col} {sort_dir}, s.carrier ASC
""".strip()
            return SQLPlan(
                reasoning="Aggregating shipment delivery days, delay counts, delay rate, and average shipping cost per logistics carrier.",
                sql=sql,
                chart_type="bar",
                selected_tables=["shipments"],
                language=lang,
                intent="carrier_delivery_performance",
            )

        # 9. Profit & Margin Analysis (by product or category)
        if re.search(r"\b(margin|profit|profitable|laba|keuntungan|untung)\b", q):
            conds = _revenue_where("o")
            if category:
                conds.append(f"p.category = '{category}'")
            where_sql = "WHERE " + " AND ".join(conds)

            is_category_group = bool(
                re.search(
                    r"\b(by\s+(?:product\s+)?category|per\s+kategori|berdasarkan\s+kategori|kategori\s+produk|categories)\b",
                    q,
                )
            )
            if re.search(r"\b(products?|produk|barang|sku)\b", q) and not is_category_group:
                order_expr = "profit_margin_pct" if re.search(r"\b(margin|persen|pct)\b", q) else "gross_profit"
                sql = f"""
SELECT
    p.product_name,
    p.category,
    SUM(oi.quantity) AS units_sold,
    ROUND(SUM(oi.line_total), 2) AS total_revenue,
    ROUND(SUM(oi.quantity * p.unit_cost), 2) AS total_cost,
    ROUND(SUM(oi.line_total - oi.quantity * p.unit_cost), 2) AS gross_profit,
    ROUND(100.0 * SUM(oi.line_total - oi.quantity * p.unit_cost) / SUM(oi.line_total), 2) AS profit_margin_pct
FROM order_items oi
JOIN orders o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id
{where_sql}
GROUP BY p.product_id, p.product_name, p.category
ORDER BY {order_expr} {sort_dir}, p.product_name ASC
LIMIT {top_n}
""".strip()
                return SQLPlan(
                    reasoning="Joining order_items, orders, and products to calculate gross profit and profit margin percentage per product.",
                    sql=sql,
                    chart_type="bar",
                    selected_tables=["order_items", "orders", "products"],
                    language=lang,
                    intent="product_profit_margin",
                )

            sql = f"""
SELECT
    p.category,
    SUM(oi.quantity) AS units_sold,
    ROUND(SUM(oi.line_total), 2) AS total_revenue,
    ROUND(SUM(oi.quantity * p.unit_cost), 2) AS total_cost,
    ROUND(SUM(oi.line_total - oi.quantity * p.unit_cost), 2) AS gross_profit,
    ROUND(100.0 * SUM(oi.line_total - oi.quantity * p.unit_cost) / SUM(oi.line_total), 2) AS profit_margin_pct
FROM order_items oi
JOIN orders o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id
{where_sql}
GROUP BY p.category
ORDER BY gross_profit {sort_dir}, p.category ASC
""".strip()
            return SQLPlan(
                reasoning="Calculating gross revenue, unit cost, gross profit, and profit margin percentage by product category.",
                sql=sql,
                chart_type="bar",
                selected_tables=["order_items", "orders", "products"],
                language=lang,
                intent="category_profit_margin",
            )

        # 10. Payment method breakdown
        if re.search(r"\b(payment|pembayaran|metode pembayaran|qris|virtual account|credit card|cod|bank transfer)\b", q):
            conds = _revenue_where("o")
            where_sql = "WHERE " + " AND ".join(conds)
            sql = f"""
SELECT
    o.payment_method,
    COUNT(o.order_id) AS order_count,
    ROUND(SUM(o.total_amount), 2) AS total_revenue,
    ROUND(AVG(o.total_amount), 2) AS avg_order_value
FROM orders o
{where_sql}
GROUP BY o.payment_method
ORDER BY total_revenue DESC, o.payment_method ASC
""".strip()
            return SQLPlan(
                reasoning="Grouping recognized orders by payment method to measure transaction volume and revenue share.",
                sql=sql,
                chart_type="doughnut",
                selected_tables=["orders"],
                language=lang,
                intent="payment_method_breakdown",
            )

        # 11. Discount analysis
        if re.search(r"\b(discount|diskon|potongan|voucher)\b", q):
            conds = _revenue_where("o")
            where_sql = "WHERE " + " AND ".join(conds)
            if re.search(r"\b(segment|segmen)\b", q):
                sql = f"""
SELECT
    c.segment,
    COUNT(o.order_id) AS order_count,
    ROUND(SUM(o.discount_amount), 2) AS total_discount,
    ROUND(AVG(o.discount_amount), 2) AS avg_discount,
    ROUND(SUM(o.total_amount), 2) AS net_revenue
FROM orders o
JOIN customers c ON c.customer_id = o.customer_id
{where_sql}
GROUP BY c.segment
ORDER BY total_discount DESC, c.segment ASC
""".strip()
                return SQLPlan(
                    reasoning="Joining orders and customers to analyze total and average discounts granted across customer segments.",
                    sql=sql,
                    chart_type="bar",
                    selected_tables=["orders", "customers"],
                    language=lang,
                    intent="discount_by_segment",
                )

            sql = f"""
SELECT
    COUNT(o.order_id) AS total_orders,
    SUM(CASE WHEN o.discount_amount > 0 THEN 1 ELSE 0 END) AS discounted_orders,
    ROUND(SUM(o.discount_amount), 2) AS total_discount,
    ROUND(AVG(o.discount_amount), 2) AS avg_discount_per_order,
    ROUND(SUM(o.total_amount), 2) AS net_revenue
FROM orders o
{where_sql}
""".strip()
            return SQLPlan(
                reasoning="Summarizing total voucher discount volume, discounted order count, and net revenue.",
                sql=sql,
                chart_type="table",
                selected_tables=["orders"],
                language=lang,
                intent="discount_summary",
            )

        # 12. Customer Segment Analysis
        if re.search(r"\b(segments?|segmen)\b", q) and not re.search(r"\btop\s+\d+\s+(?:customers?|pelanggan)\b", q):
            conds = _revenue_where("o")
            where_sql = "WHERE " + " AND ".join(conds)
            sql = f"""
SELECT
    c.segment,
    COUNT(DISTINCT c.customer_id) AS active_customers,
    COUNT(o.order_id) AS total_orders,
    ROUND(SUM(o.total_amount), 2) AS total_revenue,
    ROUND(AVG(o.total_amount), 2) AS avg_order_value
FROM orders o
JOIN customers c ON c.customer_id = o.customer_id
{where_sql}
GROUP BY c.segment
ORDER BY total_revenue DESC, c.segment ASC
""".strip()
            return SQLPlan(
                reasoning="Analyzing customer count, order volume, total revenue, and AOV across Retail, SMB, and Enterprise segments.",
                sql=sql,
                chart_type="doughnut",
                selected_tables=["orders", "customers"],
                language=lang,
                intent="segment_analysis",
            )

        # 13. Province / Geography revenue breakdown
        if re.search(r"\b(provinces?|provinsi|wilayah|daerah|regions?|geography)\b", q):
            conds = _revenue_where("o")
            where_sql = "WHERE " + " AND ".join(conds)
            limit_n = _extract_top_n(q, default=10)
            sql = f"""
SELECT
    c.province,
    COUNT(DISTINCT c.customer_id) AS customer_count,
    COUNT(o.order_id) AS total_orders,
    ROUND(SUM(o.total_amount), 2) AS total_revenue
FROM orders o
JOIN customers c ON c.customer_id = o.customer_id
{where_sql}
GROUP BY c.province
ORDER BY total_revenue {sort_dir}, c.province ASC
LIMIT {limit_n}
""".strip()
            return SQLPlan(
                reasoning="Aggregating customer orders and recognized revenue by Indonesian province.",
                sql=sql,
                chart_type="bar",
                selected_tables=["orders", "customers"],
                language=lang,
                intent="revenue_by_province",
            )

        # 14. Warehouse performance (revenue & orders)
        if re.search(r"\b(warehouses?|gudang|hubs?)\b", q):
            conds = _revenue_where("o")
            if warehouse_city:
                conds.append(f"w.city = '{warehouse_city}'")
            where_sql = "WHERE " + " AND ".join(conds)
            sql = f"""
SELECT
    w.name AS warehouse_name,
    w.city,
    COUNT(o.order_id) AS total_orders,
    ROUND(SUM(o.total_amount), 2) AS total_revenue,
    ROUND(AVG(o.total_amount), 2) AS avg_order_value
FROM orders o
JOIN warehouses w ON w.warehouse_id = o.warehouse_id
{where_sql}
GROUP BY w.warehouse_id, w.name, w.city
ORDER BY total_revenue {sort_dir}, w.name ASC
""".strip()
            return SQLPlan(
                reasoning="Joining orders and warehouses to compare order volume, total revenue, and AOV across regional hubs.",
                sql=sql,
                chart_type="bar",
                selected_tables=["orders", "warehouses"],
                language=lang,
                intent="warehouse_revenue",
            )

        # 15. Inventory / Low stock products
        if re.search(r"\b(stock|stok|inventory|inventaris|menipis|low stock)\b", q):
            limit_n = _extract_top_n(q, default=10)
            cond = f"WHERE p.category = '{category}'" if category else ""
            sql = f"""
SELECT
    p.sku,
    p.product_name,
    p.category,
    p.stock_qty,
    p.unit_price
FROM products p
{cond}
ORDER BY p.stock_qty ASC, p.product_name ASC
LIMIT {limit_n}
""".strip()
            return SQLPlan(
                reasoning="Listing products with the lowest remaining stock levels for replenishment planning.",
                sql=sql,
                chart_type="bar",
                selected_tables=["products"],
                language=lang,
                intent="low_stock_products",
            )

        # 16. Top customers (by spend or order frequency)
        if re.search(r"\b(customer|customers|pelanggan|pembeli|buyer|klien)\b", q):
            conds = _revenue_where("o")
            if segment:
                conds.append(f"c.segment = '{segment}'")
            where_sql = "WHERE " + " AND ".join(conds)
            by_orders = bool(
                re.search(r"\b(repeat|frequency|frekuensi|most orders|order terbanyak|pesanan terbanyak|sering)\b", q)
                and not re.search(r"\b(revenue|spend|spending|belanja|pendapatan|nilai)\b", q)
            )
            order_col = "total_orders" if by_orders else "total_spent"
            sql = f"""
SELECT
    c.customer_name,
    c.segment,
    c.province,
    COUNT(o.order_id) AS total_orders,
    ROUND(SUM(o.total_amount), 2) AS total_spent
FROM orders o
JOIN customers c ON c.customer_id = o.customer_id
{where_sql}
GROUP BY c.customer_id, c.customer_name, c.segment, c.province
ORDER BY {order_col} {sort_dir}, c.customer_name ASC
LIMIT {top_n}
""".strip()
            return SQLPlan(
                reasoning="Joining orders and customers to rank top customers by spending and order frequency.",
                sql=sql,
                chart_type="bar",
                selected_tables=["orders", "customers"],
                language=lang,
                intent="top_customers",
            )

        # 17. Top products (by revenue or quantity sold)
        if re.search(r"\b(product|products|produk|barang|sku|terlaris|best[- ]?sell|item)\b", q):
            conds = _revenue_where("o")
            if category:
                conds.append(f"p.category = '{category}'")
            where_sql = "WHERE " + " AND ".join(conds)
            by_qty = bool(
                re.search(r"\b(quantity|unit|units|qty|terjual|jumlah terjual|volume)\b", q)
                and not re.search(r"\b(revenue|sales|pendapatan|omzet|nilai)\b", q)
            )
            order_col = "total_units_sold" if by_qty else "total_revenue"
            sql = f"""
SELECT
    p.product_name,
    p.category,
    SUM(oi.quantity) AS total_units_sold,
    ROUND(SUM(oi.line_total), 2) AS total_revenue
FROM order_items oi
JOIN orders o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id
{where_sql}
GROUP BY p.product_id, p.product_name, p.category
ORDER BY {order_col} {sort_dir}, p.product_name ASC
LIMIT {top_n}
""".strip()
            return SQLPlan(
                reasoning="Joining order_items, orders, and products to rank top-performing products.",
                sql=sql,
                chart_type="bar",
                selected_tables=["order_items", "orders", "products"],
                language=lang,
                intent="top_products",
            )

        # 18. Product Category breakdown
        if re.search(r"\b(category|categories|kategori)\b", q):
            conds = _revenue_where("o")
            where_sql = "WHERE " + " AND ".join(conds)
            sql = f"""
SELECT
    p.category,
    COUNT(DISTINCT o.order_id) AS order_count,
    SUM(oi.quantity) AS total_units_sold,
    ROUND(SUM(oi.line_total), 2) AS total_revenue
FROM order_items oi
JOIN orders o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id
{where_sql}
GROUP BY p.category
ORDER BY total_revenue {sort_dir}, p.category ASC
""".strip()
            return SQLPlan(
                reasoning="Aggregating recognized order counts, units sold, and gross line revenue by product category.",
                sql=sql,
                chart_type="bar",
                selected_tables=["order_items", "orders", "products"],
                language=lang,
                intent="category_breakdown",
            )

        # 19. Overall Revenue / Orders / AOV summary
        if re.search(
            r"\b(revenue|sales|pendapatan|penjualan|omzet|total order|jumlah order|jumlah pesanan|aov|average order|rata-rata pesanan|rata-rata nilai)\b",
            q,
        ):
            conds = _revenue_where("o")
            joins: list[str] = []
            selected = ["orders"]
            if segment:
                joins.append("JOIN customers c ON c.customer_id = o.customer_id")
                conds.append(f"c.segment = '{segment}'")
                selected.append("customers")
            if warehouse_city:
                joins.append("JOIN warehouses w ON w.warehouse_id = o.warehouse_id")
                conds.append(f"w.city = '{warehouse_city}'")
                selected.append("warehouses")
            join_sql = ("\n" + "\n".join(joins)) if joins else ""
            where_sql = "WHERE " + " AND ".join(conds)
            sql = f"""
SELECT
    COUNT(o.order_id) AS total_orders,
    COUNT(DISTINCT o.customer_id) AS unique_customers,
    ROUND(SUM(o.total_amount), 2) AS total_revenue,
    ROUND(AVG(o.total_amount), 2) AS avg_order_value,
    ROUND(SUM(o.discount_amount), 2) AS total_discount
FROM orders o{join_sql}
{where_sql}
""".strip()
            return SQLPlan(
                reasoning="Computing headline KPIs (order count, unique customers, recognized revenue, AOV, and total discounts).",
                sql=sql,
                chart_type="table",
                selected_tables=selected,
                language=lang,
                intent="executive_kpi_summary",
            )

        # 20. General fallback: category revenue overview
        sql = """
SELECT
    p.category,
    COUNT(DISTINCT o.order_id) AS order_count,
    SUM(oi.quantity) AS total_units_sold,
    ROUND(SUM(oi.line_total), 2) AS total_revenue
FROM order_items oi
JOIN orders o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id
WHERE o.status IN ('completed', 'shipped')
GROUP BY p.category
ORDER BY total_revenue DESC, p.category ASC
""".strip()
        return SQLPlan(
            reasoning="Providing an executive category performance breakdown as a general overview of the analytics database.",
            sql=sql,
            chart_type="bar",
            selected_tables=["order_items", "orders", "products"],
            language=lang,
            intent="general_overview",
        )

    async def repair(
        self,
        question: str,
        failed_sql: str,
        error_message: str,
        schema_context: str,
        tables: list[TableSchema],
    ) -> SQLPlan:
        """Deterministically diagnose SQLite errors and repair the SQL query."""
        lang = detect_language(question)
        repaired_sql = failed_sql
        err_low = error_message.lower()
        fix_notes: list[str] = []

        # Map common faulty column names to verified schema columns
        column_fixes: dict[str, str] = {
            "o.revenue": "o.total_amount",
            "o.gross_sales": "o.total_amount",
            "o.sales_amount": "o.total_amount",
            "o.nonexistent_revenue_col": "o.total_amount",
            "o.created_at": "o.order_date",
            "s.delayed": "s.is_delayed",
            "s.delayed_flag": "s.is_delayed",
            "s.cost": "s.shipping_cost",
            "c.name": "c.customer_name",
            "p.name": "p.product_name",
            "p.price": "p.unit_price",
            "p.cost": "p.unit_cost",
            "w.warehouse_name": "w.name",
            "oi.price": "oi.unit_price",
            "oi.revenue": "oi.line_total",
            "oi.amount": "oi.line_total",
        }

        if "no such column" in err_low:
            for wrong, right in column_fixes.items():
                if re.search(rf"\b{re.escape(wrong)}\b", repaired_sql, re.IGNORECASE):
                    repaired_sql = re.sub(
                        rf"\b{re.escape(wrong)}\b", right, repaired_sql, flags=re.IGNORECASE
                    )
                    fix_notes.append(f"Replaced invalid column '{wrong}' with '{right}'.")

            # Check unqualified column typo e.g. "no such column: delayed"
            m = re.search(r"no such column:\s*([A-Za-z0-9_.]+)", error_message, re.IGNORECASE)
            if m and repaired_sql == failed_sql:
                bad_col = m.group(1)
                bare_map = {
                    "delayed": "is_delayed",
                    "delayed_flag": "is_delayed",
                    "revenue": "total_amount",
                    "gross_sales": "total_amount",
                    "nonexistent_revenue_col": "total_amount",
                    "created_at": "order_date",
                    "warehouse_name": "name",
                }
                suffix = bad_col.split(".")[-1].lower()
                if suffix in bare_map:
                    replacement = (
                        f"{bad_col.split('.')[0]}.{bare_map[suffix]}"
                        if "." in bad_col
                        else bare_map[suffix]
                    )
                    repaired_sql = re.sub(
                        rf"\b{re.escape(bad_col)}\b",
                        replacement,
                        repaired_sql,
                        flags=re.IGNORECASE,
                    )
                    fix_notes.append(f"Mapped unknown column '{bad_col}' -> '{replacement}'.")

        elif "no such table" in err_low:
            table_fixes = {
                "order": "orders",
                "customer": "customers",
                "product": "products",
                "warehouse": "warehouses",
                "shipment": "shipments",
                "order_item": "order_items",
                "sales": "orders",
            }
            m = re.search(r"no such table:\s*([A-Za-z0-9_]+)", error_message, re.IGNORECASE)
            if m:
                bad_tbl = m.group(1)
                if bad_tbl.lower() in table_fixes:
                    good_tbl = table_fixes[bad_tbl.lower()]
                    repaired_sql = re.sub(
                        rf"\b{re.escape(bad_tbl)}\b", good_tbl, repaired_sql, flags=re.IGNORECASE
                    )
                    fix_notes.append(f"Corrected table name '{bad_tbl}' -> '{good_tbl}'.")

        elif "ambiguous column name" in err_low:
            m = re.search(r"ambiguous column name:\s*([A-Za-z0-9_]+)", error_message, re.IGNORECASE)
            if m:
                amb = m.group(1)
                qual = {
                    "warehouse_id": "o.warehouse_id",
                    "order_id": "o.order_id",
                    "unit_price": "oi.unit_price",
                    "city": "w.city",
                    "province": "c.province",
                }.get(amb.lower())
                if qual:
                    repaired_sql = re.sub(rf"(?<!\.)\b{re.escape(amb)}\b", qual, repaired_sql)
                    fix_notes.append(f"Qualified ambiguous column '{amb}' as '{qual}'.")

        # If direct patch succeeded and changed SQL, return patched plan
        if repaired_sql != failed_sql:
            return SQLPlan(
                reasoning="Self-corrected SQL after SQLite error: " + " ".join(fix_notes),
                sql=repaired_sql,
                chart_type="bar",
                selected_tables=[],
                language=lang,
                intent="repaired_sql",
            )

        # Otherwise regenerate cleanly from the original analytical question
        replanned = await self.plan(question, schema_context, tables)
        replanned.reasoning = (
            f"Self-corrected by regenerating verified schema-aligned SQL after SQLite error ({error_message}). "
            + replanned.reasoning
        )
        return replanned


class _ExternalLLMPlanner(BaseSQLPlanner):
    """Shared structured-output prompt and repair flow for external LLM providers."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    async def _complete_json(self, system_prompt: str, user_prompt: str) -> str:
        """Execute provider HTTP call and return raw JSON text."""

    def _to_plan(self, raw_text: str, question: str) -> SQLPlan:
        data = _parse_json_payload(raw_text)
        chart_type: ChartType = data.get("chart_type", "table")
        if chart_type not in ("bar", "line", "doughnut", "table"):
            chart_type = "table"
        lang: LanguageCode = data.get("language", detect_language(question))
        if lang not in ("en", "id"):
            lang = detect_language(question)
        return SQLPlan(
            reasoning=str(data.get("reasoning", "Generated via external LLM planner.")),
            sql=str(data.get("sql", "")).strip(),
            chart_type=chart_type,
            selected_tables=list(data.get("selected_tables", [])),
            language=lang,
            intent="llm_generated",
        )

    async def plan(
        self,
        question: str,
        schema_context: str,
        tables: list[TableSchema],
    ) -> SQLPlan:
        del tables
        user_prompt = (
            f"Database Schema:\n{schema_context}\n\n"
            f"User Question:\n{question}\n\n"
            "Return only the JSON object."
        )
        raw = await self._complete_json(SYSTEM_SQL_PROMPT, user_prompt)
        return self._to_plan(raw, question)

    async def repair(
        self,
        question: str,
        failed_sql: str,
        error_message: str,
        schema_context: str,
        tables: list[TableSchema],
    ) -> SQLPlan:
        del tables
        user_prompt = (
            f"Database Schema:\n{schema_context}\n\n"
            f"Original Question:\n{question}\n\n"
            f"Previous Failed SQL:\n{failed_sql}\n\n"
            f"Exact SQLite Error:\n{error_message}\n\n"
            "Fix the SQL query so it executes cleanly in SQLite read-only mode. "
            "Return only the JSON object."
        )
        raw = await self._complete_json(SYSTEM_SQL_PROMPT, user_prompt)
        return self._to_plan(raw, question)


class OllamaSQLPlanner(_ExternalLLMPlanner):
    """Async Ollama /api/chat adapter with JSON structured output."""

    provider_name: str = "ollama"

    async def _complete_json(self, system_prompt: str, user_prompt: str) -> str:
        url = f"{self.settings.ollama_base_url.rstrip('/')}/api/chat"
        payload = {
            "model": self.settings.model,
            "format": "json",
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {"temperature": 0.0},
        }
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_sec) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            body = resp.json()
            return str(body["message"]["content"])


class GeminiSQLPlanner(_ExternalLLMPlanner):
    """Async Google Gemini generateContent adapter with JSON mime type."""

    provider_name: str = "gemini"

    async def _complete_json(self, system_prompt: str, user_prompt: str) -> str:
        if not self.settings.gemini_api_key:
            raise RuntimeError(
                "AGENT_GEMINI_API_KEY is not configured. Set it in .env or switch provider to 'local'."
            )
        model = self.settings.model or "gemini-2.0-flash"
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={self.settings.gemini_api_key}"
        )
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
            },
        }
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_sec) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            body = resp.json()
            return str(body["candidates"][0]["content"]["parts"][0]["text"])


class OpenAISQLPlanner(_ExternalLLMPlanner):
    """Async OpenAI-compatible /chat/completions adapter with JSON object mode."""

    provider_name: str = "openai"

    async def _complete_json(self, system_prompt: str, user_prompt: str) -> str:
        if not self.settings.openai_api_key:
            raise RuntimeError(
                "AGENT_OPENAI_API_KEY is not configured. Set it in .env or switch provider to 'local'."
            )
        url = f"{self.settings.openai_base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        payload = {
            "model": self.settings.model or "gpt-4o-mini",
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_sec) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()
            return str(body["choices"][0]["message"]["content"])


def create_planner(settings: Settings, provider_override: str | None = None) -> BaseSQLPlanner:
    """Instantiate the configured SQL planner provider."""
    provider = (provider_override or settings.provider).lower()
    if provider == "local":
        return LocalSQLPlanner()
    if provider == "ollama":
        return OllamaSQLPlanner(settings)
    if provider == "gemini":
        return GeminiSQLPlanner(settings)
    if provider == "openai":
        return OpenAISQLPlanner(settings)
    raise ValueError(f"Unsupported SQL planner provider: {provider}")
