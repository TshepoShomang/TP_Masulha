from typing import Any, Dict, List, Tuple

from flask import session

from .db_utils import table_columns


def get_cart_summary() -> Tuple[List[Dict[str, Any]], int, float]:
    """Return cart, total item count, and rounded total price."""
    cart = session.get("cart", [])
    items_count = 0
    total = 0.0
    for item in cart:
        try:
            qty = int(item.get("quantity", 1))
        except Exception:
            qty = 1
        items_count += qty
        try:
            total += float(item.get("price", 0)) * qty
        except Exception:
            continue
    return cart, items_count, round(total, 2)


def orders_viewed_column(conn):
    cols = table_columns(conn, "Orders")
    for c in ("viewed", "is_viewed", "is_explored", "explored", "is_new"):
        if c in cols:
            return c
    return None


def record_completed_order(order_id):
    """Store an order id in the admin history session list."""
    try:
        oid = int(order_id)
    except Exception:
        oid = order_id
    completed = set(session.get("admin_completed_orders", []))
    completed.add(oid)
    session["admin_completed_orders"] = list(completed)
    session.modified = True


def ensure_orders_phone_column(conn):
    """Ensure the Orders table has a phone column alongside address."""
    try:
        cols = table_columns(conn, "Orders")
        if "phone" in cols:
            return
        cur = conn.cursor()
        try:
            cur.execute("ALTER TABLE Orders ADD COLUMN IF NOT EXISTS phone VARCHAR(64)")
            conn.commit()
        finally:
            cur.close()
    except Exception:
        pass
