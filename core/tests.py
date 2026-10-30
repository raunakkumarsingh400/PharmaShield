import unittest

try:
    from django.test import SimpleTestCase as BaseTestCase
except ImportError:  # allow running with plain unittest too
    BaseTestCase = unittest.TestCase

import json
import os
from unittest import mock

from . import adminstats, mailer, qrcode_svg
from . import safety as s
from .qr import extract_code


def imap(**kwargs):
    """imap(warfarin=["Warf 5"], ibuprofen=["Brufen"]) -> ingredient map."""
    return {k.replace("_", " "): v for k, v in kwargs.items()}


class NormalizeTests(BaseTestCase):
    def test_salt_and_strength_removed(self):
        self.assertEqual(s.normalize_ingredient("Amlodipine Besylate (5mg)"), "amlodipine")
        self.assertEqual(s.normalize_ingredient("Atorvastatin Calcium"), "atorvastatin")
        self.assertEqual(s.normalize_ingredient("Metformin Hydrochloride"), "metformin")

    def test_synonyms(self):
        self.assertEqual(s.normalize_ingredient("Acetaminophen"), "paracetamol")
        self.assertEqual(s.normalize_ingredient("Amoxicillin Trihydrate"), "amoxycillin")
        self.assertEqual(s.normalize_ingredient("Potassium Clavulanate"), "clavulanic acid")
        self.assertEqual(s.normalize_ingredient("Sodium Valproate"), "valproate")

    def test_protected_names_survive(self):
        self.assertEqual(s.normalize_ingredient("Potassium Chloride"), "potassium chloride")
        self.assertEqual(s.normalize_ingredient("Calcium Carbonate"), "calcium carbonate")
        self.assertEqual(s.normalize_ingredient("Ferrous Ascorbate"), "iron")
        self.assertEqual(s.normalize_ingredient("Calcium Citrate"), "calcium")

    def test_real_dataset_variants(self):
        cases = {
            "Cefpodoxime Proxetil": "cefpodoxime",
            "Escitalopram Oxalate": "escitalopram",
            "Olmesartan Medoxomil": "olmesartan",
            "Perindopril Erbumine": "perindopril",
            "S Amlodipine": "amlodipine",
            "Paracetamol Acetaminophen": "paracetamol",
            "Penicillin G": "penicillin",
            "Cefuroxime Axetil": "cefuroxime",
        }
        for raw, expected in cases.items():
            self.assertEqual(s.normalize_ingredient(raw), expected, raw)

    def test_empty(self):
        self.assertEqual(s.normalize_ingredient(None), "")
        self.assertEqual(s.normalize_ingredient("  "), "")


class ParseTests(BaseTestCase):
    CSV_VALUE = (
        "[{'name': 'Amoxycillin', 'strength': '500mg', 'full_description': 'x'}, "
        "{'name': 'Clavulanic Acid', 'strength': '125mg', 'full_description': 'y'}]"
    )

    def test_csv_list_string(self):
        details = s.parse_ingredient_details(self.CSV_VALUE)
        self.assertEqual(
            details,
            [
                {"name": "Amoxycillin", "strength": "500mg"},
                {"name": "Clavulanic Acid", "strength": "125mg"},
            ],
        )

    def test_plain_text_fallback(self):
        self.assertEqual(
            s.parse_active_ingredients("Paracetamol, Caffeine"),
            ["Paracetamol", "Caffeine"],
        )

    def test_plain_text_with_strengths(self):
        self.assertEqual(
            s.parse_ingredient_details("Amoxycillin (500mg); Clavulanic Acid (125mg)"),
            [
                {"name": "Amoxycillin", "strength": "500mg"},
                {"name": "Clavulanic Acid", "strength": "125mg"},
            ],
        )

    def test_enrich(self):
        doc = s.enrich_medicine(
            {
                "brand_name": "Augmentin 625 Duo Tablet",
                "active_ingredients": self.CSV_VALUE,
                "price_inr": "223.42",
                "is_discontinued": "FALSE",
                "num_active_ingredients": "2",
            }
        )
        self.assertEqual(doc["ingredient_keys"], ["amoxycillin", "clavulanic acid"])
        self.assertEqual(doc["ingredient_key"], "amoxycillin+clavulanic acid")
        self.assertEqual(doc["brand_lower"], "augmentin 625 duo tablet")
        self.assertEqual(doc["price_inr"], 223.42)
        self.assertIs(doc["is_discontinued"], False)
        self.assertEqual(doc["num_active_ingredients"], 2)
        # idempotent
        self.assertEqual(s.enrich_medicine(doc)["ingredient_keys"], doc["ingredient_keys"])


class InteractionTests(BaseTestCase):
    def test_warfarin_nsaid_major(self):
        result = s.find_interactions(imap(warfarin=["Warf"], ibuprofen=["Brufen"]))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["severity"], "major")

    def test_sildenafil_nitrate_major(self):
        result = s.find_interactions(
            imap(sildenafil=["Viagra"], isosorbide_mononitrate=["Monotrate"])
        )
        self.assertEqual(result[0]["severity"], "major")

    def test_two_nsaids_flagged_as_duplicate_class(self):
        result = s.find_interactions(imap(ibuprofen=["Brufen"], diclofenac=["Voveran"]))
        self.assertEqual(len(result), 1)
        self.assertIn("same class", result[0]["description"])

    def test_fixed_dose_combo_not_self_flagged(self):
        # ibuprofen + paracetamol in ONE product must not trigger anything
        result = s.find_interactions(
            imap(ibuprofen=["Combiflam"], paracetamol=["Combiflam"])
        )
        self.assertEqual(result, [])

    def test_unrelated_drugs_clean(self):
        self.assertEqual(
            s.find_interactions(imap(paracetamol=["Dolo"], cetirizine=["Cetzine"])),
            [],
        )

    def test_sorted_major_first(self):
        result = s.find_interactions(
            imap(
                warfarin=["W"], ibuprofen=["I"], paracetamol=["P"],
            )
        )
        self.assertEqual(result[0]["severity"], "major")

    def test_rules_reference_real_tokens(self):
        for a, b, severity, _ in s.RULES:
            self.assertIn(severity, s.SEVERITY_ORDER)
            for token in (a, b):
                if token.startswith("g:"):
                    self.assertIn(token[2:], s.GROUPS)


class DoseTests(BaseTestCase):
    def test_frequency_parsing(self):
        cases = {
            "twice a day": 2, "TDS": 3, "1-0-1": 2, "1-1-1": 3, "every 8 hours": 3,
            "once daily": 1, "4 times a day": 4, "BD": 2, "as needed": None, "": None,
        }
        for text, expected in cases.items():
            self.assertEqual(s.parse_frequency(text), expected, text)

    def test_strength_to_mg(self):
        self.assertEqual(s.strength_to_mg("500mg"), 500)
        self.assertEqual(s.strength_to_mg("1 g"), 1000)
        self.assertEqual(s.strength_to_mg("500mcg"), 0.5)
        self.assertIsNone(s.strength_to_mg("125mg/5ml"))

    def test_paracetamol_overdose_across_two_products(self):
        meds = [
            {
                "medicine_name": "Dolo 650",
                "ingredient_details": [{"name": "Paracetamol", "strength": "650mg"}],
                "dose": "1 tablet",
                "frequency": "four times a day",
            },
            {
                "medicine_name": "Crocin 500",
                "ingredient_details": [{"name": "Paracetamol", "strength": "500mg"}],
                "dose": "2 tablets",
                "frequency": "twice a day",
            },
        ]
        alerts = s.find_dose_alerts(meds)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["daily_mg"], 650 * 4 + 500 * 2 * 2)
        self.assertEqual(alerts[0]["limit_mg"], 4000)

    def test_safe_dose_no_alert(self):
        meds = [
            {
                "medicine_name": "Dolo 650",
                "ingredient_details": [{"name": "Paracetamol", "strength": "650mg"}],
                "dose": "1 tablet",
                "frequency": "TDS",
            }
        ]
        self.assertEqual(s.find_dose_alerts(meds), [])

    def test_no_frequency_means_no_estimate(self):
        meds = [
            {
                "medicine_name": "Dolo 650",
                "ingredient_details": [{"name": "Paracetamol", "strength": "650mg"}],
            }
        ]
        self.assertEqual(s.find_dose_alerts(meds), [])


class AllergyTests(BaseTestCase):
    def test_penicillin_class(self):
        alerts = s.find_allergy_alerts(
            ["Penicillin"], imap(amoxycillin=["Augmentin"], clavulanic_acid=["Augmentin"])
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["ingredient"], "amoxycillin")

    def test_direct_ingredient_and_alias(self):
        self.assertEqual(
            len(s.find_allergy_alerts(["Acetaminophen"], imap(paracetamol=["Dolo"]))), 1
        )
        self.assertEqual(
            len(s.find_allergy_alerts(["NSAIDs"], imap(ibuprofen=["Brufen"]))), 1
        )

    def test_no_false_positive(self):
        self.assertEqual(
            s.find_allergy_alerts(["Penicillin"], imap(paracetamol=["Dolo"])), []
        )

    def test_parse_allergies(self):
        self.assertEqual(
            s.parse_allergies("Penicillin, sulfa; Penicillin\nAspirin"),
            ["Penicillin", "sulfa", "Aspirin"],
        )


class QrTests(BaseTestCase):
    def test_plain_code(self):
        self.assertEqual(extract_code("  PSQR-482913\n"), "PSQR-482913")

    def test_url_with_query(self):
        self.assertEqual(extract_code("https://pharma.in/v?code=PSQR-482913&x=1"), "PSQR-482913")

    def test_url_with_path(self):
        self.assertEqual(extract_code("https://pharma.in/verify/PSQR-482913/"), "PSQR-482913")

    def test_empty(self):
        self.assertEqual(extract_code(None), "")
        self.assertEqual(extract_code("   "), "")


class PasswordTests(BaseTestCase):
    def test_policy(self):
        self.assertIsNotNone(s.validate_password("short"))
        self.assertIsNotNone(s.validate_password("12345678"))
        self.assertIsNotNone(s.validate_password("Password1"))
        self.assertIsNone(s.validate_password("c0rrect-horse"))


class FakeResponse:
    def __init__(self, status=201):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class MailerTests(BaseTestCase):
    def test_brevo_request_shape(self):
        req = mailer.build_brevo_request(
            "KEY123", "sender@example.com", "PharmaShield",
            "user@example.com", "Subject", "Body text",
        )
        self.assertEqual(req.full_url, "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Api-key"), "KEY123")
        payload = json.loads(req.data)
        self.assertEqual(payload["sender"]["email"], "sender@example.com")
        self.assertEqual(payload["to"], [{"email": "user@example.com"}])
        self.assertEqual(payload["textContent"], "Body text")

    def test_brevo_success_and_failure_status(self):
        sent = []

        def ok(request, timeout):
            sent.append(request)
            return FakeResponse(201)

        mailer.send_via_brevo("k", "a@b.com", "n", "c@d.com", "s", "b", opener=ok)
        self.assertEqual(len(sent), 1)

        with self.assertRaises(RuntimeError):
            mailer.send_via_brevo(
                "k", "a@b.com", "n", "c@d.com", "s", "b",
                opener=lambda request, timeout: FakeResponse(500),
            )

    def test_provider_selection(self):
        clean = {k: "" for k in ("BREVO_API_KEY", "EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD")}

        with mock.patch.dict(os.environ, clean):
            self.assertEqual(mailer.provider(), "console")

        with mock.patch.dict(os.environ, {**clean, "EMAIL_HOST_USER": "a@gmail.com",
                                          "EMAIL_HOST_PASSWORD": "x"}):
            self.assertEqual(mailer.provider(), "smtp")

        with mock.patch.dict(os.environ, {**clean, "EMAIL_HOST_USER": "a@gmail.com",
                                          "EMAIL_HOST_PASSWORD": "x", "BREVO_API_KEY": "k"}):
            self.assertEqual(mailer.provider(), "brevo")

    def test_describe_config_reports_missing_values(self):
        clean = {"EMAIL_HOST_USER": "", "EMAIL_HOST_PASSWORD": ""}

        with mock.patch.dict(os.environ, clean):
            text = mailer.describe_config()
            self.assertIn("EMAIL_HOST_USER: MISSING", text)
            self.assertIn("EMAIL_HOST_PASSWORD: MISSING", text)

        with mock.patch.dict(os.environ, {"EMAIL_HOST_USER": "a@gmail.com",
                                          "EMAIL_HOST_PASSWORD": "abcd efgh ijkl mnop"}):
            text = mailer.describe_config()
            self.assertIn("set (a@gmail.com)", text)
            self.assertIn("16 chars", text)

    def test_brevo_without_sender_returns_false_not_crash(self):
        env = {"BREVO_API_KEY": "k", "EMAIL_FROM_ADDRESS": ""}

        with mock.patch.dict(os.environ, env):
            self.assertFalse(mailer.send_email("u@example.com", "s", "b"))


class EnvFileTests(BaseTestCase):
    def test_load_env(self):
        import tempfile
        from pharmashield.envfile import load_env

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write('# comment\nPS_TEST_A=hello\nPS_TEST_B="quoted value"\n'
                        'PS_TEST_C = spaced\nPS_TEST_KEEP=from_file\n\nbad line\n')

            with mock.patch.dict(os.environ, {"PS_TEST_KEEP": "from_terminal"}):
                self.assertTrue(load_env(path))
                self.assertEqual(os.environ["PS_TEST_A"], "hello")
                self.assertEqual(os.environ["PS_TEST_B"], "quoted value")
                self.assertEqual(os.environ["PS_TEST_C"], "spaced")
                self.assertEqual(os.environ["PS_TEST_KEEP"], "from_terminal")

            for key in ("PS_TEST_A", "PS_TEST_B", "PS_TEST_C"):
                os.environ.pop(key, None)

    def test_missing_file(self):
        from pharmashield.envfile import load_env
        self.assertFalse(load_env("/nonexistent/.env"))

    def test_empty_terminal_variable_does_not_block_file(self):
        import tempfile
        from pharmashield.envfile import load_env

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("PS_TEST_EMPTY=from_file\n")

            with mock.patch.dict(os.environ, {"PS_TEST_EMPTY": ""}):
                load_env(path)
                self.assertEqual(os.environ["PS_TEST_EMPTY"], "from_file")


class AdminStatsTests(BaseTestCase):
    def test_fill_days_zero_fills_and_scales(self):
        from datetime import date

        rows = adminstats.fill_days(
            {"2026-10-15": 10, "2026-10-13": 5}, 4, date(2026, 10, 15)
        )
        self.assertEqual([r["date"] for r in rows],
                         ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"])
        self.assertEqual([r["n"] for r in rows], [0, 5, 0, 10])
        self.assertEqual([r["pct"] for r in rows], [0, 50, 0, 100])
        self.assertEqual(rows[3]["label"], "15 Oct")

    def test_fill_days_all_zero(self):
        from datetime import date

        rows = adminstats.fill_days({}, 3, date(2026, 10, 15))
        self.assertEqual([r["pct"] for r in rows], [0, 0, 0])

    def test_result_breakdown(self):
        rows = adminstats.result_breakdown({"VERIFIED_RECORD": 3, "EXPIRED": 1})
        by = {r["key"]: r for r in rows}
        self.assertEqual(by["VERIFIED_RECORD"]["pct"], 75)
        self.assertEqual(by["EXPIRED"]["pct"], 25)
        self.assertEqual(by["NO_RECORD"]["n"], 0)
        self.assertEqual(adminstats.result_breakdown({})[0]["pct"], 0)

    def test_qr_payload_priority(self):
        self.assertEqual(adminstats.qr_payload(
            {"qr_code": "Q1", "verification_code": "V1", "batch_number": "B1"}), "Q1")
        self.assertEqual(adminstats.qr_payload(
            {"qr_code": "", "verification_code": "V1", "batch_number": "B1"}), "V1")
        self.assertEqual(adminstats.qr_payload({"batch_number": "B1"}), "B1")
        self.assertEqual(adminstats.qr_payload({}), "")


class QrCodeTests(BaseTestCase):
    def test_svg_structure(self):
        svg = qrcode_svg.make_svg("PSQR-100001")
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("<rect", svg)

    def test_matrix_size_and_finder_patterns(self):
        m = qrcode_svg.make_matrix("PSQR-100001")
        self.assertEqual(len(m), 21)                      # version 1
        for r0, c0 in ((0, 0), (0, 14), (14, 0)):
            self.assertTrue(m[r0][c0] and m[r0 + 6][c0 + 6])
            self.assertFalse(m[r0 + 1][c0 + 1])

    def test_too_long(self):
        with self.assertRaises(ValueError):
            qrcode_svg.make_matrix("x" * (qrcode_svg.MAX_BYTES + 1))

    def test_decodes_with_independent_reader(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("OpenCV not installed")

        detector = cv2.QRCodeDetector()
        aruco = cv2.QRCodeDetectorAruco()

        for text in ("PSQR-100001", "PSV-ABCD2345", "MI26D937", "x" * 62, "y" * 106):
            m = qrcode_svg.make_matrix(text)
            n, scale, border = len(m), 10, 4
            img = np.full(((n + 2 * border) * scale,) * 2, 255, np.uint8)
            for r in range(n):
                for c in range(n):
                    if m[r][c]:
                        img[(r + border) * scale:(r + border + 1) * scale,
                            (c + border) * scale:(c + border + 1) * scale] = 0
            out = detector.detectAndDecode(img)[0] or aruco.detectAndDecode(img)[0]
            self.assertEqual(out, text)


class PdfReportTests(BaseTestCase):
    def test_pdf_is_generated(self):
        try:
            from .pdfreport import build_safety_pdf
        except ImportError:
            self.skipTest("reportlab not installed")

        safety = {
            "status": "DANGER",
            "medicines": [{"medicine_name": "Dolo 650 Tablet &amp; <b>x</b>", "active_ingredients": ["Paracetamol"],
                           "dose": "1", "frequency": "TDS"}],
            "allergy_alerts": [{"allergy": "Penicillin", "ingredient": "amoxycillin", "medicines": ["Augmentin"]}],
            "dose_alerts": [{"ingredient": "paracetamol", "daily_mg": 4600, "limit_mg": 4000, "medicines": ["Dolo"]}],
            "duplicate_ingredients": [{"ingredient": "paracetamol", "medicines": ["A", "B"]}],
            "interactions": [{"drug_a": "warfarin", "drug_b": "ibuprofen", "severity": "major",
                              "description": "Bleeding \u2194 risk", "medicines": ["W", "I"]}],
            "unrecognised": ["Unknown \u0939\u093f\u0902\u0926\u0940"],
            "allergy_count": 1, "dose_count": 1, "duplicate_count": 1, "interaction_count": 1,
        }
        pdf = build_safety_pdf({"name": "Test \u0939", "user_id": "PS1"}, safety)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 1500)

        empty = dict(safety, status="NO_KNOWN_ISSUES", medicines=[], allergy_alerts=[],
                     dose_alerts=[], duplicate_ingredients=[], interactions=[], unrecognised=[],
                     allergy_count=0, dose_count=0, duplicate_count=0, interaction_count=0)
        self.assertTrue(build_safety_pdf({"name": "T", "user_id": "P"}, empty).startswith(b"%PDF"))


class FakeSessions:
    """Tiny in-memory stand-in for the MongoDB `sessions` collection."""

    def __init__(self):
        self.docs = {}

    @staticmethod
    def _live(doc, flt):
        limit = flt.get("expire_at", {})
        if "$gt" in limit and not doc["expire_at"] > limit["$gt"]:
            return False
        if "$lte" in limit and not doc["expire_at"] <= limit["$lte"]:
            return False
        return True

    def insert_one(self, doc):
        from pymongo.errors import DuplicateKeyError

        if doc["_id"] in self.docs:
            raise DuplicateKeyError("duplicate")
        self.docs[doc["_id"]] = dict(doc)

    def find_one(self, flt):
        doc = self.docs.get(flt.get("_id"))
        return dict(doc) if doc and self._live(doc, flt) else None

    def count_documents(self, flt, limit=0):
        return 1 if self.find_one(flt) else 0

    def update_one(self, flt, update):
        doc = self.docs.get(flt["_id"])
        if doc:
            doc.update(update["$set"])
        return mock.Mock(matched_count=1 if doc else 0)

    def delete_one(self, flt):
        self.docs.pop(flt["_id"], None)

    def delete_many(self, flt):
        for key in [k for k, d in self.docs.items() if self._live(d, flt)]:
            del self.docs[key]


class MongoSessionTests(BaseTestCase):
    def setUp(self):
        try:
            from . import mongo_sessions
            from django.contrib.sessions.backends.base import UpdateError
        except ImportError:
            self.skipTest("Django / pymongo not installed")

        self.ms = mongo_sessions
        self.UpdateError = UpdateError
        self.fake = FakeSessions()
        patcher = mock.patch.object(mongo_sessions, "_collection", lambda: self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_save_and_load_in_new_object(self):
        store = self.ms.SessionStore()
        store["user"] = {"name": "Raunak", "role": "admin"}
        store.create()
        store.save()

        again = self.ms.SessionStore(store.session_key)
        self.assertEqual(again["user"]["name"], "Raunak")
        self.assertIn(store.session_key, self.fake.docs)

    def test_expired_session_is_not_loaded(self):
        from datetime import datetime, timedelta, timezone

        store = self.ms.SessionStore()
        store["a"] = 1
        store.create()
        self.fake.docs[store.session_key]["expire_at"] = datetime.now(timezone.utc) - timedelta(seconds=5)

        again = self.ms.SessionStore(store.session_key)
        self.assertEqual(dict(again.items()), {})
        self.assertFalse(again.exists(store.session_key))

    def test_delete_and_flush(self):
        store = self.ms.SessionStore()
        store["a"] = 1
        store.create()
        key = store.session_key
        store.flush()
        self.assertNotIn(key, self.fake.docs)

    def test_cycle_key_keeps_data_and_removes_old_key(self):
        store = self.ms.SessionStore()
        store["a"] = 1
        store.create()
        old = store.session_key
        store.cycle_key()
        self.assertNotEqual(old, store.session_key)
        self.assertNotIn(old, self.fake.docs)
        self.assertEqual(self.ms.SessionStore(store.session_key)["a"], 1)

    def test_saving_a_deleted_session_raises_update_error(self):
        store = self.ms.SessionStore()
        store["a"] = 1
        store.create()
        self.fake.docs.clear()
        store["b"] = 2
        with self.assertRaises(self.UpdateError):
            store.save()

    def test_clear_expired(self):
        from datetime import datetime, timedelta, timezone

        store = self.ms.SessionStore()
        store["a"] = 1
        store.create()
        self.fake.docs[store.session_key]["expire_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.ms.SessionStore.clear_expired()
        self.assertEqual(self.fake.docs, {})


class TemplateSafetyTests(BaseTestCase):
    def test_no_template_reads_underscore_attributes(self):
        """Django refuses names like {{ medicine._id }} and the page crashes."""
        import re
        from pathlib import Path

        folder = Path(__file__).resolve().parent.parent / "templates"
        bad = []

        for path in folder.glob("*.html"):
            text = path.read_text(encoding="utf-8")

            for chunk in re.findall(r"{{.*?}}|{%.*?%}", text, flags=re.S):
                if re.search(r"[\w\]]\._\w", chunk) or re.search(r"[\s(|:]_\w", chunk):
                    bad.append(f"{path.name}: {chunk.strip()}")

        self.assertEqual(bad, [])

    def test_every_page_is_mobile_ready(self):
        """Each full HTML page needs a viewport tag, otherwise phones show a tiny zoomed-out desktop page."""
        from pathlib import Path

        folder = Path(__file__).resolve().parent.parent / "templates"
        missing = []

        for path in folder.glob("*.html"):
            text = path.read_text(encoding="utf-8")

            if "<html" in text and "width=device-width" not in text:
                missing.append(path.name)

        self.assertEqual(missing, [])

    def test_base_has_mobile_menu_and_css_is_valid(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        base = (root / "templates" / "base.html").read_text(encoding="utf-8")

        for needle in ('id="menuBtn"', 'id="navOverlay"', 'id="sidebar"', "viewport-fit=cover"):
            self.assertIn(needle, base)

        css = (root / "static" / "css" / "style.css").read_text(encoding="utf-8")
        self.assertEqual(css.count("{"), css.count("}"))
        self.assertIn("@media (max-width: 900px)", css)

    def test_with_string_id(self):
        class FakeId:
            def __str__(self):
                return "abc123"

        doc = adminstats.with_string_id({"_id": FakeId(), "name": "x"})
        self.assertEqual(doc["id"], "abc123")
        self.assertEqual(doc["name"], "x")
        self.assertEqual(adminstats.with_string_id({"a": 1}), {"a": 1})


if __name__ == "__main__":
    unittest.main()
