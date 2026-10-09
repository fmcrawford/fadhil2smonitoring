import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from curl_cffi import requests as cffi_requests


EVENTS = [
    {
        "group": "JKT48",
        "api_url": "https://jkt48.com/api/v1/exclusives/EX5B99/bonus?lang=id",
    },
    {
        "group": "AKB48",
        "api_url": "https://jkt48.com/api/v1/exclusives/EXD1A1/bonus?lang=id",
    },
    {
        "group": "JKT48_MNG",
        "api_url": "https://jkt48.com/api/v1/exclusives/EX24AE/bonus?lang=id",
    },
]


DEPLEXO_INGEST_URL = os.getenv(
    "DEPLEXO_INGEST_URL",
    "",
).strip()

COLLECTOR_SECRET = os.getenv(
    "COLLECTOR_SECRET",
    "",
).strip()

COLLECTOR_ID = os.getenv(
    "COLLECTOR_ID",
    (
        "cloud"
        if os.getenv("RAILWAY_PROJECT_ID")
        else "pc"
    ),
).strip().lower()

COLLECTOR_INTERVAL = int(
    os.getenv(
        "COLLECTOR_INTERVAL",
        "15",
    )
)

TIMEZONE = "Asia/Jakarta"


if not DEPLEXO_INGEST_URL:
    print(
        "ERROR: DEPLEXO_INGEST_URL belum di-set."
    )
    sys.exit(1)


if not COLLECTOR_SECRET:
    print(
        "ERROR: COLLECTOR_SECRET belum di-set."
    )
    sys.exit(1)


if COLLECTOR_ID not in ("cloud", "pc"):
    print(
        "ERROR: COLLECTOR_ID harus 'cloud' atau 'pc'."
    )
    sys.exit(1)


def now_iso():
    return datetime.now(
        ZoneInfo(TIMEZONE)
    ).isoformat()


# Satu session persisten per group.
SESSIONS = {
    event["group"]:
    cffi_requests.Session(
        impersonate="chrome"
    )
    for event in EVENTS
}


def fetch_group(event):
    group = event["group"]
    checked_at = now_iso()

    try:
        response = SESSIONS[group].get(
            event["api_url"],
            timeout=20,
        )

        status = int(
            response.status_code
        )

        print(
            f"[{checked_at}] "
            f"{group} HTTP {status}"
        )

        if status != 200:
            return {
                "ok": False,
                "http_status": status,
                "checked_at": checked_at,
                "error": (
                    f"Origin HTTP {status}"
                ),
            }

        try:
            data = response.json()

        except Exception as exc:
            return {
                "ok": False,
                "http_status": 200,
                "checked_at": checked_at,
                "error": (
                    "Origin HTTP 200 "
                    "tetapi JSON invalid: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }

        return {
            "ok": True,
            "http_status": 200,
            "checked_at": checked_at,
            "data": data,
        }

    except Exception as exc:
        print(
            f"[{checked_at}] "
            f"{group} ERROR "
            f"{type(exc).__name__}: {exc}"
        )

        return {
            "ok": False,
            "http_status": None,
            "checked_at": checked_at,
            "error": (
                f"{type(exc).__name__}: {exc}"
            ),
        }


def send_snapshot(groups):
    payload = {
        "collector_id": COLLECTOR_ID,
        "collector_time": now_iso(),
        "groups": groups,
    }

    response = requests.post(
        DEPLEXO_INGEST_URL,
        json=payload,
        headers={
            "Authorization":
            f"Bearer {COLLECTOR_SECRET}",
            "Content-Type":
            "application/json",
            "User-Agent":
            "48Group-Local-Collector/1.0",
        },
        timeout=20,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Deplexo HTTP "
            f"{response.status_code}: "
            f"{response.text[:300]}"
        )

    return response.json()


def run_once():
    groups = {}

    for index, event in enumerate(EVENTS):
        groups[event["group"]] = fetch_group(
            event
        )

        # Jangan hit dua endpoint pada millisecond yang sama.
        if index < len(EVENTS) - 1:
            time.sleep(1)

    result = send_snapshot(
        groups
    )

    print(
        f"[{now_iso()}] "
        f"Snapshot -> Deplexo OK | "
        f"{result.get('groups')} | "
        f"restocks={result.get('restocks')}"
    )


def main():
    print(
        "48Group Local Collector aktif."
    )

    print(
        f"Target: {DEPLEXO_INGEST_URL}"
    )

    print(
        f"Collector ID: {COLLECTOR_ID}"
    )

    print(
        f"Interval: {COLLECTOR_INTERVAL}s"
    )

    while True:
        started = time.time()

        try:
            run_once()

        except KeyboardInterrupt:
            raise

        except Exception as exc:
            print(
                f"[{now_iso()}] "
                f"COLLECTOR ERROR: "
                f"{type(exc).__name__}: {exc}"
            )

        elapsed = (
            time.time()
            - started
        )

        sleep_for = max(
            1,
            COLLECTOR_INTERVAL
            - elapsed,
        )

        time.sleep(
            sleep_for
        )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print(
            "\nCollector dihentikan."
        )
