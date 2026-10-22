"""Render one conversation-day as LLM input, in several formats to compare.

raw_json      every webhook delivery exactly as logged (baseline)
clean_json    deduped messages with only the fields that matter
transcript    readable chat log with times, a date/calendar header and the participant roster
compact       fewest tokens: no times, same-speaker turns merged, reactions shortened
person_focus  transcript relabelled around the one CRM person the note is for

Every derived format (all but raw_json) names the weekday and the coming week's dates so the
model can turn "Monday" or "next Wed" into calendar dates, and decodes RealScout listing links
into their address. transcript and person_focus also show the last few messages from earlier
days, marked as context only.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from src import phones
from src.sms_events import Party
from src.sms_threads import REMOVED_REACTION, ConversationDay, Message

FORMAT_NAMES = ("raw_json", "clean_json", "transcript", "compact", "person_focus")
TEAM_LABEL = "Beall Team"
CONTEXT_MESSAGES = 5

_REALSCOUT = re.compile(
    r"https?://[\w.-]*realscout\.com/\S*?/listings/p-(?P<slug>[a-z0-9-]+?)(?:-[a-z]*mls-\d+)?(?=[/?#\s]|$)\S*",
    re.I,
)
_ATTACHMENT = re.compile(r"^an? (?:image|photo|video|movie|attachment|audio message)$")


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
        person = person or focus_person(day)
        if person is None:
            return "(no contact in this conversation to write a note for)"
        return render_person_focus(day, person)
    raise ValueError(f"Unknown format {format_name!r}; choose from {', '.join(FORMAT_NAMES)}")


def focus_person(day: ConversationDay) -> Party | None:
    """Default person for person_focus: the contact who sent the most messages that day."""
    contacts = day.thread.contacts
    counts = {p.phone: 0 for p in contacts}
    for message in day.messages:
        if message.sender.phone in counts and message.kind != "reaction":
            counts[message.sender.phone] += 1
    return max(contacts, key=lambda p: counts[p.phone]) if contacts else None


def sent_messages(day: ConversationDay, party: Party) -> bool:
    return any(m.sender.phone == party.phone for m in day.messages)


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / 4))


def describe_links(text: str) -> str:
    """Replace RealScout listing URLs with the address in their slug."""
    return _REALSCOUT.sub(lambda m: f"[RealScout listing link: {m['slug'].replace('-', ' ')}]", text)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _time(message: Message) -> str:
    return message.sent_at_et.strftime("%I:%M %p").lstrip("0")


def _short_date(d: date) -> str:
    return f"{d:%a %b} {d.day}"


def _long_date(d: date) -> str:
    return f"{d:%A, %B} {d.day}, {d.year}"


def _date_lines(day: date) -> list[str]:
    calendar = [f"{_short_date(day - timedelta(days=1))} (yesterday)", f"{_short_date(day)} (today)"]
    calendar += [_short_date(day + timedelta(days=offset)) for offset in range(1, 8)]
    return [f"Date: {_long_date(day)} (Eastern time)", "Calendar: " + ", ".join(calendar)]


def _conversation_line(day: ConversationDay) -> str:
    thread = day.thread
    if not thread.is_group:
        return "One-on-one text conversation"
    # Dialpad auto-names groups with members' first names; those names add nothing.
    first_names = {p.name.split()[0].lower() for p in thread.participants.values() if p.name}
    tokens = [t.strip() for t in thread.title.split(",") if t.strip()]
    auto_named = all(t.lower() in first_names or phones.looks_like_phone(t) for t in tokens)
    return "Group text" if auto_named or not tokens else f'Group text named "{thread.title}"'


def _contact_label(party: Party, day: ConversationDay | None = None) -> str:
    notes = []
    if phones.looks_like_phone(party.name):
        label = party.name
        notes.append("no name on file")
    else:
        label = f"{party.name} {phones.pretty(party.phone)}".strip()
    if day is not None and day.thread.is_group and not sent_messages(day, party):
        notes.append("sent no messages today")
    return f"{label} ({'; '.join(notes)})" if notes else label


def _header(day: ConversationDay) -> list[str]:
    thread = day.thread
    return _date_lines(day.day) + [
        _conversation_line(day),
        f"{TEAM_LABEL}: " + ", ".join(p.name for p in thread.team),
        "Contacts: " + ", ".join(_contact_label(p, day) for p in thread.contacts),
    ]


def _earlier(day: ConversationDay) -> list[Message]:
    return [m for m in day.thread.messages if m.sent_at_et.date() < day.day][-CONTEXT_MESSAGES:]


def _stamp(message: Message, day: date) -> str:
    when = _time(message)
    return when if message.sent_at_et.date() == day else f"{_short_date(message.sent_at_et.date())} {when}"


def _reacted_to(message: Message, before: list[Message], day: date) -> str:
    quoted = " ".join(message.text.split()).rstrip("…").rstrip()
    if _ATTACHMENT.match(quoted):
        return quoted
    if quoted:
        for earlier in reversed(before):
            if earlier.kind == "text" and " ".join(earlier.text.split()).startswith(quoted):
                return f"{earlier.sender.name}'s {_stamp(earlier, day)} message"
    return f"an earlier message: “{_clip(describe_links(message.text), 120)}”"


def _body(message: Message, before: list[Message], day: date) -> str:
    if message.kind == "photo":
        return "[sent a photo]" + (f" {describe_links(message.text)}" if message.text else "")
    if message.kind == "reaction":
        target = _reacted_to(message, before, day)
        if message.reaction == REMOVED_REACTION:
            return f"[removed a reaction from {target}]"
        return f"[reacted {message.reaction} to {target}]"
    return describe_links(message.text)


def _chat_lines(day: ConversationDay, speaker) -> list[str]:
    """Earlier context (if any) then today's messages, one line each."""
    today = day.day
    earlier = _earlier(day)
    lines: list[str] = []
    if earlier:
        lines += ["Earlier in this conversation (context only, already logged):"]
        for index, message in enumerate(earlier):
            body = _body(message, earlier[:index], today).replace("\n", "\n    ")
            lines.append(f"[{_stamp(message, today)}] {speaker(message)}: {body}")
        lines += ["", "Today:"]
    shown = list(earlier)
    for message in day.messages:
        body = _body(message, shown, today).replace("\n", "\n    ")
        lines.append(f"[{_time(message)}] {speaker(message)}: {body}")
        shown.append(message)
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
                item["text"] = describe_links(message.text)
        elif message.kind == "reaction":
            item["reaction"] = message.reaction
            item["to_message"] = describe_links(message.text)
        else:
            item["text"] = describe_links(message.text)
        messages.append(item)
    payload = {
        "date": day.day.isoformat(),
        "weekday": f"{day.day:%A}",
        "timezone": "America/New_York",
        "next_7_days": [_short_date(day.day + timedelta(days=n)) for n in range(1, 8)],
        "conversation": "group" if thread.is_group else "one-on-one",
        **({"group_name": thread.title} if _conversation_line(day).startswith("Group text named") else {}),
        "team": [{"name": p.name, "phone": p.phone} for p in thread.team],
        "contacts": [
            {"name": p.name, "phone": p.phone, "sent_messages_today": sent_messages(day, p)}
            for p in thread.contacts
        ],
        "messages": messages,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_transcript(day: ConversationDay) -> str:
    return "\n".join(_header(day) + [""] + _chat_lines(day, lambda m: m.sender.name))


def render_compact(day: ConversationDay) -> str:
    thread = day.thread
    team = ", ".join(p.name for p in thread.team)
    contacts = ", ".join(p.name for p in thread.contacts)
    kind = "Group text" if thread.is_group else "Texts"
    lines = [
        f"{kind} on {_long_date(day.day)} between the {TEAM_LABEL} ({team}) and {contacts}.",
        _date_lines(day.day)[1],
        "",
    ]
    turns: list[tuple[str, list[str]]] = []
    for message in day.messages:
        if message.kind == "reaction":
            if message.reaction == REMOVED_REACTION:
                continue
            text = f"(reacted {message.reaction})"
        elif message.kind == "photo":
            text = "[photo]" + (f" {describe_links(message.text)}" if message.text else "")
        elif message.text:
            text = describe_links(message.text)
        else:
            continue
        text = " / ".join(line.strip() for line in text.splitlines() if line.strip())
        if turns and turns[-1][0] == message.sender.name:
            turns[-1][1].append(text)
        else:
            turns.append((message.sender.name, [text]))
    if not turns:
        lines.append("(no messages)")
    lines.extend(f"{speaker}: {' / '.join(texts)}" for speaker, texts in turns)
    return "\n".join(lines)


def render_person_focus(day: ConversationDay, person: Party) -> str:
    thread = day.thread
    others = [p for p in thread.contacts if p.phone != person.phone]
    team = ", ".join(p.name for p in thread.team)
    lines = [f"This note will be saved on the CRM record of: {_contact_label(person)}"]
    lines += _date_lines(day.day)
    if thread.is_group:
        lines.append(
            f"Group text with {person.name}"
            + (", " + ", ".join(p.name for p in others) if others else "")
            + f" and the {TEAM_LABEL} ({team})"
        )
        if not sent_messages(day, person):
            lines.append(f"{person.name} was on the group text but sent no messages today.")
    else:
        lines.append(f"One-on-one texts with the {TEAM_LABEL} ({team})")

    def speaker(message: Message) -> str:
        if message.sender.phone == person.phone:
            return f"{message.sender.name} (this contact)"
        if message.sender_is_team:
            return f"{message.sender.name} ({TEAM_LABEL})"
        return f"{message.sender.name} (other contact)"

    return "\n".join(lines + [""] + _chat_lines(day, speaker))
