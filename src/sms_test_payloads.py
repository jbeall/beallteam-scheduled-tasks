"""Synthetic Dialpad SMS payloads for tests. Shapes mirror the real webhook log; data is fake."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

AGENT = {"id": 1, "type": "user", "name": "Alex Agent", "phone": "+15550000001"}
AGENT_2 = {"id": 2, "type": "user", "name": "Robin Agent", "phone": "+15550000002"}
CLIENT = {"name": "Pat Client", "phone": "+15551110001"}
CLIENT_2 = {"name": "Sam Client", "phone": "+15551110002"}


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


def direct(
    event_id: int,
    iso: str,
    text: str,
    *,
    direction: str = "inbound",
    status: str = "pending",
    contact: dict[str, str] = CLIENT,
    target: dict[str, Any] = AGENT,
    mms: bool = False,
) -> dict[str, Any]:
    outbound = direction == "outbound"
    return {
        "id": event_id,
        "created_date": ms(iso),
        "direction": direction,
        "target": target,
        "contact": {"id": 900, "type": "local", "name": contact["name"], "phone": contact["phone"]},
        "sender_id": target["id"] if outbound else None,
        "from_number": target["phone"] if outbound else contact["phone"],
        "to_number": [contact["phone"] if outbound else target["phone"]],
        "mms": mms,
        "is_internal": False,
        "message_status": status,
        "text": text,
        "text_content": "" if mms else text,
        "mms_url": text if mms else None,
    }


def group(
    event_id: int,
    iso: str,
    text: str,
    *,
    sender: dict[str, Any],
    target: dict[str, Any] = AGENT,
    members: list[dict[str, Any]] | None = None,
    slim: bool = False,
) -> dict[str, Any]:
    everyone = [AGENT, AGENT_2, CLIENT, CLIENT_2]
    members = members if members is not None else [p for p in everyone if p["phone"] != target["phone"]]
    outbound = sender["phone"] == target["phone"]
    payload: dict[str, Any] = {
        "id": event_id,
        "created_date": ms(iso),
        "direction": "outbound" if outbound else "inbound",
        "contact": {"id": 777, "type": "contact_group", "name": "Pat & Sam BTGT", "phone": None},
        "from_number": sender["phone"],
        "to_number": [m["phone"] for m in members] if outbound else [target["phone"]],
        "text": text,
    }
    if not slim:
        payload["contact"]["members"] = [
            {"id": 5, "type": m.get("type", "local"), "name": m["name"], "phone": m["phone"]}
            for m in members
        ]
        payload.update(
            target=target,
            sender_id=sender.get("id"),
            mms=False,
            is_internal=False,
            message_status="pending",
            text_content=text,
            mms_url=None,
        )
    return payload


def as_sheet_cell(payload: dict[str, Any]) -> str:
    """Encode the way Pabbly writes column B: a JSON string literal holding the JSON."""
    return json.dumps(json.dumps(payload))
