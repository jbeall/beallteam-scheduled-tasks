# Dialpad SMS -> Copper activity notes: design

Working notes for the nightly job. Client names and numbers are deliberately left out.

## Source data: the "Dialpad SMS Log" Google Sheet

Pabbly Connect appends one row per Dialpad SMS webhook delivery.

| Column | Contents |
|---|---|
| A `received_at_et` | Sheets date serial in Eastern time (when Pabbly received it). Not used: `created_date` in the payload is authoritative. |
| B `raw_json` | The Dialpad SMS event. Double-encoded (a JSON string literal holding JSON) and can contain raw newlines, so it is decoded with `json.loads(..., strict=False)` until it is an object. |

Tabs: `texts` (new rows), `processed` (rows already handled), `processed (old, empty)` (unused).
The spreadsheet's own time zone is GMT; nothing depends on it.

### Payload quirks seen in real data

- **Status repeats.** Outbound 1:1 messages arrive 4 times with the same `id` (`unknown`, `pending`,
  `sent`, `delivered`). This is the subscription's `status=true` option. Dedupe by `id`.
- **One copy per Dialpad user in a group.** A group message is delivered once per Dialpad user in the
  thread, with a different `id` and `target`, `created_date` about 2 s apart, same `from_number` and
  text. Dedupe: same thread, sender and text, different team line, within 2 minutes.
- **Slim rows.** Some rows have only `id, created_date, direction, contact{id,type,name,phone},
  from_number, to_number, text` (no `target`, `sender_id`, `message_status`, `members`, and no Pabbly
  `RequestHeaders`). Cause unknown, probably on the Pabbly side. The team line comes from `to_number`
  (inbound) or `from_number` (outbound); group members are backfilled from other rows of the same
  `contact.id`.
- **Groups.** `contact.type == "contact_group"`, `contact.id` is a stable thread id (the same on every
  Dialpad user's copy), `contact.members[]` lists everyone except the target user. Members with
  `type: "user"` are Dialpad users (team). `from_number` is always the real sender; `sender_id` is null
  for about a third of inbound group messages (members of type `mobile`), so it is not used.
- **Status order.** The four status events of one message arrive in no fixed order (`delivered` was always
  last in the sample). Nothing here depends on status; if failed sends are ever flagged, pick the status
  by precedence (delivered/failed/undelivered > sent > pending/unknown), not by arrival.
- **MMS.** `text` is a `https://content.dialpad.com/s/img/...` URL and `text_content` is empty (a caption,
  if any, would be in `text_content`). Whether the image URL needs auth is unknown; test with `curl -I`.
- **Reactions.** iMessage fallbacks: `Liked “…”`, `Laughed at “…”`, and `👍 a “…”` wrapped in hair /
  zero-width spaces. Rendered as `[reacted 👍 to “…”]` or dropped.
- **Noise.** Shortcodes (5-6 digit senders), marketing texts, delivery reminders. Shortcodes are skipped;
  everything else is naturally filtered because it won't match a Copper person.
- **Ids** are 16-digit integers; keep them as strings outside Python.
- **Undocumented fields.** `contact.type`, `members`, `is_internal`, `text_content`, `mms`, `mms_url`
  are not in Dialpad's published event docs; read them with `.get()`.

## Processing model

1. Read `texts` rows; parse; collect parse errors without stopping.
2. Dedupe and group into threads: group threads by `contact.id`, 1:1 threads by the contact's number (a
   client who texts two team lines is one conversation). Names come from the thread first, then from
   every event in the batch.
3. Split each thread into **conversation-days** by Eastern calendar date. Only process days before
   today (Eastern) so every day is complete; today's rows stay for tomorrow's run.
4. Team numbers: every Dialpad line in the data, `type: "user"` group members, `TEAM_PHONE_NUMBERS`
   in `.env`, later `GET /api/v2/users` (`phone_numbers`, `forwarding_numbers`) and `GET /api/v2/numbers`.
5. Match every non-team participant to Copper (below). Conversation-days with no match are not sent
   to the LLM; their numbers go in the run report.
6. One LLM call per matched conversation-day (the summary is shared by everyone in the thread). The
   input names the weekday and the next 7 dates, lists contacts who sent nothing that day, decodes
   RealScout links to addresses, and shows the last 5 messages before that day as context only (in
   production, read from the `processed` tab).
7. One Copper activity per person per Eastern day. A person in two threads that day gets one note with
   a section per thread. Group members who did not speak still get the note.
8. Record each write in a local SQLite ledger; move handled rows from `texts` to `processed`; append a
   run summary row to a log tab.

### Known limits

- A group thread whose only rows that night are slim has no member list, so silent members can't be
  found. Fix in the sync step: cache group `contact.id` -> members from earlier runs (the Dialpad API can't
  fetch groups).
- A conversation that crosses midnight Eastern becomes two days, so two notes. Accepted for v1; the second
  day's input shows the first day's last messages as context.

## Google Sheets access

- **Recommended: OAuth "Desktop app" client + Internal consent screen**, in a Google Cloud project created
  under the beallteam.com organization (not "No organization"). Enable only the Google Sheets API, request
  only `https://www.googleapis.com/auth/spreadsheets`, and authorize once in a browser on the host
  (`run_local_server`) as a Workspace user the sheet is shared with (a dedicated automation user is best;
  Josh works). The refresh token is saved to an ACL-restricted file and used headlessly.
  Why: new Cloud orgs (since 2024) block service-account key creation by default, and Workspace
  external-sharing settings can block sharing a sheet with a `*.iam.gserviceaccount.com` address. An
  Internal app avoids both, needs no Google verification, and isn't subject to the 7-day token expiry of
  External apps in Testing. If Workspace API controls restrict Sheets, the admin ticks "Trust internal apps".
- **Fallback: service account** with a JSON key, if an admin overrides the key-creation policy for this one
  project (undo later with `gcloud org-policies delete <constraint> --project=…`, not `reset`) and allows
  external sharing for the sheet owner's OU.
- **Libraries:** google-auth + google-auth-oauthlib, and google-api-python-client or a thin REST wrapper
  (gspread is unmaintained). These need Python 3.10+.
- **Reading:** `values.batchGet` with `valueRenderOption=UNFORMATTED_VALUE` (column A is a plain number in
  Eastern wall time; the payload's `created_date` is used instead). Resolve the sheetIds of exactly
  `texts` and `processed` at startup and stop if either is missing.
- **Moving rows** (only after Copper writes succeed): re-read the snapshot range and compare, then one
  atomic `batchUpdate` of [appendCells to `processed`, deleteDimension on `texts` rows 2..K+1]. Rows Pabbly
  appends meanwhile land below and survive. Add a spare row first only when the delete would remove every
  non-frozen row (the API refuses that). Never `values.clear`. Correctness rests on the ledger, not the move.
- **Growth:** a spreadsheet is capped at 10 million cells; archive `processed` yearly and drop unused columns.

## Copper

- **Activity type.** No built-in SMS type. An admin creates "Text Message" in Workspace Settings >
  Customize > Manage Activity Types (tick "count as interaction" so it updates last-contacted).
  The job resolves the id by name from `GET /activity_types` (an object `{"user": [...], "system": [...]}`)
  and stops if it is missing or disabled. `POST /custom_activity_types` exists but cannot set
  "count as interaction".
- **Create.** `POST /activities` `{"parent": {"type": "person", "id": …}, "type": {"category": "user",
  "id": …}, "details": "…", "activity_date": <unix seconds>}`. Returns 200. `activity_date` = last
  message time that day. Attributed to the API key's user.
- **Phone matching.** API phone search defaults to *fuzzy* matching for 7+ digits (strips leading
  digits, many false positives) and is a suffix match. Instead: pull all People (and Leads) nightly at
  page size 200, normalize numbers to 10-digit NANP, index number -> records. Several records on one
  number: log to each and flag it. The key must see every record (Copper applies team permissions).
- **Idempotency.** Activities have no external id or custom fields. Ledger `(parent_type, parent_id,
  et_date) -> activity_id`; before creating, `POST /activities/search` with parent, type and the day's
  window (ignore `is_deleted` stubs); update with `PUT /activities/{id}` instead of duplicating; a
  marker line in `details`.
- **Formatting.** Notes are Markdown-backed: use blank lines between paragraphs, and escape `*`, `_`, `#`,
  `>` and leading `-` in quoted SMS text. Max length is undocumented; test a multi-line note first.
- **Interactions.** "Count as interaction" is set on the activity type; whether API-created activities
  update last-contacted the same way is undocumented. Check on a test record.
- **Leads.** Converting a lead deletes the lead record, so match phone numbers fresh each night instead of
  caching record ids.
- **Limits.** 180 requests/minute rolling.

## Dialpad API (optional for v1)

`Authorization: Bearer <key>` against `https://dialpad.com/api/v2` (admin creates it under Admin
Settings > My Company > Authentication > API Keys). Useful: `GET /users` (`phone_numbers`,
`forwarding_numbers`) and `GET /numbers` (admin key) for team numbers. `GET /subscriptions/sms` shows the
subscription settings, but one created through the deprecated v1 API (or by Pabbly) may only be visible
through the v1 archive endpoints. There is no SMS history endpoint; `POST /stats` with
`stat_type: texts, export_type: records` is an async CSV export that could cross-check the sheet, but
Dialpad support has to enable "Text Logs Export" first and it runs per office. Group ids and
Google-synced contacts cannot be fetched, so the API can't fill in missing group members. Don't PATCH the
Pabbly-owned subscription (omitted fields reset to defaults); dedupe in code instead.

Check before launch whether Dialpad's native Copper integration is enabled and logs SMS, so notes aren't
duplicated.

## LLM

- One client, two backends (`src/llm_client.py`):
  - **Ollama**, native `POST /api/chat` with `think: false`, `options.num_ctx` (fixed for the whole run;
    changing it reloads the model), `truncate: false` and `shift: false` (otherwise a prompt longer than
    the window keeps only its first few tokens and drops the rest, system prompt included, with no
    error), `keep_alive: 10m`. Default context is 4096 tokens on GPUs under 24 GB.
  - **OpenRouter**, `POST /api/v1/chat/completions` with `reasoning: {"effort": "none"}` (retried without
    it for models whose reasoning is mandatory), `provider: {"data_collection": "deny"}`, and
    `usage.cost` logged per call.
- **Models.** Qwen3.8 (Aug 2026) has open weights only at 27B (`qwen3.8:27b`, ~18 GB at Q4, Apache-2.0 per
  secondary reports) and a 2.4T MoE. Qwen3-8B (`qwen3:8b`, 2025) is a different, much smaller model.
  OpenRouter: `qwen/qwen3.8-27b` (about $0.40 / $3.00 per million tokens in/out) or
  `qwen/qwen3.8-flash` (about $0.15 / $0.47). Slugs and prices from secondary sources; confirm with
  `GET /api/v1/models`. A night of ~20 conversations is a few thousand tokens: well under a cent.
- Thinking is off by default; any `<think>` text is stripped and an unfinished one counts as no answer.
- Qwen's card suggests temperature 0.7 / top_p 0.8 / top_k 20 for non-thinking mode; the default here is
  0.2. Compare both with `sms-try-llm`.
- Never write a note from an empty, cut-off or `NOTHING TO LOG` result.

## Scheduling on the host

- **Windows Task Scheduler:** action = the venv's `python.exe -m src sms-sync`, *Start in* = the repo root
  (otherwise it runs in System32); daily ~3:30 AM (avoid 1-3 AM DST hours); "Run whether user is logged
  on or not" with a stored password (S4U has no network access); Wake to run; Start when available (one
  catch-up run after a missed night); allow on battery; time limit 2 h. Restart-on-failure may not fire on
  a non-zero exit, so retries live in the job.
- Ollama on Windows installs per user and starts at login. Run the task as that user, or run
  `ollama serve` as a service with `OLLAMA_MODELS` pointing at the models; the job health-checks
  `http://127.0.0.1:11434` first.
- **Linux:** systemd oneshot service + timer, `OnCalendar=*-*-* 03:30:00 America/New_York`,
  `Persistent=true`.
- A multi-day outage produces one catch-up run, so the job processes every row still on `texts`, not
  just "yesterday".

## Open questions

- Is a person in several threads on one day one note (proposed) or one per thread?
- Include the transcript under the summary in the Copper note, at least during the trial?
- Log to Leads as well as People? Several records on one number: log to all, or skip and report?
- Which team members text from numbers Dialpad does not know as users?
- "Qwen 3.8": Qwen3.8-27B (needs a 24 GB GPU) or Qwen3-8B? What GPU does the host have?
- Is Dialpad's native Copper integration on, and does it log SMS?
- What does "BTGT" in group names mean (strip it from the model's input)?
- Why were some rows logged without `target`/`members` (Pabbly task history for that window)?
