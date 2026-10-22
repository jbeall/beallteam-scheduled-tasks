"""Rendering conversation-days into LLM input formats."""

import json

from src.sms_events import parse_event
from src.sms_formats import FORMAT_NAMES, render
from src.sms_prompts import PROMPT_NAMES, build_messages
from src.sms_test_payloads import AGENT, CLIENT, CLIENT_2, group
from src.sms_threads import build_threads, conversation_days


def _day():
    payloads = [
        group(1, "2026-10-22T17:48:00", "How long do we have to respond?", sender=CLIENT),
        group(2, "2026-10-22T17:51:00", "9:00 p.m. Sunday (Oct 25).", sender=AGENT),
        group(3, "2026-10-22T17:52:00", "Liked “9:00 p.m. Sunday (Oct 25).”", sender=CLIENT_2),
        group(4, "2026-10-22T17:53:00", "https://content.dialpad.com/s/img/x=s0", sender=CLIENT),
        group(5, "2026-10-22T17:54:00", "Line one\nLine two", sender=CLIENT),
    ]
    [day] = conversation_days(build_threads([parse_event(p) for p in payloads]))
    return day


def test_transcript() -> None:
    text = render("transcript", _day())
    assert 'Group text named "Pat & Sam BTGT"' in text
    assert "[1:48 PM] Pat Client: How long do we have to respond?" in text
    assert "[1:52 PM] Sam Client: [reacted 👍 to “9:00 p.m. Sunday (Oct 25).”]" in text
    assert "[1:53 PM] Pat Client: [sent a photo]" in text
    assert "Pat Client: Line one\n    Line two" in text


def test_compact_drops_reactions_and_photos_and_merges_turns() -> None:
    text = render("compact", _day())
    assert "reacted" not in text and "photo" not in text
    assert "Pat Client: Line one Line two" in text


def test_clean_json_is_valid() -> None:
    payload = json.loads(render("clean_json", _day()))
    assert payload["conversation"] == "group"
    assert payload["messages"][2] == {
        "time": "1:52 PM",
        "from": "Sam Client",
        "reaction": "👍",
        "to_message": "9:00 p.m. Sunday (Oct 25).",
    }
    assert payload["messages"][3]["photo"] is True


def test_raw_json_keeps_every_delivery() -> None:
    lines = render("raw_json", _day()).splitlines()
    assert len(lines) == 5 and json.loads(lines[0])["id"] == 1


def test_person_focus_marks_the_crm_person() -> None:
    text = render("person_focus", _day())
    assert text.startswith("This note will be saved on the CRM record of: Pat Client (555) 111-0001")
    assert "PAT CLIENT (this contact): How long" in text
    assert "Alex Agent (Beall Team): 9:00 p.m." in text


def test_every_format_and_prompt_builds() -> None:
    day = _day()
    for format_name in FORMAT_NAMES:
        for prompt_name in PROMPT_NAMES:
            messages = build_messages(prompt_name, format_name, render(format_name, day), person_name="Pat Client")
            assert [m["role"] for m in messages] == ["system", "user"]
            assert "Pat Client's record" in messages[1]["content"]
