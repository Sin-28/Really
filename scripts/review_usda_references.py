#!/usr/bin/env python3
"""Generate a read-only USDA reference review packet or apply one explicit decision."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.recipes.ingredient_reference import REFERENCE_CATEGORIES
from src.recipes.reference_review_workflow import (
    apply_manifest_decision,
    build_reference_review_manifest,
    render_review_report,
)
from src.runtime import create_ingredient_identity_runtime


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _generate(args):
    runtime = create_ingredient_identity_runtime(PROJECT_ROOT / "data", auto_verify_exact=False)
    try:
        manifest = build_reference_review_manifest(
            runtime,
            max_candidates_per_category=args.max_candidates_per_category,
            query_limit=args.query_limit,
        )
        _write_json(args.manifest, manifest)
        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_review_report(manifest), encoding="utf-8")
        print(json.dumps({
            "manifest": str(Path(args.manifest).resolve()),
            "report": str(report.resolve()),
            "entries": len(manifest["entries"]),
            "all_pending_review": all(item["review_status"] == "PENDING_REVIEW" for item in manifest["entries"]),
            "persistent_database_writes": False,
        }, indent=2))
    finally:
        runtime.close()


def _decide(args):
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    attributes = json.loads(args.attributes_json) if args.attributes_json else None
    provenance = json.loads(args.provenance_json) if args.provenance_json else None
    if attributes is not None and not isinstance(attributes, dict):
        raise ValueError("--attributes-json must decode to a JSON object.")
    if provenance is not None and not isinstance(provenance, (dict, list, str)):
        raise ValueError("--provenance-json must decode to a JSON object, array, or string.")
    runtime = create_ingredient_identity_runtime(PROJECT_ROOT / "data", auto_verify_exact=False)
    try:
        outcome = apply_manifest_decision(
            runtime, manifest,
            category=args.category,
            fdc_id=args.fdc_id,
            decision=args.decision,
            reviewer_identity=args.reviewer_identity,
            provenance=provenance,
            attributes=attributes,
            allow_as_default=args.allow_as_default,
            rationale=args.rationale,
            confirm_persist=args.confirm_persist,
        )
        if outcome["write_performed"]:
            _write_json(manifest_path, manifest)
        print(json.dumps(outcome, ensure_ascii=False, indent=2, default=str))
    finally:
        runtime.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="Generate a bounded read-only review packet.")
    generate.add_argument("--manifest", default="reports/usda_reference_review_manifest_20261016.json")
    generate.add_argument("--report", default="reports/usda_reference_review_20261016.md")
    generate.add_argument("--max-candidates-per-category", type=int, default=10)
    generate.add_argument("--query-limit", type=int, default=12)
    generate.set_defaults(handler=_generate)

    decide = subparsers.add_parser("decide", help="Preview or persist one explicit reviewer decision.")
    decide.add_argument("--manifest", required=True)
    decide.add_argument("--category", required=True, choices=sorted(REFERENCE_CATEGORIES))
    decide.add_argument("--fdc-id", required=True, type=int)
    decide.add_argument("--decision", required=True, choices=("approve", "reject"))
    decide.add_argument("--reviewer-identity")
    decide.add_argument("--provenance-json", help="JSON evidence supplied by the human reviewer.")
    decide.add_argument("--attributes-json", help="Reviewer-confirmed reference attributes as a JSON object.")
    decide.add_argument("--allow-as-default", action="store_true", help="Explicitly permit a context default after review.")
    decide.add_argument("--rationale")
    decide.add_argument("--confirm-persist", action="store_true",
                        help="Required to write this explicit decision to Neo4j.")
    decide.set_defaults(handler=_decide)

    args = parser.parse_args(argv)
    args.handler(args)


if __name__ == "__main__":
    main()
