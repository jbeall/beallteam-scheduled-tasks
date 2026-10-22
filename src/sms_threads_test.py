"""Parsing, dedupe and threading of Dialpad SMS events."""

from datetime import date

from src import phones
from src.sms_events import SheetRow, decode_raw_json, parse_event, parse_rows
from src.sms_test_payloads import AGENT, AGENT_2, CLIENT, CLIENT_2, as_sheet_cell, direct, group
from src.sms_threads import build_threads, classify, conversation_days


def _events(*payloads):
    return [parse_event(p, source=f"t!{i}") for i, p in enumerate(payloads, start=2)]


def test_phones() -> None:
    assert phones.normalize("(555) 010-2111") == "+15550102111"
    assert phones.normalize("1-555-010-2111") == "+15550102111"
    assert phones.normalize("+44 20 7946 0958") == "+442079460958"
    assert phones.normalize("98900") == "98900"
    assert phones.normalize("") is None
    assert phones.is_shortcode("98900")
    assert not phones.is_shortcode("+15550102111")
    assert phones.pretty("+15550102111") == "(555) 010-2111"
    assert phones.looks_like_phone("(555) 010-0199")
    assert not phones.looks_like_phone("Pat Client")


def test_decode_double_encoded_cell_with_raw_newline() -> None:
    cell = as_sheet_cell(direct(1, "2026-10-21T20:00:00", "line one\nline two"))
    cell = cell.replace("\\\\n", "\n")  # Pabbly sometimes leaves a literal newline in the cell
    payload = decode_raw_json(cell)
    assert payload["text"] == "line one\nline two"


def test_parse_rows_reports_bad_rows_without_stopping() -> None:
    rows = [
        SheetRow("texts!2", None, as_sheet_cell(direct(1, "2026-10-21T20:00:00", "hi"))),
        SheetRow("texts!3", None, "{not json"),
        SheetRow("texts!4", None, None),
    ]
    events, errors = parse_rows(rows)
    assert [e.event_id for e in events] == [1]
    assert len(errors) == 1 and errors[0].startswith("texts!3")


def test_status_updates_collapse_to_one_message() -> None:
    copies = [
        direct(10, "2026-10-21T20:00:00", "Any updates?", direction="outbound", status=s)
        for s in ("unknown", "pending", "sent", "delivered")
    ]
    reply = direct(11, "2026-10-21T20:05:00", "Not yet")
    [thread] = build_threads(_events(*copies, reply))
    assert [m.text for m in thread.messages] == ["Any updates?", "Not yet"]
    assert len(thread.events) == 5
    assert thread.messages[0].sender_is_team and not thread.messages[1].sender_is_team
    assert [p.name for p in thread.contacts] == ["Pat Client"]


def test_group_copy_for_second_team_line_is_dropped() -> None:
    mine = group(20, "2026-10-21T22:06:23", "Showing tomorrow?", sender=AGENT)
    theirs = group(21, "2026-10-21T22:06:25", "Showing tomorrow?", sender=AGENT, target=AGENT_2)
    [thread] = build_threads(_events(mine, theirs))
    assert len(thread.messages) == 1
    assert {p.name for p in thread.team} == {"Alex Agent", "Robin Agent"}
    assert {p.name for p in thread.contacts} == {"Pat Client", "Sam Client"}


def test_repeated_text_on_same_line_is_kept() -> None:
    first = group(30, "2026-10-21T22:00:00", "Ok", sender=CLIENT)
    second = group(31, "2026-10-21T22:00:30", "Ok", sender=CLIENT)
    [thread] = build_threads(_events(first, second))
    assert [m.text for m in thread.messages] == ["Ok", "Ok"]


def test_slim_group_event_uses_members_from_rest_of_thread() -> None:
    full = group(40, "2026-10-21T22:00:00", "Offer is in", sender=AGENT)
    slim = group(41, "2026-10-21T22:10:00", "Great news!", sender=CLIENT_2, slim=True)
    [thread] = build_threads(_events(full, slim))
    assert thread.messages[1].sender.name == "Sam Client"
    assert {p.name for p in thread.contacts} == {"Pat Client", "Sam Client"}


def test_extra_team_numbers_are_not_contacts() -> None:
    assistant = {"name": "Jordan Assistant", "phone": "+15550000003"}
    event = group(
        50,
        "2026-10-21T22:00:00",
        "Docs signed",
        sender=assistant,
        members=[assistant, CLIENT],
    )
    [thread] = build_threads(_events(event), team_numbers=["(555) 000-0003"])
    assert [p.name for p in thread.contacts] == ["Pat Client"]
    assert thread.messages[0].sender_is_team


def test_classify_reactions_photos_and_text() -> None:
    assert classify("Liked “Okay, we have one buyer”", False) == ("reaction", "Okay, we have one buyer", "👍")
    assert classify("Laughed at “plant trees 😉”", False) == ("reaction", "plant trees 😉", "😂")
    ios = "\u200a\u200b👍\u200b a “\u200aWaiting on signatures\u200a”\u200a"
    assert classify(ios, False) == ("reaction", "Waiting on signatures", "👍")
    assert classify("❤️ a “See you Wed”", False) == ("reaction", "See you Wed", "❤️")
    assert classify("🤗🤗For the cottage or the main house?", False) == ("text", "🤗🤗For the cottage or the main house?", None)
    url = "https://content.dialpad.com/s/img/abc=s0"
    assert classify(url, True) == ("photo", "", None)
    assert classify(url, False) == ("photo", "", None)
    assert classify("Line 1\r\nLine 2", False) == ("text", "Line 1\nLine 2", None)


def test_skip_reasons() -> None:
    shortcode = direct(60, "2026-10-21T20:00:00", "Newsletter", contact={"name": "98900", "phone": "98900"})
    shortcode["contact"]["type"] = "shortcode"
    to_self = direct(61, "2026-10-21T20:00:00", "test", direction="outbound", contact={"name": "Me", "phone": "+15550000009"})
    threads = build_threads(_events(shortcode, to_self), team_numbers=["+15550000009"])
    reasons = {t.title: t.skip_reason for t in threads}
    assert reasons == {"98900": "automated sender (shortcode)", "Me": "team members only"}


def test_conversation_days_use_eastern_dates() -> None:
    evening = direct(70, "2026-10-22T03:30:00", "late text")  # 11:30 PM ET on Oct 21
    morning = direct(71, "2026-10-22T13:00:00", "morning text")  # 9:00 AM ET on Oct 22
    days = conversation_days(build_threads(_events(evening, morning)))
    assert [(d.day, [m.text for m in d.messages]) for d in days] == [
        (date(2026, 10, 21), ["late text"]),
        (date(2026, 10, 22), ["morning text"]),
    ]
    assert days[0].slug == "2026-10-21_pat-client_0001"


def test_international_numbers_keep_their_country_code() -> None:
    assert phones.normalize("+4791234567") == "+4791234567"
    assert phones.normalize("+6591234567") == "+6591234567"
    assert phones.normalize("5550102111") == "+15550102111"


def test_client_texting_two_team_lines_is_one_thread() -> None:
    first = direct(80, "2026-10-21T15:00:00", "Is the house still available?")
    second = direct(81, "2026-10-21T15:05:00", "Following up", target=AGENT_2)
    [thread] = build_threads(_events(first, second))
    assert [p.name for p in thread.team] == ["Alex Agent", "Robin Agent"]
    assert [p.name for p in thread.contacts] == ["Pat Client"]


def test_names_come_from_the_whole_batch_when_a_thread_only_has_slim_rows() -> None:
    named = direct(90, "2026-10-21T15:00:00", "Hi", contact=CLIENT_2)
    slim = direct(91, "2026-10-21T16:00:00", "Payment is $2,203.95")
    for key in ("target", "sender_id", "mms", "is_internal", "message_status", "text_content", "mms_url"):
        slim.pop(key)
    threads = build_threads(_events(named, slim))
    pat = next(t for t in threads if t.title == "Pat Client")
    assert [p.name for p in pat.team] == ["Alex Agent"]


def test_same_display_name_gets_the_number_added() -> None:
    own_cell = {"name": "Alex Agent", "phone": "+15550000009"}
    event = direct(95, "2026-10-21T15:00:00", "test", direction="outbound", contact=own_cell)
    [thread] = build_threads(_events(event), team_numbers=["+15550000009"])
    assert sorted(p.name for p in thread.team) == ["Alex Agent (555) 000-0001", "Alex Agent (555) 000-0009"]


def test_internal_text_marks_the_other_side_as_team() -> None:
    to_teammate = direct(96, "2026-10-21T15:00:00", "Can you cover my showing?", direction="outbound",
                         contact={"name": "Robin Agent", "phone": "+15550000002"})
    to_teammate["is_internal"] = True
    [thread] = build_threads(_events(to_teammate))
    assert thread.skip_reason == "team members only"


def test_mms_caption_comes_from_text_content() -> None:
    url = "https://content.dialpad.com/s/img/abc=s0"
    photo = direct(97, "2026-10-21T15:00:00", url, mms=True)
    photo["text_content"] = "Water stain on the ceiling - is that a problem?"
    [thread] = build_threads(_events(photo))
    message = thread.messages[0]
    assert (message.kind, message.text) == ("photo", "Water stain on the ceiling - is that a problem?")


def test_more_reaction_forms() -> None:
    assert classify("Removed a like from “Okay, we have one buyer”", False) == ("reaction", "Okay, we have one buyer", "removed")
    assert classify("Loved an image", False) == ("reaction", "an image", "❤️")
    assert classify("Reacted 😮 to “That was fast”", False) == ("reaction", "That was fast", "😮")
    assert classify('Reacted badly to "the appraisal"', False) == ("text", 'Reacted badly to "the appraisal"', None)
