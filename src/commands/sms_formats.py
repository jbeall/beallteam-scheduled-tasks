"""python -m src sms-formats: write every conversation-day in every LLM input format."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

from src.paths import FORMATS_DIR, ROOT
from src.sms_events import parse_rows, read_xlsx_rows
from src.sms_formats import FORMAT_NAMES, estimate_tokens, focus_person, render
from src.sms_prompts import PROMPT_NAMES, SYSTEM_PROMPTS, build_messages
from src.sms_threads import ConversationDay, build_threads, conversation_days

_EXTENSIONS = {"raw_json": "jsonl", "clean_json": "json"}


def team_numbers(extra: list[str] | None) -> list[str]:
    load_dotenv(ROOT / ".env")
    configured = [n.strip() for n in os.getenv("TEAM_PHONE_NUMBERS", "").split(",") if n.strip()]
    return configured + list(extra or [])


def load_days(xlsx: Path, tabs: list[str] | None, extra_team: list[str] | None):
    rows = read_xlsx_rows(xlsx, tabs)
    events, errors = parse_rows(rows)
    threads = build_threads(events, team_numbers(extra_team))
    return rows, events, errors, threads, conversation_days(threads)


def _file_name(format_name: str) -> str:
    return f"{format_name}.{_EXTENSIONS.get(format_name, 'txt')}"


def _sample_markdown(day: ConversationDay) -> str:
    person = focus_person(day)
    parts = [
        f"# All formats for one conversation-day: {day.slug}",
        "",
        "Each block below is the same conversation in a different input format. "
        "The last section is a complete, paste-ready prompt (system + user) using the "
        "`note` prompt and the `transcript` format.",
    ]
    for format_name in FORMAT_NAMES:
        text = render(format_name, day, person)
        fence = "json" if format_name.endswith("json") else "text"
        parts += [
            "",
            f"## {format_name} (≈{estimate_tokens(text):,} tokens)",
            "",
            f"```{fence}",
            text,
            "```",
        ]
    messages = build_messages("note", "transcript", render("transcript", day))
    parts += ["", "## Paste-ready prompt: note + transcript", ""]
    for message in messages:
        parts += [f"**{message['role']}**", "", "```text", message["content"], "```", ""]
    return "\n".join(parts)


def run(
    *,
    xlsx: Path,
    tabs: list[str] | None,
    extra_team: list[str] | None,
    sample: str | None,
) -> int:
    rows, events, errors, threads, days = load_days(xlsx, tabs, extra_team)
    if FORMATS_DIR.exists():
        shutil.rmtree(FORMATS_DIR)
    FORMATS_DIR.mkdir(parents=True)

    prompts_dir = FORMATS_DIR / "prompts"
    prompts_dir.mkdir()
    for name in PROMPT_NAMES:
        (prompts_dir / f"system_{name}.txt").write_text(SYSTEM_PROMPTS[name], encoding="utf-8")

    unique_messages = sum(len(t.messages) for t in threads)
    team = sorted({p for t in threads for p in t.team_phones})
    index = [
        "# SMS conversation formats (proof of concept)",
        "",
        f"- Source: `{xlsx.name}` tabs {', '.join(tabs) if tabs else '(all)'}",
        f"- {len(rows)} sheet rows -> {len(events)} parsed webhook events -> "
        f"{unique_messages} unique messages -> {len(threads)} threads -> {len(days)} conversation-days",
        f"- Team numbers (excluded from Copper lookups): {', '.join(team)}",
        f"- Parse errors: {len(errors)}",
        *[f"  - {e}" for e in errors],
        "",
        "Token counts are rough (characters / 4).",
        "",
        "| Day | Conversation | Contacts to look up in Copper | Msgs | Skip | "
        + " | ".join(FORMAT_NAMES)
        + " |",
        "|---|---|---|---|---|" + "---|" * len(FORMAT_NAMES),
    ]
    for day in days:
        folder = FORMATS_DIR / day.slug
        copy = 1
        while folder.exists():
            copy += 1
            folder = FORMATS_DIR / f"{day.slug}-{copy}"
        folder.mkdir()
        sizes = []
        for format_name in FORMAT_NAMES:
            text = render(format_name, day)
            (folder / _file_name(format_name)).write_text(text + "\n", encoding="utf-8")
            sizes.append(f"{estimate_tokens(text):,}")
        contacts = "; ".join(f"{p.name} {p.phone}" for p in day.thread.contacts)
        index.append(
            f"| {day.day} | [{day.thread.title}]({folder.name}/transcript.txt) | {contacts} | "
            f"{len(day.messages)} | {day.thread.skip_reason or ''} | " + " | ".join(sizes) + " |"
        )
    (FORMATS_DIR / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")

    candidates = [d for d in days if not d.thread.skip_reason]
    chosen = None
    if sample:
        chosen = next((d for d in days if sample.lower() in d.slug), None)
    if chosen is None and candidates:
        chosen = max(candidates, key=lambda d: len(d.messages))
    if chosen:
        (FORMATS_DIR / "sample_all_formats.md").write_text(_sample_markdown(chosen), encoding="utf-8")

    print(
        f"{len(rows)} rows -> {len(events)} events -> {unique_messages} messages -> "
        f"{len(threads)} threads -> {len(days)} conversation-days ({len(errors)} parse errors)"
    )
    for day in days:
        flag = f"  [skip: {day.thread.skip_reason}]" if day.thread.skip_reason else ""
        print(f"  {day.slug:55} {len(day.messages):3} msgs{flag}")
    print(f"Wrote {FORMATS_DIR}")
    return 0
