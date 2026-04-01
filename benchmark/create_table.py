#!/usr/bin/env python3
"""
Create a BenchExec table XML from mutant result files and run table-generator.
"""

import argparse
import glob
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime


def find_mutant_files(pattern="**/cor_1000_*mutants.csv.*.results.mutant_*.xml.bz2"):
    """Find all mutant result files matching the pattern."""
    files = glob.glob(pattern, recursive=True)
    # Sort by mutant number
    def extract_mutant_num(path):
        match = re.search(r"mutant_(\d+)\.xml\.bz2$", path)
        return int(match.group(1)) if match else 0
    files.sort(key=extract_mutant_num)
    return files


def generate_table_xml(mutant_files, output_file="mutant_results_table.xml"):
    """Generate BenchExec table XML from mutant result files."""
    if not mutant_files:
        print("Error: No mutant result files found")
        return False
    
    # Get paths relative to script directory (convert to absolute first)
    script_dir = Path(__file__).parent
    filenames = [str(Path(f).resolve().relative_to(script_dir.resolve())) for f in mutant_files]
    
    xml_lines = [
        '<?xml version="1.0" ?>',
        '',
        '<!DOCTYPE table PUBLIC "+//IDN sosy-lab.org//DTD BenchExec table 1.10//EN" "https://www.sosy-lab.org/benchexec/table-1.10.dtd">',
        '<table>',
        '  <union id="mutant_results">',
    ]
    
    # Add result entries for each mutant
    for idx, filename in enumerate(filenames, 1):
        result_id = f"m{idx}"
        xml_lines.append(f'    <result id="{result_id}" filename="{filename}"/>')
    
    xml_lines.extend([
        '  </union>',
        '</table>',
    ])
    
    xml_content = '\n'.join(xml_lines)
    
    output_path = Path(output_file)
    output_path.write_text(xml_content)
    print(f"Created {output_file}")
    print(f"Aggregating {len(filenames)} mutant results")
    return True


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
