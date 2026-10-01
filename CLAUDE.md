# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

Backend for the **Python Master 2026** AI chatbot: a Flask + MySQL API that answers
candidates' questions from an internal FAQ first and falls back to Google Gemini
(with FAQ retrieval context) when no FAQ matches. It also hands conversations over to
human staff after repeated bot failures.

All code lives in `pmaster-ai-assistant-feature-HaiAnhAPIandDB/` (not the repo root).
Run every command below from inside that folder.

## Setup and run

```bash
cd pmaster-ai-assistant-feature-HaiAnhAPIandDB
pip install -r requirements.txt
# create .env (see Environment below), then create the database:
mysql -u root -p < DB_PythonMaster.sql
python import_faqs.py              # load danh_sach_faq.xlsx (sheet "Trang tính1") into `faqs`
python create_staff.py <username>  # grant the 'staff' role to a user
python app.py                      # serves on 0.0.0.0:5000
```

Existing databases from older versions need the migrations, in order:
`migration_agent_support.sql`, then `migration_conversation_summary.sql`.

`demo-website.html` is a static page for manually testing the chat widget against a local server.

There is no test suite, linter, or formatter configured. Verify changes by running the
app and hitting the endpoints (e.g. with `demo-website.html` or curl).

## Environment

Read via `python-dotenv` in `config.py`. `.env` is never committed.

- Required: `DB_PASSWORD`, `GEMINI_API_KEY`
- DB: `DB_HOST` (localhost), `DB_USER` (root), `DB_NAME` (gemini_chat_db)
- Gemini: `GEMINI_MODEL_NAME`, `GEMINI_MODERATION_MODEL_NAME`, `GEMINI_THINKING_LEVEL`,
  `GEMINI_MAX_OUTPUT_TOKENS`, `GEMINI_TIMEOUT_MS`, `GEMINI_MAX_RETRIES`
- Tuning: `CONVERSATION_RECENT_TURNS`, `CONVERSATION_SUMMARY_MIN_OLD_MESSAGES`,
  `RAG_TOP_K`, `RAG_MAX_CHARS_PER_FIELD`, `UPLOAD_FOLDER`

Add new tunables to `config.py` as `os.getenv(...)` with a sensible default, not as
literals in the modules.

## Layout

- `app.py`: Flask app, logging, registers blueprints and the CORS/site-key hooks
- `routes/chat.py`: public `/api/chat/*` endpoints (init, history, faq, request-agent, chat, image)
- `routes/staff.py`: `/api/staff/*` endpoints (conversations, notifications, claim, reply, close)
- `routes/uploads.py`: serves uploaded images
- `cors.py`: every `/api/chat*` request must carry a valid `site_key` (body or `X-Site-Key`)
  whose `allowed_domain` matches the `Origin` host
- `security.py`: prompt-injection and internal-data-request detection, staff role check
- `moderation.py`: Gemini-based content violation check (logs to `violation_logs`)
- `faq_matcher.py`: FAQ matching (strong phrase, keyword ratio, Gemini intent) and RAG candidates
- `gemini_client.py`: the only place that calls Gemini (chat, summarise, intent classify, retry)
- `conversation_memory.py`: sends recent turns verbatim and summarises older ones
- `image_handler.py`: image validation (5 MB limit), storage and Gemini preparation
- `database.py`: all SQL helpers (PyMySQL, `DictCursor`)
- `DB_PythonMaster.sql`: full schema (users, sites, conversations, messages, faqs,
  violation_logs, agent_notifications)

## Chat pipeline (`POST /api/chat`)

Order matters; keep it when editing:

1. Get or create the user and conversation. A closed conversation reopens to the bot;
   one in `waiting_agent`/`agent` only stores the message and the bot stays silent.
2. Reject prompt injection, then internal-data requests, then moderation violations,
   **before** any FAQ lookup or Gemini call.
3. Save the user message.
4. Exact/fuzzy FAQ match returns the standard answer (`tra_loi_chuan`) without Gemini.
5. Otherwise retrieve top `RAG_TOP_K` FAQs as context and ask Gemini, which must prefer
   internal sources and not invent facts.
6. After 3 turns the bot couldn't answer (`FAIL_COUNT_ESCALATE_THRESHOLD`) the conversation
   escalates to staff and an `agent_notifications` row is created.

## Conventions

- Code comments, docstrings and user-facing messages are in **Vietnamese**; keep new ones
  in Vietnamese to match.
- Use parameterised SQL (`%s` placeholders) through helpers in `database.py`; open a
  connection per request and close it in `finally`.
- Use `logging` (not `print`). Never log API keys or the content of user messages.
- Keep a single retry layer in `gemini_client._send_with_retry` (retryable: 429/500/502/503/504);
  don't add SDK-level retries on top.
- Schema changes go in `DB_PythonMaster.sql` **and** a new `migration_*.sql` for existing databases.

## Known limitations

- `/api/staff/*` trusts the `staff_id` sent by the client (query or `X-Staff-Id`) and
  only checks its role; there is no staff login. Don't expose these routes publicly.
- No `.env.example` or `.gitignore` exist yet, despite `CHANGELOG_MUST_HAVE.md` mentioning one.
- Comments reference `AUDIT_REPORT.md`, which is not in this repo.
