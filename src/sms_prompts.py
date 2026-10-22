"""Prompt styles for summarizing a conversation-day into a Copper activity note."""

from __future__ import annotations

PROMPT_NAMES = ("note", "bullets", "json")
NOTHING_TO_LOG = "NOTHING TO LOG"

_ROLE = (
    "You write activity notes in the CRM for the Beall Team, a real estate team. "
    "You will receive one day of text messages between the team and one or more contacts."
)
_RULES = (
    "Rules:\n"
    "- Use people's names. Write in the past tense.\n"
    "- Record properties (with addresses), offers, prices, credits, repairs, inspections, "
    "appointments, dates and deadlines, decisions, and any follow-up someone promised.\n"
    "- Only state facts that are in the messages. Do not guess, and do not add advice.\n"
    "- Ignore reactions (likes, emoji) and photos unless they confirm something."
)

SYSTEM_PROMPTS = {
    "note": (
        f"{_ROLE}\n\nWrite 2-4 plain sentences recording what was discussed. "
        "No greeting, no markdown, no bullet points.\n\n"
        f"{_RULES}\n- If there is nothing worth recording (spam, automated messages, only "
        f"reactions or pleasantries), reply with exactly: {NOTHING_TO_LOG}"
    ),
    "bullets": (
        f"{_ROLE}\n\nReply in exactly this layout and nothing else:\n"
        "Summary: <one sentence>\n"
        "Details:\n- <up to 4 short bullets of facts>\n"
        "Next steps:\n- <who will do what, and by when; or None>\n\n"
        f"{_RULES}\n- If there is nothing worth recording (spam, automated messages, only "
        f"reactions or pleasantries), reply with exactly: {NOTHING_TO_LOG}"
    ),
    "json": (
        f"{_ROLE}\n\nReply with only a JSON object, no other text, in this shape:\n"
        '{"log": true, "summary": "2-4 sentences", '
        '"next_steps": [{"who": "name", "what": "task", "when": "date/time or null"}], '
        '"properties": ["street addresses mentioned"]}\n'
        'Set "log" to false (and leave the other fields empty) when there is nothing worth '
        "recording: spam, automated messages, or only reactions and pleasantries.\n\n"
        f"{_RULES}"
    ),
}

_INTROS = {
    "raw_json": "Below are raw Dialpad SMS webhook events in JSON, one per line.",
    "clean_json": "Below is one day of text messages as JSON.",
}
_DEFAULT_INTRO = "Below is one day of text messages."


def build_messages(
    prompt_name: str,
    format_name: str,
    conversation: str,
    *,
    person_name: str | None = None,
) -> list[dict[str, str]]:
    if prompt_name not in SYSTEM_PROMPTS:
        raise ValueError(f"Unknown prompt {prompt_name!r}; choose from {', '.join(PROMPT_NAMES)}")
    ask = {
        "note": "Write the CRM note.",
        "bullets": "Write the CRM note in the layout above.",
        "json": "Reply with the JSON object.",
    }[prompt_name]
    if person_name:
        ask = f"{ask} It will be saved on {person_name}'s record."
    intro = _INTROS.get(format_name, _DEFAULT_INTRO)
    return [
        {"role": "system", "content": SYSTEM_PROMPTS[prompt_name]},
        {"role": "user", "content": f"{intro}\n\n{conversation}\n\n{ask}"},
    ]
