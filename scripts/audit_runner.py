#!/usr/bin/env python3
"""
Run the bundled SEO audit checks and write machine-readable plus report artifacts.

This script reuses generate_report.py's collection, scoring, HTML, and Markdown
renderers so there is one reporting contract and one scoring config.

Usage:
    python audit_runner.py https://example.com
    python audit_runner.py https://example.com --json audit.json --html SEO-REPORT.html
    python audit_runner.py https://example.com --urls-file ./audits/example.com/temp/urls.json

The optional --urls-file flag enables hybrid mode: site-level and aggregate checks
still run on the root URL, but per-page checks (parse_html, social meta, schema,
readability, article) are executed for every URL listed in the JSON file. The file
must follow the urls.json contract produced by audit-prospect-cowork.
"""

import argparse
import json
import os
import sys
from urllib.parse import urlparse

# Force UTF-8 on stdout/stderr so emoji prints in generate_report.py don't crash
# on Windows cp1252 consoles. Idempotent and harmless on Unix.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from generate_report import (
    collect_report_findings,
    calculate_overall_score,
    collect_data,
    generate_html,
    load_scoring_config,
    render_action_plan,
    render_markdown_report,
    write_text_output,
)


def default_html_path(url: str) -> str:
    # Option A flat naming with 02-BHUNA prefix; the file lands in the cwd
    # (which the caller sets to audits/<domain>/) so the audit folder is
    # self-contained.
    return "02-BHUNA-dashboard.html"


def write_json(path: str, payload: dict) -> str:
    output_dir = os.path.dirname(path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    return os.path.abspath(path)


def main():
    parser = argparse.ArgumentParser(description="Run SEO audit and write report artifacts")
    parser.add_argument("url", help="Website URL to audit")
    parser.add_argument("--json", dest="json_output", default="02-BHUNA-data.json",
                        help="JSON output path (default: 02-BHUNA-data.json)")
    parser.add_argument("--html", default="", help="HTML report output path (default: 02-BHUNA-dashboard.html)")
    parser.add_argument("--markdown", default="02-BHUNA-rapport.md",
                        help="Markdown audit output path (default: 02-BHUNA-rapport.md)")
    parser.add_argument("--action-plan", default="02-BHUNA-actions.md",
                        help="Markdown action-plan output path (default: 02-BHUNA-actions.md)")
    parser.add_argument("--actions-json", default="02-BHUNA-actions.json",
                        help="Canonical actions JSON output path "
                             "(default: 02-BHUNA-actions.json). Conforms to "
                             "seo_skills/schemas/action.schema.json. Consumed "
                             "by seo-action-tracker (consolidator) and "
                             "notion-roadmap-builder.")
    parser.add_argument("--no-html", action="store_true", help="Do not write the HTML dashboard")
    parser.add_argument("--no-json", action="store_true", help="Do not write JSON results")
    parser.add_argument("--no-markdown", action="store_true", help="Do not write markdown/action-plan artifacts")
    parser.add_argument("--no-canonical-actions", action="store_true",
                        help="Do not write the canonical actions JSON/MD (skips "
                             "02-BHUNA-actions.json + .md). By default ON for "
                             "downstream pipeline (seo-action-tracker).")
    parser.add_argument("--client-name", default=None,
                        help="Pre-detected client name to inject in the canonical "
                             "actions meta. If None, detection runs from home HTML.")
    parser.add_argument("--audit-date", default=None,
                        help="ISO audit date for canonical actions meta. "
                             "Default: today.")
    parser.add_argument("--urls-file", default=None,
                        help="Optional path to a urls.json file (audit-prospect-cowork contract). "
                             "Enables hybrid mode: per-page checks iterate over the listed URLs.")
    args = parser.parse_args()

    scoring_config = load_scoring_config()
    data = collect_data(args.url, urls_file=args.urls_file)
    scores = calculate_overall_score(data, scoring_config=scoring_config)
    payload = {
        "url": args.url,
        "scores": scores,
        "data": data,
    }

    written = []
    if not args.no_json:
        written.append(("JSON results", write_json(args.json_output, payload)))
    if not args.no_html:
        written.append(("HTML report", write_text_output(args.html or default_html_path(args.url), generate_html(data, scores))))
    if not args.no_markdown:
        written.append(("Full audit report", write_text_output(args.markdown, render_markdown_report(data, scores, scoring_config))))
        written.append(("Action plan", write_text_output(args.action_plan, render_action_plan(data, scores))))

    if not args.no_canonical_actions:
        # Canonical JSON + MD actions, conforming to seo_skills action.schema.json.
        # Consumed downstream by seo-action-tracker (consolidator) and
        # notion-roadmap-builder. Symmetric with generate_report.py behaviour,
        # but inline here so audit_runner.py is a one-shot orchestrator.
        try:
            from seo_skills.bhuna_actions_builder import build_bhuna_actions, write_actions_files
            from datetime import date
            findings = collect_report_findings(data)
            audit_date = args.audit_date or date.today().isoformat()
            actions_data = build_bhuna_actions(
                findings=findings,
                data=data,
                scores=scores,
                audit_date=audit_date,
                client_name=args.client_name,
            )
            json_out_path = os.path.abspath(args.actions_json)
            output_dir = os.path.dirname(json_out_path) or "."
            # write_actions_files writes both .json and .md side by side. We
            # honour --actions-json by renaming the JSON output if needed.
            json_path, md_path = write_actions_files(actions_data, output_dir)
            if os.path.abspath(str(json_path)) != json_out_path:
                os.replace(str(json_path), json_out_path)
                json_path = json_out_path
            written.append(("Canonical actions JSON", str(json_path)))
            written.append(("Canonical actions MD", str(md_path)))
        except ImportError:
            print("  Skipped canonical actions (seo_skills not installed: pip install -e <repo>)")
        except Exception as exc:
            print(f"  Skipped canonical actions due to error: {exc}")

    print("\nAudit artifacts:")
    for label, path in written:
        print(f"   {label}: {path}")
    print(f"   Overall Score: {scores['overall']}/100")


if __name__ == "__main__":
    main()
