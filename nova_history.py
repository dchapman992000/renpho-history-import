#!/usr/bin/env python3
"""Export and restore RENPHO MorphoScan Nova (R-MSC04) Full Reports between profiles."""
import argparse
import getpass
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from renpho_import import same_address, upload

MORPHOSCAN_NOVA_DEVICE_TYPE = "00053"
CHANNELS = {f"z{hz}{part}" for hz in (20, 100) for part in ("Body", "HandL", "HandR", "FootL", "FootR")}
REGIONAL = {
    prefix + field for prefix in ("la", "ra", "ll", "rl", "t")
    for field in ("Muscle", "MuscleMass", "MuscleStd", "BodyFatPct", "BodyFatMass", "BodyFatStd")
}
METRICS = CHANNELS | REGIONAL | set(
    "weight bmi bodyfat bmr bodyage sinewRatio muscle water subfat visfat protein bone whr "
    "bodyScore bodyType obesityDegree ffmCtrl bfmCtrl smi weightTarget weightControl "
    "sinew fatFreeWeight".split()
)
METRICS.update(field + "Std" for field in ("bmi", "bmr", "bfm", "bfp", "smm", "weight"))
METRICS.update(field + bound for bound in ("Min", "Max") for field in (
    "bmi", "bmr", "bfm", "bfp", "smm", "weight", "bone", "proteinMass", "waterMass", "muscleMass"
))
# Only copy measurement metadata. Account IDs and report IDs belong to the destination.
METADATA = set("gender height birthday measureAge pregnantMode babyMode headValue smmMass protocolVersion".split())


def full_match(expected, actual):
    return all(type(actual.get(k)) in (int, float) and
               math.isclose(expected[k], actual[k], rel_tol=0, abs_tol=0.0001) for k in METRICS)


def equivalent(expected, actual):
    return expected["localCreatedAt"] == actual.get("localCreatedAt") and full_match(expected, actual)


def fingerprint(row):
    measurements = [row.get(k) for k in ("weight", *sorted(CHANNELS))]
    if not all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in measurements):
        return None
    address = str(row.get("mac") or "").replace(":", "").replace("-", "").strip().lower()
    return (address, *measurements)


def validate(row):
    if not isinstance(row, dict) or row.get("deviceType") != MORPHOSCAN_NOVA_DEVICE_TYPE or not row.get("reportId"):
        raise ValueError("Expected an exported Nova Full Report")
    if not isinstance(row.get("localCreatedAt"), str) or not row["localCreatedAt"]:
        raise ValueError("Missing original local measurement time")
    if type(row.get("timeStamp")) is not int or row["timeStamp"] <= 0:
        raise ValueError("Invalid measurement timestamp")
    if not isinstance(row.get("mac"), str) or not row["mac"].strip("0:- "):
        raise ValueError("Missing source device address")
    if not all(type(row.get(k)) in (int, float) and math.isfinite(row[k]) for k in METRICS):
        raise ValueError("Incomplete Full Report at " + str(row["timeStamp"]))


def history(client):
    uid = str(client.user_id)
    rows = []
    for table in client.discover_user_tables(uid, strict=True):
        for fetch, composition in ((client.get_body_composition_measurements, True), (client.get_measurements, False)):
            rows.extend((r, composition) for r in fetch(table, uid)
                        if str(r.get("bUserId") or r.get("userId")) == uid)
    return rows


def plan(source, destination):
    devices = [r for r, comp in destination if comp and r.get("deviceType") == MORPHOSCAN_NOVA_DEVICE_TYPE and r.get("reportId")]
    pending, conflicts, planned = [], [], {}
    # The ten impedances together with the mass identify possible date-shifted copies.
    # A collision here requires review because the times alone disagree.
    fingerprints = {}
    for actual in devices:
        key = fingerprint(actual)
        if key is not None:
            fingerprints.setdefault(key, set()).add(actual.get("timeStamp"))
    for row in source:
        validate(row)
        stamp = row["timeStamp"]
        device = next((d for d in devices if same_address(d, row["mac"])), None)
        if device is None:
            raise ValueError("Destination has no Nova Full Report from the source scale")
        if stamp in planned:
            previous = planned[stamp]
            if not (same_address(previous, row["mac"]) and equivalent(row, previous)):
                conflicts.append(stamp)
            continue
        planned[stamp] = row
        candidates = [(r, comp) for r, comp in destination if r.get("timeStamp") == stamp]
        if not candidates:
            key = fingerprint(row)
            dates = fingerprints.setdefault(key, set())
            if dates - {stamp}:
                conflicts.append(stamp)
            else:
                pending.append((row, device))
            dates.add(stamp)
        elif not any(comp and r.get("deviceType") == MORPHOSCAN_NOVA_DEVICE_TYPE and r.get("reportId")
                     and same_address(r, row["mac"]) and equivalent(row, r) for r, comp in candidates):
            conflicts.append(stamp)
    return pending, conflicts


def payload(row, device, uid, zone):
    instant = datetime.fromtimestamp(row["timeStamp"], zone)
    standard = next((d for month in range(1, 13)
                     if not (d := instant.replace(month=month, day=1)).dst()), instant)
    data = {k: row[k] for k in METRICS | METADATA if row.get(k) is not None}
    data.update(id=0, userId=int(uid), bUserId=int(uid), dataSource=1, mac=device["mac"],
                deviceType=MORPHOSCAN_NOVA_DEVICE_TYPE, isAuto=device["isAuto"], displayModuleType=device["displayModuleType"],
                timeStamp=row["timeStamp"], localCreatedAt=instant.strftime("%Y-%m-%d %H:%M:%S"),
                timeZone=standard.tzname(), WC=data.pop("weightControl"))
    return data


def main():
    from renpho import RenphoClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "plan", "import"))
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--account-id", required=True)
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--email")
    auth.add_argument("--session", type=Path)
    parser.add_argument("--timezone", help="IANA measurement time zone for import")
    args = parser.parse_args()
    if not args.account_id.isdecimal():
        parser.error("--account-id must be numeric")
    if args.action == "import" and not args.timezone:
        parser.error("import requires --timezone")
    zone = ZoneInfo(args.timezone) if args.timezone else None
    if args.action != "export":
        rows = json.loads(args.json.read_text())
        if not isinstance(rows, list):
            parser.error("Expected a JSON array of Full Reports")
        for row in rows:
            validate(row)
    if args.session:
        client = RenphoClient.from_session_file(args.session, expected_user_id=args.account_id)
    else:
        client = RenphoClient(args.email, getpass.getpass("RENPHO password: "))
        client.login(expected_user_id=args.account_id)
    destination = history(client)
    if args.action == "export":
        rows = [row for row, comp in destination if comp and row.get("deviceType") == MORPHOSCAN_NOVA_DEVICE_TYPE and row.get("reportId")]
        for row in rows:
            validate(row)
        with os.fdopen(os.open(args.json, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump(rows, f, allow_nan=False, indent=2)
            f.write("\n")
        print(f"Exported {len(rows)} Nova Full Reports.")
        return
    pending, conflicts = plan(rows, destination)
    print(f"{len(rows)} source reports, {len(pending)} missing measurements, {len(conflicts)} conflicts.")
    if conflicts:
        raise ValueError("Conflicting measurement timestamps: " + ", ".join(map(str, conflicts)))
    if args.action == "plan" or not pending or input("Type UPLOAD to proceed: ") != "UPLOAD":
        return
    for row, device in pending:
        upload(client, [payload(row, device, client.user_id, zone)], zone,
               endpoint="RenphoHealth/scale/uploadMeasureData",
               instant=datetime.fromtimestamp(row["timeStamp"], zone))
        missing, conflicts = plan([row], history(client))
        if missing or conflicts:
            raise ValueError(f"Full Report readback failed at {row['timeStamp']}. Run plan before retrying.")
        print(f"Verified Full Report at {row['timeStamp']}.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        sys.exit(str(error))
