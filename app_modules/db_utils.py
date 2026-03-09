from typing import Optional, Sequence, Tuple

import psycopg2
from flask import current_app
from psycopg2 import sql

from .media_utils import delete_product_image, extract_public_id_from_url

ORDER_ITEM_TABLE_CANDIDATES = ["OrderItems", "order_items", "Order_Items", "orderitems"]


def get_db_connection():
    """Create a new PostgreSQL connection using DATABASE_URL."""
    database_url = current_app.config.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured.")
    return psycopg2.connect(database_url)


def table_columns(conn, table_name: str):
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            ("public", table_name.lower()),
        )
        rows = cur.fetchall()
        return {str(r[0]).lower() for r in rows}
    except Exception:
        return set()


def table_id_column(conn, table_name: str, candidates: Sequence[str] = ("id", "order_id", "OrderID", "orderid")):
    cols = {c.lower() for c in table_columns(conn, table_name)}
    for cand in candidates:
        if cand.lower() in cols:
            return cand
    for c in cols:
        if c.endswith("id"):
            return c
    return "id"


def resolve_product_columns(conn):
    cols_raw = table_columns(conn, "Products")
    cols_lookup = {c.lower(): c for c in cols_raw}

    def pick(candidates):
        for cand in candidates:
            key = cand.lower()
            if key in cols_lookup:
                return cols_lookup[key]
        return None

    return {
        "id": table_id_column(conn, "Products", ("id", "product_id", "ProductID", "productid")),
        "name": pick(("name", "product_name", "title")),
        "price": pick(("price", "amount", "cost")),
        "image": pick(("image", "image_url", "photo", "picture", "imagepath")),
        "image_public_id": pick(("image_public_id", "cloudinary_id", "public_id", "imagepublicid")),
        "quantity": pick(("quantity", "qty", "stock", "in_stock")),
    }


def fetch_product_by_id(product_id: int):
    conn = get_db_connection()
    try:
        columns = resolve_product_columns(conn)
        select_parts = [f"{columns['id']} AS id"]
        aliases = ["id"]
        for alias, column in (
            ("name", columns["name"]),
            ("price", columns["price"]),
            ("image", columns["image"]),
            ("image_public_id", columns["image_public_id"]),
            ("quantity", columns["quantity"]),
        ):
            if column:
                select_parts.append(f"{column} AS {alias}")
                aliases.append(alias)
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(select_parts)} FROM Products WHERE {columns['id']} = %s", (product_id,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        data = {}
        for idx, alias in enumerate(aliases):
            data[alias] = row[idx]
        if data.get("id") is not None:
            try:
                data["id"] = int(data["id"])
            except Exception:
                pass
        if data.get("price") is not None:
            try:
                data["price"] = float(data["price"])
            except Exception:
                pass
        if data.get("quantity") is not None:
            try:
                data["quantity"] = int(data["quantity"])
            except Exception:
                pass
        return data
    finally:
        conn.close()


def resolve_table_columns_generic(conn, table_candidates):
    for name in table_candidates:
        try:
            cols = table_columns(conn, name)
            if cols:
                lookup = {c.lower(): c for c in cols}
                return lookup, name
        except Exception:
            continue
    return None, None


def ensure_guest_table(conn):
    """Ensure a Guests table exists for guest checkout tracking."""
    try:
        cols = table_columns(conn, "Guests")
        if cols:
            return
    except Exception:
        pass
    cur = conn.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS Guests (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) UNIQUE NOT NULL,
                first_name VARCHAR(120),
                last_name VARCHAR(120),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def get_or_create_guest(conn, email: str, first_name: Optional[str], last_name: Optional[str]):
    """Return an existing guest id for email or create a new guest record."""
    if not email:
        return None
    ensure_guest_table(conn)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id FROM Guests WHERE LOWER(email) = LOWER(%s) LIMIT 1",
            (email,),
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            try:
                return int(row[0])
            except Exception:
                return row[0]
        cur.execute(
            "INSERT INTO Guests (email, first_name, last_name) VALUES (%s, %s, %s) RETURNING id",
            (email, first_name, last_name),
        )
        new_row = cur.fetchone()
        if new_row and new_row[0] is not None:
            try:
                return int(new_row[0])
            except Exception:
                return new_row[0]
        return None
    finally:
        cur.close()


def fetch_user_id(conn, email: Optional[str]):
    """Return the numeric user id for a given email if available."""
    if not email:
        return None
    try:
        user_id_col = table_id_column(conn, "Users", ("id", "user_id", "UserID", "userid"))
        if not user_id_col:
            return None
        cur = conn.cursor()
        cur.execute(f"SELECT {user_id_col} FROM Users WHERE email = %s LIMIT 1", (email,))
        row = cur.fetchone()
        cur.close()
        if not row or row[0] is None:
            return None
        try:
            return int(row[0])
        except Exception:
            return row[0]
    except Exception:
        return None


def ensure_order_items_table(conn):
    """Ensure OrderItems/order_items table exists with required columns."""
    lookup, table_name = resolve_table_columns_generic(conn, ORDER_ITEM_TABLE_CANDIDATES)
    if lookup and table_name:
        required_defs = {
            "order_id": "INTEGER NOT NULL",
            "product_id": "INTEGER NOT NULL",
            "quantity": "INTEGER NOT NULL DEFAULT 1",
            "price": "NUMERIC(12, 2) NOT NULL DEFAULT 0",
        }
        existing = set(lookup.keys())
        missing = [col for col in required_defs if col not in existing]
        if missing:
            cur = conn.cursor()
            try:
                for col in missing:
                    cur.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col} {required_defs[col]}"
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()
        return lookup, table_name

    cur = conn.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS OrderItems (
                order_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                price NUMERIC(12, 2) NOT NULL DEFAULT 0,
                PRIMARY KEY (order_id, product_id)
            )
            """
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return resolve_table_columns_generic(conn, ORDER_ITEM_TABLE_CANDIDATES)


def ensure_orders_schema(conn):
    """Ensure Orders table has phone, user/guest linkage, payment status, and viewed columns."""
    try:
        cols = {c.lower(): c for c in table_columns(conn, "Orders")}
    except Exception:
        return
    statements = []
    if "phone" not in cols:
        statements.append("ALTER TABLE Orders ADD COLUMN IF NOT EXISTS phone VARCHAR(64)")
    if "user_id" not in cols:
        statements.append("ALTER TABLE Orders ADD COLUMN IF NOT EXISTS user_id INTEGER")
    if "guest_id" not in cols:
        statements.append("ALTER TABLE Orders ADD COLUMN IF NOT EXISTS guest_id INTEGER")
    if "payment_status" not in cols and "status" not in cols:
        statements.append("ALTER TABLE Orders ADD COLUMN IF NOT EXISTS payment_status VARCHAR(32) DEFAULT 'paid'")
    if "viewed" not in cols and "is_viewed" not in cols:
        statements.append("ALTER TABLE Orders ADD COLUMN IF NOT EXISTS viewed BOOLEAN DEFAULT FALSE")
    if not statements:
        return
    cur = conn.cursor()
    try:
        for stmt in statements:
            cur.execute(stmt)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def delete_order_items_for_product(conn, product_id):
    lookup, table_name = resolve_table_columns_generic(conn, ORDER_ITEM_TABLE_CANDIDATES)
    if not lookup:
        return
    prod_col = None
    for cand in ("product_id", "productid", "product"):
        if cand in lookup:
            prod_col = lookup[cand]
            break
    if not prod_col:
        for key, actual in lookup.items():
            if "product" in key and key.endswith("id"):
                prod_col = actual
                break
    if not prod_col:
        return
    cur = conn.cursor()
    try:
        stmt = sql.SQL("DELETE FROM {table} WHERE {col} = %s").format(
            table=sql.Identifier(table_name),
            col=sql.Identifier(prod_col),
        )
        cur.execute(stmt, (product_id,))
    finally:
        cur.close()


def remove_product_record(product_id: int):
    product = fetch_product_by_id(product_id)
    if not product:
        return False, "Product not found.", None
    conn = get_db_connection()
    try:
        columns = resolve_product_columns(conn)
        delete_order_items_for_product(conn, product_id)
        cur = conn.cursor()
        cur.execute(f"DELETE FROM Products WHERE {columns['id']} = %s", (product_id,))
        if cur.rowcount == 0:
            conn.rollback()
            return False, "Product not found.", None
        conn.commit()
        cur.close()
    except Exception as exc:
        conn.rollback()
        current_app.logger.exception("Failed to delete product %s: %s", product_id, exc)
        return False, "Unable to delete product.", None
    finally:
        conn.close()
    public_id = product.get("image_public_id") or extract_public_id_from_url(product.get("image"))
    delete_product_image(public_id)
    return True, "Product deleted.", product
