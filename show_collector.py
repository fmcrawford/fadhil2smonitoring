import os
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from curl_cffi import requests as cffi_requests


SHOW_INGEST_URL = os.getenv("SHOW_INGEST_URL", "").strip()
COLLECTOR_SECRET = os.getenv("COLLECTOR_SECRET", "").strip()
SHOW_REFRESH_INTERVAL = int(
    os.getenv("SHOW_REFRESH_INTERVAL", "900")
)
SHOW_DAYS_AHEAD = int(
    os.getenv("SHOW_DAYS_AHEAD", "14")
)
TIMEZONE = "Asia/Jakarta"

if not SHOW_INGEST_URL:
    print("ERROR: SHOW_INGEST_URL belum di-set.")
    sys.exit(1)

if not COLLECTOR_SECRET:
    print("ERROR: COLLECTOR_SECRET belum di-set.")
    sys.exit(1)

SESSION = cffi_requests.Session(impersonate="chrome")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://jkt48.com/",
}


def now_iso():
    return datetime.now(
        ZoneInfo(TIMEZONE)
    ).isoformat()


def month_sequence(start_date, end_date):
    result = []
    cursor = date(
        start_date.year,
        start_date.month,
        1,
    )

    while cursor <= end_date:
        result.append(
            (cursor.month, cursor.year)
        )

        if cursor.month == 12:
            cursor = date(
                cursor.year + 1,
                1,
                1,
            )
        else:
            cursor = date(
                cursor.year,
                cursor.month + 1,
                1,
            )

    return result


def fetch_show_schedule():
    checked_at = now_iso()
    now = datetime.now(
        ZoneInfo(TIMEZONE)
    )
    window_start = now.date()
    window_end = (
        window_start
        + timedelta(days=SHOW_DAYS_AHEAD)
    )
    shows_by_ref = {}

    for month, year in month_sequence(
        window_start,
        window_end,
    ):
        url = (
            "https://jkt48.com/api/v1/schedules"
            f"?lang=id&month={month}&year={year}"
        )

        response = SESSION.get(
            url,
            headers=HEADERS,
            timeout=20,
        )

        print(
            f"[{checked_at}] "
            f"SCHEDULE {month}/{year} "
            f"HTTP {response.status_code}"
        )

        if int(response.status_code) != 200:
            raise RuntimeError(
                f"Schedule HTTP "
                f"{response.status_code} "
                f"untuk {month}/{year}"
            )

        payload = response.json()

        if not payload.get("status"):
            continue

        for show in payload.get("data", []):
            if show.get("type") != "SHOW":
                continue

            ref_code = str(
                show.get("reference_code")
                or ""
            ).strip()

            show_date_str = str(
                show.get("date")
                or ""
            ).strip()

            if not ref_code or not show_date_str:
                continue

            try:
                show_date = datetime.strptime(
                    show_date_str,
                    "%Y-%m-%d",
                ).date()
            except ValueError:
                continue

            if not (
                window_start
                <= show_date
                <= window_end
            ):
                continue

            detail_data = {}

            detail_url = (
                "https://jkt48.com/api/v1/"
                "theater-shows/"
                f"{ref_code}?lang=id"
            )

            detail_res = SESSION.get(
                detail_url,
                headers=HEADERS,
                timeout=20,
            )

            print(
                f"[{checked_at}] "
                f"SHOW {ref_code} "
                f"HTTP {detail_res.status_code}"
            )

            if int(detail_res.status_code) == 200:
                try:
                    detail_json = detail_res.json()

                    if detail_json.get("status"):
                        detail_data = (
                            detail_json.get("data", {})
                        )
                except Exception:
                    detail_data = {}

            members = []

            for member in detail_data.get(
                "jkt48_member",
                [],
            ):
                name = str(
                    member.get("name")
                    or ""
                ).strip()

                if (
                    name
                    and name not in members
                ):
                    members.append(name)

            schedule_id = show.get(
                "schedule_id"
            )

            show_url = (
                "https://jkt48.com/theater/"
                f"schedule/id/{schedule_id}"
                if schedule_id
                else ""
            )

            shows_by_ref[ref_code] = {
                "reference_code": ref_code,
                "schedule_id": schedule_id,
                "title": (
                    show.get("title")
                    or detail_data.get("title")
                    or "Theater Show"
                ),
                "date": show_date_str,
                "start_time": show.get(
                    "start_time"
                ),
                "end_time": show.get(
                    "end_time"
                ),
                "member_type": (
                    show.get(
                        "jkt48_member_type"
                    )
                    or "SHOW"
                ),
                "members": members,
                "show_url": show_url,
            }

            time.sleep(0.2)

    items = sorted(
        shows_by_ref.values(),
        key=lambda item: (
            item.get("date") or "",
            item.get("start_time") or "",
            item.get("title") or "",
        ),
    )

    return {
        "ok": True,
        "checked_at": checked_at,
        "window_start": (
            window_start.isoformat()
        ),
        "window_end": (
            window_end.isoformat()
        ),
        "items": items,
    }


def send_snapshot(payload):
    response = requests.post(
        SHOW_INGEST_URL,
        json=payload,
        headers={
            "Authorization": (
                f"Bearer {COLLECTOR_SECRET}"
            ),
            "Content-Type": (
                "application/json"
            ),
            "User-Agent": (
                "48Group-Show-Collector/1.0"
            ),
        },
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Backend HTTP "
            f"{response.status_code}: "
            f"{response.text[:300]}"
        )

    return response.json()


def run_once():
    checked_at = now_iso()

    try:
        payload = fetch_show_schedule()
    except Exception as exc:
        payload = {
            "ok": False,
            "checked_at": checked_at,
            "error": (
                f"{type(exc).__name__}: "
                f"{exc}"
            ),
        }

    result = send_snapshot(payload)

    print(
        f"[{now_iso()}] "
        "Show snapshot -> backend OK | "
        f"{result}"
    )


def main():
    print("48Group Show Collector aktif.")
    print(f"Target: {SHOW_INGEST_URL}")
    print(
        f"Refresh: "
        f"{SHOW_REFRESH_INTERVAL}s"
    )
    print(
        f"Window: {SHOW_DAYS_AHEAD} hari"
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
                "SHOW COLLECTOR ERROR: "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

        elapsed = (
            time.time() - started
        )

        sleep_for = max(
            5,
            SHOW_REFRESH_INTERVAL
            - elapsed,
        )

        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nShow Collector dihentikan."
        )
