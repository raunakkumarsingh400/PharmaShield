# PharmaShield

Django + MongoDB medicine verification and medication-safety app.

## Features

- Registration / login with PharmaShield ID, lockout after repeated failures
- Secure password reset (single-use, 30-minute email link)
- Medicine search (index-backed prefix search by brand or ingredient)
- **Cheaper alternatives**: same ingredients, strengths and form, cheapest first
- Personal medicine list with dose and frequency
- **Safety engine** (`core/safety.py`)
  - allergy check (ingredient and class level: penicillin, sulfa, NSAIDs ...)
  - estimated daily-dose check across all your medicines (paracetamol, ibuprofen, diclofenac ...)
  - duplicate active ingredients (handles salts and spelling: acetaminophen = paracetamol)
  - ~40 built-in interaction rules with severity, plus therapeutic-duplication (e.g. two NSAIDs)
  - extra interactions can still be added in the MongoDB `drug_interactions` collection
- Medicine verification with expiry check and community-report flagging
- **Google-style QR scanner**: dimmed camera view, blue/red/yellow/green corner
  brackets and a moving rainbow scan line; auto-verifies on scan; also accepts an
  uploaded QR image. The QR can hold a plain code, a link ending in `?code=...`,
  or JSON like `{"code": "..."}`
- Scan history, suspicious-medicine reports, admin medicine database
- **Safety report PDF**: "Download PDF Report" on the Safety Check page (medicines,
  allergy / dose / duplicate / interaction findings, disclaimer)
- **Admin dashboard** (`/admin-dashboard/`): totals, scans per day (last 14 days),
  verification-result breakdown, most-reported / flagged codes, recent scans and reports
- **Admin QR generator** (`/admin-qr/`): QR for every batch (search, print the whole
  sheet or save as PDF, download each QR as SVG), plus a QR for any text. Pure-Python
  encoder, no extra library; scanning it on Verify finds the medicine

## Requirements

- **Python 3.12 or newer** (3.13 recommended) -> installs the latest **Django 6.x**.
- Python 3.10 / 3.11 still works: `requirements.txt` then installs Django 5.2 LTS.
- Check with `python --version`, and `python check_setup.py` prints both versions.

## Setup

```powershell
py -m venv pharma_ai
.\pharma_ai\Scripts\Activate.ps1
pip install -r requirements.txt
```

Optional environment variables:

```powershell
$env:PHARMASHIELD_MONGO_URI="mongodb://127.0.0.1:27017"
$env:PHARMASHIELD_DB="pharmashield"
```

### Demo data (small, ~100 rows) - recommended for testing

`demo_data/PharmaShield_Demo_Data.xlsx` has 108 real brand names with fictional
batch numbers, verification codes, barcodes and QR codes (8 expired and 6
expiring-soon rows for testing). Import only this file - no need for the big CSV:

```powershell
pip install -r requirements.txt
python import_medicines.py demo_data/PharmaShield_Demo_Data.xlsx
```

Then open the QR sheets on another screen (or print them) and scan the codes from
the **Verify Medicine** page:

- `demo_data/PharmaShield_QR_SUCCESS.pdf` - 94 valid codes, Verify shows a verified result.
- `demo_data/PharmaShield_QR_ERROR.pdf` - 8 expired batches (expired result) and 6 codes
  that are not in the database (no-record result: a fake look-alike code, unknown codes,
  a website link and plain text).

Codes that expire within 60 days are left out of both sheets because their status changes
with the date (they verify with an "expires soon" warning).

### Full dataset (optional)

Import the big medicine CSV (fast bulk import, safe to re-run; also cleans blank
records created by older versions). Rows with the same `product_id` are merged
with the demo rows:

```powershell
python import_medicines.py indian_pharmaceutical_products_clean.csv
```

Create the admin account (there is no default admin password any more):

```powershell
python create_admin.py
```

Run:

```powershell
python manage.py runserver
```

## Automatic logout

- **Idle timeout:** logged out after 15 minutes without activity. A warning
  banner appears 60 seconds before, with a "Stay logged in" button.
- **Maximum session:** logged out 8 hours after login, even if active.
- Back button cannot show a logged-in page after logout (pages are not cached).

Change the limits with environment variables:

```powershell
$env:IDLE_TIMEOUT_MINUTES="10"
$env:ABSOLUTE_TIMEOUT_MINUTES="240"
```

## Favicon

Icons live in `static/favicon/` (`favicon.ico`, 16/32/48/64 px PNGs, Apple touch
icon, Android 192/512 px and `site.webmanifest`). Every page links them in its
`<head>`. After changing an icon, bump the `?v=3` number in the templates so
browsers fetch the new one.

## Scanner notes

- Camera opens in HD with continuous autofocus; **torch** and **zoom** controls
  appear automatically when the phone/webcam supports them.
- Decoding uses the browser's built-in QR detector (`BarcodeDetector`, Chrome/Edge on
  Android and many desktops) and falls back to the **jsQR** library (loaded from
  jsDelivr, pinned to 1.4.0). Every frame is tried twice (whole frame + sharper
  centre crop), so small or far-away QR codes are caught faster.
- After 8 seconds without a QR, a tip appears (distance, glare, torch).
- The camera only works on `https://` or `http://localhost`. If the library cannot load
  (no internet) use "Upload QR Image" or type the code.
- `verify/?code=PSQR-100001` verifies a code directly (useful if you print QR
  codes that contain a link).

## Password-reset email

With no email settings, the reset link is **only printed in the terminal**
running the server and nothing reaches an inbox. Configure one option:

**Easiest: run `python setup_env.py`.** It asks for your Atlas string and Gmail
details and writes a correct `.env` for you (no Notepad, no `.env.txt` mistakes).
While DEBUG is on and no email is configured, the Forgot Password page also shows the
reset link on screen so you are never blocked during development.

**Or create the `.env` by hand:** Copy `.env.example` to `.env` (same folder as
`manage.py`), fill in the values and restart the server. It is read automatically
by `runserver`, `import_medicines.py` and `create_admin.py`, so you do not have
to set variables in PowerShell each time.

**A) Gmail (on your own computer / paid hosting)**

1. Google Account -> Security -> turn on 2-Step Verification.
2. Google Account -> Security -> App passwords -> create one (16 characters).
3. In PowerShell (same window as the server):

```powershell
$env:EMAIL_HOST_USER="yourname@gmail.com"
$env:EMAIL_HOST_PASSWORD="abcd efgh ijkl mnop"
python manage.py runserver
```

**B) Brevo API (works on Render free, which blocks SMTP ports 25/465/587)**

1. Create a free account at brevo.com -> Senders, Domains -> add and verify a sender email.
2. SMTP & API -> API Keys -> create a key.
3. Set the variables:

```text
BREVO_API_KEY=xkeysib-...
EMAIL_FROM_ADDRESS=the-sender-email-you-verified@example.com
EMAIL_FROM_NAME=PharmaShield
```

If both are set, Brevo is used. Failures are printed in the server log
(`Email via smtp failed ...`) so you can see the exact reason.

## Favicon

Icons live in `static/favicon/`. The pages link them with `?v=4`; bump that
number in the templates (and `pharmashield/urls.py`) when you change the icon,
so browsers drop the old cached one.

## Works on every screen size

Responsive layout for phones (320px and up), tablets, laptops and large monitors:

- **Desktop (above 900px):** fixed sidebar.
- **Tablet / phone (900px and below):** top bar with a menu button; the menu slides in
  (closes with the X, a tap outside, a menu link or the Escape key).
- Cards and stat boxes re-flow automatically; buttons become full width and at least 44px
  high on phones; form fields use 16px text so iPhones do not zoom in.
- The QR scanner box always fits the screen, also with the phone held sideways.
- Respects phone notches (safe areas) and the "reduce motion" setting.

## Everything lives in MongoDB Atlas

Users, medicines, scans, reports, allergies, password-reset tokens **and login
sessions** are all stored in Atlas (`sessions` collection, cleaned up
automatically). The web server keeps nothing on its own disk, so a host with a
temporary filesystem (Render free) can sleep, restart or redeploy without losing
data or logging everyone out. `python manage.py migrate` is no longer needed.

## Deploying on Render (free)

1. Put the project on GitHub (do not upload `.env` or the big CSV).
2. Atlas -> Network Access -> add **0.0.0.0/0** (Render's IP changes).
3. Render -> New -> **Blueprint** -> choose the repo (it reads `render.yaml`), or
   New -> Web Service with:
   - Build: `pip install -r requirements.txt && python manage.py collectstatic --noinput`
   - Start: `gunicorn pharmashield.wsgi`
4. Environment variables (Render dashboard):

```text
DEBUG=False
DJANGO_SECRET_KEY=<long random string>        (the Blueprint generates it)
PHARMASHIELD_MONGO_URI=<your Atlas string>
PHARMASHIELD_DB=pharmashield
BREVO_API_KEY=<key>   EMAIL_FROM_ADDRESS=<verified sender>   (password-reset email)
```

5. Load the data once from your own computer (it goes straight into Atlas):

```powershell
python import_medicines.py demo_data/PharmaShield_Demo_Data.xlsx
python create_admin.py
```

With `DEBUG=False` the app refuses to start without `DJANGO_SECRET_KEY`.
The free Render instance sleeps after ~15 minutes idle; the first request after
that takes about a minute, but data and logins are kept.

## Tests

```powershell
python manage.py test core
```

## Extending interactions

Add documents to `drug_interactions` using canonical lowercase ingredient names:

```json
{ "drug_a": "warfarin", "drug_b": "ibuprofen", "severity": "major", "description": "..." }
```

## Medical disclaimer

PharmaShield is decision support only. The built-in rules are a curated starter
set and must be reviewed by a qualified pharmacist or clinician before real-world
use. It does not replace professional medical advice.
