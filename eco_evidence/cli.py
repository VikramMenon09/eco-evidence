"""Command-line entry point; no installation needed from the checkout."""

import argparse
import json
from pathlib import Path
import sys

from .core import BundleReader, EvidenceError, NetworkReader, Query, export_bundle
from .registry import INDICATORS


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create reproducible environmental economics evidence bundles.")
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
    fetch.add_argument("--timeout", type=float, default=15, help="Per-request timeout in seconds (1–30)")
    args = parser.parse_args(argv)
    if args.command == "indicators":
        for item in INDICATORS:
            print(f"{item.alias:12} {item.code:18} {item.unit}\n  {item.name}\n  {item.metadata_url}")
        return 0
    try:
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
