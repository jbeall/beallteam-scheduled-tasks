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
- **Groups.** `contact.type == "contact_group"`, `contact.id` is a stable thread id, `contact.members[]`
  lists everyone except the target user. Members with `type: "user"` are Dialpad users (team).
  `from_number` is always the real sender.
- **MMS.** `text` is a `https://content.dialpad.com/s/img/...` URL and `text_content` is empty.
- **Reactions.** iMessage fallbacks: `Liked “…”`, `Laughed at “…”`, and `👍 a “…”` wrapped in hair /
  zero-width spaces. Rendered as `[reacted 👍 to “…”]` or dropped.
- **Noise.** Shortcodes (5-6 digit senders), marketing texts, delivery reminders. Shortcodes are skipped;
  everything else is naturally filtered because it won't match a Copper person.
- **Ids** are 16-digit integers; keep them as strings outside Python.
- **Undocumented fields.** `contact.type`, `members`, `is_internal`, `text_content`, `mms`, `mms_url`
  are not in Dialpad's published event docs; read them with `.get()`.

## Processing model

1. Read `texts` rows; parse; collect parse errors without stopping.
2. Dedupe and group into threads: group threads by `contact.id`, 1:1 threads by (team line, contact number).
3. Split each thread into **conversation-days** by Eastern calendar date. Only process days before
   today (Eastern) so every day is complete; today's rows stay for tomorrow's run.
4. Team numbers: every Dialpad line in the data, `type: "user"` group members, `TEAM_PHONE_NUMBERS`
   in `.env`, later `GET /api/v2/users` (`phone_numbers`, `forwarding_numbers`) and `GET /api/v2/numbers`.
5. Match every non-team participant to Copper (below). Conversation-days with no match are not sent
   to the LLM; their numbers go in the run report.
6. One LLM call per matched conversation-day (the summary is shared by everyone in the thread).
7. One Copper activity per person per Eastern day. A person in two threads that day gets one note with
   a section per thread. Group members who did not speak still get the note.
8. Record each write in a local SQLite ledger; move handled rows from `texts` to `processed`; append a
   run summary row to a log tab.

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
- **Limits.** 180 requests/minute rolling. Newline rendering and max length of `details` are not
  documented; test a multi-line note before settling the layout.

## Dialpad API (optional for v1)

`Authorization: Bearer <key>` against `https://dialpad.com/api/v2`. Useful: `GET /users` and
`GET /numbers` (admin key) for team numbers, `GET /subscriptions/sms` to see the live subscription
settings. There is no SMS history endpoint; `POST /stats` with `stat_type: texts, export_type: records`
is an async CSV export that could cross-check the sheet. Google-synced contacts and group ids cannot be
fetched, so the API can't fill in missing group members.

## Open questions

- Is a person in several threads on one day one note (proposed) or one per thread?
- Include the transcript under the summary in the Copper note, at least during the trial?
- Log to Leads as well as People? Several records on one number: log to all, or skip and report?
- Which team members text from numbers Dialpad does not know as users?
- Why were some rows logged without `target`/`members` (Pabbly task history for that window)?
