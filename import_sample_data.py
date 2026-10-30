"""
Load the small demo dataset (sample_data/PharmaShield_Sample_Data.xlsx) into MongoDB.

    python import_sample_data.py            # add / update the sample records
    python import_sample_data.py --reset    # first DELETE medicines, scans and reports, then load

Needs: pip install openpyxl
"""
import os
import sys
from datetime import datetime, timezone

from openpyxl import load_workbook
from pymongo import MongoClient, UpdateOne

from core.safety import enrich_medicine


MONGO_URI = os.getenv("PHARMASHIELD_MONGO_URI", "mongodb://127.0.0.1:27017")
DB_NAME = os.getenv("PHARMASHIELD_DB", "pharmashield")
XLSX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_data", "PharmaShield_Sample_Data.xlsx")

IGNORED_COLUMNS = {"demo_scenario"}


def read_sheet(workbook, name):
    sheet = workbook[name]
    rows = sheet.iter_rows(values_only=True)
    headers = [str(h).strip() if h is not None else "" for h in next(rows)]

    for values in rows:
        record = {}

        for header, value in zip(headers, values):
            if not header or header in IGNORED_COLUMNS or value is None:
                continue

            record[header] = value.strip() if isinstance(value, str) else value

        if record:
            yield record


def main():
    reset = "--reset" in sys.argv

    client = MongoClient(MONGO_URI)
    client.admin.command("ping")
    db = client[DB_NAME]

    if reset:
        answer = input(
            f"This deletes ALL medicines, scans and reports in '{DB_NAME}'. Type YES to continue: "
        )

        if answer.strip() != "YES":
            print("Cancelled.")
            return

        for name in ("medicines", "scans", "reports"):
            db[name].delete_many({})

    workbook = load_workbook(XLSX, read_only=True, data_only=True)

    operations = []

    for record in read_sheet(workbook, "Medicines"):
        product_id = str(record.get("product_id", "")).strip()

        if not product_id or not record.get("batch_number"):
            continue

        for key in ("product_id", "barcode", "batch_number", "pack_size"):
            if key in record:
                record[key] = str(record[key]).strip()

        record["is_discontinued"] = str(record.get("is_discontinued", "")).upper() == "TRUE"

        operations.append(
            UpdateOne({"product_id": product_id}, {"$set": enrich_medicine(record)}, upsert=True)
        )

    if operations:
        db.medicines.bulk_write(operations, ordered=False)

    reports = 0

    for record in read_sheet(workbook, "Reports"):
        created = datetime.strptime(str(record["created_at"]), "%Y-%m-%d").replace(tzinfo=timezone.utc)

        db.reports.update_one(
            {"user_email": record["user_email"], "code": str(record["code"])},
            {"$set": {"reason": record["reason"], "created_at": created}},
            upsert=True,
        )
        reports += 1

    print(f"Loaded {len(operations)} medicines and {reports} reports from the sample workbook.")
    print("Try these codes on the Verify page, or scan sample_data/qr_codes/*.png")


if __name__ == "__main__":
    main()
