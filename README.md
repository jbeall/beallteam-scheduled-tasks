# beallteam-scheduled-tasks

Nightly jobs for the Beall Team. The first one turns Dialpad text messages into Copper CRM
activity notes:

```
Dialpad SMS webhook -> Pabbly -> Google Sheet ("texts" tab)
        -> parse, dedupe, group into conversations (one per thread per Eastern day)
        -> match each contact's phone number to a Copper Person
        -> summarize the conversation with an LLM (Qwen via Ollama or OpenRouter)
        -> one "Text Message" activity per person per day in Copper
        -> move handled rows to the "processed" tab
```

**Status: proof of concept.** Today the code parses an `.xlsx` export of the sheet, renders each
conversation in several LLM input formats, and runs those formats through an LLM so the summaries
can be compared. Google Sheets, Copper and the nightly schedule come next (see Roadmap).

## Setup (Python 3.11+)

Windows:

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

macOS / Linux:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`tzdata` is in requirements because Windows has no built-in time zone database; conversations
are bucketed by Eastern calendar day.

## 1. Render the input formats

Download the sheet as `.xlsx` (File > Download > Microsoft Excel), then:

```
python -m src sms-formats --xlsx "Dialpad SMS Log.xlsx"
```

This writes `out/formats/` (gitignored, it contains client messages):

- `index.md`: every conversation-day, who would be looked up in Copper, and the rough token size of each format
- `<date>_<conversation>/`: the same conversation as `raw_json.jsonl`, `clean_json.json`, `transcript.txt`, `compact.txt`, `person_focus.txt`
- `sample_all_formats.md`: the busiest conversation in every format, plus a paste-ready prompt
- `prompts/`: the system prompts

| Format | What the model sees |
|---|---|
| `raw_json` | Every webhook delivery exactly as logged, duplicates and request headers included (baseline) |
| `clean_json` | Deduped messages as JSON: time, sender name, text; reactions and photos marked |
| `transcript` | Date + 7-day calendar, team members and contacts (silent ones marked), up to 5 earlier messages as context, then `[1:48 PM] Name: text` lines |
| `compact` | One-line header and calendar, no times, back-to-back messages from one person merged, reactions shortened to `(reacted 👍)` |
| `person_focus` | The transcript relabelled around the one contact the note is for (`Pat Client (this contact)`, `(Beall Team)`, `(other contact)`) |

All derived formats name the weekday and the coming week's dates (so "Monday" can become
"Mon Oct 26"), turn RealScout listing links into their address, and point reactions at the message
they react to instead of repeating it.

Team numbers are excluded from Copper lookups: every Dialpad line in the log, Dialpad users in
group member lists, and `TEAM_PHONE_NUMBERS` from `.env` (or `--team-number`) for teammates who
text from numbers Dialpad doesn't know as users.

## 2. Compare LLM summaries

Set the `LLM_*` values in `.env` (examples are in `.env.example`), then:

```
python -m src sms-try-llm --xlsx "Dialpad SMS Log.xlsx"
python -m src sms-try-llm --xlsx "Dialpad SMS Log.xlsx" --formats transcript,compact,person_focus --prompts note,bullets,json
python -m src sms-try-llm --xlsx "Dialpad SMS Log.xlsx" --only smith --model qwen3.5:9b
```

Each run writes `out/llm/<model>_<time>/results.md` (a summary table, then every conversation
with each format/prompt's output) and `results.jsonl`. `person_focus` runs once per contact in the
conversation, which is what per-person notes would cost, including contacts who sent nothing that
day.

Prompt styles: `note` (1-4 plain sentences), `bullets` (Summary / Details / Promised follow-ups),
`json` (`summary`, `promised_follow_ups`, `properties`, `self_identified`, `log`). All of them tell the
model to write calendar dates, keep hedges ("Josh said he believes..."), copy addresses exactly, never
guess a name for a bare phone number, never invent next steps, and answer `NOTHING TO LOG` (or
`"log": false`) only for ads, automated notices, or a lone emoji/thank-you.

Notes:

- Qwen3-family models (including Qwen3.8) "think" before answering. `LLM_THINK=false` sends
  `think: false` to Ollama and `reasoning: {"effort": "none"}` to OpenRouter; any leftover `<think>`
  block is stripped, and an unfinished one counts as no answer.
- OpenRouter requests send `provider.data_collection: "deny"` (`LLM_DATA_COLLECTION`) so client texts
  only go to providers that don't store or train on them.
- Ollama's default context window is small and long prompts are cut off silently. The client sends
  `LLM_NUM_CTX` (default 16384) and flags a result whose prompt filled the window. The `raw_json`
  format for a busy group text can run past 10,000 tokens.

## Tests

```
pip install pytest
python -m pytest src
```

Tests use synthetic payloads (`src/sms_test_payloads.py`); no client data is committed.

## Roadmap

1. **Pick the format and prompt** from `sms-try-llm` runs against the Qwen model that will be used.
2. **Read the live sheet** with a Google service account (or OAuth desktop credentials if Workspace
   policy blocks service accounts).
3. **Copper matching, read-only**: nightly pull of all People (and Leads) into a local phone index;
   a `sms-sync --dry-run` that writes the notes it *would* create and lists unmatched numbers.
4. **Copper writes**: a "Text Message" activity type, one activity per person per Eastern day,
   a local SQLite ledger so reruns update instead of duplicating, then move handled rows from
   `texts` to `processed` and append a run summary to a log tab.
5. **Schedule it** (Windows Task Scheduler or a systemd timer) after a week of clean dry runs.
6. **Dialpad API**: pull team numbers from `GET /users` and `GET /numbers` instead of configuring them.
