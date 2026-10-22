"""Dedupe Dialpad SMS events and group them into threads and conversation-days.

Duplicates seen in the webhook log:
- One delivery per status update (pending / sent / delivered / unknown) with the same event id.
- One copy per Dialpad user in a group thread: different event id and target line, same sender and text.
Slim payloads (no target / members / status) are kept; group members are backfilled from
other events in the same thread, and names from every event in the batch.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from src import phones
from src.sms_events import EASTERN, MMS_URL_PREFIX, Party, SmsEvent

CROSS_COPY_WINDOW = timedelta(minutes=2)
REMOVED_REACTION = "removed"

# Hair space / zero-width space / word joiner / BOM wrap iOS reaction fallbacks. ZWJ (U+200D)
# is left alone because emoji sequences use it.
_INVISIBLE = dict.fromkeys(map(ord, "\u200a\u200b\u2060\ufeff"), None)
_TAPBACKS = "Liked|Loved|Disliked|Laughed at|Emphasized|Questioned"
_QUOTED = r"[“\"](?P<quoted>.*)[”\"]"
_REACTION_PATTERNS = (
    re.compile(rf"^(?P<reaction>{_TAPBACKS}) {_QUOTED}$", re.S),
    re.compile(rf"^(?P<reaction>{_TAPBACKS}) (?P<quoted>an? (?:image|photo|video|movie|attachment|audio message))$"),
    re.compile(rf"^(?P<reaction>[^\w\s]{{1,10}}) a {_QUOTED}$", re.S),  # "👍 a “…”"
    re.compile(rf"^Reacted (?P<reaction>[^\w\s]{{1,10}}) to {_QUOTED}$", re.S),
    re.compile(rf"^(?P<reaction>Removed) an? [\w ]{{1,30}}? from {_QUOTED}$", re.S),
)
_REACTION_EMOJI = {
    "Liked": "👍",
    "Loved": "❤️",
    "Disliked": "👎",
    "Laughed at": "😂",
    "Emphasized": "‼️",
    "Questioned": "❓",
    "Removed": REMOVED_REACTION,
}
_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Message:
    event_id: int
    sent_at: datetime
    sender: Party
    sender_is_team: bool
    kind: str  # "text" | "photo" | "reaction"
    text: str  # message body; the photo caption; or the quoted text a reaction points at
    reaction: str | None = None  # emoji, or REMOVED_REACTION for a removed tapback

    @property
    def sent_at_et(self) -> datetime:
        return self.sent_at.astimezone(EASTERN)


@dataclass
class Thread:
    key: tuple[str, ...]  # ("group", contact_id) or ("direct", contact number)
    is_group: bool
    title: str
    participants: dict[str, Party]  # phone -> Party; team first, then contacts by name
    team_phones: frozenset[str]
    messages: list[Message]
    events: list[SmsEvent]  # every delivery, duplicates included, oldest first
    skip_reason: str | None

    @property
    def team(self) -> list[Party]:
        return [p for phone, p in self.participants.items() if phone in self.team_phones]

    @property
    def contacts(self) -> list[Party]:
        return [p for phone, p in self.participants.items() if phone not in self.team_phones]


@dataclass
class ConversationDay:
    thread: Thread
    day: date  # Eastern calendar date
    messages: list[Message]
    events: list[SmsEvent]

    @property
    def slug(self) -> str:
        """Folder/report name. The suffix (end of the group id or contact number) keeps two
        threads with the same title apart."""
        title = _SLUG.sub("-", self.thread.title.lower()).strip("-")[:40] or "thread"
        suffix = phones.digits(self.thread.key[-1])[-4:] or "x"
        return f"{self.day:%Y-%m-%d}_{title}_{suffix}"


def clean_text(text: str) -> str:
    text = text.translate(_INVISIBLE).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def classify(text: str, is_mms: bool) -> tuple[str, str, str | None]:
    """Return (kind, body, reaction) for one message. For MMS, text is the caption (or the URL)."""
    if is_mms or text.startswith(MMS_URL_PREFIX):
        caption = "" if text.startswith(MMS_URL_PREFIX) else clean_text(text)
        return "photo", caption, None
    body = clean_text(text)
    for pattern in _REACTION_PATTERNS:
        match = pattern.match(body)
        if match:
            reaction = match["reaction"]
            return "reaction", match["quoted"].strip(), _REACTION_EMOJI.get(reaction, reaction)
    return "text", body, None


def line_of(event: SmsEvent) -> str | None:
    """The Dialpad (team) number this copy of the event belongs to."""
    if event.target:
        return event.target.phone
    if event.direction == "outbound":
        return event.from_number
    return event.to_numbers[0] if event.to_numbers else None


def _external_of(event: SmsEvent) -> str:
    if event.contact_phone:
        return event.contact_phone
    if event.direction == "inbound":
        return event.from_number or ""
    return event.to_numbers[0] if event.to_numbers else ""


def _thread_key(event: SmsEvent) -> tuple[str, ...]:
    # A client who texts two team lines is one conversation, so 1:1 threads key on the
    # client's number alone.
    if event.is_group:
        return ("group", event.contact_id)
    return ("direct", _external_of(event))


def _best_copy(copies: list[SmsEvent]) -> SmsEvent:
    return max(copies, key=lambda e: (not e.is_slim, len(e.members), e.target is not None))


def _name_votes(event: SmsEvent) -> Iterable[tuple[str | None, str]]:
    yield line_of(event), event.target.name if event.target else ""
    for member in event.members:
        yield member.phone, member.name
    if event.is_group:
        yield event.from_number, ""
        for number in event.to_numbers:
            yield number, ""
    else:
        yield _external_of(event), event.contact_name


def _tally(votes: Iterable[tuple[str | None, str]]) -> dict[str, Counter[str]]:
    names: dict[str, Counter[str]] = defaultdict(Counter)
    for phone, name in votes:
        if phone:
            names[phone][name if name and not phones.looks_like_phone(name) else ""] += 1
    return names


def _best_name(votes: Counter[str] | None) -> str | None:
    named = [(count, name) for name, count in (votes or {}).items() if name]
    return max(named)[1] if named else None


def build_threads(events: Iterable[SmsEvent], team_numbers: Iterable[str] = ()) -> list[Thread]:
    """Team numbers = every Dialpad line seen + Dialpad users in group member lists + the other
    side of internal (team-to-team) texts + the configured extras (team members texting from
    numbers Dialpad doesn't know as users)."""
    ordered = sorted(events, key=lambda e: (e.created_at, e.event_id))
    team = {n for n in (phones.normalize(x) for x in team_numbers) if n}
    team |= {line for line in (line_of(e) for e in ordered) if line}
    team |= {phone for e in ordered for phone in e.user_phones}
    team |= {_external_of(e) for e in ordered if e.is_internal and not e.is_group}
    team_phones = frozenset(p for p in team if p)

    copies_by_id: dict[int, list[SmsEvent]] = defaultdict(list)
    for event in ordered:
        copies_by_id[event.event_id].append(event)

    events_by_key: dict[tuple[str, ...], list[SmsEvent]] = defaultdict(list)
    reps_by_key: dict[tuple[str, ...], list[SmsEvent]] = defaultdict(list)
    for copies in copies_by_id.values():
        rep = _best_copy(copies)
        key = _thread_key(rep)
        events_by_key[key].extend(copies)
        reps_by_key[key].append(rep)

    directory = _tally(vote for copies in copies_by_id.values() for vote in _name_votes(_best_copy(copies)))
    threads = [
        _build_thread(key, reps, events_by_key[key], team_phones, directory)
        for key, reps in reps_by_key.items()
    ]
    threads.sort(key=lambda t: t.events[0].created_at if t.events else datetime.min)
    return threads


def _build_thread(
    key: tuple[str, ...],
    reps: list[SmsEvent],
    all_events: list[SmsEvent],
    team_phones: frozenset[str],
    directory: dict[str, Counter[str]],
) -> Thread:
    reps = sorted(reps, key=lambda e: (e.created_at, e.event_id))
    is_group = key[0] == "group"
    names = _tally(vote for event in reps for vote in _name_votes(event))

    # Prefer the name this thread uses, then the name used anywhere in the batch.
    display = {
        phone: _best_name(names[phone]) or _best_name(directory.get(phone)) or phones.pretty(phone)
        for phone in names
    }
    repeated = {name for name, count in Counter(display.values()).items() if count > 1}
    parties = {
        phone: Party(phone, f"{name} {phones.pretty(phone)}" if name in repeated else name)
        for phone, name in display.items()
    }
    ordered_phones = sorted(
        parties,
        key=lambda p: (p not in team_phones, parties[p].name.lower()),
    )
    participants = {phone: parties[phone] for phone in ordered_phones}

    messages: list[Message] = []
    seen: list[tuple[tuple[str, str, str, str | None], str | None, datetime]] = []
    for event in reps:
        kind, body, reaction = classify(event.text, event.is_mms)
        sender_phone = event.from_number or ""
        signature = (sender_phone, kind, body if kind != "photo" else (event.mms_url or body), reaction)
        line = line_of(event)
        if any(
            sig == signature and seen_line != line and abs(event.created_at - at) <= CROSS_COPY_WINDOW
            for sig, seen_line, at in seen
        ):
            continue  # same message delivered to another Dialpad user's line
        seen.append((signature, line, event.created_at))
        messages.append(
            Message(
                event_id=event.event_id,
                sent_at=event.created_at,
                sender=participants.get(sender_phone)
                or Party(sender_phone, phones.pretty(sender_phone)),
                sender_is_team=sender_phone in team_phones,
                kind=kind,
                text=body,
                reaction=reaction,
            )
        )

    if is_group:
        group_names = Counter(e.contact_name for e in reps if e.contact_name)
        title = group_names.most_common(1)[0][0] if group_names else "Group text"
    else:
        title = participants[key[1]].name if key[1] in participants else phones.pretty(key[1])

    contacts = [p for p in participants if p not in team_phones]
    skip_reason = None
    if any(e.contact_type == "shortcode" for e in reps) or any(
        phones.is_shortcode(p) for p in contacts
    ):
        skip_reason = "automated sender (shortcode)"
    elif not contacts:
        skip_reason = "team members only"

    return Thread(
        key=key,
        is_group=is_group,
        title=title,
        participants=participants,
        team_phones=team_phones,
        messages=messages,
        events=sorted(all_events, key=lambda e: (e.created_at, e.event_id)),
        skip_reason=skip_reason,
    )


def conversation_days(threads: Iterable[Thread]) -> list[ConversationDay]:
    """One entry per thread per Eastern calendar day. The Copper note is per person per day,
    so a person in several threads that day gets one note covering all of them."""
    days: list[ConversationDay] = []
    for thread in threads:
        messages_by_day: dict[date, list[Message]] = defaultdict(list)
        for message in thread.messages:
            messages_by_day[message.sent_at_et.date()].append(message)
        events_by_day: dict[date, list[SmsEvent]] = defaultdict(list)
        for event in thread.events:
            events_by_day[event.created_at_et.date()].append(event)
        for day in sorted(messages_by_day):
            days.append(
                ConversationDay(thread, day, messages_by_day[day], events_by_day.get(day, []))
            )
    days.sort(key=lambda d: (d.day, d.messages[0].sent_at))
    return days
