"""
PharmaShield safety engine.

Pure Python (no Django / MongoDB imports) so it can be unit-tested on its own.

Provides:
  * ingredient normalisation (salts, spelling variants, brand synonyms)
  * parsing of the CSV ``active_ingredients`` field
  * built-in interaction rules (extendable from the MongoDB collection)
  * therapeutic-duplication checks (e.g. two NSAIDs)
  * estimated daily dose check against typical adult maximums
  * allergy checks (ingredient and drug-class level)

IMPORTANT: this is decision support, not medical advice. The rule set is a
curated starter list of well-known interactions and must be reviewed by a
qualified pharmacist/clinician before real-world use.
"""

import ast
import re


# =========================================================
# NORMALISATION
# =========================================================

# Words that describe the salt/hydrate form, not the active molecule.
SALT_WORDS = {
    "hydrochloride", "hcl", "dihydrochloride", "hydrobromide", "bromide",
    "maleate", "besylate", "besilate", "mesylate", "tartrate", "succinate",
    "sulphate", "sulfate", "phosphate", "acetate", "citrate", "fumarate",
    "carbonate", "dihydrate", "trihydrate", "monohydrate", "hemihydrate",
    "anhydrous", "diethylamine", "tromethamine", "trometamol",
    "oxalate", "medoxomil", "erbumine", "proxetil", "axetil", "cilexetil",
    "ip", "bp", "usp",
}

# Metal counter-ions. Only stripped when something else remains.
METAL_WORDS = {"sodium", "potassium", "calcium", "magnesium"}

# Whole-name mappings (checked BEFORE any stripping).
CANON = {
    "potassium chloride": "potassium chloride",
    "sodium chloride": "sodium chloride",
    "calcium carbonate": "calcium carbonate",
    "magnesium hydroxide": "magnesium hydroxide",
    "aluminium hydroxide": "aluminium hydroxide",
    "aluminum hydroxide": "aluminium hydroxide",
    "sodium bicarbonate": "sodium bicarbonate",
    "ferrous ascorbate": "iron",
    "ferrous sulphate": "iron",
    "ferrous sulfate": "iron",
    "ferrous fumarate": "iron",
    "ferrous gluconate": "iron",
    "carbonyl iron": "iron",
    "elemental iron": "iron",
    "ferric ammonium citrate": "iron",
    "zinc sulphate": "zinc",
    "zinc sulfate": "zinc",
    "zinc gluconate": "zinc",
    "zinc acetate": "zinc",
    "paracetamol acetaminophen": "paracetamol",
    "penicillin g": "penicillin",
    "penicillin v": "penicillin",
    "procaine penicillin g": "penicillin",
    "benzathine penicillin g": "benzathine penicillin",
}

# Alias -> canonical molecule name (Indian/INN spelling used by the CSV).
SYNONYMS = {
    "acetaminophen": "paracetamol",
    "amoxicillin": "amoxycillin",
    "albuterol": "salbutamol",
    "epinephrine": "adrenaline",
    "norepinephrine": "noradrenaline",
    "glyburide": "glibenclamide",
    "frusemide": "furosemide",
    "rifampin": "rifampicin",
    "cefalexin": "cephalexin",
    "acetylsalicylic acid": "aspirin",
    "acetyl salicylic acid": "aspirin",
    "nitroglycerin": "glyceryl trinitrate",
    "gtn": "glyceryl trinitrate",
    "thyroxine": "levothyroxine",
    "valproic acid": "valproate",
    "nicoumalone": "acenocoumarol",
    "clavulanate": "clavulanic acid",
    "ferrous": "iron",
    "ferric": "iron",
    "ciprofloxacin": "ciprofloxacin",
}


def normalize_ingredient(value):
    """Return a canonical, comparison-friendly ingredient name."""
    if value is None:
        return ""

    text = str(value).strip().lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|iu|%)\b", " ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return ""

    if text in CANON:
        return CANON[text]

    if text in SYNONYMS:
        return SYNONYMS[text]

    words = text.split()

    # stereo-isomer prefix, e.g. "S Amlodipine" (levamlodipine), "R Pantoprazole"
    if len(words) > 1 and words[0] in {"s", "r", "d", "l", "dl"}:
        words = words[1:]

    stripped = [w for w in words if w not in SALT_WORDS]
    if stripped:
        words = stripped

    if len(words) > 1:
        no_metal = [w for w in words if w not in METAL_WORDS]
        if no_metal:
            words = no_metal

    result = " ".join(words)

    return CANON.get(result, SYNONYMS.get(result, result))


# =========================================================
# PARSING
# =========================================================


def _safe_literal(text):
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError):
        return None


def parse_ingredient_details(value):
    """
    Parse the CSV's list-like ``active_ingredients`` field.

    Returns a list of ``{"name": str, "strength": str}`` dictionaries.
    """
    if not value:
        return []

    if isinstance(value, dict):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        text = str(value).strip()
        parsed = _safe_literal(text) if text[:1] in "[({" else None

        if isinstance(parsed, (list, tuple)):
            items = list(parsed)
        elif isinstance(parsed, dict):
            items = [parsed]
        else:
            items = [p for p in re.split(r"[,;|]", text.strip("[]")) if p.strip()]

    details = []

    for item in items:
        if isinstance(item, dict):
            name = (
                item.get("name")
                or item.get("ingredient")
                or item.get("active_ingredient")
                or ""
            )
            strength = item.get("strength") or ""
        else:
            name = str(item)
            strength = ""

            # plain text such as "Amoxycillin (500mg)"
            inline = re.match(r"^(.*?)\s*\(([^)]*\d[^)]*)\)\s*$", name.strip())

            if inline and inline.group(1).strip():
                name, strength = inline.group(1), inline.group(2)

        name = str(name).strip().strip("'\"")
        strength = str(strength).strip()

        if name:
            details.append({"name": name, "strength": strength})

    return details


def parse_active_ingredients(value):
    """Return only the ingredient names."""
    return [d["name"] for d in parse_ingredient_details(value)]


def _to_float(value, default=None):
    try:
        return float(str(value).strip().replace(",", ""))
    except (ValueError, TypeError):
        return default


def _to_bool(value):
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def enrich_medicine(doc):
    """
    Add derived, indexed fields to a medicine document.

    Safe to call repeatedly (idempotent) and on CSV rows or admin-entered rows.
    """
    doc = dict(doc)

    raw = doc.get("active_ingredients")

    if isinstance(raw, str) and raw:
        doc["active_ingredients_raw"] = raw

    details = parse_ingredient_details(raw) or parse_ingredient_details(
        doc.get("ingredient_details")
    )

    if not details and doc.get("primary_ingredient"):
        details = [
            {
                "name": str(doc["primary_ingredient"]).strip(),
                "strength": str(doc.get("primary_strength", "")).strip(),
            }
        ]

    keys = []
    strength_parts = []

    for d in details:
        key = normalize_ingredient(d["name"])

        if key and key not in keys:
            keys.append(key)

        strength_parts.append(
            f"{key}:{d['strength'].lower().replace(' ', '')}"
        )

    doc["active_ingredients"] = [d["name"] for d in details]
    doc["ingredient_details"] = details
    doc["ingredient_keys"] = keys
    doc["ingredient_key"] = "+".join(sorted(keys))
    doc["strength_key"] = "+".join(sorted(strength_parts))

    brand = doc.get("brand_name") or doc.get("medicine_name") or ""
    doc["brand_lower"] = str(brand).strip().lower()
    doc["primary_key"] = normalize_ingredient(doc.get("primary_ingredient", ""))

    if "price_inr" in doc and not isinstance(doc["price_inr"], (int, float)):
        doc["price_inr"] = _to_float(doc["price_inr"])

    if "is_discontinued" in doc and not isinstance(doc["is_discontinued"], bool):
        doc["is_discontinued"] = _to_bool(doc["is_discontinued"])

    if "num_active_ingredients" in doc:
        number = _to_float(doc["num_active_ingredients"])
        doc["num_active_ingredients"] = int(number) if number is not None else None

    return doc


# =========================================================
# DRUG GROUPS
# =========================================================

GROUPS = {
    "nsaid": {
        "ibuprofen", "diclofenac", "naproxen", "aceclofenac", "ketorolac",
        "nimesulide", "etoricoxib", "celecoxib", "mefenamic acid",
        "piroxicam", "indomethacin", "etodolac", "lornoxicam",
    },
    "ssri": {
        "fluoxetine", "sertraline", "paroxetine", "escitalopram",
        "citalopram", "fluvoxamine",
    },
    "statin": {"simvastatin", "atorvastatin", "rosuvastatin", "pravastatin"},
    "benzodiazepine": {
        "alprazolam", "clonazepam", "diazepam", "lorazepam", "chlordiazepoxide",
    },
    "opioid": {"tramadol", "codeine", "morphine", "tapentadol"},
    "ppi": {
        "omeprazole", "pantoprazole", "rabeprazole", "esomeprazole",
        "lansoprazole",
    },
    "nitrate": {
        "isosorbide mononitrate", "isosorbide dinitrate", "glyceryl trinitrate",
    },
    "pde5": {"sildenafil", "tadalafil", "vardenafil"},
    "ace_arb": {
        "enalapril", "ramipril", "lisinopril", "perindopril", "telmisartan",
        "losartan", "olmesartan", "valsartan", "irbesartan", "candesartan",
    },
    "ace": {"enalapril", "ramipril", "lisinopril", "perindopril"},
    "arb": {
        "telmisartan", "losartan", "olmesartan", "valsartan", "irbesartan",
        "candesartan",
    },
    "k_raising": {
        "spironolactone", "eplerenone", "amiloride", "triamterene",
        "potassium chloride",
    },
    "thiazide": {"hydrochlorothiazide", "chlorthalidone", "indapamide"},
    "vka": {"warfarin", "acenocoumarol"},
    "macrolide": {"clarithromycin", "erythromycin"},
    "fluoroquinolone": {
        "ciprofloxacin", "levofloxacin", "ofloxacin", "moxifloxacin",
    },
    "beta_blocker": {
        "metoprolol", "atenolol", "propranolol", "bisoprolol", "carvedilol",
        "nebivolol",
    },
    "rate_limiting_ccb": {"verapamil", "diltiazem"},
    "polyvalent_cation": {
        "calcium", "calcium carbonate", "magnesium", "magnesium hydroxide",
        "aluminium hydroxide", "iron", "zinc",
    },
}

# Groups where two *different* members taken together is therapeutic duplication.
DUPLICATION_GROUPS = {
    "nsaid": "NSAID pain-killers",
    "ssri": "SSRI antidepressants",
    "statin": "statin cholesterol medicines",
    "benzodiazepine": "benzodiazepine sedatives",
    "ppi": "proton-pump inhibitors",
}

SEVERITY_ORDER = {"major": 0, "moderate": 1, "minor": 2}


def _members(token):
    if token.startswith("g:"):
        return GROUPS[token[2:]]
    return {token}


# (side A, side B, severity, description)
RULES = [
    ("g:vka", "g:nsaid", "major",
     "Higher risk of serious bleeding. Avoid the combination unless a doctor has specifically prescribed it."),
    ("g:vka", "aspirin", "major",
     "Higher risk of serious bleeding. Use only under a doctor's supervision."),
    ("g:vka", "metronidazole", "major",
     "Metronidazole can strongly raise the blood-thinning effect (INR). The dose usually needs monitoring."),
    ("g:vka", "g:fluoroquinolone", "moderate",
     "Fluoroquinolone antibiotics can raise INR and bleeding risk. INR monitoring is advised."),
    ("g:vka", "paracetamol", "moderate",
     "Regular high-dose paracetamol can raise INR. Occasional use is usually acceptable; ask your doctor."),
    ("g:vka", "rifampicin", "moderate",
     "Rifampicin lowers the effect of warfarin-type medicines; dose adjustment is usually needed."),
    ("aspirin", "g:nsaid", "moderate",
     "Increased stomach-bleeding risk. Ibuprofen and some NSAIDs can also reduce aspirin's heart-protective effect."),
    ("g:pde5", "g:nitrate", "major",
     "Can cause a dangerous drop in blood pressure. This combination is contraindicated."),
    ("tadalafil", "tamsulosin", "moderate",
     "May cause low blood pressure and dizziness, especially when standing up."),
    ("g:ace_arb", "g:k_raising", "moderate",
     "May raise blood potassium to unsafe levels. Potassium monitoring is advised."),
    ("g:ace_arb", "g:nsaid", "moderate",
     "NSAIDs can weaken the blood-pressure effect and increase the risk of kidney injury."),
    ("g:ace", "g:arb", "moderate",
     "Taking an ACE inhibitor with an ARB (dual blockade) raises the risk of kidney problems and high potassium."),
    ("g:ssri", "tramadol", "major",
     "Risk of serotonin syndrome and seizures. Avoid unless supervised by a doctor."),
    ("g:ssri", "linezolid", "major",
     "Risk of serotonin syndrome. Generally avoided."),
    ("g:ssri", "g:nsaid", "moderate",
     "Increased risk of stomach and other bleeding."),
    ("clarithromycin", "simvastatin", "major",
     "Large rise in simvastatin levels can cause serious muscle damage (rhabdomyolysis). Avoid together."),
    ("erythromycin", "simvastatin", "major",
     "Large rise in simvastatin levels can cause serious muscle damage (rhabdomyolysis). Avoid together."),
    ("clarithromycin", "atorvastatin", "moderate",
     "May raise atorvastatin levels and the risk of muscle side effects."),
    ("amlodipine", "simvastatin", "moderate",
     "Amlodipine raises simvastatin levels; the simvastatin dose is usually capped."),
    ("g:benzodiazepine", "g:opioid", "major",
     "Combined sedation and slowed breathing can be life-threatening. Use only under close medical supervision."),
    ("methotrexate", "g:nsaid", "major",
     "NSAIDs can raise methotrexate levels and cause serious toxicity."),
    ("methotrexate", "trimethoprim", "major",
     "Both reduce folate; the combination can cause severe blood-count problems."),
    ("methotrexate", "sulfamethoxazole", "major",
     "Can cause severe blood-count problems and methotrexate toxicity."),
    ("digoxin", "amiodarone", "major",
     "Amiodarone raises digoxin levels and can cause digoxin toxicity. Dose reduction and monitoring are needed."),
    ("digoxin", "g:macrolide", "major",
     "Macrolide antibiotics can raise digoxin levels and cause toxicity."),
    ("g:fluoroquinolone", "g:nsaid", "moderate",
     "Increased risk of seizures and central-nervous-system side effects."),
    ("g:fluoroquinolone", "g:polyvalent_cation", "moderate",
     "Calcium, iron, zinc, magnesium and antacids reduce antibiotic absorption. Space doses by at least 2 hours."),
    ("levothyroxine", "g:polyvalent_cation", "moderate",
     "Calcium, iron and antacids reduce thyroid-hormone absorption. Separate doses by about 4 hours."),
    ("lithium", "g:nsaid", "major",
     "NSAIDs can raise lithium levels and cause lithium toxicity."),
    ("lithium", "g:ace_arb", "moderate",
     "May raise lithium levels. Monitoring is advised."),
    ("lithium", "g:thiazide", "moderate",
     "Thiazide diuretics can raise lithium levels."),
    ("clopidogrel", "omeprazole", "moderate",
     "Omeprazole may reduce clopidogrel's effect. Pantoprazole is usually preferred."),
    ("clopidogrel", "esomeprazole", "moderate",
     "Esomeprazole may reduce clopidogrel's effect. Pantoprazole is usually preferred."),
    ("g:beta_blocker", "g:rate_limiting_ccb", "major",
     "Risk of very slow heart rate and heart block. Needs close medical supervision."),
    ("allopurinol", "azathioprine", "major",
     "Allopurinol raises azathioprine levels and can cause severe bone-marrow suppression."),
    ("theophylline", "ciprofloxacin", "moderate",
     "Ciprofloxacin can raise theophylline levels and cause toxicity."),
]


# =========================================================
# CHECKS
# =========================================================


def _pair_key(a, b):
    return tuple(sorted((a, b)))


def find_interactions(ingredient_map):
    """
    ``ingredient_map``: canonical ingredient -> list of medicine display names.

    Returns a list of interaction dictionaries sorted by severity.
    """
    present = set(ingredient_map)
    found = {}

    def add(a, b, severity, description):
        meds_a = ingredient_map[a]
        meds_b = ingredient_map[b]

        # Both ingredients from the very same single product (fixed-dose combo)
        if set(meds_a) == set(meds_b) and len(set(meds_a)) == 1:
            return

        key = _pair_key(a, b)

        if key in found:
            return

        found[key] = {
            "drug_a": key[0],
            "drug_b": key[1],
            "severity": severity,
            "description": description,
            "medicines": list(dict.fromkeys(list(meds_a) + list(meds_b))),
            "source": "built-in rules",
        }

    for side_a, side_b, severity, description in RULES:
        for a in _members(side_a) & present:
            for b in _members(side_b) & present:
                if a != b:
                    add(a, b, severity, description)

    for group, label in DUPLICATION_GROUPS.items():
        members = sorted(GROUPS[group] & present)

        for i, a in enumerate(members):
            for b in members[i + 1:]:
                add(
                    a,
                    b,
                    "moderate",
                    f"Both belong to the same class ({label}). "
                    "Taking two together adds side-effect risk without extra benefit.",
                )

    return sorted(
        found.values(),
        key=lambda x: (SEVERITY_ORDER.get(x["severity"], 9), x["drug_a"]),
    )


# ---------- dose ----------

# Typical adult maximum per day (mg). Informational only.
MAX_DAILY_MG = {
    "paracetamol": 4000,
    "ibuprofen": 3200,
    "diclofenac": 150,
    "aceclofenac": 200,
    "naproxen": 1250,
    "tramadol": 400,
}


def strength_to_mg(strength):
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*(mg|g|mcg|µg)\s*",
        str(strength or "").lower(),
    )

    if not match:
        return None

    value = float(match.group(1))
    unit = match.group(2)

    if unit == "g":
        return value * 1000

    if unit in ("mcg", "µg"):
        return value / 1000

    return value


def parse_frequency(text):
    """Doses per day from text such as 'twice a day', 'TDS', '1-0-1', 'every 8 hours'."""
    if not text:
        return None

    t = str(text).lower()

    pattern = re.search(r"(\d+)\s*[-–]\s*(\d+)\s*[-–]\s*(\d+)(?:\s*[-–]\s*(\d+))?", t)
    if pattern:
        return sum(int(g) for g in pattern.groups() if g is not None) or None

    hours = re.search(r"every\s*(\d+)\s*(?:hours?|hrs?|h)\b", t) or re.search(
        r"\bq(\d+)h\b", t
    )
    if hours and int(hours.group(1)) > 0:
        return 24 / int(hours.group(1))

    times = re.search(r"(\d+)\s*(?:times|x)\b", t)
    if times:
        return int(times.group(1))

    if re.search(r"\b(four times|qid|qds)\b", t):
        return 4
    if re.search(r"\b(thrice|three times|tds|tid)\b", t):
        return 3
    if re.search(r"\b(twice|two times|bd|bid)\b", t):
        return 2
    if re.search(r"\b(once|od|daily|every day|nightly)\b", t):
        return 1

    return None


def _parse_dose(dose):
    """Return (explicit_mg or None, unit_count)."""
    text = str(dose or "").strip().lower()

    if not text:
        return None, 1.0

    mg = re.search(r"(\d+(?:\.\d+)?)\s*(mg|g|mcg|µg)\b", text)
    explicit = strength_to_mg(f"{mg.group(1)} {mg.group(2)}") if mg else None

    if re.search(r"\b(half|1/2)\b", text):
        return explicit, 0.5

    count = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(?:x\s*)?"
        r"(?:tablets?|tabs?|caps?|capsules?|pills?|sachets?|units?)?",
        text,
    )

    return explicit, float(count.group(1)) if count else 1.0


def estimate_daily_mg(medicine):
    """Return {canonical ingredient: estimated mg per day} for one medicine."""
    frequency = parse_frequency(medicine.get("frequency"))

    if not frequency:
        return {}

    details = medicine.get("ingredient_details")

    if not details:
        details = parse_ingredient_details(medicine.get("active_ingredients"))

    if not details and medicine.get("primary_ingredient"):
        details = [
            {
                "name": medicine["primary_ingredient"],
                "strength": medicine.get("primary_strength", ""),
            }
        ]

    explicit_mg, count = _parse_dose(medicine.get("dose"))
    result = {}

    for d in details:
        key = normalize_ingredient(d.get("name"))
        strength = strength_to_mg(d.get("strength"))

        if explicit_mg is not None and len(details) == 1:
            per_dose = explicit_mg
        elif strength is not None:
            per_dose = strength * count
        else:
            continue

        if key:
            result[key] = result.get(key, 0) + per_dose * frequency

    return result


def find_dose_alerts(medicines):
    totals = {}
    sources = {}

    for medicine in medicines:
        name = medicine.get("medicine_name") or medicine.get("brand_name") or "Unknown"

        for ingredient, mg in estimate_daily_mg(medicine).items():
            totals[ingredient] = totals.get(ingredient, 0) + mg
            sources.setdefault(ingredient, []).append(name)

    alerts = []

    for ingredient, total in totals.items():
        limit = MAX_DAILY_MG.get(ingredient)

        if limit and total > limit:
            alerts.append(
                {
                    "ingredient": ingredient,
                    "daily_mg": round(total),
                    "limit_mg": limit,
                    "medicines": list(dict.fromkeys(sources[ingredient])),
                }
            )

    return sorted(alerts, key=lambda a: a["ingredient"])


# ---------- allergy ----------

ALLERGY_CLASSES = {
    "penicillin": {
        "amoxycillin", "ampicillin", "cloxacillin", "piperacillin",
        "penicillin", "phenoxymethylpenicillin", "benzathine penicillin",
        "flucloxacillin", "dicloxacillin",
    },
    "cephalosporin": {
        "cefixime", "ceftriaxone", "cefuroxime", "cefpodoxime", "cephalexin",
        "cefadroxil", "cefotaxime", "cefdinir", "cefaclor", "ceftazidime",
    },
    "sulfa": {"sulfamethoxazole", "sulfasalazine", "sulfadiazine"},
    "nsaid": GROUPS["nsaid"] | {"aspirin"},
    "macrolide": GROUPS["macrolide"] | {"azithromycin", "roxithromycin"},
    "fluoroquinolone": GROUPS["fluoroquinolone"],
}

ALLERGY_ALIASES = {
    "penicillins": "penicillin",
    "cephalosporins": "cephalosporin",
    "sulphonamide": "sulfa",
    "sulphonamides": "sulfa",
    "sulpha": "sulfa",
    "sulfa drugs": "sulfa",
    "nsaids": "nsaid",
    "macrolides": "macrolide",
    "quinolone": "fluoroquinolone",
    "quinolones": "fluoroquinolone",
    "fluoroquinolones": "fluoroquinolone",
}


def parse_allergies(text):
    items = re.split(r"[,;\n]", str(text or ""))
    cleaned = []

    for item in items:
        item = item.strip()

        if item and item.lower() not in [c.lower() for c in cleaned]:
            cleaned.append(item[:60])

    return cleaned[:30]


def find_allergy_alerts(allergies, ingredient_map):
    alerts = []

    for allergy in allergies or []:
        norm = normalize_ingredient(allergy)
        class_name = ALLERGY_ALIASES.get(norm, norm)

        matched = set()

        if norm in ingredient_map:
            matched.add(norm)

        if class_name in ALLERGY_CLASSES:
            matched |= ALLERGY_CLASSES[class_name] & set(ingredient_map)

        for ingredient in sorted(matched):
            alerts.append(
                {
                    "allergy": allergy,
                    "ingredient": ingredient,
                    "medicines": list(dict.fromkeys(ingredient_map[ingredient])),
                }
            )

    return alerts


# =========================================================
# PASSWORD POLICY
# =========================================================

COMMON_PASSWORDS = {
    "password", "password1", "12345678", "123456789", "qwertyui", "qwerty123",
    "admin123", "admin1234", "iloveyou", "11111111", "abcd1234", "pharmashield",
}


def validate_password(password):
    """Return an error message, or None if the password is acceptable."""
    if len(password) < 8:
        return "Password must be at least 8 characters."

    if password.isdigit():
        return "Password cannot be only numbers."

    if password.lower() in COMMON_PASSWORDS:
        return "That password is too common. Choose a different one."

    return None
