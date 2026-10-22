"""Rendering conversation-days into LLM input formats."""

import json

from src.sms_events import parse_event
from src.sms_formats import FORMAT_NAMES, render
from src.sms_prompts import PROMPT_NAMES, build_messages, is_nothing_to_log
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
    assert text.startswith("Date: Thursday, October 22, 2026 (Eastern time)\nCalendar: Wed Oct 21 (yesterday), Thu Oct 22 (today), Fri Oct 23,")
    assert 'Group text named "Pat & Sam BTGT"' in text
    assert "[1:48 PM] Pat Client: How long do we have to respond?" in text
    assert "[1:52 PM] Sam Client: [reacted 👍 to Alex Agent's 1:51 PM message]" in text
    assert "[1:53 PM] Pat Client: [sent a photo]" in text
    assert "Pat Client: Line one\n    Line two" in text


def test_compact_shortens_reactions_and_photos_and_merges_turns() -> None:
    text = render("compact", _day())
    assert "Sam Client: (reacted 👍)" in text
    assert "Pat Client: [photo] / Line one / Line two" in text


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
    assert "Pat Client (this contact): How long" in text
    assert "Alex Agent (Beall Team): 9:00 p.m." in text
    assert "Sam Client (other contact): [reacted" in text


def test_every_format_and_prompt_builds() -> None:
    day = _day()
    for format_name in FORMAT_NAMES:
        for prompt_name in PROMPT_NAMES:
            messages = build_messages(prompt_name, format_name, render(format_name, day), person_name="Pat Client")
            assert [m["role"] for m in messages] == ["system", "user"]
            assert "Pat Client's record" in messages[1]["content"]


def test_person_focus_without_contacts() -> None:
    from src.sms_test_payloads import direct

    to_self = direct(9, "2026-10-22T17:00:00", "test", direction="outbound", contact={"name": "Me", "phone": "+15550000009"})
    [day] = conversation_days(build_threads([parse_event(to_self)], team_numbers=["+15550000009"]))
    assert render("person_focus", day) == "(no contact in this conversation to write a note for)"


def test_silent_group_member_is_called_out() -> None:
    day = _day()
    robin = next(p for p in day.thread.team if p.name == "Robin Agent")
    assert "Contacts: Pat Client (555) 111-0001, Sam Client (555) 111-0002" in render("transcript", day)
    quiet = [group(1, "2026-10-22T17:48:00", "Inspection is at 9", sender=AGENT)]
    [quiet_day] = conversation_days(build_threads([parse_event(p) for p in quiet]))
    text = render("transcript", quiet_day)
    assert "Pat Client (555) 111-0001 (sent no messages today)" in text
    sam = next(p for p in quiet_day.thread.contacts if p.name == "Sam Client")
    assert "Sam Client was on the group text but sent no messages today." in render("person_focus", quiet_day, sam)
    assert robin.name == "Robin Agent"


def test_auto_named_group_title_is_hidden() -> None:
    payload = group(1, "2026-10-22T17:48:00", "Hi", sender=AGENT)
    payload["contact"]["name"] = "Pat, Sam, Alex"
    [day] = conversation_days(build_threads([parse_event(payload)]))
    text = render("transcript", day)
    assert "\nGroup text\n" in text and "named" not in text


def test_realscout_links_are_decoded() -> None:
    from src.sms_formats import describe_links

    url = "https://agent.realscout.com/homesearch/listings/p-12-main-st-springfield-12345-brightmls-612"
    assert describe_links(f"Look at {url} today") == "Look at [RealScout listing link: 12 main st springfield 12345] today"
    assert describe_links("https://example.com/x") == "https://example.com/x"


def test_earlier_days_are_shown_as_context_only() -> None:
    payloads = [
        group(1, "2026-10-21T22:00:00", "Showing request for Wed at 5", sender=AGENT),
        group(2, "2026-10-22T13:00:00", "Liked “Showing request for Wed at 5”", sender=CLIENT),
        group(3, "2026-10-22T13:01:00", "Can the photographer come at 1?", sender=CLIENT_2),
    ]
    days = conversation_days(build_threads([parse_event(p) for p in payloads]))
    text = render("transcript", days[1])
    assert "Earlier in this conversation (context only, already logged):\n[Wed Oct 21 6:00 PM] Alex Agent: Showing request for Wed at 5" in text
    assert "Today:\n[9:00 AM] Pat Client: [reacted 👍 to Alex Agent's Wed Oct 21 6:00 PM message]" in text
    assert "Earlier" not in render("transcript", days[0])


def test_nothing_to_log_detection_is_lenient() -> None:
    assert is_nothing_to_log("NOTHING TO LOG")
    assert is_nothing_to_log("**Nothing to log.**")
    assert not is_nothing_to_log("Nothing to log about the HOA, but Pat asked about fees.")
