#!/usr/bin/env python3
"""
Create a BenchExec table XML from mutant result files and run table-generator.
"""

import argparse
import glob
import os
import re
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed


RESULTS_PATTERN = "*_1000_*equivalent_mutants.csv.*.mutant_*.xml.bz2"
LATEST_RESULT_RE = re.compile(
    r"^(?P<prefix>.+?)_1000_(?P<kind>non_)?equivalent_mutants\.csv\.(?P<stamp>.+?)\.mutant_(?P<mutant>\d+)\.xml\.bz2$"
)


def find_mutant_files(pattern="**/cor_1000_*mutants.csv.*.results.mutant_*.xml.bz2"):
    """Find all mutant result files matching the pattern."""
    files = glob.glob(pattern, recursive=True)

    # Sort by mutant number
    def extract_mutant_num(path):
        match = re.search(r"mutant_(\d+)\.xml\.bz2$", path)
        return int(match.group(1)) if match else 0

    files.sort(key=extract_mutant_num)
    return files


def find_latest_jobs(results_dir):
    """Find the latest equivalent and non-equivalent result sets per prefix."""
    discovered = {}

    for path in Path(results_dir).glob(RESULTS_PATTERN):
        match = LATEST_RESULT_RE.match(path.name)
        if not match:
            continue

        prefix = match.group("prefix")
        kind = match.group("kind") or ""
        stamp = match.group("stamp")
        mutant_num = int(match.group("mutant"))

        key = (prefix, kind, stamp)
        entry = discovered.setdefault(key, {"files": [], "mutants": set()})
        entry["files"].append(path)
        entry["mutants"].add(mutant_num)

    latest_by_prefix = {}
    for (prefix, kind, stamp), entry in discovered.items():
        key = (prefix, kind)
        if key not in latest_by_prefix or stamp > latest_by_prefix[key][0]:
            latest_by_prefix[key] = (stamp, entry)

    latest_jobs = []
    for (prefix, kind), (stamp, entry) in sorted(latest_by_prefix.items()):
        if len(entry["mutants"]) != 1000:
            print(
                f"Warning: latest set for {prefix} {kind or 'equivalent'} has {len(entry['mutants'])} mutants, expected 1000"
            )

        pattern = str(
            Path(results_dir)
            / f"{prefix}_1000_{kind}equivalent_mutants.csv.{stamp}.mutant_*.xml.bz2"
        )
        output_file = str(Path(__file__).parent / f"{prefix}_{kind}equivalent.xml")
        latest_jobs.append((pattern, output_file))

    return latest_jobs


def generate_table_xml(mutant_files, output_file="mutant_results_table.xml"):
    """Generate BenchExec table XML from mutant result files."""
    if not mutant_files:
        print("Error: No mutant result files found")
        return False

    # Get paths relative to script directory (convert to absolute first)
    script_dir = Path(__file__).parent
    filenames = [
        str(Path(f).resolve().relative_to(script_dir.resolve())) for f in mutant_files
    ]

    xml_lines = [
        '<?xml version="1.0" ?>',
        "",
        '<!DOCTYPE table PUBLIC "+//IDN sosy-lab.org//DTD BenchExec table 1.10//EN" "https://www.sosy-lab.org/benchexec/table-1.10.dtd">',
        "<table>",
        '  <union id="mutant_results">',
    ]

    # Add result entries for each mutant
    for idx, filename in enumerate(filenames, 1):
        result_id = f"m{idx}"
        xml_lines.append(f'    <result id="{result_id}" filename="{filename}"/>')

    xml_lines.extend(
        [
            "  </union>",
            "</table>",
        ]
    )

    xml_content = "\n".join(xml_lines)

    output_path = Path(output_file)
    output_path.write_text(xml_content)
    print(f"Created {output_file}")
    print(f"Aggregating {len(filenames)} mutant results")
    return True


def run_job(pattern, output_file, benchexec_path):
    """Run the full create-table flow for a single pattern/output pair."""
    print(f"Searching for mutant result files matching: {pattern}")
    files = find_mutant_files(pattern)

    if not files:
        print(f"No files found matching pattern: {pattern}")
        return 1

    print(f"Found {len(files)} mutant result files")

    if not generate_table_xml(files, output_file):
        return 1

    if not run_table_generator(output_file, benchexec_path):
        return 1

    return 0


def run_jobs_parallel(jobs, benchexec_path):
    """Run independent jobs in parallel and return the worst exit code."""
    if not jobs:
        return 0

    max_workers = min(len(jobs), os.cpu_count() or 1)
    if max_workers <= 1:
        exit_code = 0
        for pattern, output_file in jobs:
            job_exit_code = run_job(pattern, output_file, benchexec_path)
            if job_exit_code != 0:
                exit_code = job_exit_code
        return exit_code

    exit_code = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(run_job, pattern, output_file, benchexec_path): (
                pattern,
                output_file,
            )
            for pattern, output_file in jobs
        }
        for future in as_completed(futures):
            job_exit_code = future.result()
            if job_exit_code != 0:
                exit_code = job_exit_code

    return exit_code


def run_table_generator(xml_file, benchexec_path="./benchexec"):
    """Run the BenchExec table-generator tool."""
    generator = Path(benchexec_path) / "bin" / "table-generator"

    if not generator.exists():
        print(f"Error: table-generator not found at {generator}")
        return False

    try:
        cmd = [str(generator), "-x", str(Path(xml_file).absolute())]
        print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=Path(xml_file).parent, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error running table-generator: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Create and process BenchExec table from mutant result files"
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Process the latest timestamped equivalent and non-equivalent result sets for each prefix",
    )
    parser.add_argument(
        "--pattern",
        default="cor_1000_*mutants.csv.*.results.mutant_*.xml.bz2",
        help="Glob pattern for mutant result files (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        default="mutant_results_table.xml",
        help="Output XML file (default: %(default)s)",
    )
    parser.add_argument(
        "--benchexec-path",
        default=Path(__file__).parent / "benchexec",
        help="Path to benchexec directory (default: %(default)s)",
    )
    parser.add_argument(
        "--no-generate",
        action="store_true",
        help="Only create XML, don't run table-generator",
    )

    args = parser.parse_args()

    if args.latest:
        script_dir = Path(__file__).parent.resolve()
        jobs = find_latest_jobs(script_dir / "results")

        if not jobs:
            print("No latest mutant result sets found")
            return 1

        return run_jobs_parallel(jobs, args.benchexec_path)

    print("Searching for mutant result files...")
    files = find_mutant_files(args.pattern)

    if not files:
        print(f"No files found matching pattern: {args.pattern}")
        return 1

    print(f"Found {len(files)} mutant result files")

    if not generate_table_xml(files, args.output):
        return 1

    if not args.no_generate:
        if not run_table_generator(args.output, args.benchexec_path):
            return 1

    print("Done!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
