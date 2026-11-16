import csv
import hashlib
import hmac
import io
import os
import re
import secrets
import sqlite3
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, abort, g, jsonify, render_template, request, session

from grocer_prices import (
    GrocerError,
    GrocerPriceClient,
    RetailerPageError,
    choose_product_match,
    fetch_retailer_product_price,
    grocer_product_id_from_url,
    grocer_product_url,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "instance"))
DB_PATH = Path(os.environ.get("DATABASE_PATH", DATA_DIR / "household.db"))
MONTHS = {
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
}
MONTH_ORDER = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
CODE_PATTERN = re.compile(r"^[A-Z0-9]{2,3}$")
STORE_PATTERN = re.compile(r"\s+([A-Z0-9]{2,3})$")


def create_app(test_config=None):
    app = Flask(__name__, instance_path=str(DATA_DIR), instance_relative_config=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    secret_path = DATA_DIR / "secret_key"
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        if not secret_path.exists():
            secret_path.write_text(secrets.token_hex(32), encoding="utf-8")
            secret_path.chmod(0o600)
        secret_key = secret_path.read_text(encoding="utf-8").strip()
    app.config.update(
        SECRET_KEY=secret_key,
        DATABASE=str(DB_PATH),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    app.teardown_appcontext(close_db)
    app.config["GROCER_CACHE_DIR"] = str(DATA_DIR / "grocer-cache")

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    def api_error(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description}), error.code
        return error

    @app.before_request
    def protect_mutations():
        if request.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.path not in {"/api/setup", "/api/login"}:
                token = session.get("csrf_token", "")
                submitted = request.headers.get("X-CSRF-Token", "")
                if not token or not hmac.compare_digest(token, submitted):
                    abort(403)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/api/history")
    @login_required
    def purchase_history():
        db = get_db()
        today = date.today()
        first_month_index = today.year * 12 + today.month - 1 - 11
        start_year, start_month_index = divmod(first_month_index, 12)
        start_month = start_month_index + 1
        start_date = date(start_year, start_month, 1)
        end_date = date(today.year, today.month, 1)

        months = []
        month_lookup = {}
        for offset in range(12):
            year, month_index = divmod(first_month_index + offset, 12)
            month_date = date(year, month_index + 1, 1)
            row = {
                "month": month_date.strftime("%Y-%m"),
                "label": month_date.strftime("%b %Y"),
                "total": 0.0,
                "food": 0.0,
                "household": 0.0,
                "count": 0,
            }
            months.append(row)
            month_lookup[row["month"]] = row

        purchases = [dict(row) for row in db.execute(
            """SELECT id, item_name, store, category, quantity, unit_price, total, purchase_date
               FROM purchases WHERE purchase_date >= ? AND purchase_date < date(?, '+1 month')
               ORDER BY purchase_date, id""",
            (start_date.isoformat(), end_date.isoformat()),
        )]
        manual_spend = [dict(row) for row in db.execute(
            """SELECT id, month, category, amount, note, created_at
               FROM manual_spend WHERE month >= ? AND month <= ?
               ORDER BY month DESC, id DESC""",
            (start_date.strftime("%Y-%m"), end_date.strftime("%Y-%m")),
        )]
        store_totals = {}
        category_totals = {"Food": 0.0, "Household": 0.0}
        for purchase in purchases:
            month = month_lookup.get(purchase["purchase_date"][:7])
            if not month:
                continue
            amount = float(purchase["total"] or 0)
            category = history_category(purchase["category"])
            month["total"] += amount
            month[category.lower()] += amount
            month["count"] += 1
            category_totals[category] += amount
            store = purchase["store"] or "Unassigned"
            store_totals[store] = store_totals.get(store, 0.0) + amount

        for entry in manual_spend:
            month = month_lookup.get(entry["month"])
            if not month:
                continue
            amount = float(entry["amount"] or 0)
            category = history_category(entry["category"])
            month["total"] += amount
            month[category.lower()] += amount
            category_totals[category] += amount
            no_store_label = "Manual spend · store not recorded"
            store_totals[no_store_label] = store_totals.get(no_store_label, 0.0) + amount

        for month in months:
            for key in ("total", "food", "household"):
                month[key] = round(month[key], 2)
        total_spend = round(sum(month["total"] for month in months), 2)
        active_months = [month for month in months if month["total"] > 0]
        top_month = max(active_months, key=lambda month: month["total"], default=None)
        top_store = max(store_totals.items(), key=lambda entry: entry[1], default=None)
        largest = max(purchases, key=lambda purchase: purchase["total"], default=None)
        item_frequency = {}
        for purchase in purchases:
            key = purchase["item_name"]
            entry = item_frequency.setdefault(key, {"name": key, "count": 0, "quantity": 0})
            entry["count"] += 1
            entry["quantity"] += int(purchase["quantity"] or 0)
        most_repeated = max(item_frequency.values(), key=lambda entry: (entry["count"], entry["quantity"]), default=None)
        undated = db.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(total), 0) AS total FROM purchases WHERE purchase_date IS NULL"
        ).fetchone()

        return jsonify({
            "start_date": start_date.isoformat(),
            "end_date": today.isoformat(),
            "months": months,
            "purchases": purchases,
            "manual_spend": manual_spend,
            "categories": [
                {"category": name, "total": round(amount, 2),
                 "share": round(amount / total_spend * 100) if total_spend else 0}
                for name, amount in category_totals.items()
            ],
            "stores": [
                {"store": name, "total": round(amount, 2),
                 "share": round(amount / total_spend * 100) if total_spend else 0}
                for name, amount in sorted(store_totals.items(), key=lambda entry: entry[1], reverse=True)
            ],
            "highlights": {
                "total_spend": total_spend,
                "purchase_count": len(purchases),
                "average_active_month": round(total_spend / len(active_months), 2) if active_months else 0,
                "top_month": top_month,
                "top_store": {"store": top_store[0], "total": round(top_store[1], 2)} if top_store else None,
                "largest_purchase": ({
                    "name": largest["item_name"],
                    "store": largest["store"],
                    "total": round(largest["total"], 2),
                    "date": largest["purchase_date"],
                } if largest else None),
                "most_repeated_item": most_repeated,
                "active_months": len(active_months),
            },
            "undated_legacy": {"count": undated["count"], "total": round(undated["total"], 2)},
        })

    @app.post("/api/manual-spend")
    @login_required
    def add_manual_spend():
        data = json_body()
        month = clean_text(data.get("month"), 7)
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            return error_response("Choose a month.", 400)
        try:
            date.fromisoformat(f"{month}-01")
        except ValueError:
            return error_response("Choose a valid month.", 400)
        category = clean_text(data.get("category"), 20).title()
        if category not in {"Food", "Household"}:
            return error_response("Choose Food or Household.", 400)
        amount = nonnegative_number(data.get("amount"), "amount")
        if amount <= 0:
            return error_response("Amount must be greater than zero.", 400)
        note = clean_text(data.get("note"), 160)
        db = get_db()
        cursor = db.execute(
            "INSERT INTO manual_spend (month, category, amount, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (month, category, round(amount, 2), note, now()),
        )
        db.commit()
        return jsonify({"ok": True, "id": cursor.lastrowid}), 201

    @app.delete("/api/manual-spend/<int:entry_id>")
    @login_required
    def delete_manual_spend(entry_id):
        cursor = get_db().execute("DELETE FROM manual_spend WHERE id = ?", (entry_id,))
        if cursor.rowcount == 0:
            return error_response("Manual spend entry not found.", 404)
        get_db().commit()
        return jsonify({"ok": True, "deleted_id": entry_id})

    @app.get("/api/session")
    def session_state():
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
        user = current_user()
        count = get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return jsonify({"user": user, "needs_setup": count == 0, "csrf_token": session["csrf_token"]})

    @app.post("/api/setup")
    def setup():
        data = json_body()
        name = required_text(data, "name", 60)
        username = normalized_username(data.get("username") or name)
        password = valid_password(data.get("password"))
        db = get_db()
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return error("The household account is already set up.", 409)
        cursor = db.execute(
            "INSERT INTO users (name, username, password_hash, role, created_at) VALUES (?, ?, ?, 'admin', ?)",
            (name, username, hash_password(password), now()),
        )
        db.commit()
        start_session(cursor.lastrowid)
        return jsonify({"ok": True})

    @app.post("/api/login")
    def login():
        data = json_body()
        username = normalized_username(data.get("username", ""))
        password = str(data.get("password", ""))
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            return error("That username and password did not match.", 401)
        start_session(user["id"])
        return jsonify({"ok": True})

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/state")
    @login_required
    def state():
        db = get_db()
        today = date.today()
        items = [dict(row) for row in db.execute(
            "SELECT * FROM items WHERE active = 1 ORDER BY category COLLATE NOCASE, name COLLATE NOCASE"
        )]
        pricing_setting = db.execute(
            "SELECT value FROM settings WHERE key = 'pricing_store_id'"
        ).fetchone()
        pricing_store_id = int(pricing_setting[0]) if pricing_setting else None
        pricing_store = db.execute(
            "SELECT value FROM settings WHERE key = 'pricing_store_name'"
        ).fetchone()
        refresh_rows = db.execute(
            "SELECT store_id, store_name, refreshed_at, source_modified FROM price_refresh"
        ).fetchall()
        shopping = [dict(row) for row in db.execute(
            """SELECT s.id, s.item_id, s.quantity, s.note, s.created_at,
                      i.name, i.category, i.store, i.store_code, i.unit_price,
                      i.product_url, i.forecast_qty
               FROM shopping_list s JOIN items i ON i.id = s.item_id
               WHERE i.active = 1 ORDER BY i.store COLLATE NOCASE, i.name COLLATE NOCASE"""
        )]
        recent = [dict(row) for row in db.execute(
            """SELECT id, item_name, store, category, quantity, unit_price, total, purchase_date,
                      source, source_period, created_at
               FROM purchases ORDER BY COALESCE(purchase_date, substr(created_at, 1, 10)) DESC, id DESC LIMIT 12"""
        )]
        count_items = len(items)
        list_value = sum(row["quantity"] * row["unit_price"] for row in shopping)
        month_spend = db.execute(
            "SELECT COALESCE(SUM(total), 0) FROM purchases WHERE purchase_date LIKE ?",
            (today.strftime("%Y-%m") + "%",),
        ).fetchone()[0]
        history = db.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(total), 0) AS total FROM purchases WHERE purchase_date IS NULL"
        ).fetchone()
        legacy_month_rows = [dict(row) for row in db.execute(
            """SELECT substr(source_period, 1, instr(source_period, ' ·') - 1) AS month,
                      SUM(total) AS total
               FROM purchases WHERE purchase_date IS NULL AND instr(source_period, ' ·') > 0
               GROUP BY month"""
        )]
        legacy_month_rows.sort(key=lambda row: MONTH_ORDER.index(row["month"]) if row["month"] in MONTH_ORDER else 99)
        first_month = today.month - 5
        first_year = today.year
        if first_month <= 0:
            first_month += 12
            first_year -= 1
        month_rows = db.execute(
            """SELECT substr(purchase_date, 1, 7) AS month, SUM(total) AS total
               FROM purchases WHERE purchase_date IS NOT NULL AND purchase_date >= ?
               GROUP BY month ORDER BY month""",
            (date(first_year, first_month, 1).isoformat(),),
        ).fetchall()
        return jsonify({
            "user": current_user(),
            "items": items,
            "shopping": shopping,
            "recent": recent,
            "pricing": {
                "store_id": pricing_store_id,
                "store_name": pricing_store[0] if pricing_store else None,
                "refreshes": [dict(row) for row in refresh_rows],
            },
            "stats": {
                "item_count": count_items,
                "shopping_count": len(shopping),
                "list_value": round(list_value, 2),
                "month_spend": round(month_spend, 2),
                "legacy_count": history["count"],
                "legacy_total": round(history["total"], 2),
                "legacy_monthly_spend": legacy_month_rows,
                "monthly_spend": [dict(row) for row in month_rows],
            },
        })

    @app.post("/api/items")
    @login_required
    def create_item():
        data = json_body()
        name = required_text(data, "name", 140)
        category = clean_text(data.get("category"), 60) or "Other"
        store = clean_text(data.get("store"), 100) or "Unassigned"
        store_code = clean_text(data.get("store_code"), 8).upper()
        quantity = whole_number(data.get("forecast_qty", 1), "usual quantity")
        price = nonnegative_number(data.get("unit_price", 0), "unit price")
        product_url = valid_url(data.get("product_url", ""))
        grocer_product_id = optional_positive_id(data.get("grocer_product_id"))
        grocer_product_url = valid_url(data.get("grocer_product_url", ""))
        db = get_db()
        cursor = db.execute(
            """INSERT INTO items
                    (name, category, store, store_code, forecast_qty, unit_price, product_url,
                     grocer_product_id, grocer_product_url, active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (name, category, store, store_code, quantity, price, product_url,
                 grocer_product_id, grocer_product_url, now(), now()),
        )
        db.commit()
        return jsonify({"id": cursor.lastrowid}), 201

    @app.put("/api/items/<int:item_id>")
    @login_required
    def update_item(item_id):
        data = json_body()
        db = get_db()
        item = db.execute("SELECT * FROM items WHERE id = ? AND active = 1", (item_id,)).fetchone()
        if not item:
            return error("Item not found.", 404)
        values = (
            required_text(data, "name", 140),
            clean_text(data.get("category"), 60) or "Other",
            clean_text(data.get("store"), 100) or "Unassigned",
            clean_text(data.get("store_code", item["store_code"]), 8).upper(),
            whole_number(data.get("forecast_qty", 1), "usual quantity"),
            nonnegative_number(data.get("unit_price", 0), "unit price"),
            valid_url(data.get("product_url", "")),
            optional_positive_id(data.get("grocer_product_id")),
            valid_url(data.get("grocer_product_url", "")),
            now(), item_id,
        )
        db.execute(
            """UPDATE items SET name=?, category=?, store=?, store_code=?, forecast_qty=?,
                    unit_price=?, product_url=?, grocer_product_id=?, grocer_product_url=?,
                    updated_at=? WHERE id=?""", values,
        )
        db.commit()
        return jsonify({"ok": True})

    @app.get("/api/pricing/stores")
    @login_required
    def pricing_stores():
        client = GrocerPriceClient(app.config["GROCER_CACHE_DIR"])
        try:
            stores, catalog_modified = client.stores_for_vendor("PAK'nSAVE")
        except GrocerError as error:
            return error_response(str(error), 502)
        return jsonify({"stores": stores, "catalog_modified": catalog_modified})

    @app.put("/api/pricing/store")
    @login_required
    def set_pricing_store():
        data = json_body()
        store_id = integer_id(data.get("store_id"))
        store_name = required_text(data, "store_name", 120)
        client = GrocerPriceClient(app.config["GROCER_CACHE_DIR"])
        try:
            stores, _ = client.stores_for_vendor("PAK'nSAVE")
        except GrocerError as error:
            return error_response(str(error), 502)
        if not any(store["id"] == store_id and store["name"] == store_name for store in stores):
            return error_response("Choose a Pak’n Save store from Grocer’s store list.", 400)
        db = get_db()
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('pricing_store_id', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(store_id),),
        )
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('pricing_store_name', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (store_name,),
        )
        db.commit()
        return jsonify({"ok": True})

    @app.get("/api/pricing/search")
    @login_required
    def search_pricing_products():
        query = clean_text(request.args.get("q"), 100)
        if len(query) < 2:
            return error_response("Enter at least two characters to search Grocer.", 400)
        client = GrocerPriceClient(app.config["GROCER_CACHE_DIR"])
        try:
            matches = client.search_products(query)
            store_setting = get_db().execute("SELECT value FROM settings WHERE key = 'pricing_store_id'").fetchone()
            prices = client.prices_for_store(store_setting[0])[0] if store_setting else {}
        except GrocerError as error:
            return error_response(str(error), 502)
        results = []
        for product in matches:
            price_cents = prices.get(product["id"])
            results.append({
                **product,
                "price": price_cents / 100 if price_cents is not None else None,
                "url": grocer_product_url(product["id"], product["name"]),
            })
        return jsonify({"results": results})

    @app.post("/api/pricing/refresh")
    @login_required
    def refresh_prices():
        db = get_db()
        requested_store = clean_text(json_body().get("store"), 100)
        if requested_store and requested_store != "Pak 'n Save":
            items = db.execute(
                "SELECT id, name, store, product_url FROM items WHERE active = 1 AND store = ?",
                (requested_store,),
            ).fetchall()
            refreshed_at = now()
            updated_items = []
            unmatched_items = []
            for item in items:
                if not item["product_url"]:
                    unmatched_items.append({"item_id": item["id"], "name": item["name"], "reason": "Add a retailer product link"})
                    continue
                try:
                    retailer_result = fetch_retailer_product_price(item["product_url"], item["store"])
                except RetailerPageError as error:
                    unmatched_items.append({"item_id": item["id"], "name": item["name"], "reason": str(error)})
                    continue
                db.execute(
                    """UPDATE items SET unit_price = ?, price_refreshed_at = ?,
                       price_refreshed_store_id = NULL, price_refreshed_store_name = ?, updated_at = ?
                       WHERE id = ?""",
                    (retailer_result["price"], refreshed_at, f"{item['store']} product page", refreshed_at, item["id"]),
                )
                updated_items.append({"item_id": item["id"], "name": item["name"], "price": retailer_result["price"]})
            db.commit()
            return jsonify({
                "store_name": requested_store,
                "source": "retailer product pages",
                "refreshed_at": refreshed_at,
                "updated": updated_items,
                "unmatched": unmatched_items,
            })

        setting = db.execute("SELECT value FROM settings WHERE key = 'pricing_store_id'").fetchone()
        store_name_setting = db.execute("SELECT value FROM settings WHERE key = 'pricing_store_name'").fetchone()
        if not setting or not store_name_setting:
            return error_response("The fixed Highland Park Grocer pricing source is not configured.", 400)
        store_id = int(setting[0])
        store_name = store_name_setting[0]
        client = GrocerPriceClient(app.config["GROCER_CACHE_DIR"])
        try:
            prices, source_modified = client.prices_for_store(store_id)
            products = client.products()
        except GrocerError as error:
            return error_response(str(error), 502)

        refreshed_at = now()
        updated_items = []
        unmatched_items = []
        items = db.execute(
            "SELECT id, name, store, product_url, grocer_product_id, grocer_product_url FROM items WHERE active = 1 AND store_code = 'PS'"
        ).fetchall()
        product_by_id = {product["id"]: product for product in products}
        for item in items:
            if item["product_url"]:
                try:
                    retailer_result = fetch_retailer_product_price(item["product_url"], item["store"])
                except RetailerPageError:
                    pass
                else:
                    db.execute(
                        """UPDATE items SET unit_price = ?, price_refreshed_at = ?,
                           price_refreshed_store_id = NULL, price_refreshed_store_name = ?, updated_at = ?
                           WHERE id = ?""",
                        (retailer_result["price"], refreshed_at, f"{item['store']} product page", refreshed_at, item["id"]),
                    )
                    updated_items.append({"item_id": item["id"], "name": item["name"], "price": retailer_result["price"]})
                    continue
            product = product_by_id.get(item["grocer_product_id"]) if item["grocer_product_id"] else None
            if not product:
                product = choose_product_match(item["name"], products)
            if not product:
                unmatched_items.append({"item_id": item["id"], "name": item["name"]})
                continue
            price_cents = prices.get(product["id"])
            if price_cents is None:
                unmatched_items.append({"item_id": item["id"], "name": item["name"], "reason": "No price for selected store"})
                continue
            product_url = grocer_product_url(product["id"], product["name"])
            db.execute(
                     """UPDATE items SET unit_price = ?, grocer_product_id = ?, grocer_product_url = ?,
                         price_refreshed_at = ?, price_refreshed_store_id = ?, price_refreshed_store_name = ?, updated_at = ?
                   WHERE id = ?""",
                     (price_cents / 100, product["id"], product_url, refreshed_at, store_id, store_name, refreshed_at, item["id"]),
            )
            updated_items.append({"item_id": item["id"], "name": item["name"], "price": price_cents / 100})
        db.execute(
            """INSERT INTO price_refresh (store_id, store_name, refreshed_at, source_modified)
               VALUES (?, ?, ?, ?) ON CONFLICT(store_id) DO UPDATE SET
               store_name=excluded.store_name, refreshed_at=excluded.refreshed_at,
               source_modified=excluded.source_modified""",
            (store_id, store_name, refreshed_at, source_modified),
        )
        db.commit()
        return jsonify({
            "store_name": store_name,
            "refreshed_at": refreshed_at,
            "source_modified": source_modified,
            "updated": updated_items,
            "unmatched": unmatched_items,
        })

    @app.post("/api/items/<int:item_id>/refresh-price")
    @login_required
    def refresh_item_price(item_id):
        db = get_db()
        item = db.execute(
            """SELECT id, name, store, product_url, grocer_product_id, grocer_product_url
               FROM items WHERE id = ? AND active = 1""",
            (item_id,),
        ).fetchone()
        if not item:
            return error_response("Catalog item not found.", 404)
        store = db.execute(
            "SELECT value FROM settings WHERE key = 'pricing_store_id'"
        ).fetchone()
        store_name = db.execute(
            "SELECT value FROM settings WHERE key = 'pricing_store_name'"
        ).fetchone()
        grocer_url_in_product_field = grocer_product_id_from_url(item["product_url"])
        manual_grocer_url = item["grocer_product_url"] or (
            item["product_url"] if grocer_url_in_product_field else ""
        )
        product_id = (
            item["grocer_product_id"]
            or grocer_product_id_from_url(manual_grocer_url)
            or grocer_url_in_product_field
        )
        retailer_url = "" if grocer_url_in_product_field else item["product_url"]
        price = None
        source = None
        source_modified = ""
        grocer_failure = ""
        retailer_failure = "No retailer product link is saved."
        if retailer_url:
            try:
                retailer_result = fetch_retailer_product_price(retailer_url, item["store"])
                price = retailer_result["price"]
                source = f"{item['store']} product page"
                source_modified = retailer_result["source_modified"]
            except RetailerPageError as error:
                retailer_failure = str(error)

        if price is None and product_id and store and store_name:
            client = GrocerPriceClient(app.config["GROCER_CACHE_DIR"])
            try:
                prices, source_modified = client.prices_for_store(store[0])
                price_cents = prices.get(product_id)
                if price_cents is not None:
                    price = price_cents / 100
                    source = store_name[0]
            except GrocerError as error:
                grocer_failure = str(error)
            if price is None and not grocer_failure:
                grocer_failure = "Grocer has no price for this product at the default Highland Park store."
        elif price is None and product_id:
            grocer_failure = "Grocer’s default Highland Park price source is not configured."

        if price is None:
            failures = [message for message in (grocer_failure, retailer_failure) if message]
            return error_response("Price lookup failed. " + " ".join(failures), 404)
        refreshed_at = now()
        retailer_product_url = retailer_url
        saved_grocer_url = manual_grocer_url
        db.execute(
                """UPDATE items SET unit_price = ?, product_url = ?, grocer_product_id = COALESCE(grocer_product_id, ?),
                    grocer_product_url = CASE WHEN ? != '' THEN ? ELSE grocer_product_url END,
                    price_refreshed_at = ?, price_refreshed_store_id = ?,
                    price_refreshed_store_name = ?, updated_at = ?
               WHERE id = ?""",
            (price, retailer_product_url, product_id, saved_grocer_url, saved_grocer_url, refreshed_at,
                 store[0] if store_name and source == store_name[0] else None,
                 source, refreshed_at, item_id),
        )
        db.commit()
        return jsonify({
            "item_id": item_id,
            "name": item["name"],
            "price": price,
            "store_name": source,
            "source": "Grocer" if store_name and source == store_name[0] else "Retailer page",
            "refreshed_at": refreshed_at,
            "source_modified": source_modified,
        })

    @app.delete("/api/items/<int:item_id>")
    @login_required
    def archive_item(item_id):
        db = get_db()
        db.execute("UPDATE items SET active = 0, updated_at = ? WHERE id = ?", (now(), item_id))
        db.execute("DELETE FROM shopping_list WHERE item_id = ?", (item_id,))
        db.commit()
        return jsonify({"ok": True})

    @app.post("/api/shopping")
    @login_required
    def add_to_shopping():
        data = json_body()
        item_id = integer_id(data.get("item_id"))
        quantity = positive_whole_number(data.get("quantity", 1), "quantity")
        note = clean_text(data.get("note"), 160)
        db = get_db()
        item = db.execute("SELECT id FROM items WHERE id = ? AND active = 1", (item_id,)).fetchone()
        if not item:
            return error("Choose an active catalog item.", 400)
        db.execute(
            """INSERT INTO shopping_list (item_id, quantity, note, created_at)
               VALUES (?, ?, ?, ?) ON CONFLICT(item_id) DO UPDATE SET
               quantity=excluded.quantity, note=excluded.note, created_at=excluded.created_at""",
            (item_id, quantity, note, now()),
        )
        db.commit()
        return jsonify({"ok": True})

    @app.delete("/api/shopping/<int:entry_id>")
    @login_required
    def remove_from_shopping(entry_id):
        db = get_db()
        db.execute("DELETE FROM shopping_list WHERE id = ?", (entry_id,))
        db.commit()
        return jsonify({"ok": True})

    @app.post("/api/shopping/store")
    @login_required
    def add_store_list_to_shopping():
        data = json_body()
        store = required_text(data, "store", 100)
        db = get_db()
        items = db.execute(
            "SELECT id, forecast_qty FROM items WHERE active = 1 AND store = ? AND forecast_qty > 0",
            (store,),
        ).fetchall()
        if not items:
            return error("No items with a positive usual quantity were found for that store.", 404)
        timestamp = now()
        db.executemany(
            """INSERT INTO shopping_list (item_id, quantity, note, created_at)
               VALUES (?, ?, '', ?) ON CONFLICT(item_id) DO UPDATE SET
               quantity=excluded.quantity, note='', created_at=excluded.created_at""",
            [(item["id"], item["forecast_qty"], timestamp) for item in items],
        )
        db.commit()
        return jsonify({"ok": True, "added": len(items)})

    @app.post("/api/purchases")
    @login_required
    def add_purchase():
        data = json_body()
        item_id = integer_id(data.get("item_id"))
        quantity = positive_whole_number(data.get("quantity", 1), "quantity")
        db = get_db()
        item = db.execute("SELECT * FROM items WHERE id = ? AND active = 1", (item_id,)).fetchone()
        if not item:
            return error("Item not found.", 404)
        price = nonnegative_number(data.get("unit_price", item["unit_price"]), "unit price")
        purchased_on = clean_text(data.get("purchased_on"), 10) or date.today().isoformat()
        try:
            date.fromisoformat(purchased_on)
        except ValueError:
            return error("Use a valid purchase date.")
        total = round(quantity * price, 2)
        cursor = db.execute(
            """INSERT INTO purchases
                    (item_id, item_name, store, category, quantity, unit_price, total, purchase_date, source, source_period, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual', '', ?)""",
                (item_id, item["name"], item["store"], item["category"], quantity, price, total, purchased_on, now()),
        )
        db.execute("UPDATE items SET unit_price = ?, updated_at = ? WHERE id = ?", (price, now(), item_id))
        db.execute("DELETE FROM shopping_list WHERE item_id = ?", (item_id,))
        db.commit()
        return jsonify({"ok": True, "id": cursor.lastrowid}), 201

    @app.delete("/api/purchases/<int:purchase_id>")
    @login_required
    def delete_purchase(purchase_id):
        db = get_db()
        cursor = db.execute("DELETE FROM purchases WHERE id = ?", (purchase_id,))
        if cursor.rowcount == 0:
            return error_response("Purchase record not found.", 404)
        db.commit()
        return jsonify({"ok": True, "deleted_id": purchase_id})

    @app.put("/api/purchases/<int:purchase_id>")
    @login_required
    def update_purchase(purchase_id):
        data = json_body()
        unit_price = nonnegative_number(data.get("unit_price"), "unit price")
        db = get_db()
        purchase = db.execute(
            "SELECT id, quantity FROM purchases WHERE id = ?",
            (purchase_id,),
        ).fetchone()
        if not purchase:
            return error_response("Purchase record not found.", 404)
        total = round(purchase["quantity"] * unit_price, 2)
        db.execute(
            "UPDATE purchases SET unit_price = ?, total = ? WHERE id = ?",
            (unit_price, total, purchase_id),
        )
        db.commit()
        return jsonify({
            "ok": True,
            "id": purchase_id,
            "unit_price": unit_price,
            "total": total,
        })

    @app.get("/api/export.csv")
    @login_required
    def export_csv():
        db = get_db()
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["item", "store", "category", "quantity", "unit_price", "total", "purchase_date", "source"])
        for row in db.execute(
            """SELECT item_name, store, category, quantity, unit_price, total, purchase_date, source
               FROM purchases ORDER BY COALESCE(purchase_date, created_at), id"""
        ):
            writer.writerow([row[0], row[1], row[2], row[3], f"{row[4]:.2f}", f"{row[5]:.2f}", row[6] or row[7], row[7]])
        return Response(
            output.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=household-purchases.csv"},
        )

    @app.post("/api/members")
    @login_required
    def create_member():
        if current_user()["role"] != "admin":
            return error("Only the household owner can add members.", 403)
        data = json_body()
        name = required_text(data, "name", 60)
        username = normalized_username(data.get("username") or name)
        password = valid_password(data.get("password"))
        try:
            get_db().execute(
                "INSERT INTO users (name, username, password_hash, role, created_at) VALUES (?, ?, ?, 'member', ?)",
                (name, username, hash_password(password), now()),
            )
            get_db().commit()
        except sqlite3.IntegrityError:
            return error("That username is already in use.", 409)
        return jsonify({"ok": True}), 201

    initialize_database(app)
    return app


def initialize_database(app):
    with app.app_context():
        db = get_db()
        db.executescript(
            """CREATE TABLE IF NOT EXISTS users (
                   id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT NOT NULL UNIQUE,
                   password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member', created_at TEXT NOT NULL
               );
               CREATE TABLE IF NOT EXISTS items (
                   id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'Other',
                   store TEXT NOT NULL DEFAULT 'Unassigned', store_code TEXT NOT NULL DEFAULT '',
                   forecast_qty REAL NOT NULL DEFAULT 1, unit_price REAL NOT NULL DEFAULT 0,
                   product_url TEXT NOT NULL DEFAULT '', grocer_product_id INTEGER,
                   grocer_product_url TEXT NOT NULL DEFAULT '', price_refreshed_at TEXT,
                   price_refreshed_store_id INTEGER, price_refreshed_store_name TEXT,
                   active INTEGER NOT NULL DEFAULT 1,
                   created_at TEXT NOT NULL, updated_at TEXT NOT NULL
               );
               CREATE TABLE IF NOT EXISTS shopping_list (
                   id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL UNIQUE REFERENCES items(id) ON DELETE CASCADE,
                   quantity REAL NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
               );
               CREATE TABLE IF NOT EXISTS purchases (
                   id INTEGER PRIMARY KEY, item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
                   item_name TEXT NOT NULL, store TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'Other', quantity REAL NOT NULL,
                   unit_price REAL NOT NULL, total REAL NOT NULL, purchase_date TEXT,
                   source TEXT NOT NULL DEFAULT 'manual', source_period TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
               );
               CREATE TABLE IF NOT EXISTS settings (
                   key TEXT PRIMARY KEY, value TEXT NOT NULL
               );
               CREATE TABLE IF NOT EXISTS price_refresh (
                   store_id INTEGER PRIMARY KEY, store_name TEXT NOT NULL,
                   refreshed_at TEXT NOT NULL, source_modified TEXT NOT NULL DEFAULT ''
               );
               CREATE TABLE IF NOT EXISTS manual_spend (
                   id INTEGER PRIMARY KEY, month TEXT NOT NULL, category TEXT NOT NULL,
                   amount REAL NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
               );
               CREATE INDEX IF NOT EXISTS purchases_date_idx ON purchases(purchase_date);
               CREATE INDEX IF NOT EXISTS purchases_item_idx ON purchases(item_id);"""
        )
        item_columns = {row[1] for row in db.execute("PRAGMA table_info(items)").fetchall()}
        purchase_columns = {row[1] for row in db.execute("PRAGMA table_info(purchases)").fetchall()}
        if "category" not in purchase_columns:
            db.execute("ALTER TABLE purchases ADD COLUMN category TEXT NOT NULL DEFAULT 'Other'")
        db.execute(
            """UPDATE purchases SET category = COALESCE(
                   (SELECT category FROM items WHERE items.id = purchases.item_id), category)
               WHERE category = '' OR category = 'Other'"""
        )
        for column, definition in (
            ("grocer_product_id", "INTEGER"),
            ("grocer_product_url", "TEXT NOT NULL DEFAULT ''"),
            ("price_refreshed_at", "TEXT"),
            ("price_refreshed_store_id", "INTEGER"),
            ("price_refreshed_store_name", "TEXT"),
        ):
            if column not in item_columns:
                db.execute(f"ALTER TABLE items ADD COLUMN {column} {definition}")
        db.execute(
            """UPDATE items SET store_code = CASE lower(store)
                 WHEN 'pak ''n save' THEN 'PS'
                 WHEN 'chemist warehouse' THEN 'CM'
                 WHEN 'bostock' THEN 'BO'
                 WHEN 'butcher' THEN 'BT2'
                 WHEN 'woolworths' THEN 'WW'
                 WHEN 'nespresso' THEN 'NS'
                 ELSE store_code END
               WHERE store_code = ''"""
        )
        for table, column in (
            ("items", "forecast_qty"),
            ("shopping_list", "quantity"),
            ("purchases", "quantity"),
        ):
            db.execute(
                f"UPDATE {table} SET {column} = CAST(ROUND({column}) AS INTEGER) "
                f"WHERE {column} != CAST({column} AS INTEGER)"
            )
        db.commit()
        imported = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        if imported == 0:
            for filename in ("food.csv", "household.csv"):
                path = BASE_DIR / filename
                if path.exists():
                    import_legacy_file(db, path)
            db.commit()


def import_legacy_file(db, path):
    with path.open(newline="", encoding="utf-8-sig") as source:
        sample = source.read(4096)
        source.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters="\t,;")
        except csv.Error:
            dialect = csv.excel_tab
        rows = list(csv.reader(source, dialect))
    if not rows:
        return 0
    month_columns = []
    for column, label in enumerate(rows[0]):
        normalized = label.strip().lower()
        if normalized.endswith("rr"):
            normalized = normalized[:-1]
        if normalized in MONTHS:
            month_columns.append((column, normalized.title()))
    category = "Household" if path.name.lower().startswith("household") else "Other"
    store = "Unassigned"
    imported = 0
    for row in rows:
        row += [""] * max(0, 5 - len(row))
        label = next((value.strip() for value in row if value.strip()), "")
        if not label:
            continue
        if len(row) >= 3 and not row[1].strip() and not any(value.strip() for value in row[2:]):
            match = STORE_PATTERN.search(label)
            if match:
                store = label[:match.start()].strip()
            elif label.lower() not in {"forecast", "qty", "price", "total"}:
                category = label
            continue
        code = row[0].strip().upper()
        name = row[1].strip()
        if not CODE_PATTERN.fullmatch(code) or not name or name.lower() in {"sub total", "total"}:
            continue
        try:
            forecast_qty = max(0, float(row[2].replace(",", ""))) if row[2].strip() else 1
        except ValueError:
            forecast_qty = 1
        unit_price = parse_money(row[3])
        existing = db.execute(
            "SELECT id FROM items WHERE lower(name) = lower(?) AND store_code = ?",
            (name, code),
        ).fetchone()
        if existing:
            item_id = existing["id"]
        else:
            cursor = db.execute(
                """INSERT INTO items
                   (name, category, store, store_code, forecast_qty, unit_price, active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (name, category, store, code, forecast_qty, unit_price, now(), now()),
            )
            item_id = cursor.lastrowid
            imported += 1
        for column, month in month_columns:
            quantity_text = row[column].strip() if column < len(row) else ""
            amount_text = row[column + 1].strip() if column + 1 < len(row) else ""
            try:
                quantity = float(quantity_text.replace(",", "")) if quantity_text else 0
            except ValueError:
                quantity = 0
            total = parse_money(amount_text)
            if quantity <= 0 or total <= 0:
                continue
            db.execute(
                """INSERT INTO purchases
                         (item_id, item_name, store, category, quantity, unit_price, total, purchase_date, source, source_period, created_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'legacy', ?, ?)""",
                     (item_id, name, store, category, quantity, round(total / quantity, 4), total,
                 f"{month} · {path.stem}", now()),
            )
    return imported


def parse_money(value):
    try:
        return max(0, float(value.strip().replace("$", "").replace(",", "")))
    except (AttributeError, ValueError):
        return 0


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app_config_database(), timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def current_app_config_database():
    from flask import current_app
    return current_app.config["DATABASE"]


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return error("Sign in to continue.", 401)
        return view(*args, **kwargs)
    return wrapped


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    row = get_db().execute("SELECT id, name, username, role FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def start_session(user_id):
    session.clear()
    session["user_id"] = user_id
    session["csrf_token"] = secrets.token_urlsafe(32)


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password, stored):
    try:
        salt_hex, digest_hex = stored.split(":", 1)
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="Expected a JSON object.")
    return data


def clean_text(value, limit):
    return str(value or "").strip()[:limit]


def required_text(data, key, limit):
    value = clean_text(data.get(key), limit)
    if not value:
        abort(400, description=f"{key.replace('_', ' ').capitalize()} is required.")
    return value


def normalized_username(value):
    username = re.sub(r"\s+", "", str(value or "").strip().lower())[:40]
    if not re.fullmatch(r"[a-z0-9._-]{3,40}", username):
        abort(400, description="Username must be at least 3 characters and use letters, numbers, dots, dashes, or underscores.")
    return username


def valid_password(value):
    password = str(value or "")
    if len(password) < 10 or len(password) > 200:
        abort(400, description="Use a password between 10 and 200 characters.")
    return password


def nonnegative_number(value, label):
    try:
        number = float(value)
    except (TypeError, ValueError):
        abort(400, description=f"{label.capitalize()} must be a number.")
    if not 0 <= number <= 100000:
        abort(400, description=f"{label.capitalize()} must be between 0 and 100000.")
    return number


def positive_number(value, label):
    number = nonnegative_number(value, label)
    if number <= 0:
        abort(400, description=f"{label.capitalize()} must be greater than zero.")
    return number


def whole_number(value, label):
    if isinstance(value, bool):
        abort(400, description=f"{label.capitalize()} must be a whole number.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        abort(400, description=f"{label.capitalize()} must be a whole number.")
    if not number.is_integer() or not 0 <= number <= 100000:
        abort(400, description=f"{label.capitalize()} must be a whole number between 0 and 100000.")
    return int(number)


def positive_whole_number(value, label):
    number = whole_number(value, label)
    if number <= 0:
        abort(400, description=f"{label.capitalize()} must be a whole number greater than zero.")
    return number


def integer_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        abort(400, description="Invalid item selection.")


def valid_url(value):
    url = clean_text(value, 500)
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        abort(400, description="Product links must start with http:// or https://.")
    return url


def error(message, status=400):
    return jsonify({"error": message}), status


def error_response(message, status=400):
    return jsonify({"error": message}), status


def history_category(category):
    return "Household" if str(category or "").strip().lower() == "household" else "Food"


def optional_positive_id(value):
    if value in {None, ""}:
        return None
    result = integer_id(value)
    if result <= 0:
        abort(400, description="Grocer product ID must be positive.")
    return result


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


from flask import current_app


app = create_app()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")), debug=False)