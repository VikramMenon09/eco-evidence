"""Command-line entry point; no installation needed from the checkout."""

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .core import BundleReader, EvidenceError, NetworkReader, Query, export_bundle
from .inspection import inspect_bundle, render_text
from .panel import export_panel
from .registry import INDICATORS
from .study import export_study


def main(argv=None):
    parser = argparse.ArgumentParser(prog="eco-evidence", description="Create reproducible environmental economics evidence bundles.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("indicators", help="List the reviewed indicator registry (no network).")
    fetch = commands.add_parser("fetch", help="Fetch one indicator, or replay an archived query offline.")
    fetch.add_argument("--indicator", required=True, help="Registry alias or World Bank indicator code")
    fetch.add_argument("--countries", required=True, help="Comma-separated ISO3-style World Bank economy codes, e.g. USA,IND")
    fetch.add_argument("--start-year", type=int, required=True)
    fetch.add_argument("--end-year", type=int, required=True)
    fetch.add_argument("--output", type=Path, required=True, help="New output directory; never overwrites an existing bundle")
    fetch.add_argument("--cache-dir", type=Path, default=Path(".cache/eco-evidence"))
    fetch.add_argument("--refresh", action="store_true", help="Retrieve every page afresh instead of reusing cache entries")
    fetch.add_argument("--offline", type=Path, help="Saved bundle to replay; makes zero network requests")
    fetch.add_argument("--timeout", type=float, default=15, help="Blocking network-operation timeout in seconds (1–30); not a total download deadline")
    panel = commands.add_parser("panel", help="Join 2–3 archived indicators by country and year, entirely offline.")
    panel.add_argument("--bundle", type=Path, action="append", required=True,
                       help="Input evidence bundle; repeat for each distinct indicator")
    panel.add_argument("--output", type=Path, required=True,
                       help="New panel directory; includes coverage and replayable input bundles")
    inspect = commands.add_parser("inspect", help="Inspect archived metadata and compare source vintages offline.")
    inspect.add_argument("--bundle", type=Path, required=True, help="Indicator evidence bundle to inspect")
    inspect.add_argument("--baseline", type=Path,
                         help="Reference bundle with the exact same query; compares baseline to bundle")
    inspect.add_argument("--format", choices=("text", "json"), default="text",
                         help="Report format on stdout (default: text); never writes inside an archive")
    study = commands.add_parser("study", help="Compare output per person with renewable energy shares offline.")
    study.add_argument("--gdp-bundle", type=Path, required=True)
    study.add_argument("--population-bundle", type=Path, required=True)
    study.add_argument("--renewables-bundle", type=Path, required=True)
    study.add_argument("--output", type=Path, required=True,
                       help="New study directory; retains source inputs and all requested country-years")
    args = parser.parse_args(argv)
    if args.command == "indicators":
        for item in INDICATORS:
            print(f"{item.alias:12} {item.code:18} {item.unit}\n  {item.name}\n  {item.metadata_url}")
        return 0
    try:
        if args.command == "study":
            provenance = export_study(args.gdp_bundle, args.population_bundle, args.renewables_bundle, args.output)
            print(json.dumps({"output": str(args.output.resolve()), "rows": provenance["row_count"],
                              "derived": provenance["derived_count"], "complete_pairs": provenance["pair_complete_count"],
                              "data_origin": provenance["data_origin"]}, sort_keys=True))
            return 0
        if args.command == "inspect":
            report = inspect_bundle(args.bundle, args.baseline)
            if args.format == "json":
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(render_text(report), end="")
            return 0
        if args.command == "panel":
            provenance = export_panel(args.bundle, args.output)
            print(json.dumps({"output": str(args.output.resolve()), "rows": provenance["row_count"],
                              "complete_cases": provenance["complete_case_count"],
                              "indicators": [item["alias"] for item in provenance["inputs"]],
                              "data_origin": provenance["data_origin"]}, sort_keys=True))
            return 0
        if args.offline and args.refresh:
            raise EvidenceError("--offline and --refresh cannot be combined.")
        query = Query(args.indicator, tuple(code.strip().upper() for code in args.countries.split(",")),
                      args.start_year, args.end_year)
        reader = BundleReader(args.offline) if args.offline else NetworkReader(args.cache_dir, refresh=args.refresh, timeout=args.timeout)
        provenance = export_bundle(query, reader, args.output)
    except (EvidenceError, OSError) as exc:
        print(f"eco-evidence: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(args.output.resolve()), "rows": provenance["row_count"],
                      "counts": provenance["counts"], "data_origin": provenance["data_origin"]}, sort_keys=True))
    return 0
