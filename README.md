# RENPHO History Import

Restore a RENPHO Health guest-mode CSV into your signed-in RENPHO Health account. The importer previews conflicts, uploads in small batches, and reads each batch back before continuing.

Tested end to end with a **RENPHO ES-CS20M**, Android RENPHO Health **7.11.6**, and an export of **749 readings**. Other models and export formats are untested. The script uses an unofficial RENPHO API that may change.

## Before you start

- Keep your original CSV. This tool expects the English RENPHO Health export with pound-based weight and mass columns. [Synthetic CSV](examples/synthetic.csv) shows the input format.
- Sign into the destination RENPHO Health account and take a normal weigh-in with the **same physical ES-CS20M** that produced the export. The script reuses the device address from the most recent standard-scale reading with device type `00007` and a usable address. If you've used multiple scales with that device type, verify the most recent one is the intended scale before proceeding.
- Use your RENPHO Health email and **RENPHO password**. The script prompts for the password locally. If you normally use Google sign-in, verify that password sign-in opens the same RENPHO profile before uploading.
- Know the time zone used in the CSV. Use an IANA zone such as `America/Chicago` so historical daylight-saving offsets are applied. The script stops on ambiguous or nonexistent local timestamps.

## Install

Requires Python 3.10 or newer.

```sh
git clone https://github.com/dchapman992000/renpho-history-import.git
cd renpho-history-import
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Plan, test, then import

Set these shell variables for your own export and account.

```sh
csv="/path/to/your-renpho-export.csv"
zone="America/Chicago"
email="you@example.com"
```

**1. Run the read-only plan.** This prints the signed-in RENPHO account ID and counts matching, missing, and conflicting readings.

```sh
.venv/bin/python renpho_import.py plan --csv "$csv" --timezone "$zone" --email "$email"
```

Copy the account ID from the plan after confirming it belongs to the intended profile.

```sh
account_id="YOUR_NUMERIC_RENPHO_ACCOUNT_ID"
```

If the plan reports a conflict, inspect that exact timestamp in the app. An earlier test row with incorrect metrics or missing scale attribution can be deleted in the app if you recognize it as your own test. Keep genuine readings. Rerun the plan after any cleanup. Uploads are blocked while conflicts remain.

**2. Test one reading.** The default is the newest missing CSV reading. Use `--at "YYYY-MM-DD HH:MM:SS"` to select another missing timestamp.

```sh
.venv/bin/python renpho_import.py test --csv "$csv" --timezone "$zone" \
  --email "$email" --account-id "$account_id"
```

Type `UPLOAD` at the prompt. After the script verifies the row, check the date, weight, muscle mass, muscle percentage, and data source in the app.

**3. Start with a limited import.**

```sh
.venv/bin/python renpho_import.py import --csv "$csv" --timezone "$zone" \
  --email "$email" --account-id "$account_id" --limit 20
```

If those entries look right, repeat the command with a higher limit or omit `--limit` to finish. The script sends up to 10 readings per batch, newest first. It waits five seconds before reading the batch back and another five seconds before sending the next batch. This pacing is a conservative default. RENPHO's rate limit is unknown.

If the run stops, check the last batch in RENPHO History and rerun `plan` before resuming. If RENPHO sends a rate-limit wait time, honor it before running the plan. The importer skips matching timestamps and stops on conflicts. A rejected or interrupted batch may have saved some rows.

## What is restored

The ES-CS20M upload uses RENPHO's standard-scale history route. On that route, muscle mass in kilograms goes into `sinew`, and skeletal muscle percentage goes into `muscle`. The app calculates muscle percentage from muscle mass and weight. The script also restores weight, BMI, body fat percentage, bone mass, fat-free mass, protein percentage, body water percentage, subcutaneous fat, visceral fat, BMR, and metabolic age when present.

The standard-scale payload has no standalone skeletal muscle mass (`smmMass`) field. Keep the CSV if you need that column as originally exported. The importer also leaves raw impedance and other device measurements out because the CSV lacks them.

Authentication, encryption, and history reads use [renpho-py](https://github.com/ChocoTonic/renpho-py). The script talks directly to RENPHO and keeps the password, session token, and device address in memory.
