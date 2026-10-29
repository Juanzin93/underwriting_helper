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
| Integrity | Immutable facts/runs/policies/audit (DB triggers), corrections append-only, transactions for runs |
| Errors | Generic error pages; logs record exception types, not document text or values |
| Git | Data dir outside repo by default; `.gitignore` excludes PDFs/images/DBs/backups; pre-commit hook blocks them |

## Encryption at rest — limitation
The app does **not** encrypt the SQLite database or documents itself (SQLCipher needs non-standard builds on
Windows). Recommended:
1. Enable **BitLocker** on the drive holding `%LOCALAPPDATA%` (Windows Pro/Enterprise), or Device Encryption on Home.
2. Or set `UWH_DATA_DIR` to a folder inside a **VeraCrypt** volume that you mount only while working.
3. Store backups only on encrypted media; they contain everything.
4. Use a Windows account with a strong password and screen lock.

## Data retention and deletion
* *Case → Delete case…* (type the case number) permanently deletes the case, facts, history, runs, reports,
  its audit trail and documents not shared with another case; a tombstone audit entry (case number only) remains.
* Deleted rows may persist in SQLite free pages/WAL until compaction: after large deletions run
  `sqlite3 uwhelper.db "VACUUM;"` (app stopped) and remember backups still contain the data.
* Define your own retention period and delete closed cases and old backups accordingly.

## Remaining risks
* Local malware or other users with access to your profile can read data.
* The local model can misread or mis-map numbers; verification is the control — do not bulk-verify blindly.
* Heuristic injection detection can miss attacks; it is a warning only.
* OCR errors can change digits; compare with the page image.
* PDF rendering libraries (PyMuPDF/MuPDF) process untrusted files; keep them updated.
* Ollama itself listens on 127.0.0.1:11434 without authentication; other local processes can use it.
* Ollama may check for its own updates; that is outside this app (see LOCAL_ONLY_CHECKLIST.md).
