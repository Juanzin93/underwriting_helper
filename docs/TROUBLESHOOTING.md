# Troubleshooting

## Ollama
| Symptom | Fix |
|---|---|
| `unavailable: Cannot reach Ollama` | Start the Ollama app (tray icon) or run `ollama serve`. Test: `curl http://127.0.0.1:11434/api/version` |
| `model_missing` | `ollama pull qwen3:8b` (or the configured model); check `ollama list` |
| `remote_model_refused` | You selected a `:cloud` model. Choose a local model |
| `timeout` | First call loads the model (can take a minute). Increase `ollama_timeout_seconds`, use `qwen3:4b`, or lower `extraction_max_chars_per_chunk` |
| `bad_response` (malformed JSON) | Update Ollama (structured outputs need ≥ 0.5); retry; try a different model |
| Few/no facts extracted | Check *Extraction runs* notes: `value_not_on_page` means the model cited numbers not on the page (discarded on purpose); `no_period` means the page did not state a period — enter values manually |
| Ollama bound to 0.0.0.0 | Remove any `OLLAMA_HOST=0.0.0.0` environment variable so Ollama stays on loopback |

## NVIDIA GPU memory
* `nvidia-smi` shows VRAM use; `ollama ps` shows whether the model runs on GPU or CPU ("100% GPU").
* `qwen3:8b` (Q4) needs roughly 5–6 GB VRAM plus context; with 8192 context on a 6–8 GB GPU prefer `qwen3:4b`
  or lower `ollama_num_ctx` to 4096. 16 GB system RAM is enough for CPU fallback but it is slow.
* Close other GPU-heavy apps; update the NVIDIA driver. Ollama falls back to CPU automatically if VRAM is short.
* `OLLAMA_KEEP_ALIVE` / the app's `keep_alive=10m` keeps the model loaded between chunks.

## OCR (Tesseract)
* `tesseract --version` in a new PowerShell. If not found: `winget install --id UB-Mannheim.TesseractOCR`, then
  set `tesseract_cmd = "C:\\Program Files\\Tesseract-OCR\\tesseract.exe"` in `uwhelper.toml` if not on PATH.
* "page(s) have no extractable text — Tesseract OCR is not installed" → install, then *Re-process* the document.
* Poor accuracy: use 300 dpi scans; `ocr_dpi = 400` for small print; other languages need traineddata
  files and `tesseract_lang = "eng+spa"`.

## Windows permissions
* "running scripts is disabled": run `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`, or
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
* Port in use: `scripts\start.ps1 -Port 8800`. Find the owner: `netstat -ano | findstr 8765`.
* Windows Firewall prompt: not needed (loopback only) — you can click Cancel.
* Antivirus locking `uwhelper.db`: exclude the data directory from real-time scanning or retry.
* OneDrive-synced folders can corrupt SQLite and upload data to the cloud — keep the data dir out of OneDrive.

## App
* "Missing or invalid CSRF token": the app was restarted; reload the page.
* "Policy files with errors" banner: fix the YAML shown, or bump the version if you edited a registered one.
* Runs say INCOMPLETE: open *Missing info* and *Contradictions*; verified-only runs ignore unverified values.
