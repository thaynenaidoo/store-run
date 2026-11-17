import tempfile
import sqlite3
import unittest
from datetime import date
from pathlib import Path
from urllib.error import HTTPError

from unittest.mock import patch

from app import create_app, initialize_database
from grocer_prices import (
    RetailerPageError,
    choose_product_match,
    fetch_retailer_product_price,
    grocer_product_id_from_url,
)


class HouseholdAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "DATABASE": str(Path(self.temp_dir.name) / "test.db"),
            "SECRET_KEY": "test-secret-key",
            "IMPORT_SAMPLE_CSVS": True,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def setup_owner(self):

        response = self.client.post("/api/setup", json={
            "name": "Test Owner",
            "username": "owner",
            "password": "long-test-password",
        })
        self.assertEqual(response.status_code, 200)
        state = self.client.get("/api/session").get_json()
        return state["csrf_token"]

    def test_first_run_import_and_shared_shopping_flow(self):
        session_state = self.client.get("/api/session").get_json()
        self.assertTrue(session_state["needs_setup"])
        token = self.setup_owner()
        state = self.client.get("/api/state").get_json()
        self.assertGreater(state["stats"]["item_count"], 50)
        self.assertGreater(state["stats"]["legacy_count"], 0)
        self.assertTrue(any(row["month"] == "March" for row in state["stats"]["legacy_monthly_spend"]))

        headers = {"X-CSRF-Token": token}
        created = self.client.post("/api/items", headers=headers, json={
            "name": "Test oats",
            "category": "Pantry",
            "store": "Market",
            "forecast_qty": 2,
            "unit_price": 4.5,
        })
        self.assertEqual(created.status_code, 201)
        item_id = created.get_json()["id"]
        added = self.client.post("/api/shopping", headers=headers, json={
            "item_id": item_id,
            "quantity": 3,
        })
        self.assertEqual(added.status_code, 200)
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["shopping"][0]["quantity"], 3)

        purchased = self.client.post("/api/purchases", headers=headers, json={
            "item_id": item_id,
            "quantity": 3,
            "unit_price": 4.75,
            "purchased_on": date.today().isoformat(),
        })
        self.assertEqual(purchased.status_code, 201)
        state = self.client.get("/api/state").get_json()
        self.assertFalse(any(row["item_id"] == item_id for row in state["shopping"]))
        recent_purchase = next(row for row in state["recent"] if row["item_name"] == "Test oats")
        self.assertEqual(recent_purchase["total"], 14.25)
        self.assertEqual(state["stats"]["month_spend"], 14.25)

    def test_quantity_endpoints_reject_fractional_values(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        fractional_item = self.client.post("/api/items", headers=headers, json={
            "name": "Fractional forecast",
            "forecast_qty": 1.5,
            "unit_price": 2.0,
        })
        self.assertEqual(fractional_item.status_code, 400)
        self.assertIn("whole number", fractional_item.get_json()["error"])

        valid_item = self.client.post("/api/items", headers=headers, json={
            "name": "Whole quantity item",
            "forecast_qty": 2,
            "unit_price": 2.0,
        })
        self.assertEqual(valid_item.status_code, 201)
        item_id = valid_item.get_json()["id"]
        fractional_shopping = self.client.post("/api/shopping", headers=headers, json={
            "item_id": item_id,
            "quantity": 1.5,
        })
        self.assertEqual(fractional_shopping.status_code, 400)
        self.assertIn("whole number", fractional_shopping.get_json()["error"])

        fractional_purchase = self.client.post("/api/purchases", headers=headers, json={
            "item_id": item_id,
            "quantity": 1.5,
            "unit_price": 2.0,
        })
        self.assertEqual(fractional_purchase.status_code, 400)
        self.assertIn("whole number", fractional_purchase.get_json()["error"])

    def test_history_reports_twelve_months_and_category_store_breakdowns(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        dated_purchases = [
            ("Pantry test food", "Pantry", "Pak 'n Save", 12.50),
            ("Bathroom test item", "Household", "Chemist Warehouse", 8.25),
        ]
        for name, category, store, price in dated_purchases:
            item = self.client.post("/api/items", headers=headers, json={
                "name": name,
                "category": category,
                "store": store,
                "forecast_qty": 1,
                "unit_price": price,
            })
            self.assertEqual(item.status_code, 201)
            purchase = self.client.post("/api/purchases", headers=headers, json={
                "item_id": item.get_json()["id"],
                "quantity": 1,
                "unit_price": price,
                "purchased_on": "2026-11-16",
            })
            self.assertEqual(purchase.status_code, 201)

        history = self.client.get("/api/history")
        self.assertEqual(history.status_code, 200)
        data = history.get_json()
        self.assertEqual(len(data["months"]), 12)
        november = next(month for month in data["months"] if month["month"] == "2026-11")
        self.assertEqual(november["total"], 20.75)
        self.assertEqual(november["food"], 12.5)
        self.assertEqual(november["household"], 8.25)
        self.assertEqual({row["store"] for row in data["stores"]}, {"Pak 'n Save", "Chemist Warehouse"})
        self.assertGreater(data["undated_legacy"]["count"], 0)
        self.assertEqual(data["highlights"]["purchase_count"], 2)

    def test_deleting_purchase_recalculates_history_and_month_spend(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        item = self.client.post("/api/items", headers=headers, json={
            "name": "Delete test household item",
            "category": "Household",
            "store": "Test Store",
            "forecast_qty": 1,
            "unit_price": 9.50,
        })
        self.assertEqual(item.status_code, 201)
        purchase = self.client.post("/api/purchases", headers=headers, json={
            "item_id": item.get_json()["id"],
            "quantity": 2,
            "unit_price": 9.50,
            "purchased_on": "2026-11-16",
        })
        self.assertEqual(purchase.status_code, 201)
        purchase_id = purchase.get_json()["id"]

        removed = self.client.delete(f"/api/purchases/{purchase_id}", headers=headers)

        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.client.get("/api/state").get_json()["stats"]["month_spend"], 0)
        history = self.client.get("/api/history").get_json()
        self.assertEqual(history["highlights"]["total_spend"], 0)
        self.assertEqual(history["highlights"]["purchase_count"], 0)
        self.assertEqual(history["purchases"], [])
        self.assertEqual(history["categories"][1]["total"], 0)

    def test_deleting_missing_purchase_returns_404(self):
        token = self.setup_owner()
        response = self.client.delete("/api/purchases/999999", headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 404)

    def test_amending_purchase_price_recalculates_record_and_history(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        item = self.client.post("/api/items", headers=headers, json={
            "name": "Price correction test",
            "category": "Pantry",
            "store": "Pak 'n Save",
            "forecast_qty": 1,
            "unit_price": 5,
        })
        purchase = self.client.post("/api/purchases", headers=headers, json={
            "item_id": item.get_json()["id"],
            "quantity": 2,
            "unit_price": 5,
            "purchased_on": "2026-11-17",
        })
        purchase_id = purchase.get_json()["id"]

        amended = self.client.put(
            f"/api/purchases/{purchase_id}",
            headers=headers,
            json={"unit_price": 4.25},
        )

        self.assertEqual(amended.status_code, 200)
        self.assertEqual(amended.get_json()["unit_price"], 4.25)
        self.assertEqual(amended.get_json()["total"], 8.5)
        history = self.client.get("/api/history").get_json()
        november = next(month for month in history["months"] if month["month"] == "2026-11")
        self.assertEqual(november["total"], 8.5)
        self.assertEqual(november["food"], 8.5)

    def test_manual_monthly_spend_updates_history_categories_and_can_be_removed(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        food = self.client.post("/api/manual-spend", headers=headers, json={
            "month": "2026-10", "category": "Food", "amount": 125.50, "note": "October food",
        })
        household = self.client.post("/api/manual-spend", headers=headers, json={
            "month": "2026-10", "category": "Household", "amount": 35.25, "note": "October supplies",
        })
        self.assertEqual(food.status_code, 201)
        self.assertEqual(household.status_code, 201)
        history = self.client.get("/api/history").get_json()
        october = next(month for month in history["months"] if month["month"] == "2026-10")
        self.assertEqual(october["food"], 125.50)
        self.assertEqual(october["household"], 35.25)
        self.assertEqual(october["total"], 160.75)
        self.assertEqual(history["highlights"]["purchase_count"], 0)
        self.assertEqual(history["manual_spend"][0]["note"], "October supplies")
        self.assertTrue(any(row["store"] == "Manual spend · store not recorded" for row in history["stores"]))

        removed = self.client.delete(f"/api/manual-spend/{food.get_json()['id']}", headers=headers)
        self.assertEqual(removed.status_code, 200)
        history = self.client.get("/api/history").get_json()
        october = next(month for month in history["months"] if month["month"] == "2026-10")
        self.assertEqual(october["food"], 0)
        self.assertEqual(october["total"], 35.25)

    def test_manual_monthly_spend_rejects_bad_month_category_and_amount(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        for data in (
            {"month": "2026-13", "category": "Food", "amount": 10},
            {"month": "2026-10", "category": "Other", "amount": 10},
            {"month": "2026-10", "category": "Food", "amount": 0},
        ):
            response = self.client.post("/api/manual-spend", headers=headers, json=data)
            self.assertEqual(response.status_code, 400)

    def test_manual_store_run_can_be_added_to_a_past_month_with_unknown_amount(self):
        token = self.setup_owner()
        response = self.client.post("/api/manual-spend", headers={"X-CSRF-Token": token}, json={
            "month": "2026-08",
            "category": "Food",
            "store": "Local Butcher",
            "detail": "Meat",
            "note": "Monthly shop",
            "amount": None,
        })
        self.assertEqual(response.status_code, 201)
        history = self.client.get("/api/history").get_json()
        august = next(month for month in history["months"] if month["month"] == "2026-08")
        self.assertEqual(august["total"], 0)
        self.assertEqual(august["unknown_amount_count"], 1)
        entry = history["manual_spend"][0]
        self.assertEqual(entry["store"], "Local Butcher")
        self.assertEqual(entry["detail"], "Meat")
        self.assertIsNone(entry["amount"])
        self.assertTrue(any(row["store"] == "Local Butcher" and row["unknown_amount_count"] == 1 for row in history["stores"]))

    def test_household_name_is_persisted_and_exposed_in_state(self):
        token = self.setup_owner()
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["household"]["name"], "Our Household")
        updated = self.client.put("/api/household", headers={"X-CSRF-Token": token}, json={"name": "The Test Home"})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(self.client.get("/api/state").get_json()["household"]["name"], "The Test Home")

    def test_sample_csv_import_is_disabled_by_default(self):
        app = create_app({
            "TESTING": True,
            "DATABASE": str(Path(self.temp_dir.name) / "no-samples.db"),
            "SECRET_KEY": "test-secret-key",
            "IMPORT_SAMPLE_CSVS": False,
        })
        client = app.test_client()
        client.post("/api/setup", json={"name": "Owner", "username": "owner", "password": "long-test-password"})
        self.assertEqual(client.get("/api/session").get_json()["needs_setup"], False)
        self.assertEqual(client.get("/api/state").get_json()["stats"]["item_count"], 0)

    def test_existing_manual_spend_rows_migrate_without_losing_data(self):
        database_path = Path(self.temp_dir.name) / "legacy-manual-spend.db"
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """CREATE TABLE manual_spend (
                       id INTEGER PRIMARY KEY, month TEXT NOT NULL, category TEXT NOT NULL,
                       amount REAL NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                   )"""
            )
            connection.execute(
                "INSERT INTO manual_spend (month, category, amount, note, created_at) VALUES (?, ?, ?, ?, ?)",
                ("2026-08", "Food", 42.5, "Legacy entry", "2026-11-17T00:00:00+00:00"),
            )
        app = create_app({
            "TESTING": True,
            "DATABASE": str(database_path),
            "SECRET_KEY": "test-secret-key",
            "IMPORT_SAMPLE_CSVS": False,
        })
        with app.app_context():
            from app import get_db
            row = get_db().execute("SELECT * FROM manual_spend").fetchone()
            self.assertEqual(row["amount"], 42.5)
            self.assertEqual(row["note"], "Legacy entry")
            self.assertEqual(row["store"], "Manual spend · store not recorded")
            self.assertEqual(row["household_id"], 1)

    def test_existing_fractional_quantities_are_normalized_on_startup(self):
        self.setup_owner()
        item = self.client.get("/api/state").get_json()["items"][0]
        with self.app.app_context():
            from app import get_db
            get_db().execute("UPDATE items SET forecast_qty = 3.1 WHERE id = ?", (item["id"],))
            get_db().commit()
        initialize_database(self.app)
        state = self.client.get("/api/state").get_json()
        normalized = next(row for row in state["items"] if row["id"] == item["id"])
        self.assertEqual(normalized["forecast_qty"], 3)

    def test_mutations_require_csrf_token(self):
        self.setup_owner()
        response = self.client.post("/api/items", json={"name": "No token"})
        self.assertEqual(response.status_code, 403)

    def test_member_creation_is_owner_only(self):
        token = self.setup_owner()
        response = self.client.post("/api/members", headers={"X-CSRF-Token": token}, json={
            "name": "Household Member",
            "username": "member",
            "password": "another-long-password",
        })
        self.assertEqual(response.status_code, 201)

    def test_store_list_bulk_add_uses_usual_quantities(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        response = self.client.post("/api/shopping/store", headers=headers, json={"store": "Pak 'n Save"})
        self.assertEqual(response.status_code, 200)
        state = self.client.get("/api/state").get_json()
        pak_items = [item for item in state["items"] if item["store"] == "Pak 'n Save" and item["forecast_qty"] > 0]
        pak_shopping = [item for item in state["shopping"] if item["store"] == "Pak 'n Save"]
        self.assertEqual(len(pak_shopping), len(pak_items))
        self.assertAlmostEqual(
            sum(row["quantity"] * row["unit_price"] for row in pak_shopping),
            sum(item["forecast_qty"] * item["unit_price"] for item in pak_items),
        )

    def test_grocer_match_requires_high_confidence(self):
        products = [
            {"id": 1, "name": "Woodland Grade 7 Eggs 20pk Barn", "brand": "Woodland", "size": "20pk"},
            {"id": 2, "name": "Woodland Eggs Free Range Size 7 10 Pack", "brand": "Woodland", "size": "10pk"},
        ]
        self.assertIsNone(choose_product_match("Eggs 20pk Woodland s7", products))
        self.assertEqual(choose_product_match("Woodland Grade 7 Eggs 20pk Barn", products)["id"], 1)

    def test_editing_item_without_store_code_preserves_its_store(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        item = next(row for row in self.client.get("/api/state").get_json()["items"] if row["store_code"] == "PS")
        response = self.client.put(f"/api/items/{item['id']}", headers=headers, json={
            "name": item["name"],
            "category": item["category"],
            "store": item["store"],
            "forecast_qty": item["forecast_qty"],
            "unit_price": item["unit_price"],
            "product_url": item["product_url"],
        })
        self.assertEqual(response.status_code, 200)
        saved = next(row for row in self.client.get("/api/state").get_json()["items"] if row["id"] == item["id"])
        self.assertEqual(saved["store_code"], "PS")

    def test_price_refresh_updates_saved_grocer_product_for_selected_store(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        item = self.client.get("/api/state").get_json()["items"]
        pak_item = next(row for row in item if row["store_code"] == "PS")
        with patch("app.GrocerPriceClient") as grocer_client:
            instance = grocer_client.return_value
            instance.stores_for_vendor.return_value = ([{"id": 1234, "name": "PAK'nSAVE Highland Park"}], "catalog stamp")
            instance.prices_for_store.return_value = ({98765: 2149}, "price file stamp")
            instance.products.return_value = [{"id": 98765, "name": pak_item["name"], "brand": "", "size": ""}]
            selected = self.client.put("/api/pricing/store", headers=headers, json={
                "store_id": 1234,
                "store_name": "PAK'nSAVE Highland Park",
            })
            self.assertEqual(selected.status_code, 200)
            refreshed = self.client.post("/api/pricing/refresh", headers=headers, json={})

        self.assertEqual(refreshed.status_code, 200)
        result = refreshed.get_json()
        self.assertEqual(len(result["updated"]), 1)
        state = self.client.get("/api/state").get_json()
        updated = next(row for row in state["items"] if row["id"] == pak_item["id"])
        self.assertEqual(updated["unit_price"], 21.49)
        self.assertEqual(updated["grocer_product_id"], 98765)
        self.assertIn("/product/98765/", updated["grocer_product_url"])
        self.assertEqual(state["pricing"]["refreshes"][0]["store_name"], "PAK'nSAVE Highland Park")

    def test_item_price_refresh_updates_only_the_requested_item(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        initial_items = self.client.get("/api/state").get_json()["items"]
        target, untouched = initial_items[:2]
        target_price = target["unit_price"]
        untouched_price = untouched["unit_price"]
        edit = self.client.put(f"/api/items/{target['id']}", headers=headers, json={
            "name": target["name"],
            "category": target["category"],
            "store": target["store"],
            "store_code": target["store_code"],
            "forecast_qty": target["forecast_qty"],
            "unit_price": target_price,
            "product_url": target["product_url"],
            "grocer_product_id": 98765,
            "grocer_product_url": "https://grocer.nz/product/98765/test-item",
        })
        self.assertEqual(edit.status_code, 200)
        with patch("app.GrocerPriceClient") as grocer_client:
            grocer_client.return_value.stores_for_vendor.return_value = (
                [{"id": 1234, "name": "PAK'nSAVE Highland Park"}],
                "catalog stamp",
            )
            grocer_client.return_value.prices_for_store.return_value = ({98765: 359}, "price file stamp")
            selected = self.client.put("/api/pricing/store", headers=headers, json={
                "store_id": 1234,
                "store_name": "PAK'nSAVE Highland Park",
            })
            self.assertEqual(selected.status_code, 200)
            refreshed = self.client.post(f"/api/items/{target['id']}/refresh-price", headers=headers, json={})

        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.get_json()["price"], 3.59)
        state = self.client.get("/api/state").get_json()
        updated = {row["id"]: row for row in state["items"]}
        self.assertEqual(updated[target["id"]]["unit_price"], 3.59)
        self.assertEqual(updated[target["id"]]["price_refreshed_store_name"], "PAK'nSAVE Highland Park")
        self.assertEqual(updated[untouched["id"]]["unit_price"], untouched_price)

    def test_item_price_refresh_reports_when_no_fallback_link_exists(self):
        token = self.setup_owner()
        item = self.client.get("/api/state").get_json()["items"][0]
        response = self.client.post(
            f"/api/items/{item['id']}/refresh-price",
            headers={"X-CSRF-Token": token},
            json={},
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("No retailer product link is saved", response.get_json()["error"])

    def test_item_refresh_uses_product_id_from_manual_grocer_url(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        item = self.client.get("/api/state").get_json()["items"][0]
        edited = self.client.put(f"/api/items/{item['id']}", headers=headers, json={
            "name": item["name"],
            "category": item["category"],
            "store": item["store"],
            "store_code": item["store_code"],
            "forecast_qty": item["forecast_qty"],
            "unit_price": item["unit_price"],
            "product_url": "",
            "grocer_product_url": "https://grocer.nz/product/98765/test-item",
        })
        self.assertEqual(edited.status_code, 200)
        with patch("app.GrocerPriceClient") as grocer_client:
            grocer_client.return_value.stores_for_vendor.return_value = (
                [{"id": 1234, "name": "PAK'nSAVE Highland Park"}],
                "catalog stamp",
            )
            grocer_client.return_value.prices_for_store.return_value = ({98765: 359}, "price file stamp")
            selected = self.client.put("/api/pricing/store", headers=headers, json={
                "store_id": 1234,
                "store_name": "PAK'nSAVE Highland Park",
            })
            self.assertEqual(selected.status_code, 200)
            refreshed = self.client.post(f"/api/items/{item['id']}/refresh-price", headers=headers, json={})
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.get_json()["price"], 3.59)
        saved = next(row for row in self.client.get("/api/state").get_json()["items"] if row["id"] == item["id"])
        self.assertEqual(saved["grocer_product_id"], 98765)
        self.assertIsNotNone(saved["price_refreshed_at"])

    def test_item_refresh_recognizes_grocer_url_pasted_in_retailer_link_field(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        item = self.client.get("/api/state").get_json()["items"][0]
        grocer_url = "https://grocer.nz/product/41271/ufc-coconut-water-100-natural"
        edited = self.client.put(f"/api/items/{item['id']}", headers=headers, json={
            "name": item["name"],
            "category": item["category"],
            "store": item["store"],
            "store_code": item["store_code"],
            "forecast_qty": item["forecast_qty"],
            "unit_price": item["unit_price"],
            "product_url": grocer_url,
            "grocer_product_url": "",
        })
        self.assertEqual(edited.status_code, 200)
        with patch("app.GrocerPriceClient") as grocer_client, patch("app.fetch_retailer_product_price") as retailer_lookup:
            grocer_client.return_value.stores_for_vendor.return_value = (
                [{"id": 1234, "name": "PAK'nSAVE Highland Park"}],
                "catalog stamp",
            )
            grocer_client.return_value.prices_for_store.return_value = ({41271: 399}, "price file stamp")
            selected = self.client.put("/api/pricing/store", headers=headers, json={
                "store_id": 1234,
                "store_name": "PAK'nSAVE Highland Park",
            })
            self.assertEqual(selected.status_code, 200)
            refreshed = self.client.post(f"/api/items/{item['id']}/refresh-price", headers=headers, json={})
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.get_json()["price"], 3.99)
        retailer_lookup.assert_not_called()
        saved = next(row for row in self.client.get("/api/state").get_json()["items"] if row["id"] == item["id"])
        self.assertEqual(saved["grocer_product_id"], 41271)
        self.assertEqual(saved["grocer_product_url"], grocer_url)
        self.assertEqual(saved["product_url"], "")

    def test_item_refresh_falls_back_to_retailer_price_metadata(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        item = next(row for row in self.client.get("/api/state").get_json()["items"] if row["store_code"] == "PS")
        edited = self.client.put(f"/api/items/{item['id']}", headers=headers, json={
            "name": item["name"],
            "category": item["category"],
            "store": item["store"],
            "store_code": item["store_code"],
            "forecast_qty": item["forecast_qty"],
            "unit_price": item["unit_price"],
            "product_url": "https://www.paknsave.co.nz/shop/product/12345/test-product",
            "grocer_product_id": 98765,
        })
        self.assertEqual(edited.status_code, 200)
        with patch("app.GrocerPriceClient") as grocer_client, patch("app.fetch_retailer_product_price") as retailer_lookup:
            grocer_client.return_value.stores_for_vendor.return_value = (
                [{"id": 1234, "name": "PAK'nSAVE Highland Park"}],
                "catalog stamp",
            )
            grocer_client.return_value.prices_for_store.return_value = ({}, "price file stamp")
            retailer_lookup.return_value = {
                "price": 4.25,
                "source_modified": "Mon, 16 Nov 2026 01:00:00 GMT",
                "url": "https://www.paknsave.co.nz/shop/product/12345/test-product",
            }
            selected = self.client.put("/api/pricing/store", headers=headers, json={
                "store_id": 1234,
                "store_name": "PAK'nSAVE Highland Park",
            })
            self.assertEqual(selected.status_code, 200)
            refreshed = self.client.post(f"/api/items/{item['id']}/refresh-price", headers=headers, json={})
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.get_json()["price"], 4.25)
        self.assertEqual(refreshed.get_json()["source"], "Retailer page")
        saved = next(row for row in self.client.get("/api/state").get_json()["items"] if row["id"] == item["id"])
        self.assertEqual(saved["unit_price"], 4.25)
        self.assertEqual(saved["price_refreshed_store_name"], "Pak 'n Save product page")
        retailer_lookup.assert_called_once_with("https://www.paknsave.co.nz/shop/product/12345/test-product", "Pak 'n Save")

    def test_chemist_warehouse_refresh_uses_its_product_page_first(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        item = self.client.get("/api/state").get_json()["items"][0]
        product_url = "https://www.chemistwarehouse.co.nz/buy/12345/test-product"
        edited = self.client.put(f"/api/items/{item['id']}", headers=headers, json={
            "name": item["name"],
            "category": item["category"],
            "store": "Chemist Warehouse",
            "store_code": item["store_code"],
            "forecast_qty": item["forecast_qty"],
            "unit_price": item["unit_price"],
            "product_url": product_url,
            "grocer_product_id": "",
            "grocer_product_url": "",
        })
        self.assertEqual(edited.status_code, 200)
        with patch("app.fetch_retailer_product_price") as retailer_lookup, patch("app.GrocerPriceClient") as grocer_client:
            retailer_lookup.return_value = {
                "price": 12.49,
                "source_modified": "",
                "url": product_url,
            }
            refreshed = self.client.post(f"/api/items/{item['id']}/refresh-price", headers=headers, json={})

        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.get_json()["price"], 12.49)
        self.assertEqual(refreshed.get_json()["store_name"], "Chemist Warehouse product page")
        retailer_lookup.assert_called_once_with(product_url, "Chemist Warehouse")
        grocer_client.assert_not_called()

    def test_bulk_price_refresh_uses_selected_retailer_product_pages(self):
        token = self.setup_owner()
        headers = {"X-CSRF-Token": token}
        item = self.client.get("/api/state").get_json()["items"][0]
        product_url = "https://www.chemistwarehouse.co.nz/buy/12345/test-product"
        edited = self.client.put(f"/api/items/{item['id']}", headers=headers, json={
            "name": item["name"],
            "category": item["category"],
            "store": "Chemist Warehouse",
            "store_code": item["store_code"],
            "forecast_qty": item["forecast_qty"],
            "unit_price": item["unit_price"],
            "product_url": product_url,
        })
        self.assertEqual(edited.status_code, 200)
        with patch("app.fetch_retailer_product_price") as retailer_lookup:
            retailer_lookup.return_value = {"price": 9.99, "source_modified": "", "url": product_url}
            refreshed = self.client.post(
                "/api/pricing/refresh",
                headers=headers,
                json={"store": "Chemist Warehouse"},
            )

        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.get_json()["updated"][0]["price"], 9.99)
        retailer_lookup.assert_called_once_with(product_url, "Chemist Warehouse")

    def test_grocer_url_id_and_retailer_host_validation(self):
        self.assertEqual(grocer_product_id_from_url("https://grocer.nz/product/224191/eggs"), 224191)
        self.assertIsNone(grocer_product_id_from_url("https://example.com/product/224191/eggs"))
        with self.assertRaises(RetailerPageError):
            fetch_retailer_product_price("https://example.com/product/123", "Pak 'n Save")

    def test_retailer_lookup_reads_structured_nzd_price(self):
        response = unittest.mock.MagicMock()
        response.geturl.return_value = "https://www.paknsave.co.nz/shop/product/123/test-product"
        response.headers = {"Last-Modified": "Mon, 16 Nov 2026 01:00:00 GMT"}
        response.read.return_value = (
            b'<script type="application/ld+json">'
            b'{"@type":"Product","offers":{"@type":"Offer","price":"4.75","priceCurrency":"NZD"}}'
            b'</script>'
        )
        with patch("grocer_prices.build_opener") as build_opener:
            build_opener.return_value.open.return_value.__enter__.return_value = response
            result = fetch_retailer_product_price(
                "https://www.paknsave.co.nz/shop/product/123/test-product",
                "Pak 'n Save",
            )
        self.assertEqual(result["price"], 4.75)
        self.assertEqual(result["source_modified"], "Mon, 16 Nov 2026 01:00:00 GMT")

    def test_bostocks_product_domain_is_allowed_for_price_lookup(self):
        product_url = "https://bostocksorganic.co.nz/shop/organic-chicken/"
        response = unittest.mock.MagicMock()
        response.geturl.return_value = product_url
        response.headers = {"Last-Modified": ""}
        response.read.return_value = (
            b'<script type="application/ld+json">'
            b'{"@type":"Product","offers":{"@type":"Offer","price":"18.50","priceCurrency":"NZD"}}'
            b'</script>'
        )
        with patch("grocer_prices.build_opener") as build_opener:
            build_opener.return_value.open.return_value.__enter__.return_value = response
            result = fetch_retailer_product_price(product_url, "Bostock's Organic")
        self.assertEqual(result["price"], 18.5)

    def test_retailer_403_explains_manual_page_fallback(self):
        with patch("grocer_prices.build_opener") as build_opener:
            build_opener.return_value.open.side_effect = HTTPError(
                "https://www.paknsave.co.nz/shop/product/5310201_ea_000pns",
                403,
                "Forbidden",
                {},
                None,
            )
            with self.assertRaises(RetailerPageError) as raised:
                fetch_retailer_product_price(
                    "https://www.paknsave.co.nz/shop/product/5310201_ea_000pns",
                    "Pak 'n Save",
                )
        self.assertIn("blocked automated price lookup (HTTP 403)", str(raised.exception))
        self.assertIn("Open the retailer product page", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
