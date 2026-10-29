# Licensing notes (not legal advice)

| Component | License | Notes |
|---|---|---|
| pypdfium2 (bundles Google PDFium) | BSD-3-Clause / Apache-2.0 (PDFium: BSD-3 + third-party notices shipped in the wheel) | Reads and renders PDFs |
| ReportLab (open-source toolkit) | BSD | Writes report, memo and sample PDFs |
| Pillow | MIT-CMU (HPND) | Images for OCR and page previews |
| charset-normalizer | MIT | ReportLab dependency |
| FastAPI, Pydantic, httpx | MIT / BSD | |
| Uvicorn, Starlette, Jinja2, python-multipart | BSD | |
| PyYAML | MIT | |
| certifi | MPL-2.0 | httpx dependency (CA bundle); MPL is file-level: obligations apply only to changes made to certifi's own files |
| Other runtime packages (anyio, h11, httpcore, idna, click, starlette, pydantic-core, typing-extensions, …) | MIT / BSD / Apache-2.0 / PSF | checked from the installed package metadata of `requirements.lock` |
| pytest | MIT | dev only |
| Tesseract OCR | Apache-2.0 | separate install |
| Ollama | MIT | separate install |
| Qwen3 models (`qwen3:8b`, `qwen3:4b`) | Apache-2.0 | check the model card for the exact tag you pull; other models carry their own licenses (e.g. Llama community license, Gemma terms) |

The demo policies and sample data are original, synthetic and illustrative.

## PDF library decision (Milestone 8.2)
Until Milestone 7 the app used PyMuPDF (AGPL-3.0). AGPL would have required offering the complete source code to
anyone the app is given to (or a commercial licence). It has been **replaced** by pypdfium2 (reading/rendering),
ReportLab (writing) and Pillow (images), all permissive. No AGPL or GPL component remains among the runtime
dependencies listed in `requirements.lock`. Fonts used in generated PDFs are read from the PC (e.g. Arial/Segoe UI on
Windows) and embedded as subsets, which those fonts' licences allow for documents.
