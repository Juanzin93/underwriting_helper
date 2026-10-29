# Checklist: confirm no external AI service is used

1. `python -m uwhelper doctor` → all OK: loopback bind, loopback Ollama URL, model installed locally (not cloud).
2. `ollama list` shows the model with a size of several GB (cloud models show `-` / `:cloud`).
3. `ollama ps` while extracting shows the model running locally (GPU/CPU).
4. No API keys exist in the app: search the code — `Select-String -Path src\* -Recurse -Pattern "api_key|openai|anthropic|https://"` finds no service endpoints.
5. Disconnect from the internet (airplane mode). Upload a PDF, run extraction, run screening, generate a report — all work.
6. Watch connections while using the app: `Get-NetTCPConnection -OwningProcess (Get-Process python).Id` shows only
   `127.0.0.1` peers (8765 server, 11434 Ollama).
7. In the browser's DevTools → Network, every request goes to `127.0.0.1:8765` (no CDN, fonts or analytics).
8. Confirm `OLLAMA_HOST` is unset or `127.0.0.1`, and you are not signed in to Ollama cloud features
   (`ollama signout` if you ever signed in).
9. Optional hard guarantee: Windows Defender Firewall outbound block rules for `python.exe` (in `.venv`) and
   `ollama.exe`. Loopback traffic is unaffected; Ollama model downloads then need the rule disabled temporarily.
10. Keep the data directory out of OneDrive/Dropbox/Google Drive sync folders.

Note: Ollama and pip may contact the internet for downloads/updates you initiate; the application itself makes
no outbound connections other than to loopback Ollama.
