"""Command line entry point: ``python -m uwhelper <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from uwhelper import __version__
from uwhelper.config import ConfigError, load_settings


def _logging(settings) -> None:
    settings.ensure_dirs()
    handler = RotatingFileHandler(settings.data_dir / "uwhelper.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stderr))


def cmd_serve(args, settings) -> int:
    import uvicorn

    from uwhelper.web.app import create_app

    app = create_app(settings)
    print(f"UW Helper {__version__} → http://{settings.host}:{settings.port}  (data: {settings.data_dir})")
    print("Local only. Press Ctrl+C to stop.")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info", access_log=False,
                server_header=False, proxy_headers=False)
    return 0


def cmd_init(args, settings) -> int:
    from uwhelper.bootstrap import initialise

    _, report = initialise(settings)
    print(f"Database ready at {settings.db_path}")
    print(f"Policies loaded: {report.loaded or 'none new'}; unchanged: {report.unchanged}")
    for e in report.errors:
        print(f"POLICY ERROR: {e}")
    return 1 if report.errors else 0


def cmd_doctor(args, settings) -> int:
    from uwhelper.bootstrap import initialise
    from uwhelper.diagnostics import run_checks

    initialise(settings)
    failed = 0
    for c in run_checks(settings, check_ollama=not args.skip_ollama):
        mark = "INFO" if c["ok"] is None else ("OK  " if c["ok"] else "FAIL")
        failed += c["ok"] is False
        print(f"[{mark}] {c['name']}: {c['detail']}")
    return 1 if failed else 0


def cmd_load_sample(args, settings) -> int:
    from uwhelper.bootstrap import initialise
    from uwhelper.sample_data import load_sample_case

    db, _ = initialise(settings)
    with db.session() as conn:
        case_id = load_sample_case(conn, settings)
        number = conn.execute("SELECT case_number FROM loan_cases WHERE id=?", (case_id,)).fetchone()[0]
    print(f"Loaded fictional sample case {number} (id {case_id}). Start the app and open it from the dashboard.")
    return 0


def cmd_load_lifecycle_demo(args, settings) -> int:
    from uwhelper.bootstrap import initialise
    from uwhelper.sample_lifecycle import load_lifecycle_demo

    db, _ = initialise(settings)
    with db.session() as conn:
        created = load_lifecycle_demo(conn, settings)
    print("Loaded fictional demo cases (one per stage, all SYNTHETIC):")
    for stage, cid in created.items():
        print(f"  {stage:<14} {cid}")
    return 0


def cmd_sample_pdfs(args, settings) -> int:
    from uwhelper.sample_data import write_sample_pdfs

    for p in write_sample_pdfs(Path(args.out)):
        print(p)
    return 0


def cmd_verify_audit(args, settings) -> int:
    from uwhelper.bootstrap import initialise
    from uwhelper.repo.audit import verify_chain

    db, _ = initialise(settings)
    with db.session() as conn:
        v = verify_chain(conn)
    print(f"Audit chain {'INTACT' if v['ok'] else 'PROBLEMS FOUND'}: {v['checked']} events, "
          f"{v['purged_placeholders']} purged placeholders. Head hash: {v['head']}")
    for p in v["problems"]:
        print("  " + p)
    return 0 if v["ok"] else 1


def cmd_backup(args, settings) -> int:
    from uwhelper.backup import create_backup

    out = Path(args.out) if args.out else settings.data_dir / "backups" / "uwhelper-backup.zip"
    if not args.out:
        from datetime import datetime

        out = out.with_name(f"uwhelper-backup-{datetime.now():%Y%m%d-%H%M%S}.zip")
    path = create_backup(settings, out)
    print(f"Backup written: {path}\nIt contains sensitive borrower data — store it on encrypted media.")
    return 0


def cmd_restore(args, settings) -> int:
    from uwhelper.backup import restore_backup

    info = restore_backup(settings, Path(args.file), force=args.force)
    print(f"Restored {info['files']} file(s) from backup created {info['created_at']} into {settings.data_dir}")
    return 0


def cmd_extract(args, settings) -> int:
    from uwhelper.bootstrap import initialise
    from uwhelper.llm.client import client_from_settings
    from uwhelper.llm.extraction import extract_document_facts

    db, _ = initialise(settings)
    with db.session() as conn, client_from_settings(settings) as client:
        s = extract_document_facts(conn, client, args.case_id, args.document_id,
                                   max_chars=settings.extraction_max_chars_per_chunk, retries=settings.ollama_retries)
    print(f"{s.status}: {s.facts_created} candidate fact(s); chunks failed {s.chunks_failed}/{s.chunks_total}. {s.error_message or ''}")
    return 0 if s.status != "failed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uwhelper", description="Local-first commercial loan underwriting workbench")
    parser.add_argument("--data-dir", help="Override the data directory (default: %%LOCALAPPDATA%%\\UWHelper)")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve", help="Start the web app on 127.0.0.1")
    p.add_argument("--port", type=int)
    p.add_argument("--model", help="Ollama model, e.g. qwen3:4b")
    sub.add_parser("init", help="Create/upgrade the database and load policy files")
    p = sub.add_parser("doctor", help="Check local-only configuration, Ollama, model and Tesseract")
    p.add_argument("--skip-ollama", action="store_true")
    sub.add_parser("load-sample", help="Load the fictional sample loan case")
    sub.add_parser("load-lifecycle-demo", help="Load fictional cases at every lifecycle stage, incl. a serviced loan")
    sub.add_parser("verify-audit", help="Verify the audit log hash chain")
    p = sub.add_parser("sample-pdfs", help="Write the synthetic sample PDFs to a folder")
    p.add_argument("--out", default="samples/generated")
    p = sub.add_parser("backup", help="Write a backup ZIP (database + documents + reports)")
    p.add_argument("--out")
    p = sub.add_parser("restore", help="Restore a backup ZIP into the data directory")
    p.add_argument("file")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("extract", help="Run local extraction for one document")
    p.add_argument("case_id")
    p.add_argument("document_id")
    args = parser.parse_args(argv)

    overrides: dict = {}
    if args.data_dir:
        overrides["data_dir"] = args.data_dir
    if getattr(args, "port", None):
        overrides["port"] = args.port
    if getattr(args, "model", None):
        overrides["ollama_model"] = args.model
    try:
        settings = load_settings(**overrides)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    _logging(settings)
    handlers = {"serve": cmd_serve, "init": cmd_init, "doctor": cmd_doctor, "load-sample": cmd_load_sample,
                "load-lifecycle-demo": cmd_load_lifecycle_demo, "verify-audit": cmd_verify_audit,
                "sample-pdfs": cmd_sample_pdfs, "backup": cmd_backup, "restore": cmd_restore, "extract": cmd_extract}
    try:
        return handlers[args.command](args, settings)
    except Exception as exc:  # concise CLI errors; details in the local log
        logging.getLogger("uwhelper").debug("command failed", exc_info=exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
