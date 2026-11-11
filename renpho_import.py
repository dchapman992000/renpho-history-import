#!/usr/bin/env python3
"""Restore ES-CS20M history from a RENPHO Health CSV."""
import argparse
import csv
import getpass
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

UPLOAD = "RenphoHealth/scale/uploadMeasureData/"
MASS = {
    "Weight(lb)": "weight", "Muscle Mass(lb)": "muscle",
    "Bone Mass(lb)": "bone", "Fat-Free Mass(lb)": "fatFreeWeight",
    "Skeletal Muscle Mass(lb)": "smmMass",
}
METRICS = {
    "BMI": "bmi", "Body Fat Percentage(%)": "bodyfat",
    "Body Water Percentage(%)": "water", "Protein Percentage(%)": "protein",
    "Subcutaneous Fat(%)": "subfat", "Visceral Fat": "visfat",
    "BMR(kcal)": "bmr", "Metabolic Age": "bodyage",
}


def read_export(path, zone):
    records = {}
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        columns = {"Date", "Time", *MASS, *METRICS}
        columns.add("Skeletal Muscle Percentage(%)")
        missing = columns - set(reader.fieldnames or [])
        if missing:
            raise ValueError("Missing CSV columns: " + ", ".join(sorted(missing)))
        for row in reader:
            local = datetime.strptime(row["Date"] + " " + row["Time"], "%Y.%m.%d %H:%M:%S")
            time = local.replace(tzinfo=zone)
            stamp = int(time.timestamp())
            if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) != local:
                raise ValueError(f"Nonexistent local time: {local}")
            if local.replace(tzinfo=zone, fold=1).utcoffset() != time.utcoffset():
                raise ValueError(f"Ambiguous local time: {local}")
            offset = time.strftime("%z")
            record = {
                "timeStamp": stamp, "localCreatedAt": local.isoformat(sep=" "),
                "timeZone": f"GMT{offset[:3]}:{offset[3:]}",
            }
            for column, field in (MASS | METRICS).items():
                value = row[column].strip()
                if value in ("", "--"):
                    continue
                number = float(value)
                if not math.isfinite(number) or number < 0:
                    raise ValueError(f"Invalid {column} at {local}")
                if field in ("bmr", "bodyage") and not number.is_integer():
                    raise ValueError(f"Expected whole number in {column} at {local}")
                record[field] = (
                    round(number * 0.45359237, 4) if column in MASS
                    else int(number) if field in ("bmr", "bodyage")
                    else number
                )
            mass = record.pop("muscle", None)
            if mass is not None:
                record["sinew"] = mass
            percentage = row["Skeletal Muscle Percentage(%)"].strip()
            if percentage not in ("", "--"):
                number = float(percentage)
                if not 0 <= number <= 100:
                    raise ValueError(f"Invalid skeletal muscle percentage at {local}")
                record["muscle"] = number
            if record.get("weight", 0) <= 0:
                raise ValueError(f"Missing or invalid weight at {local}")
            if stamp in records and records[stamp] != record:
                raise ValueError(f"Different readings share a timestamp: {local}")
            records[stamp] = record
    if not records:
        raise ValueError("CSV contains no readings")
    return [records[stamp] for stamp in sorted(records)]


def history(client):
    # The library uses the legacy route only when the composition route is
    # empty. Manual records and scale records can live in different stores.
    account = str(client.user_id)
    tables = {
        scale["tableName"]
        for scale in client.get_device_info().get("scale", [])
        if scale.get("tableName") and account in {str(i) for i in scale.get("userIds", [])}
    }
    shard = client.measurement_table_for(account)
    if shard:
        tables.add(shard)

    records = {}
    for table in sorted(tables):
        for store, fetch in (
            ("scale history", client.get_measurements),
            ("body composition", client.get_body_composition_measurements),
        ):
            for row in fetch(table, account):
                owner = row.get("bUserId") or row.get("userId")
                if str(owner) != account or row.get("timeStamp") is None:
                    continue
                stamp = int(row["timeStamp"])
                if abs(stamp) >= 100_000_000_000:
                    stamp //= 1000
                records.setdefault(stamp, []).append((store, row))
    return records


def es_cs20m_mac(existing):
    for stamp in sorted(existing, reverse=True):
        for store, row in existing[stamp]:
            mac = row.get("mac")
            if (store == "scale history" and row.get("deviceType") == "00007"
                    and isinstance(mac, str) and mac.strip("0:- ")):
                return mac
    raise ValueError("No ES-CS20M reading with a usable device address was returned. No upload sent.")


def same_address(row, mac):
    def normalize(value):
        return str(value or "").replace(":", "").replace("-", "").strip().lower()

    return normalize(row.get("mac")) == normalize(mac)


def scale_match(expected, candidates, mac):
    return any(
        store == "scale history"
        and actual.get("deviceType") == "00007"
        and same_address(actual, mac)
        and not differences(expected, actual, {"smmMass"})
        for store, actual in candidates
    )


def differences(expected, actual, ignored=frozenset()):
    fields = ((set(MASS.values()) | set(METRICS.values()) | {"sinew"}) & expected.keys()) - ignored
    return [
        field for field in sorted(fields)
        if actual.get(field) is None
        or not math.isclose(float(actual[field]), expected[field], abs_tol=0.055)
    ]


def upload(client, rows, zone):
    from renpho.constants import APP_VERSION, PLATFORM
    from renpho.crypto import encrypt_request

    offset = datetime.now(zone).strftime("%z")
    hour = int(offset[1:3])
    short_offset = f"{offset[0]}{hour}" + (f":{offset[3:]}" if offset[3:] != "00" else "")
    headers = {
        "token": client.token, "userId": str(client.user_id),
        "appVersion": APP_VERSION, "platform": PLATFORM,
        "Content-Type": "application/json", "timeZone": short_offset,
        "zoneId": zone.key,
    }
    result = client._transport.post(UPLOAD, encrypt_request(rows), headers=headers)
    if result.get("code") not in (0, "0", 200, "200", 20000, "20000") and (
        str(result.get("msg", "")).lower() != "success"
    ):
        code = str(result.get("code", "unknown"))
        if code == "429":
            raise RuntimeError("RENPHO rate-limited the upload (code 429). Run the read-only plan before resuming.")
        if code == "-114":
            raise RuntimeError(
                f"RENPHO returned -114 (request decryption failed) with appVersion {APP_VERSION}. "
                "The upload protocol remains unverified. Check RENPHO History for the test row before retrying."
            )
        detail = next(
            (" ".join(value.split())[:240] for key in ("msg", "message", "errorMessage")
             if isinstance(value := result.get(key), str) and value.strip()),
            "",
        )
        if client.token:
            detail = detail.replace(client.token, "[redacted]")
        display_code = code if code.lstrip("-").isdigit() else "unknown"
        suffix = f". Server message: {detail}" if detail else ". RENPHO supplied no error message."
        raise RuntimeError(f"RENPHO rejected the upload (code {display_code}){suffix}")


def plan(records, existing, mac):
    matches, pending, conflicts = 0, [], []
    for record in records:
        candidates = existing.get(record["timeStamp"])
        if not candidates:
            pending.append(record)
        elif scale_match(record, candidates, mac):
            matches += 1
        else:
            conflicts.append(record)
    print(f"{matches} matching ES-CS20M readings, {len(pending)} missing, {len(conflicts)} conflicts.")
    for record in conflicts:
        candidates = existing[record["timeStamp"]]
        fields = min(
            (differences(record, actual, {"smmMass"}) for _, actual in candidates),
            key=len,
        )
        detail = ", ".join(fields) if fields else "device type, history store, or address differs"
        print(f"  {record['localCreatedAt']} ({detail})")
    return matches, pending, conflicts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "test", "import"))
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--timezone", required=True, help="Time zone used by the export, e.g. America/Chicago")
    parser.add_argument("--email", required=True)
    parser.add_argument("--account-id", help="Expected RENPHO account ID, required to upload")
    parser.add_argument("--at", help="Test this exact local timestamp (YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--limit", type=int, help="Import at most this many readings in this run")
    args = parser.parse_args()
    if args.action != "plan" and (not args.account_id or not args.account_id.isdecimal()):
        parser.error("Upload actions require the numeric --account-id from the plan")
    if args.at and args.action != "test":
        parser.error("--at applies to the test action")
    if args.limit is not None and (args.action != "import" or args.limit < 1):
        parser.error("--limit requires the import action and a positive count")

    zone = ZoneInfo(args.timezone)
    records = read_export(args.csv, zone)
    print(f"{len(records)} CSV readings from {records[0]['localCreatedAt']} through {records[-1]['localCreatedAt']}.")
    from renpho import RenphoClient

    client = RenphoClient(args.email, getpass.getpass("RENPHO password: "))
    client.login(expected_user_id=args.account_id)
    print(f"Authenticated RENPHO account ID: {client.user_id}", flush=True)
    existing = history(client)
    mac = es_cs20m_mac(existing)
    matches, pending, conflicts = plan(records, existing, mac)
    if args.action == "plan":
        return
    if conflicts:
        raise ValueError("Resolve timestamp conflicts in RENPHO before uploading.")
    if not pending:
        print("Every CSV reading already matches.")
        return
    if args.action == "test":
        selected = next((r for r in pending if r["localCreatedAt"] == args.at), None) if args.at else pending[-1]
        if selected is None:
            raise ValueError("--at must match a CSV reading that is missing in RENPHO.")
        pending = [selected]
    else:
        if not matches:
            raise ValueError("Run test first and verify the imported reading in the app.")
        pending = list(reversed(pending))[:args.limit]

    print("Standalone skeletal muscle mass stays in the CSV. The upload carries skeletal muscle percentage.")
    print(f"Selected {len(pending)} reading(s), newest first, starting at {pending[0]['localCreatedAt']}.")
    if input("Type UPLOAD to proceed: ") != "UPLOAD":
        return

    for index in range(0, len(pending), 10):
        batch = pending[index:index + 10]
        payload = [
            dict(
                ((key, value) for key, value in record.items() if key != "smmMass"),
                bUserId=str(client.user_id), userId=int(client.user_id),
                deviceType="00007", displayModuleType=0, isAuto=2, method=5, mac=mac,
            )
            for record in batch
        ]
        upload(client, payload, zone)
        time.sleep(5)
        refreshed = history(client)
        for record in batch:
            candidates = refreshed.get(record["timeStamp"], [])
            if not scale_match(record, candidates, mac):
                raise ValueError(
                    f"Readback could not verify {record['localCreatedAt']}. "
                    "Check RENPHO History and run plan before resuming."
                )
        print(f"Verified {index + len(batch)} of {len(pending)} reading(s).", flush=True)
        if index + len(batch) < len(pending):
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        sys.exit(str(error))
    except (EOFError, KeyboardInterrupt):
        sys.exit("Stopped. Check RENPHO History and run plan before resuming.")
    except Exception as error:
        response = getattr(error, "response", None)
        if getattr(response, "status_code", None) == 429:
            retry_after = response.headers.get("Retry-After", "")
            wait = f" Wait at least {retry_after} seconds." if retry_after.isdecimal() else ""
            sys.exit("RENPHO rate-limited the request (HTTP 429)." + wait + " Run plan before resuming.")
        sys.exit(f"Stopped ({type(error).__name__}). Check RENPHO History and run plan before resuming.")
