"""Prompt styles for summarizing a conversation-day into a Copper activity note."""

from __future__ import annotations

PROMPT_NAMES = ("note", "bullets", "json")
NOTHING_TO_LOG = "NOTHING TO LOG"

_ROLE = (
    "You write SMS activity notes in the Copper CRM of the Beall Team, a real estate team. "
    "Team members read these notes later when they review a contact's history. You will get one "
    "day of texts from one conversation. The header lists the Beall Team members and the contacts."
)
_RULES = (
    "Rules:\n"
    "- Say who said what, using names from the header. A contact shown only as a phone number gets a "
    "name only if they give it in the messages; otherwise call them \"the contact at <number>\".\n"
    "- Contacts can be clients, other agents, lenders, appraisers, inspectors or vendors. Do not call "
    "anyone a client, buyer or seller unless the messages say so.\n"
    "- Record properties, offers, prices, credits, repairs, inspections, appraisals, showings, "
    "appointments, deadlines, decisions, and follow-ups someone promised.\n"
    "- Copy addresses and property names exactly as written. Never add a city, state, ZIP or street type.\n"
    "- Write days as calendar dates using the Calendar line (\"Mon Oct 26\"), never \"Monday\", "
    "\"tomorrow\" or \"next week\".\n"
    "- Only report what the messages say. When someone says they think, believe, are not sure or are "
    "guessing, keep that and say who (\"Josh said he believes...\"). Report what someone says another "
    "person said as theirs (\"Josh said the other agent told him...\"). Never add advice, next steps or opinions "
    "of your own.\n"
    "- Reactions and photos matter only when they answer a question.\n"
    "- Messages under \"Earlier in this conversation\" are context only. Use them to understand today's "
    "messages; do not summarize them.\n"
    "- Texts can contain typos and autocorrect errors. Write what they evidently mean."
)
_SKIP = (
    "Anything a Beall Team member sent (a listing link, a check-in, a question) is worth recording. "
    f"Reply with exactly {NOTHING_TO_LOG} only if every message is an ad, a promotion, an automated "
    "notice, or a lone emoji or thank-you."
)

SYSTEM_PROMPTS = {
    "note": (
        f"{_ROLE}\n\nWrite 1-4 plain sentences in the past tense. One sentence is enough for a short "
        "exchange. No greeting, no markdown, no bullet points.\n\n"
        f"{_RULES}\n\n{_SKIP}"
    ),
    "bullets": (
        f"{_ROLE}\n\n{_SKIP} Otherwise reply in exactly this layout and nothing else:\n"
        "Summary: <one sentence>\n"
        "Details:\n- <up to 4 short bullets of facts from the messages>\n"
        "Promised follow-ups:\n- <only promises someone made in these messages, e.g. \"Josh said he "
        "would email a code\"; otherwise None>\n\n"
        f"{_RULES}"
    ),
    "json": (
        f"{_ROLE}\n\nReply with only a JSON object, no other text, with these keys in this order:\n"
        '{"summary": "", "promised_follow_ups": [], "properties": [], "self_identified": [], "log": true}\n'
        "- summary: the CRM note, 1-4 plain sentences in the past tense (empty when log is false).\n"
        '- promised_follow_ups: one {"who": "<name from the header>", "what": "<the promise>", '
        '"when": "<calendar date or time stated in the messages, or null>"} per explicit promise in '
        "the messages; otherwise [].\n"
        "- properties: each address or property name exactly as written in the messages.\n"
        '- self_identified: for a contact shown only as a phone number who gives a name, company or '
        'email: {"phone": "...", "name": "...", "company": "...", "email": "..."}.\n'
        "- log: false only if every message is an ad, a promotion, an automated notice, or a lone emoji "
        "or thank-you. Anything a Beall Team member sent is worth recording.\n\n"
        f"{_RULES}"
    ),
}

_INTROS = {
    "raw_json": (
        "Below are raw Dialpad SMS webhook events in JSON, one per line. The same message can appear "
        "once per delivery status and once per team line (same text and sender); count it once. "
        "Ignore RequestHeaders and status fields. created_date is milliseconds since the epoch (UTC); "
        "the team works in Eastern time."
    ),
    "clean_json": "Below is one day of text messages as JSON.",
}
_DEFAULT_INTRO = "Below is one day of text messages."


def build_messages(
    prompt_name: str,
    format_name: str,
    conversation: str,
    *,
    person_name: str | None = None,
    person_silent: bool = False,
) -> list[dict[str, str]]:
    """One system message plus one user message (Qwen3.8's Ollama template folds extra system
    messages into the first one anyway)."""
    if prompt_name not in SYSTEM_PROMPTS:
        raise ValueError(f"Unknown prompt {prompt_name!r}; choose from {', '.join(PROMPT_NAMES)}")
    ask = {
        "note": "Write the CRM note.",
        "bullets": "Write the CRM note in the layout above.",
        "json": "Reply with the JSON object.",
    }[prompt_name]
    if person_name:
        ask = f"{ask} It will be saved on {person_name}'s record."
        if person_silent:
            ask += (
                f" {person_name} was on the group text but sent no messages today: summarize what the "
                f"group discussed and never attribute another person's message to {person_name}."
            )
    intro = _INTROS.get(format_name, _DEFAULT_INTRO)
    return [
        {"role": "system", "content": SYSTEM_PROMPTS[prompt_name]},
        {"role": "user", "content": f"{intro}\n\n{conversation}\n\n{ask}"},
    ]


def is_nothing_to_log(output: str) -> bool:
    """Lenient match for the skip sentinel: case, punctuation and markdown don't matter."""
    letters = "".join(ch for ch in output.upper() if ch.isalpha() or ch == " ")
    return " ".join(letters.split()) == NOTHING_TO_LOG
