"""Check the built package's public CLI, independently of source-tree imports.

Install a wheel, then run: python -I scripts/check_installed.py
Uses only the standard library and the existing, explicitly synthetic archive.
"""

import csv
import hashlib
from importlib.metadata import version
import io
import json
import os
from pathlib import Path
import subprocess
import sysconfig
import tempfile

import eco_evidence


def main():
    checkout = Path(__file__).resolve().parents[1]
    if Path(eco_evidence.__file__).resolve().is_relative_to(checkout / "eco_evidence"):
        raise RuntimeError("This check must import an installed wheel, not the source tree.")
    command = Path(sysconfig.get_path("scripts")) / ("eco-evidence.exe" if os.name == "nt" else "eco-evidence")
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONNOUSERSITE"] = "1"
    fixture = checkout / "examples" / "synthetic-population"
    installed_version = version("eco-evidence")
    if eco_evidence.__version__ != installed_version:
        raise RuntimeError("Package metadata and runtime versions disagree.")

    with tempfile.TemporaryDirectory(prefix="eco-installed-check-") as directory:
        def run(arguments, expected=0):
            result = subprocess.run(
                [str(command), *arguments], cwd=directory, env=environment,
                text=True, capture_output=True, timeout=30, check=False,
            )
            if result.returncode != expected:
                raise RuntimeError(f"Installed command returned {result.returncode}: {result.stderr}")
            return result

        if run(["--version"]).stdout.strip() != f"eco-evidence {installed_version}":
            raise RuntimeError("The installed version command disagrees with package metadata.")
        arguments = [
            "fetch", "--indicator", "population", "--countries", "USA",
            "--start-year", "2020", "--end-year", "2022",
            "--offline", str(fixture), "--output", "replay",
        ]
        result = json.loads(run(arguments).stdout)
        bundle = Path(directory) / "replay"
        csv_bytes = (bundle / "data.csv").read_bytes()
        if csv_bytes != (fixture / "data.csv").read_bytes():
            raise RuntimeError("Installed replay changed the synthetic fixture's CSV.")
        manifest = json.loads((bundle / "provenance.json").read_text(encoding="utf-8"))
        if manifest["tool_version"] != installed_version:
            raise RuntimeError("Exported provenance records the wrong installed version.")
        if manifest["csv_sha256"] != hashlib.sha256(csv_bytes).hexdigest():
            raise RuntimeError("Exported CSV checksum is incorrect.")
        rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))
        expected_rows = [("2020", "not_returned", ""), ("2021", "missing", ""), ("2022", "available", "0")]
        if ([(row["year"], row["status"], row["value"]) for row in rows] != expected_rows
                or any(row["data_origin"] != "synthetic_fixture" for row in rows)):
            raise RuntimeError("Zero, missing, omitted or synthetic-origin semantics changed.")
        if result["rows"] != 3 or result["data_origin"] != "synthetic_fixture":
            raise RuntimeError("Installed CLI summary is inconsistent with its output.")
        def snapshot():
            return {path.relative_to(directory): path.read_bytes() if path.is_file() else None
                    for path in Path(directory).rglob("*")}

        original = snapshot()
        refused = run(arguments, expected=2)
        if refused.stdout or "Output already exists" not in refused.stderr:
            raise RuntimeError("The second command did not refuse the existing output as expected.")
        if original != snapshot():
            raise RuntimeError("Refusing an existing output changed the earlier evidence.")
    print(f"Installed eco-evidence {installed_version}: replay, provenance and preservation passed.")


if __name__ == "__main__":
    main()
