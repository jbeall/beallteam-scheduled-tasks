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

## Open questions

- Is a person in several threads on one day one note (proposed) or one per thread?
- Include the transcript under the summary in the Copper note, at least during the trial?
- Log to Leads as well as People? Several records on one number: log to all, or skip and report?
- Which team members text from numbers Dialpad does not know as users?
- Why were some rows logged without `target`/`members` (Pabbly task history for that window)?
