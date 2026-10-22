"""python -m src sms-try-llm: send each format x prompt to the LLM and save the outputs side by side."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from src.llm_client import LlmClient, LlmConfig, LlmError
from src.paths import LLM_RUNS_DIR
from src.commands.sms_formats import load_days
from src.sms_formats import FORMAT_NAMES, focus_person, render
from src.sms_prompts import PROMPT_NAMES, build_messages

_SLUG = re.compile(r"[^a-zA-Z0-9.]+")


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines()) or "> (empty)"


def _summary_table(records: list[dict]) -> list[str]:
    totals: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        totals[(record["format"], record["prompt"])].append(record)
    lines = [
        "| Format | Prompt | Runs | Errors | Avg prompt tokens | Avg output tokens | Avg seconds | Total cost |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for (format_name, prompt_name), group in totals.items():
        ok = [r for r in group if not r.get("error")]

        def avg(key: str) -> str:
            values = [r[key] for r in ok if r.get(key) is not None]
            return f"{sum(values) / len(values):,.1f}" if values else "-"

        costs = [r["cost_usd"] for r in ok if r.get("cost_usd") is not None]
        lines.append(
            f"| {format_name} | {prompt_name} | {len(group)} | {len(group) - len(ok)} | "
            f"{avg('prompt_tokens')} | {avg('completion_tokens')} | {avg('seconds')} | "
            f"{'$%.4f' % sum(costs) if costs else '-'} |"
        )
    return lines


def run(
    *,
    xlsx: Path,
    tabs: list[str] | None,
    extra_team: list[str] | None,
    formats: list[str],
    prompts: list[str],
    only: str | None,
    limit: int | None,
    provider: str | None,
    model: str | None,
) -> int:
    for name in formats:
        if name not in FORMAT_NAMES:
            raise LlmError(f"Unknown format {name!r}; choose from {', '.join(FORMAT_NAMES)}")
    for name in prompts:
        if name not in PROMPT_NAMES:
            raise LlmError(f"Unknown prompt {name!r}; choose from {', '.join(PROMPT_NAMES)}")

    *_, days = load_days(xlsx, tabs, extra_team)
    days = [d for d in days if not d.thread.skip_reason]
    if only:
        days = [d for d in days if only.lower() in d.slug]
    if limit:
        days = days[:limit]
    if not days:
        print("No conversation-days matched.")
        return 1

    config = LlmConfig.from_env(provider=provider, model=model)
    stem = f"{_SLUG.sub('-', config.model).strip('-')}_{datetime.now():%Y%m%d-%H%M%S}"
    run_dir = LLM_RUNS_DIR / stem
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = LLM_RUNS_DIR / f"{stem}-{suffix}"
    run_dir.mkdir(parents=True)
    jsonl = (run_dir / "results.jsonl").open("w", encoding="utf-8")
    calls = len(days) * len(formats) * len(prompts)
    print(f"{config.provider} {config.model}: {len(days)} conversation-days x {len(formats)} formats x {len(prompts)} prompts = {calls} calls")

    records: list[dict] = []
    sections: list[str] = []
    with LlmClient(config) as client:
        for day in days:
            person = focus_person(day)
            sections += [
                "",
                f"## {day.day} - {day.thread.title} ({len(day.messages)} messages)",
                "",
                "<details><summary>Conversation (transcript format)</summary>",
                "",
                "```text",
                render("transcript", day),
                "```",
                "",
                "</details>",
            ]
            for format_name in formats:
                conversation = render(format_name, day, person)
                for prompt_name in prompts:
                    focus = person.name if format_name == "person_focus" else None
                    messages = build_messages(prompt_name, format_name, conversation, person_name=focus)
                    record = {
                        "day": day.slug,
                        "format": format_name,
                        "prompt": prompt_name,
                        "person": focus,
                        "model": config.model,
                        "provider": config.provider,
                    }
                    try:
                        result = client.chat(messages, json_output=prompt_name == "json")
                        record.update(
                            output=result.text,
                            seconds=round(result.seconds, 2),
                            prompt_tokens=result.prompt_tokens,
                            completion_tokens=result.completion_tokens,
                            cost_usd=result.cost_usd,
                            thinking_chars=result.thinking_chars,
                            warning=result.warning,
                        )
                        status = f"{result.seconds:.1f}s {result.prompt_tokens}+{result.completion_tokens} tok"
                    except LlmError as exc:
                        record["error"] = str(exc)
                        status = f"ERROR {exc}"[:200]
                    records.append(record)
                    jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
                    jsonl.flush()
                    print(f"  {day.slug} {format_name}/{prompt_name}: {status}")

                    meta = [f"{record.get('prompt_tokens') or '?'} prompt tok", f"{record.get('seconds', '?')}s"]
                    if record.get("warning"):
                        meta.append(f"WARNING: {record['warning']}")
                    if record.get("thinking_chars"):
                        meta.append(f"{record['thinking_chars']} chars of thinking stripped")
                    label = f"{format_name} / {prompt_name}" + (f" (for {focus})" if focus else "")
                    sections += [
                        "",
                        f"### {label} - {', '.join(meta)}",
                        "",
                        _quote(record.get("output") or f"ERROR: {record.get('error')}"),
                    ]
    jsonl.close()

    report = [
        f"# LLM summary comparison: {config.model}",
        "",
        f"Provider `{config.provider}` at `{config.base_url}`, temperature {config.temperature}, "
        f"thinking {'on' if config.think else 'off'}, run {datetime.now():%Y-%m-%d %H:%M}.",
        "",
        *_summary_table(records),
        *sections,
    ]
    (run_dir / "results.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"Wrote {run_dir / 'results.md'}")
    return 0
