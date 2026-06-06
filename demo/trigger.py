"""
Demo trigger — fires a fake Jira webhook at the local Anton server.

Usage:
    python demo/trigger.py
    python demo/trigger.py --ticket BUG-99 --port 8000
"""

import argparse
import json
import httpx


DEMO_PAYLOAD = {
    "webhookEvent": "jira:issue_created",
    "issue": {
        "key": "BUG-42",
        "fields": {
            "summary": "Completing a todo un-completes it on second call",
            "description": (
                "BUG REPORT — P1 Critical\n\n"
                "When a user marks a todo as complete, it works the first time. "
                "But calling complete() a second time on the same todo reverts it back to incomplete. "
                "Root cause appears to be in todos/service.py — the complete() method uses "
                "`not todo.completed` (toggle) instead of always setting `todo.completed = True`.\n\n"
                "Steps to reproduce:\n"
                "1. Create a todo\n"
                "2. Mark it complete (POST /todos/1/complete)\n"
                "3. Mark it complete again\n"
                "4. Observe: todo.completed is now False\n\n"
                "CI is failing: test_complete_is_idempotent"
            ),
            "priority": {"name": "Critical"},
            "status": {"name": "Open"},
            "reporter": {"displayName": "Alex Chen"},
            "labels": ["bug", "p1", "todo-service"],
            "components": [{"name": "todo-service"}],
        },
    },
}


def main():
    parser = argparse.ArgumentParser(description="Fire a demo Jira webhook")
    parser.add_argument("--ticket", default="BUG-42", help="Jira ticket key")
    parser.add_argument("--port", default=8000, type=int, help="Port Anton is running on")
    args = parser.parse_args()

    payload = DEMO_PAYLOAD.copy()
    payload["issue"]["key"] = args.ticket

    url = f"http://localhost:{args.port}/webhook/jira"
    print(f"\n🚀  Firing Jira webhook → {url}")
    print(f"    Ticket : {args.ticket}")
    print(f"    Summary: {payload['issue']['fields']['summary']}\n")

    try:
        resp = httpx.post(url, json=payload, timeout=5)
        print(f"✅  Server accepted — HTTP {resp.status_code}")
    except httpx.ConnectError:
        print(f"❌  Could not connect. Is Anton running?  (uvicorn main:app --port {args.port})")


if __name__ == "__main__":
    main()
