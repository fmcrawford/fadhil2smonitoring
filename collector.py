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

COLLECTOR_INTERVAL = int(
    os.getenv(
        "COLLECTOR_INTERVAL",
        "60",
    )
)

COLLECTOR_403_BACKOFF = int(
    os.getenv(
        "COLLECTOR_403_BACKOFF",
        "180",
    )
)

COLLECTOR_GROUP_BACKOFF_MAX = int(
    os.getenv(
        "COLLECTOR_GROUP_BACKOFF_MAX",
        "900",
    )
)

TIMEZONE = "Asia/Jakarta"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://jkt48.com/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


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


def now_iso():
    return datetime.now(
        ZoneInfo(TIMEZONE)
    ).isoformat()


# Session dan cooldown dipisah per source.
GROUP_STATE = {
    event["group"]: {
        "session": None,
        "warmed": False,
        "failures": 0,
        "retry_at": 0.0,
    }
    for event in EVENTS
}


def new_group_session(group):
    state = GROUP_STATE[group]
    state["session"] = cffi_requests.Session(
        impersonate="chrome"
    )
    state["warmed"] = False
    return state["session"]


def warm_up_group(group):
    state = GROUP_STATE[group]

    if state["session"] is None:
        new_group_session(group)

    if state["warmed"]:
        return

    checked_at = now_iso()

    try:
        response = state["session"].get(
            "https://jkt48.com/",
            headers=HEADERS,
            timeout=20,
        )

        print(
            f"[{checked_at}] "
            f"{group} WARMUP HTTP "
            f"{response.status_code}"
        )

        state["warmed"] = True

    except Exception as exc:
        print(
            f"[{checked_at}] "
            f"{group} WARMUP ERROR "
            f"{type(exc).__name__}: {exc}"
        )


for _event in EVENTS:
    new_group_session(
        _event["group"]
    )


def fetch_group(event):
    group = event["group"]
    checked_at = now_iso()
    state = GROUP_STATE[group]
    now_ts = time.time()

    if now_ts < state["retry_at"]:
        retry_in = max(
            1,
            int(state["retry_at"] - now_ts),
        )

        print(
            f"[{checked_at}] "
            f"{group} COOLDOWN "
            f"{retry_in}s"
        )

        return {
            "ok": False,
            "retrying": True,
            "http_status": 403,
            "checked_at": checked_at,
            "retry_in": retry_in,
            "error": (
                "Cooldown setelah Origin HTTP 403"
            ),
        }

    try:
        warm_up_group(group)

        response = state["session"].get(
            event["api_url"],
            headers=HEADERS,
            timeout=20,
        )

        status = int(
            response.status_code
        )

        print(
            f"[{checked_at}] "
            f"{group} HTTP {status}"
        )

        if status == 403:
            state["failures"] += 1

            delay = min(
                COLLECTOR_403_BACKOFF
                * (2 ** (state["failures"] - 1)),
                COLLECTOR_GROUP_BACKOFF_MAX,
            )

            state["retry_at"] = (
                time.time() + delay
            )

            new_group_session(group)

            print(
                f"[{checked_at}] "
                f"{group} backoff {delay}s "
                f"(403 strike {state['failures']})"
            )

            return {
                "ok": False,
                "retrying": False,
                "http_status": 403,
                "checked_at": checked_at,
                "retry_in": delay,
                "error": "Origin HTTP 403",
            }

        if status != 200:
            return {
                "ok": False,
                "retrying": False,
                "http_status": status,
                "checked_at": checked_at,
                "retry_in": 0,
                "error": (
                    f"Origin HTTP {status}"
                ),
            }

        try:
            data = response.json()

        except Exception as exc:
            return {
                "ok": False,
                "retrying": False,
                "http_status": 200,
                "checked_at": checked_at,
                "retry_in": 0,
                "error": (
                    "Origin HTTP 200 "
                    "tetapi JSON invalid: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }

        state["failures"] = 0
        state["retry_at"] = 0.0

        return {
            "ok": True,
            "http_status": 200,
            "checked_at": checked_at,
            "retry_in": 0,
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
            "retrying": False,
            "http_status": None,
            "checked_at": checked_at,
            "retry_in": 0,
            "error": (
                f"{type(exc).__name__}: {exc}"
            ),
        }


def send_snapshot(groups):
    payload = {
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

        # Beri jarak antarsource agar pola request
        # tidak terlalu bursty.
        if index < len(EVENTS) - 1:
            time.sleep(2)

    result = send_snapshot(
        groups
    )

    print(
        f"[{now_iso()}] "
        f"Snapshot -> backend OK | "
        f"{result.get('groups')} | "
        f"restocks={result.get('restocks')}"
    )

    return {
        "group_count": len(groups),
    }


def main():
    print(
        "48Group Local Collector aktif."
    )

    print(
        f"Target: {DEPLEXO_INGEST_URL}"
    )

    print(
        f"Interval: {COLLECTOR_INTERVAL}s"
    )

    print(
        f"403 base backoff: "
        f"{COLLECTOR_403_BACKOFF}s"
    )

    print(
        f"403 max backoff: "
        f"{COLLECTOR_GROUP_BACKOFF_MAX}s"
    )

    while True:
        started = time.time()
        cycle = None

        try:
            cycle = run_once()

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
