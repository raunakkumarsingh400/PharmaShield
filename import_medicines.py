"""
Import the medicine CSV into MongoDB.

Fast (bulk upserts), skips blank rows, converts types, and adds the derived
fields used for search, alternatives and safety checks. Safe to re-run.

    python import_medicines.py [path/to/file.csv | path/to/file.xlsx]

For a small demo, import only PharmaShield_Demo_Data.xlsx (sheet "Medicines").
Importing the big CSV as well is optional; rows with the same product_id are merged.
"""
from pharmashield.envfile import load_env

load_env()

import csv
import os
import sys

from pymongo import MongoClient, UpdateOne

from core.safety import enrich_medicine


MONGO_URI = os.getenv("PHARMASHIELD_MONGO_URI", "mongodb://127.0.0.1:27017")
DB_NAME = os.getenv("PHARMASHIELD_DB", "pharmashield")
DEFAULT_CSV = "indian_pharmaceutical_products_clean.csv"
BATCH = 2000


def iter_rows(path):
    """Yield dict rows from a .csv or .xlsx file."""
    if path.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        sheet = wb["Medicines"] if "Medicines" in wb.sheetnames else wb.worksheets[0]
        rows = sheet.iter_rows(values_only=True)
        headers = [str(h).strip() if h is not None else "" for h in next(rows)]

        for values in rows:
            yield {
                h: ("" if v is None else str(v).strip())
                for h, v in zip(headers, values)
                if h
            }
        wb.close()
        return

    csv.field_size_limit(10_000_000)

    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            yield {k: (v or "").strip() for k, v in row.items() if k}


def main():
    csv_file = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV

    client = MongoClient(MONGO_URI)
    client.admin.command("ping")
    collection = client[DB_NAME].medicines

    # Remove blank documents created by older versions of this script.
    removed = collection.delete_many(
        {"$or": [{"product_id": ""}, {"product_id": {"$exists": False}}],
         "brand_name": {"$in": ["", None]},
         "medicine_name": {"$in": ["", None]}}
    ).deleted_count

    if removed:
        print(f"Removed {removed} blank records from earlier imports.")

    total = skipped = 0
    operations = []

    for row in iter_rows(csv_file):
        product_id = row.get("product_id", "")

        if not product_id or not row.get("brand_name"):
            skipped += 1
            continue

        document = enrich_medicine(row)
        operations.append(
            UpdateOne({"product_id": product_id}, {"$set": document}, upsert=True)
        )
        total += 1

        if len(operations) >= BATCH:
            collection.bulk_write(operations, ordered=False)
            operations = []
            print(f"  ...{total} records", end="\r")

    if operations:
        collection.bulk_write(operations, ordered=False)

    print(f"Imported/updated {total} medicine records ({skipped} blank/invalid rows skipped).")


if __name__ == "__main__":
    main()
