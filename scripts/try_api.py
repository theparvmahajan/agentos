import sys
import time

import httpx

BASE = "http://localhost:8000"
DEFAULT = "Compare PostgreSQL and MySQL for a small startup and summarize the tradeoffs"
TERMINAL = {"completed", "failed", "cancelled"}


def main() -> None:
    objective = " ".join(sys.argv[1:]) or DEFAULT
    try:
        with httpx.Client(base_url=BASE, timeout=30) as client:
            resp = client.post("/missions", json={"objective": objective})
            resp.raise_for_status()
            mission = resp.json()
            print(f"created {mission['id']}  (HTTP {resp.status_code}, Location: {resp.headers['location']})")

            last = None
            while True:
                mission = client.get(f"/missions/{mission['id']}").json()
                snapshot = (mission["status"], tuple((t["key"], t["status"]) for t in mission["tasks"]))
                if snapshot != last:
                    tasks = " ".join(f"{t['key']}={t['status']}" for t in mission["tasks"])
                    print(f"{time.strftime('%H:%M:%S')}  mission={mission['status']}  {tasks}")
                    last = snapshot
                if mission["status"] in TERMINAL:
                    break
                time.sleep(2)
    except httpx.ConnectError:
        raise SystemExit("cannot reach the API: start it with `make serve`") from None

    print(f"\n--- {mission['status']} ---")
    print(mission["result"] or mission["error"])


if __name__ == "__main__":
    main()