"""Render one conversation-day as LLM input, in several formats to compare.

raw_json      every webhook delivery exactly as logged (baseline)
clean_json    deduped messages with only the fields that matter
transcript    readable chat log with times and a participant roster
compact       fewest tokens: no times, reactions/photos dropped, same-speaker turns merged
person_focus  transcript relabelled around the one CRM person the note is for
"""

from __future__ import annotations

import json
import re

from src import phones
from src.sms_events import Party
from src.sms_threads import ConversationDay, Message

FORMAT_NAMES = ("raw_json", "clean_json", "transcript", "compact", "person_focus")
TEAM_LABEL = "Beall Team"

_HAS_WORD = re.compile(r"\w")


def render(format_name: str, day: ConversationDay, person: Party | None = None) -> str:
    if format_name == "raw_json":
        return render_raw_json(day)
    if format_name == "clean_json":
        return render_clean_json(day)
    if format_name == "transcript":
        return render_transcript(day)
    if format_name == "compact":
        return render_compact(day)
    if format_name == "person_focus":
        return render_person_focus(day, person or focus_person(day))
    raise ValueError(f"Unknown format {format_name!r}; choose from {', '.join(FORMAT_NAMES)}")


def focus_person(day: ConversationDay) -> Party:
    """Default person for person_focus: the contact who sent the most messages that day."""
    contacts = day.thread.contacts
    counts = {p.phone: 0 for p in contacts}
    for message in day.messages:
        if message.sender.phone in counts:
            counts[message.sender.phone] += 1
    return max(contacts, key=lambda p: counts[p.phone]) if contacts else day.thread.team[0]


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / 4))


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _time(message: Message) -> str:
    return message.sent_at_et.strftime("%I:%M %p").lstrip("0")


def _body(message: Message) -> str:
    if message.kind == "photo":
        return "[sent a photo]" + (f" {message.text}" if message.text else "")
    if message.kind == "reaction":
        return f"[reacted {message.reaction} to “{_clip(message.text, 80)}”]"
    return message.text


def _indent_continuation(text: str) -> str:
    return text.replace("\n", "\n    ")


def _contact_label(party: Party) -> str:
    if phones.looks_like_phone(party.name):
        return f"{party.name} (name unknown)"
    return f"{party.name} {phones.pretty(party.phone)}".strip()


def _header(day: ConversationDay) -> list[str]:
    thread = day.thread
    lines = [f"Text messages from {day.day:%A, %B} {day.day.day}, {day.day.year} (Eastern time)"]
    if thread.is_group:
        lines.append(f'Group text named "{thread.title}"')
    else:
        lines.append("One-on-one text conversation")
    lines.append(f"{TEAM_LABEL} (real estate agents/staff): " + ", ".join(p.name for p in thread.team))
    lines.append("Contacts: " + ", ".join(_contact_label(p) for p in thread.contacts))
    return lines


def render_raw_json(day: ConversationDay) -> str:
    return "\n".join(json.dumps(event.raw, ensure_ascii=False) for event in day.events)


def render_clean_json(day: ConversationDay) -> str:
    thread = day.thread
    messages = []
    for message in day.messages:
        item: dict[str, object] = {"time": _time(message), "from": message.sender.name}
        if message.kind == "photo":
            item["photo"] = True
            if message.text:
                item["text"] = message.text
        elif message.kind == "reaction":
            item["reaction"] = message.reaction
            item["to_message"] = _clip(message.text, 80)
        else:
            item["text"] = message.text
        messages.append(item)
    payload = {
        "date": day.day.isoformat(),
        "timezone": "America/New_York",
        "conversation": "group" if thread.is_group else "one-on-one",
        **({"group_name": thread.title} if thread.is_group else {}),
        "team": [{"name": p.name, "phone": p.phone} for p in thread.team],
        "contacts": [{"name": p.name, "phone": p.phone} for p in thread.contacts],
        "messages": messages,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_transcript(day: ConversationDay) -> str:
    lines = _header(day) + [""]
    for message in day.messages:
        lines.append(f"[{_time(message)}] {message.sender.name}: {_indent_continuation(_body(message))}")
    return "\n".join(lines)


def render_compact(day: ConversationDay) -> str:
    thread = day.thread
    team = ", ".join(p.name for p in thread.team)
    contacts = ", ".join(p.name for p in thread.contacts)
    kind = "Group text" if thread.is_group else "Texts"
    lines = [f"{kind} on {day.day:%b} {day.day.day}, {day.day.year} between the {TEAM_LABEL} ({team}) and {contacts}.", ""]
    turns: list[tuple[str, list[str]]] = []
    for message in day.messages:
        if message.kind != "text" or not _HAS_WORD.search(message.text):
            continue
        text = " ".join(message.text.split())
        if turns and turns[-1][0] == message.sender.name:
            turns[-1][1].append(text)
        else:
            turns.append((message.sender.name, [text]))
    if not turns:
        lines.append("(no text content - only reactions, photos or emoji)")
    lines.extend(f"{speaker}: {' / '.join(texts)}" for speaker, texts in turns)
    return "\n".join(lines)


def render_person_focus(day: ConversationDay, person: Party) -> str:
    thread = day.thread
    others = [p for p in thread.contacts if p.phone != person.phone]
    lines = [
        f"This note will be saved on the CRM record of: {_contact_label(person)}",
        f"Text messages from {day.day:%A, %B} {day.day.day}, {day.day.year} (Eastern time)",
    ]
    if thread.is_group:
        lines.append(
            f"Group text with {person.name}"
            + (", " + ", ".join(p.name for p in others) if others else "")
            + f" and the {TEAM_LABEL} (" + ", ".join(p.name for p in thread.team) + ")"
        )
    else:
        lines.append(f"One-on-one texts with the {TEAM_LABEL} (" + ", ".join(p.name for p in thread.team) + ")")
    lines.append("")
    for message in day.messages:
        if message.sender.phone == person.phone:
            who = f"{message.sender.name.upper()} (this contact)"
        elif message.sender_is_team:
            who = f"{message.sender.name} ({TEAM_LABEL})"
        else:
            who = message.sender.name
        lines.append(f"[{_time(message)}] {who}: {_indent_continuation(_body(message))}")
    return "\n".join(lines)
