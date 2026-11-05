"""Watch a mission live over the WebSocket, from the terminal.

    python -m scripts.try_stream "Compare PostgreSQL and MySQL for a small startup"
    python -m scripts.try_stream --mission 3f2c0c1e-....      # watch one that already exists

Needs the API, the scheduler and at least one worker running (see the README).
This is exactly what the dashboard will receive.
"""

import argparse
import json
import sys
import time

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

DEFAULT_OBJECTIVE = "Compare PostgreSQL and MySQL for a small startup"


def describe(message: dict) -> str:
    kind = message["type"]
    if kind == "snapshot":
        mission = message["mission"]
        tasks = ", ".join(f"{t['key']}={t['status']}" for t in mission["tasks"]) or "no tasks yet"
        return f"SNAPSHOT  mission {mission['status']}  [{tasks}]"
    if kind == "task":
        task = message["task"]
        extra = f"  (attempt {task['attempts']})" if task["status"] == "running" else ""
        if task["status"] == "failed":
            extra = f"  error: {task['error']}"
        return f"task      {task['key']:<12} {task['agent']:<10} -> {task['status']}{extra}"
    mission = message["mission"]
    return f"MISSION   -> {mission['status']}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch a mission over the WebSocket")
    parser.add_argument("objective", nargs="?", default=DEFAULT_OBJECTIVE)
    parser.add_argument("--mission", help="watch an existing mission id instead of creating one")
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    mission_id = args.mission
    if mission_id is None:
        response = httpx.post(f"{args.base_url}/api/v1/missions", json={"objective": args.objective})
        response.raise_for_status()
        mission_id = response.json()["id"]
        print(f"created mission {mission_id}")

    ws_url = args.base_url.replace("http", "ws", 1) + f"/api/v1/missions/{mission_id}/stream"
    started = time.time()
    last: dict | None = None
    with connect(ws_url) as ws:
        try:
            while True:
                message = json.loads(ws.recv())
                print(f"{time.time() - started:6.2f}s  {describe(message)}", flush=True)
                last = message
        except ConnectionClosed as closed:
            code = closed.rcvd.code if closed.rcvd else None
            print(f"stream closed (code {code})")
            if code == 4404:
                sys.exit("no such mission")

    if last and last["type"] == "mission" and last["mission"].get("result"):
        print("\nresult:\n" + last["mission"]["result"])


if __name__ == "__main__":
    main()
