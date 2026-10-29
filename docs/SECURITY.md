# Security, privacy and threat model

This is a single-user, local application. Binding to localhost **reduces** exposure but is **not** complete
security: anything running as your Windows user (malware, browser extensions, other local software) can read
the data directory and talk to `127.0.0.1`.

## Controls implemented
| Area | Control |
|---|---|
| Network exposure | Uvicorn binds `127.0.0.1`; non-loopback bind refused unless explicitly overridden |
| No external AI | Ollama URL must be `http://` loopback; `trust_env=False` (proxies ignored); redirects refused; cloud/remote models refused (`:cloud` tags, `remote_host`/`remote_model` metadata) |
| No telemetry | No analytics, crash reporting, CDNs, web fonts or remote logs; API docs (which load CDN assets) disabled; CSP `default-src 'self'` |
| Browser attacks on localhost | CSRF token on every state change, Origin check, Host allow-list (DNS rebinding), `X-Frame-Options: DENY`, `frame-ancestors 'none'`, no inline scripts |
| Uploads | Extension allow-list + magic-byte check, size limit (50 MB default), page limit, hash-named storage (no path traversal), originals served only as downloads |
| Document content | Never executed; PDFs parsed by PyMuPDF; text rendered escaped (Jinja autoescape); report pages get a no-script CSP |
| Prompt injection | Delimited data with per-request nonce, delimiter spoofing neutralised, system prompt forbids following document instructions, JSON-schema output, per-fact validation, unknown fields dropped, value must appear on cited page, heuristic page flagging, model has no tools, **nothing used until a human verifies** |
| SQL injection | Parameterised SQL only; Pydantic validation of every form |
| Integrity | Immutable facts/runs/policies/decisions/fundings/payments/audit (DB triggers), corrections append-only, transactions for every multi-row write |
| Audit evidence | Hash-chained audit log, verified on the System and Audit pages and by `uwhelper verify-audit` |
| Lifecycle safety | Gate checks; hard blockers cannot be overridden; human confirmation for decisions, waivers and funding; no money movement or document execution |
| Errors | Generic error pages; logs record exception types, not document text or values |
| Telemetry in dependencies | FastAPI's built-in OpenTelemetry support disabled (`telemetry` off, `OTEL_SDK_DISABLED=true`); no exporter installed; test asserts it |
| Integrations | Registry with two switches (`UWH_EXTERNAL_INTEGRATIONS_ALLOWED` + per-name `UWH_ENABLED_INTEGRATIONS`), share-confirmation helper; none implemented |
| API | JSON API needs `X-CSRF-Token`; no CORS headers; errors carry a request ID but never input values |
| Notes & imports | Note/call text stays in the database only (audit events hold ids, never note text; nothing in the log file); CSV imports are read in memory, not stored |
| CRE imports | Rent-roll and T-12 CSVs are parsed in memory; only the parsed units/lines are stored (immutable once verified). No model is involved; nothing leaves the PC |
| Lender catalog | Imported from local files only; matching runs locally; the app never contacts lenders or sends deal data |
| Submission packages | Built and exported locally only after a named confirmation of the listed files; files re-hashed before export; the app never transmits a package |
| CSV export | Portfolio CSV is generated on request and saved by the browser to this PC; cells a spreadsheet would run as formulas are escaped |
| Attribution | Actor = Windows account name or `UWH_ACTOR_NAME`; this is attribution, not authentication |
| Redirects | Form "return to" targets must be local paths (no open redirects) |
| Static files | Settings refuse a data/DB/document folder inside `web/static` |
| Tests | Autouse guard blocks non-loopback sockets and DNS during tests |
| Git | `.env` ignored; data dir outside repo by default; `.gitignore` excludes PDFs/images/DBs/backups; pre-commit hook blocks them |

## Encryption at rest — limitation
The app does **not** encrypt the SQLite database or documents itself (SQLCipher needs non-standard builds on
Windows). Recommended:
1. Enable **BitLocker** on the drive holding `%LOCALAPPDATA%` (Windows Pro/Enterprise), or Device Encryption on Home.
2. Or set `UWH_DATA_DIR` to a folder inside a **VeraCrypt** volume that you mount only while working.
3. Store backups only on encrypted media; they contain everything.
4. Use a Windows account with a strong password and screen lock.

## Data retention and deletion
* Funded/serviced loans cannot be purged from the app (servicing and audit records must be retained); archive them.
* *Case → Delete case…* (type the case number) permanently deletes the case, facts, history, runs, reports,
  its audit trail and documents not shared with another case; a tombstone audit entry (case number only) remains.
* Deleted rows may persist in SQLite free pages/WAL until compaction: after large deletions run
  `sqlite3 uwhelper.db "VACUUM;"` (app stopped) and remember backups still contain the data.
* Define your own retention period and delete closed cases and old backups accordingly.

## Tamper evidence — limitation
SQLite triggers stop accidental edits through the application, and the hash chain detects edits made without
recomputing it. Neither makes the file immutable: anyone who can write to the database can drop triggers, rewrite
events and recompute all hashes. To detect that, periodically record the head hash (System page) somewhere else
(paper, another device) and keep backups on separate encrypted media.

## Remaining risks
* Local malware or other users with access to your profile can read data.
* The local model can misread or mis-map numbers; verification is the control — do not bulk-verify blindly.
* Heuristic injection detection can miss attacks; it is a warning only.
* OCR errors can change digits; compare with the page image.
* PDF rendering libraries (PyMuPDF/MuPDF) process untrusted files; keep them updated.
* Ollama itself listens on 127.0.0.1:11434 without authentication; other local processes can use it.
* Ollama may check for its own updates; that is outside this app (see LOCAL_ONLY_CHECKLIST.md).
