#!/usr/bin/env python3
"""
Henter morgentogenes forsinkelser Roskilde -> Holbæk fra Rejseplanen API 2.0
og gemmer dem i Supabase (upsert på service_date + train_number).

Kun Pythons standardbibliotek bruges, så der skal ikke installeres noget på Pi'en.

Miljøvariabler (lægges i .env i samme mappe som scriptet, se README.md):
  REJSEPLANEN_API_KEY        din accessId fra Rejseplan Labs (påkrævet)
  SUPABASE_URL               fx https://abcd1234.supabase.co (påkrævet)
  SUPABASE_KEY               service_role-nøglen (påkrævet, holdes hemmelig)
  SUPABASE_TABLE             tabelnavn (standard: train_delays)
  REJSEPLANEN_BASE_URL       API-roden (standard: https://www.rejseplan.dk/api)
  FROM_STOP_ID / TO_STOP_ID  stop-id'er, hvis navneopslaget skal springes over
  FROM_STOP_NAME             standard: "Roskilde St."
  TO_STOP_NAME               standard: "Holbæk St."
  WINDOW_START / WINDOW_END  afgangsvindue, standard 07:00 og 09:00

Brug:
  python3 forsinkelser.py               # i dag
  python3 forsinkelser.py --dry-run     # vis rækkerne uden at skrive til Supabase
  python3 forsinkelser.py --debug       # skriv rå API-svar til stderr
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def load_dotenv() -> None:
    """Læser en .env-fil i samme mappe som scriptet, hvis den findes.
    Variabler, der allerede er sat i miljøet, bliver ikke overskrevet."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


load_dotenv()
TZ = ZoneInfo("Europe/Copenhagen")
BASE_URL = os.environ.get("REJSEPLANEN_BASE_URL", "https://www.rejseplan.dk/api").rstrip("/")
DEBUG = False


def log(msg: str) -> None:
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def as_list(x) -> list:
    """HAFAS-JSON giver nogle gange et objekt og nogle gange en liste."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


# ---------------------------------------------------------------- Rejseplanen

def api_get(service: str, **params) -> dict:
    params["accessId"] = os.environ["REJSEPLANEN_API_KEY"]
    params["format"] = "json"  # JSON undgår de indlejrede XML-Note-elementer fra 25/11-ændringen
    url = f"{BASE_URL}/{service}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:1000]
        raise SystemExit(f"Rejseplanen svarede {e.code} på {service}: {detail}")
    if DEBUG:
        log(f"{service} svar: {body[:4000]}")
    data = json.loads(body)
    # HAFAS sender fejl som JSON med errorCode i stedet for HTTP-fejl
    if isinstance(data, dict) and data.get("errorCode"):
        raise SystemExit(f"Rejseplanen-fejl på {service}: {data.get('errorCode')} {data.get('errorText', '')}")
    return data


def find_stop_id(name: str) -> str:
    data = api_get("location.name", input=name, type="S", maxNo=5)
    candidates = []
    for item in as_list(data.get("stopLocationOrCoordLocation")):
        candidates += as_list(item.get("StopLocation"))
    candidates += as_list(data.get("StopLocation"))
    if not candidates:
        raise SystemExit(f"Fandt intet stop for '{name}'. Sæt stop-id'et manuelt i env-filen.")
    stop = candidates[0]
    stop_id = str(stop.get("extId") or stop.get("id"))
    log(f"Stop '{name}' -> {stop.get('name')} ({stop_id})")
    return stop_id


def is_train(dep: dict) -> bool:
    texts = [dep.get("name", "")]
    for p in as_list(dep.get("ProductAtStop")) + as_list(dep.get("Product")):
        texts += [p.get("catOut", ""), p.get("catOutL", ""), p.get("name", "")]
    return not any("bus" in str(t).lower() for t in texts)


def train_number(dep: dict) -> str:
    for p in as_list(dep.get("ProductAtStop")) + as_list(dep.get("Product")):
        if p.get("num"):
            return str(p["num"])
    m = re.search(r"(\d+)\s*$", dep.get("name", ""))
    if m:
        return m.group(1)
    raise ValueError(f"Kan ikke finde tognummer i {dep.get('name')!r}")


def parse_dt(d: str | None, t: str | None) -> datetime | None:
    if not d or not t:
        return None
    if len(t) == 5:
        t += ":00"
    return datetime.fromisoformat(f"{d}T{t}")


def stop_matches(stop: dict, stop_id: str, stop_name: str) -> bool:
    ids = {str(stop.get("extId", "")), str(stop.get("id", ""))}
    if stop_id in ids or any(f"L={stop_id}@" in i for i in ids):
        return True
    return stop_name.lower().split(" st")[0] in str(stop.get("name", "")).lower()


def stops_for(dep: dict) -> list[dict]:
    stops = as_list((dep.get("Stops") or {}).get("Stop"))
    if stops:
        return stops
    # Fald tilbage på journeyDetail, hvis passlist ikke gav stoppene med
    ref = (dep.get("JourneyDetailRef") or {}).get("ref")
    if not ref:
        return []
    detail = api_get("journeyDetail", id=ref, date=dep.get("date"))
    return as_list((detail.get("Stops") or {}).get("Stop"))


def minutes(a: datetime, b: datetime) -> int:
    return round((a - b).total_seconds() / 60)


def build_rows(service_date: date, from_id: str, to_id: str, to_name: str,
               window_start: str, window_end: str) -> list[dict]:
    start = datetime.combine(service_date, datetime.strptime(window_start, "%H:%M").time())
    end = datetime.combine(service_date, datetime.strptime(window_end, "%H:%M").time())
    duration = int((end - start).total_seconds() // 60)

    data = api_get("departureBoard", id=from_id, direction=to_id,
                   date=service_date.isoformat(), time=window_start,
                   duration=duration, passlist=1)
    departures = as_list(data.get("Departure"))
    log(f"{len(departures)} afgange fundet i tavlen")

    fetched_at = datetime.now(timezone.utc).isoformat()
    rows: dict[str, dict] = {}
    for dep in departures:
        if not is_train(dep):
            continue
        sched_dep = parse_dt(dep.get("date"), dep.get("time"))
        if sched_dep is None or not (start <= sched_dep <= end):
            continue
        # rtTime mangler, når toget kører til tiden
        actual_dep = parse_dt(dep.get("rtDate") or dep.get("date"), dep.get("rtTime")) or sched_dep
        cancelled = bool(dep.get("cancelled")) or dep.get("reachable") is False

        arr_stop = next((s for s in stops_for(dep) if stop_matches(s, to_id, to_name)), None)
        if arr_stop is None:
            log(f"Springer {dep.get('name')} over: kører ikke til {to_name}")
            continue
        sched_arr = parse_dt(arr_stop.get("arrDate"), arr_stop.get("arrTime"))
        if sched_arr is None:
            log(f"Springer {dep.get('name')} over: ingen ankomsttid i {to_name}")
            continue
        actual_arr = parse_dt(arr_stop.get("rtArrDate") or arr_stop.get("arrDate"),
                              arr_stop.get("rtArrTime")) or sched_arr
        cancelled = cancelled or bool(arr_stop.get("cancelled")) or bool(arr_stop.get("cancelledArrival"))

        num = train_number(dep)
        rows[num] = {
            "service_date": service_date.isoformat(),
            "train_number": num,
            "train_name": dep.get("name", "").strip(),
            "scheduled_departure_roskilde": sched_dep.isoformat(),
            "actual_departure_roskilde": actual_dep.isoformat(),
            "departure_delay_minutes": 0 if cancelled else minutes(actual_dep, sched_dep),
            "scheduled_arrival_holbaek": sched_arr.isoformat(),
            "actual_arrival_holbaek": actual_arr.isoformat(),
            "arrival_delay_minutes": 0 if cancelled else minutes(actual_arr, sched_arr),
            "cancelled": cancelled,
            "fetched_at": fetched_at,
        }
    return sorted(rows.values(), key=lambda r: r["scheduled_departure_roskilde"])


# ------------------------------------------------------------------- Supabase

def upsert(rows: list[dict]) -> None:
    table = os.environ.get("SUPABASE_TABLE", "train_delays")
    url = (os.environ["SUPABASE_URL"].rstrip("/") + f"/rest/v1/{table}"
           + "?on_conflict=service_date,train_number")
    key = os.environ["SUPABASE_KEY"]
    req = urllib.request.Request(
        url, data=json.dumps(rows).encode("utf-8"), method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log(f"Supabase: {len(rows)} rækker gemt (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Supabase svarede {e.code}: {e.read().decode('utf-8', 'replace')[:1000]}")


def main() -> None:
    global DEBUG
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="trafikdag YYYY-MM-DD (standard: i dag)")
    ap.add_argument("--dry-run", action="store_true", help="skriv ikke til Supabase")
    ap.add_argument("--debug", action="store_true", help="vis rå API-svar")
    args = ap.parse_args()
    DEBUG = args.debug

    service_date = date.fromisoformat(args.date) if args.date else datetime.now(TZ).date()
    from_name = os.environ.get("FROM_STOP_NAME", "Roskilde St.")
    to_name = os.environ.get("TO_STOP_NAME", "Holbæk St.")
    from_id = os.environ.get("FROM_STOP_ID") or find_stop_id(from_name)
    to_id = os.environ.get("TO_STOP_ID") or find_stop_id(to_name)

    rows = build_rows(service_date, from_id, to_id, to_name,
                      os.environ.get("WINDOW_START", "07:00"),
                      os.environ.get("WINDOW_END", "09:00"))
    if not rows:
        log("Ingen tog fundet i vinduet. Intet skrevet.")
        return
    for r in rows:
        log(f"{r['train_name']:>10}  afg. {r['scheduled_departure_roskilde'][11:16]} "
            f"+{r['departure_delay_minutes']}  ank. {r['scheduled_arrival_holbaek'][11:16]} "
            f"+{r['arrival_delay_minutes']}{'  AFLYST' if r['cancelled'] else ''}")
    if args.dry_run:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        upsert(rows)


if __name__ == "__main__":
    main()
