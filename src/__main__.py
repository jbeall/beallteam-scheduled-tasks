"""python -m src <command>"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.commands import sms_formats, sms_try_llm
from src.llm_client import LlmError
from src.sms_events import SmsParseError
from src.sms_formats import FORMAT_NAMES
from src.sms_prompts import PROMPT_NAMES


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src",
        description="Beall Team scheduled tasks: Dialpad SMS -> Copper activity notes.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_source_args(command: argparse.ArgumentParser) -> None:
        command.add_argument("--xlsx", type=Path, required=True, help="Export of the Dialpad SMS Google Sheet")
        command.add_argument(
            "--tabs",
            type=_csv,
            default=None,
            help="Comma-separated tab names to read (default: every tab with a raw_json column)",
        )
        command.add_argument(
            "--team-number",
            action="append",
            default=None,
            help="Extra team phone number to treat as internal (repeatable; also TEAM_PHONE_NUMBERS in .env)",
        )

    formats = sub.add_parser(
        "sms-formats",
        help="Write each conversation-day in every LLM input format to out/formats/",
    )
    add_source_args(formats)
    formats.add_argument("--sample", default=None, help="Slug fragment of the conversation-day to show in sample_all_formats.md")

    try_llm = sub.add_parser(
        "sms-try-llm",
        help="Summarize conversation-days with the configured LLM in each format x prompt; writes out/llm/",
    )
    add_source_args(try_llm)
    try_llm.add_argument(
        "--formats",
        type=_csv,
        default=["raw_json", "transcript", "compact"],
        help=f"Comma-separated formats ({', '.join(FORMAT_NAMES)}); default raw_json,transcript,compact",
    )
    try_llm.add_argument(
        "--prompts",
        type=_csv,
        default=["note"],
        help=f"Comma-separated prompt styles ({', '.join(PROMPT_NAMES)}); default note",
    )
    try_llm.add_argument("--only", default=None, help="Only conversation-days whose slug contains this text")
    try_llm.add_argument("--limit", type=int, default=None, help="Stop after this many conversation-days")
    try_llm.add_argument("--provider", default=None, help="Override LLM_PROVIDER (ollama, openrouter, openai)")
    try_llm.add_argument("--model", default=None, help="Override LLM_MODEL")

    args = parser.parse_args(argv)
    try:
        if args.command == "sms-formats":
            return sms_formats.run(
                xlsx=args.xlsx,
                tabs=args.tabs,
                extra_team=args.team_number,
                sample=args.sample,
            )
        if args.command == "sms-try-llm":
            return sms_try_llm.run(
                xlsx=args.xlsx,
                tabs=args.tabs,
                extra_team=args.team_number,
                formats=args.formats,
                prompts=args.prompts,
                only=args.only,
                limit=args.limit,
                provider=args.provider,
                model=args.model,
            )
    except (SmsParseError, LlmError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
