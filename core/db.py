import hashlib
import hmac
import os
import random
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, MongoClient

from . import adminstats, safety
from .qr import extract_code
from .safety import (
    enrich_medicine,
    normalize_ingredient,
    parse_active_ingredients,
    validate_password,
)


# =========================================================
# CONFIGURATION
# =========================================================

MONGO_URI = os.getenv("PHARMASHIELD_MONGO_URI", "mongodb://127.0.0.1:27017")
DB_NAME = os.getenv("PHARMASHIELD_DB", "pharmashield")

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
RESET_TOKEN_MINUTES = 30
REPORT_FLAG_THRESHOLD = 3
RECONNECT_SECONDS = 10
IST = timezone(timedelta(hours=5, minutes=30))


# =========================================================
# HELPERS
# =========================================================


def now():
    return datetime.now(timezone.utc)


def aware(value):
    """MongoDB returns naive datetimes; treat them as UTC."""
    if value and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value


def hash_password(password):
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260000)

    return f"{salt.hex()}:{key.hex()}:260000"


def verify_password(password, stored):
    try:
        parts = stored.split(":")
        salt, key = parts[0], parts[1]
        iterations = int(parts[2]) if len(parts) > 2 else 120000  # legacy hashes

        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), iterations
        )

        return hmac.compare_digest(actual, bytes.fromhex(key))

    except Exception:
        return False


def sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


def parse_date(value):
    """Parse the date formats seen on medicine packs. Returns a date or None."""
    text = str(value or "").strip()

    if not text:
        return None

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%Y", "%Y-%m", "%b %Y", "%B %Y"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue

        if fmt in ("%m/%Y", "%Y-%m", "%b %Y", "%B %Y"):
            # month-only expiry means the LAST day of that month
            year = parsed.year + (parsed.month // 12)
            month = parsed.month % 12 + 1
            return (datetime(year, month, 1) - timedelta(days=1)).date()

        return parsed.date()

    return None


# Kept for backwards compatibility with any external imports
ingredient_names_normalize = normalize_ingredient


def ingredient_names(medicine):
    """Canonical ingredient keys of a medicine (works for old and new documents)."""
    keys = list(medicine.get("ingredient_keys") or [])

    if not keys:
        for name in parse_active_ingredients(medicine.get("active_ingredients")):
            key = normalize_ingredient(name)

            if key and key not in keys:
                keys.append(key)

        for field in ("primary_ingredient", "generic_name"):
            key = normalize_ingredient(medicine.get(field, ""))

            if key and key not in keys:
                keys.append(key)

    return keys


def display_ingredients(medicine):
    ingredients = parse_active_ingredients(medicine.get("active_ingredients"))

    if not ingredients and medicine.get("primary_ingredient"):
        ingredients = [medicine.get("primary_ingredient")]

    return ingredients


def build_ingredient_map(medicines):
    """canonical ingredient -> [medicine display names]"""
    mapping = {}

    for medicine in medicines:
        name = (
            medicine.get("medicine_name")
            or medicine.get("brand_name")
            or "Unknown medicine"
        )

        for key in ingredient_names(medicine):
            mapping.setdefault(key, []).append(name)

    return {k: list(dict.fromkeys(v)) for k, v in mapping.items()}


# =========================================================
# DATABASE CLASS
# =========================================================


class Database:
    def __init__(self):
        self.client = None
        self.db = None
        self._online = False
        self._last_attempt = 0.0
        self.error = ""

    # -----------------------------------------------------
    # connection (auto-reconnects if MongoDB was down at start)
    # -----------------------------------------------------

    @property
    def online(self):
        if not self._online and time.time() - self._last_attempt > RECONNECT_SECONDS:
            self.connect()

        return self._online

    def connect(self):
        self._last_attempt = time.time()

        try:
            self.client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
            self.client.admin.command("ping")
            self.db = self.client[DB_NAME]
            self._create_indexes()
            self._online = True
            self.error = ""

        except Exception as exc:
            self._online = False
            self.error = str(exc)

    def _create_indexes(self):
        d = self.db

        d.users.create_index("email", unique=True)
        d.users.create_index("user_id", unique=True)

        d.medicines.create_index([("brand_lower", ASCENDING)])
        d.medicines.create_index([("ingredient_keys", ASCENDING)])
        d.medicines.create_index([("product_id", ASCENDING)])
        d.medicines.create_index(
            [("ingredient_key", ASCENDING), ("strength_key", ASCENDING)]
        )
        d.medicines.create_index(
            [("medicine_name", ASCENDING), ("batch_number", ASCENDING)]
        )
        for field in ("verification_code", "batch_number", "barcode", "qr_code"):
            d.medicines.create_index([(field, ASCENDING)], sparse=True)

        d.user_medicines.create_index([("user_email", ASCENDING)])
        d.scans.create_index([("user_email", ASCENDING), ("created_at", DESCENDING)])
        d.reports.create_index([("code", ASCENDING)])
        d.profiles.create_index("user_email", unique=True)
        d.drug_interactions.create_index([("drug_a", ASCENDING), ("drug_b", ASCENDING)])
        d.password_resets.create_index("token_hash", unique=True)
        d.password_resets.create_index("expires_at", expireAfterSeconds=0)
        d.sessions.create_index("expire_at", expireAfterSeconds=0)

    # =====================================================
    # USERS
    # =====================================================

    def add_user(self, name, email, mobile, password):
        if not self.online:
            return False, "MongoDB is offline.", None

        name = name.strip()
        email = email.strip().lower()
        mobile = mobile.strip()

        if not name or not email or not mobile or not password:
            return False, "All fields are required.", None

        problem = validate_password(password)

        if problem:
            return False, problem, None

        try:
            if self.db.users.find_one({"email": email}):
                return False, "An account with this email already exists.", None

            while True:
                user_id = f"PS{random.randint(100000, 999999)}"

                if not self.db.users.find_one({"user_id": user_id}):
                    break

            self.db.users.insert_one(
                {
                    "user_id": user_id,
                    "name": name,
                    "email": email,
                    "mobile": mobile,
                    "password_hash": hash_password(password),
                    "role": "user",
                    "created_at": now(),
                }
            )

            return True, "Account created successfully.", user_id

        except Exception as exc:
            return False, str(exc), None

    def create_admin(self, email, password, name="PharmaShield Admin"):
        """Create/update the ADMIN account. Called from create_admin.py only."""
        if not self.online:
            return False, "MongoDB is offline."

        problem = validate_password(password)

        if problem:
            return False, problem

        self.db.users.update_one(
            {"user_id": "ADMIN"},
            {
                "$set": {
                    "name": name,
                    "email": email.strip().lower(),
                    "role": "admin",
                    "password_hash": hash_password(password),
                    "failed_attempts": 0,
                    "locked_until": None,
                },
                "$setOnInsert": {
                    "user_id": "ADMIN",
                    "mobile": "",
                    "created_at": now(),
                },
            },
            upsert=True,
        )

        return True, "Admin account is ready."

    def login(self, user_id, password):
        if not self.online:
            return None, "MongoDB is offline."

        generic = "Invalid PharmaShield ID or password."
        user = self.db.users.find_one({"user_id": user_id.strip().upper()})

        if not user:
            verify_password(password, "00" * 16 + ":" + "00" * 32)  # keep timing similar
            return None, generic

        locked_until = aware(user.get("locked_until"))

        if locked_until and locked_until > now():
            minutes = int((locked_until - now()).total_seconds() // 60) + 1
            return None, f"Too many failed attempts. Try again in {minutes} minute(s)."

        if verify_password(password, user.get("password_hash", "")):
            self.db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"failed_attempts": 0, "locked_until": None}},
            )

            return user, "Login successful."

        attempts = int(user.get("failed_attempts", 0)) + 1
        update = {"failed_attempts": attempts}

        if attempts >= MAX_FAILED_LOGINS:
            update = {
                "failed_attempts": 0,
                "locked_until": now() + timedelta(minutes=LOCK_MINUTES),
            }

        self.db.users.update_one({"_id": user["_id"]}, {"$set": update})

        return None, generic

    # -----------------------------------------------------
    # password reset (single-use, expiring token)
    # -----------------------------------------------------

    def create_reset_token(self, user_id, email):
        """Return (token, user) when ID and email match, else (None, None)."""
        if not self.online:
            return None, None

        user = self.db.users.find_one(
            {"user_id": user_id.strip().upper(), "email": email.strip().lower()}
        )

        if not user:
            return None, None

        token = secrets.token_urlsafe(32)

        self.db.password_resets.delete_many({"user_id": user["user_id"]})
        self.db.password_resets.insert_one(
            {
                "user_id": user["user_id"],
                "token_hash": sha256(token),
                "expires_at": now() + timedelta(minutes=RESET_TOKEN_MINUTES),
                "created_at": now(),
            }
        )

        return token, user

    def reset_token_valid(self, token):
        if not self.online or not token:
            return False

        record = self.db.password_resets.find_one({"token_hash": sha256(token)})

        return bool(record and aware(record["expires_at"]) > now())

    def reset_password_with_token(self, token, new_password):
        if not self.online:
            return False, "MongoDB is offline."

        problem = validate_password(new_password)

        if problem:
            return False, problem

        record = self.db.password_resets.find_one_and_delete(
            {"token_hash": sha256(token or "")}
        )

        if not record or aware(record["expires_at"]) <= now():
            return False, "This reset link is invalid or has expired."

        self.db.users.update_one(
            {"user_id": record["user_id"]},
            {
                "$set": {
                    "password_hash": hash_password(new_password),
                    "password_updated_at": now(),
                    "failed_attempts": 0,
                    "locked_until": None,
                }
            },
        )

        return True, "Password updated successfully."

    # =====================================================
    # VERIFY MEDICINE
    # =====================================================

    VERIFY_FIELDS = [
        "product_id", "medicine_name", "brand_name", "generic_name",
        "primary_ingredient", "primary_strength", "strength", "dosage_form",
        "manufacturer", "batch_number", "manufacturing_date", "expiry_date",
        "mrp", "price_inr", "source", "source_url",
    ]

    def verify(self, code, email):
        if not self.online:
            return None, "MongoDB is offline."

        code = extract_code(code)

        if not code:
            return None, "Enter a QR/barcode/batch code."

        medicine = self._find_by_code(code)

        report_count = self.db.reports.count_documents({"code": code})
        medicine_data = {}
        warnings = []

        if medicine:
            medicine_data = {k: medicine.get(k, "") for k in self.VERIFY_FIELDS}
            medicine_data["active_ingredients"] = ", ".join(
                display_ingredients(medicine)
            )

            expiry = parse_date(medicine.get("expiry_date"))

            if expiry and expiry < now().date():
                result = "EXPIRED"
                message = f"This batch expired on {expiry:%d %b %Y}. Do not use it."
            else:
                result = "VERIFIED_RECORD"
                message = "A matching medicine record was found in the database."

                if expiry and (expiry - now().date()).days <= 60:
                    warnings.append(
                        f"Expires soon ({expiry:%d %b %Y})."
                    )
        else:
            result = "NO_RECORD"
            message = (
                "No matching record was found. This does not by itself "
                "prove that the medicine is counterfeit."
            )

        if report_count >= REPORT_FLAG_THRESHOLD:
            warnings.append(
                f"This code has been reported as suspicious {report_count} times "
                "by other users. Check with the pharmacist or manufacturer before use."
            )

            if result == "VERIFIED_RECORD":
                result = "REPORTED"
                message = "A record exists, but this code has been flagged by users."

        elif report_count:
            warnings.append(
                f"This code has {report_count} suspicious-medicine report(s)."
            )

        self.db.scans.insert_one(
            {
                "user_email": email,
                "input_code": code,
                "result": result,
                "medicine": medicine_data,
                "created_at": now(),
            }
        )

        return {
            "result": result,
            "message": message,
            "warnings": warnings,
            "report_count": report_count,
            "medicine": medicine_data,
        }, None

    def scans(self, email):
        if not self.online:
            return []

        return list(
            self.db.scans.find({"user_email": email})
            .sort("created_at", -1)
            .limit(50)
        )

    # =====================================================
    # ADMIN: ADD / UPDATE MEDICINE
    # =====================================================

    def add_medicine(self, data):
        if not self.online:
            return False, "MongoDB is offline."

        data = {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()}

        if not data.get("medicine_name") or not data.get("manufacturer") or not data.get(
            "batch_number"
        ):
            return (
                False,
                "Medicine name, manufacturer and batch number are required.",
            )

        if not data.get("brand_name"):
            data["brand_name"] = data["medicine_name"]

        if not data.get("primary_ingredient"):
            data["primary_ingredient"] = data.get("generic_name", "")

        data = enrich_medicine(data)

        self.db.medicines.update_one(
            {"batch_number": data["batch_number"]},
            {"$set": {**data, "updated_at": now()}},
            upsert=True,
        )

        return True, "Medicine record saved."

    # =====================================================
    # SEARCH
    # =====================================================

    def search_medicines(self, query, limit=20):
        """Prefix search (index-backed) with a 'contains' fallback."""
        if not self.online or not query:
            return []

        query = query.strip().lower()[:80]
        ingredient_key = normalize_ingredient(query)
        prefix = "^" + re.escape(query)

        results = list(
            self.db.medicines.find(
                {
                    "$or": [
                        {"brand_lower": {"$regex": prefix}},
                        {"ingredient_keys": ingredient_key},
                        {"ingredient_keys": {"$regex": "^" + re.escape(ingredient_key)}},
                    ]
                }
            ).limit(limit)
        )

        if len(results) < 5:
            seen = {r["_id"] for r in results}
            extra = self.db.medicines.find(
                {"brand_lower": {"$regex": re.escape(query)}}
            ).limit(limit)

            for item in extra:
                if item["_id"] not in seen and len(results) < limit:
                    results.append(item)

        return results

    def get_medicine_by_id(self, product_id):
        if not self.online or not product_id:
            return None

        return self.db.medicines.find_one({"product_id": str(product_id).strip()})

    def get_medicine_by_name(self, brand_name):
        if not self.online or not brand_name:
            return None

        name = brand_name.strip()

        return self.db.medicines.find_one(
            {
                "$or": [
                    {"brand_lower": name.lower()},
                    {"brand_name": name},
                    {"medicine_name": name},
                ]
            }
        )

    def find_alternatives(self, product_id, limit=15):
        """Same ingredients + same strengths, cheaper first."""
        medicine = self.get_medicine_by_id(product_id)

        if not medicine or not medicine.get("ingredient_key"):
            return None, []

        query = {
            "ingredient_key": medicine["ingredient_key"],
            "strength_key": medicine.get("strength_key", ""),
            "product_id": {"$ne": medicine.get("product_id")},
            "is_discontinued": {"$ne": True},
            "price_inr": {"$ne": None},
        }

        if medicine.get("dosage_form"):
            query["dosage_form"] = medicine["dosage_form"]

        alternatives = list(
            self.db.medicines.find(query).sort("price_inr", ASCENDING).limit(limit)
        )

        return medicine, alternatives

    # =====================================================
    # USER MEDICINES
    # =====================================================

    def add_user_medicine(
        self, email, product_id=None, medicine_name="", dose="", frequency="", duration=""
    ):
        if not self.online:
            return False, "MongoDB is offline."

        email = email.strip().lower()
        product_id = (product_id or "").strip()
        medicine_name = medicine_name.strip()

        medicine = None

        if product_id:
            medicine = self.get_medicine_by_id(product_id)

        if medicine is None and medicine_name:
            medicine = self.get_medicine_by_name(medicine_name)

        if medicine:
            medicine = enrich_medicine(medicine)
            display_name = (
                medicine.get("brand_name") or medicine.get("medicine_name") or medicine_name
            )
            stored_product_id = medicine.get("product_id", product_id)
        else:
            display_name = medicine_name
            stored_product_id = product_id
            medicine = enrich_medicine(
                {"brand_name": medicine_name, "active_ingredients": ""}
            ) if medicine_name else {}

        if not display_name:
            return False, "Medicine name is required."

        if self.db.user_medicines.find_one(
            {
                "user_email": email,
                "medicine_name": display_name,
                "product_id": stored_product_id,
            }
        ):
            return False, "This medicine is already in your list."

        found = bool(medicine and medicine.get("ingredient_keys"))

        self.db.user_medicines.insert_one(
            {
                "user_email": email,
                "product_id": stored_product_id,
                "medicine_name": display_name,
                "brand_name": (medicine or {}).get("brand_name", display_name),
                "dose": dose.strip(),
                "frequency": frequency.strip(),
                "duration": duration.strip(),
                "active_ingredients": medicine.get("active_ingredients", []),
                "ingredient_details": medicine.get("ingredient_details", []),
                "ingredient_keys": medicine.get("ingredient_keys", []),
                "primary_ingredient": medicine.get("primary_ingredient", ""),
                "primary_strength": medicine.get("primary_strength", ""),
                "manufacturer": medicine.get("manufacturer", ""),
                "price_inr": medicine.get("price_inr"),
                "created_at": now(),
            }
        )

        if not found:
            return True, (
                "Added, but this medicine was not found in the database, so "
                "safety checks cannot use its ingredients. Search and add it from the database."
            )

        return True, "Medicine added to your list."

    def get_user_medicines(self, email):
        if not self.online:
            return []

        return [
            adminstats.with_string_id(doc)
            for doc in self.db.user_medicines.find(
                {"user_email": email.strip().lower()}
            ).sort("created_at", -1)
        ]

    def remove_user_medicine(self, email, medicine_id):
        if not self.online:
            return False, "MongoDB is offline."

        try:
            object_id = ObjectId(medicine_id)
        except Exception:
            return False, "Invalid medicine record."

        result = self.db.user_medicines.delete_one(
            {"_id": object_id, "user_email": email.strip().lower()}
        )

        if result.deleted_count:
            return True, "Medicine removed from your list."

        return False, "Medicine record not found."

    # =====================================================
    # PROFILE (allergies)
    # =====================================================

    def get_allergies(self, email):
        if not self.online:
            return []

        profile = self.db.profiles.find_one({"user_email": email.strip().lower()})

        return (profile or {}).get("allergies", [])

    def save_allergies(self, email, text):
        if not self.online:
            return False, "MongoDB is offline."

        allergies = safety.parse_allergies(text)

        self.db.profiles.update_one(
            {"user_email": email.strip().lower()},
            {"$set": {"allergies": allergies, "updated_at": now()}},
            upsert=True,
        )

        return True, "Allergy list saved."

    # =====================================================
    # SAFETY ENGINE
    # =====================================================

    def check_duplicate_ingredients(self, email, medicines=None):
        medicines = medicines if medicines is not None else self.get_user_medicines(email)
        ingredient_map = build_ingredient_map(medicines)

        return [
            {"ingredient": ingredient, "medicines": names}
            for ingredient, names in ingredient_map.items()
            if len(names) > 1
        ]

    def check_interactions(self, ingredient_map):
        """Built-in rules + any extra pairs stored in `drug_interactions`."""
        interactions = safety.find_interactions(ingredient_map)

        if not self.online or len(ingredient_map) < 2:
            return interactions

        seen = {frozenset((i["drug_a"], i["drug_b"])) for i in interactions}
        names = list(ingredient_map)

        for pair in self.db.drug_interactions.find(
            {"drug_a": {"$in": names}, "drug_b": {"$in": names}}
        ):
            key = frozenset((pair["drug_a"], pair["drug_b"]))

            if key in seen or pair["drug_a"] == pair["drug_b"]:
                continue

            seen.add(key)
            interactions.append(
                {
                    "drug_a": pair["drug_a"],
                    "drug_b": pair["drug_b"],
                    "severity": str(pair.get("severity", "")).lower(),
                    "description": pair.get("description", ""),
                    "medicines": list(
                        dict.fromkeys(
                            ingredient_map[pair["drug_a"]] + ingredient_map[pair["drug_b"]]
                        )
                    ),
                    "source": "database",
                }
            )

        return sorted(
            interactions,
            key=lambda x: (safety.SEVERITY_ORDER.get(x["severity"], 9), x["drug_a"]),
        )

    def medication_safety_check(self, email):
        medicines = self.get_user_medicines(email)
        ingredient_map = build_ingredient_map(medicines)

        duplicates = [
            {"ingredient": i, "medicines": names}
            for i, names in ingredient_map.items()
            if len(names) > 1
        ]
        interactions = self.check_interactions(ingredient_map)
        dose_alerts = safety.find_dose_alerts(medicines)
        allergy_alerts = safety.find_allergy_alerts(
            self.get_allergies(email), ingredient_map
        )
        unrecognised = [
            m.get("medicine_name", "")
            for m in medicines
            if not m.get("ingredient_keys") and not ingredient_names(m)
        ]

        major = sum(1 for i in interactions if i["severity"] == "major")

        if allergy_alerts or major or dose_alerts:
            status = "DANGER"
        elif duplicates or interactions:
            status = "ATTENTION_REQUIRED"
        else:
            status = "NO_KNOWN_ISSUES"

        return {
            "medicines": medicines,
            "duplicate_ingredients": duplicates,
            "interactions": interactions,
            "dose_alerts": dose_alerts,
            "allergy_alerts": allergy_alerts,
            "unrecognised": unrecognised,
            "duplicate_count": len(duplicates),
            "interaction_count": len(interactions),
            "major_count": major,
            "dose_count": len(dose_alerts),
            "allergy_count": len(allergy_alerts),
            "status": status,
        }

    # =====================================================
    # ADMIN: DASHBOARD + QR PAGE
    # =====================================================

    def _find_by_code(self, code):
        return self.db.medicines.find_one(
            {
                "$or": [
                    {"verification_code": code},
                    {"batch_number": code},
                    {"barcode": code},
                    {"qr_code": code},
                ]
            }
        )

    def admin_overview(self, days=14):
        """Numbers and chart data for the admin dashboard."""
        if not self.online:
            return None

        d = self.db
        today = datetime.now(IST).date()
        since = now() - timedelta(days=days + 1)

        per_day = {
            row["_id"]: row["n"]
            for row in d.scans.aggregate(
                [
                    {"$match": {"created_at": {"$gte": since}}},
                    {
                        "$group": {
                            "_id": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": "$created_at",
                                    "timezone": "+05:30",
                                }
                            },
                            "n": {"$sum": 1},
                        }
                    },
                ]
            )
        }

        results = {
            (row["_id"] or "UNKNOWN"): row["n"]
            for row in d.scans.aggregate(
                [{"$group": {"_id": "$result", "n": {"$sum": 1}}}]
            )
        }

        top_reports = []

        for row in d.reports.aggregate(
            [
                {"$sort": {"created_at": 1}},  # so $last is the newest reason
                {
                    "$group": {
                        "_id": "$code",
                        "count": {"$sum": 1},
                        "last_reason": {"$last": "$reason"},
                        "last_at": {"$max": "$created_at"},
                    }
                },
                {"$sort": {"count": -1, "last_at": -1}},
                {"$limit": 10},
            ]
        ):
            medicine = self._find_by_code(row["_id"]) if row["_id"] else None
            top_reports.append(
                {
                    "code": row["_id"],
                    "count": row["count"],
                    "flagged": row["count"] >= REPORT_FLAG_THRESHOLD,
                    "reason": row.get("last_reason", ""),
                    "last_at": row.get("last_at"),
                    "medicine": (medicine or {}).get("brand_name")
                    or (medicine or {}).get("medicine_name", ""),
                }
            )

        flagged = next(
            d.reports.aggregate(
                [
                    {"$group": {"_id": "$code", "n": {"$sum": 1}}},
                    {"$match": {"n": {"$gte": REPORT_FLAG_THRESHOLD}}},
                    {"$count": "c"},
                ]
            ),
            {"c": 0},
        )["c"]

        start_of_day = datetime.now(IST).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)

        return {
            "totals": {
                "medicines": d.medicines.estimated_document_count(),
                "users": d.users.count_documents({"role": {"$ne": "admin"}}),
                "scans": d.scans.estimated_document_count(),
                "scans_today": d.scans.count_documents(
                    {"created_at": {"$gte": start_of_day}}
                ),
                "reports": d.reports.estimated_document_count(),
                "flagged": flagged,
            },
            "days": adminstats.fill_days(per_day, days, today),
            "results": adminstats.result_breakdown(results),
            "top_reports": top_reports,
            "recent_reports": list(d.reports.find().sort("created_at", -1).limit(8)),
            "recent_scans": list(d.scans.find().sort("created_at", -1).limit(8)),
            "threshold": REPORT_FLAG_THRESHOLD,
        }

    def list_batches(self, query="", limit=48, skip=0):
        """Medicines that have a batch/QR code, for the QR generator page."""
        if not self.online:
            return [], 0

        query = (query or "").strip()[:80]
        has_code = {"batch_number": {"$exists": True, "$nin": ["", None]}}

        if query:
            rx = {"$regex": re.escape(query.lower())}
            flt = {
                "$and": [
                    has_code,
                    {
                        "$or": [
                            {"brand_lower": rx},
                            {"batch_number": {"$regex": re.escape(query), "$options": "i"}},
                            {"qr_code": {"$regex": re.escape(query), "$options": "i"}},
                            {"verification_code": {"$regex": re.escape(query), "$options": "i"}},
                        ]
                    },
                ]
            }
        else:
            flt = has_code

        total = self.db.medicines.count_documents(flt)
        rows = list(
            self.db.medicines.find(flt)
            .sort("brand_lower", ASCENDING)
            .skip(max(0, skip))
            .limit(limit)
        )

        for row in rows:
            row["qr_payload"] = adminstats.qr_payload(row)

        return rows, total

    # =====================================================
    # REPORTS & STATS
    # =====================================================

    def report(self, email, data):
        if not self.online:
            return False, "MongoDB is offline."

        code = str(data.get("code", "")).strip()
        reason = str(data.get("reason", "")).strip()

        if not code or not reason:
            return False, "Both the code and the reason are required."

        recent = self.db.reports.find_one(
            {
                "user_email": email,
                "code": code,
                "created_at": {"$gt": now() - timedelta(days=1)},
            }
        )

        if recent:
            return False, "You already reported this code in the last 24 hours."

        self.db.reports.insert_one(
            {
                "user_email": email,
                "code": code,
                "reason": reason[:1000],
                "created_at": now(),
            }
        )

        return True, "Report submitted."

    def stats(self, email=None):
        empty = {
            "medicines": 0, "users": 0, "scans": 0, "reports": 0,
            "user_medicines": 0, "duplicate_ingredients": 0, "interactions": 0,
            "dose_alerts": 0, "allergy_alerts": 0,
        }

        if not self.online:
            return empty

        stats = dict(empty)

        for collection in ("medicines", "users", "scans", "reports"):
            stats[collection] = self.db[collection].estimated_document_count()

        if email:
            safety_result = self.medication_safety_check(email)
            stats["user_medicines"] = len(safety_result["medicines"])
            stats["duplicate_ingredients"] = safety_result["duplicate_count"]
            stats["interactions"] = safety_result["interaction_count"]
            stats["dose_alerts"] = safety_result["dose_count"]
            stats["allergy_alerts"] = safety_result["allergy_count"]

        return stats


db = Database()
db.connect()
