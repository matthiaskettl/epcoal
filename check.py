#!/usr/bin/env python3

import argparse
import subprocess
import sys
import time
from pathlib import Path


def run_command(cmd, cwd):
    print("$", " ".join(str(part) for part in cmd))
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )

    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)

    return result


def run_timed_step(step_name, cmd, cwd):
    start = time.perf_counter()
    result = run_command(cmd, cwd)
    elapsed = time.perf_counter() - start
    print(f"[timing] {step_name}: {elapsed:.3f}s")
    return result, elapsed


def classify_cpachecker_output(output_text):
    if "Verification result: TRUE" in output_text:
        return "equivalent"
    if "Verification result: FALSE" in output_text:
        return "not equivalent"
    return "unknown"


def main():
    total_start = time.perf_counter()

    parser = argparse.ArgumentParser(
        description="Transform original/mutant C files, merge them, and run CPAchecker."
    )
    parser.add_argument("original", help="Path to original C file")
    parser.add_argument("--mutant", help="Path to mutant C file", required=True)
    parser.add_argument(
        "--workdir",
        default=str(Path(__file__).resolve().parent),
        help="Working directory containing transformer.py and merge.py",
    )
    parser.add_argument(
        "--datamodel",
        type=int,
        choices=[32, 64],
        default=32,
        help="Data model of the input program (32 or 64 bit)",
    )
    parser.add_argument(
        "--cpachecker",
        default=str((Path(__file__).resolve().parent / "lib" / "cpachecker" / "bin" / "cpachecker").resolve()),
        help="Path to CPAchecker executable",
    )
    parser.add_argument("--output-dir", default="output", help="Directory for generated files")
    parser.add_argument("--original-prefix", default="original_", help="Prefix for original transformation")
    parser.add_argument("--mutant-prefix", default="mutant_", help="Prefix for mutant transformation")
    args = parser.parse_args()

    workdir = Path(args.workdir).resolve()
    original_in = Path(args.original).resolve()
    mutant_in = Path(args.mutant).resolve()
    cpachecker = Path(args.cpachecker).resolve()

    transformer_py = workdir / "transformer.py"
    merge_py = workdir / "merge.py"
    output_dir = (workdir / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    original_out = output_dir / "original_transformed.c"
    mutant_out = output_dir / "mutant_transformed.c"
    merged_out = output_dir / "merged.c"

    for required in (transformer_py, merge_py, original_in, mutant_in):
        if not required.exists():
            print(f"Error: required path does not exist: {required}", file=sys.stderr)
            return 2

    if not cpachecker.exists():
        print(f"Error: CPAchecker executable not found at: {cpachecker}", file=sys.stderr)
        return 2

    py = sys.executable

    # 1) Transform original
    result, _ = run_timed_step(
        "transform original",
        [
            py,
            str(transformer_py),
            str(original_in),
            str(original_out),
            "--prefix",
            args.original_prefix,
        ],
        cwd=workdir,
    )
    if result.returncode != 0:
        print("Transformation failed for original program.", file=sys.stderr)
        return result.returncode

    # 2) Transform mutant
    result, _ = run_timed_step(
        "transform mutant",
        [
            py,
            str(transformer_py),
            str(mutant_in),
            str(mutant_out),
            "--prefix",
            args.mutant_prefix,
        ],
        cwd=workdir,
    )
    if result.returncode != 0:
        print("Transformation failed for mutant program.", file=sys.stderr)
        return result.returncode

    # 3) Merge
    result, _ = run_timed_step(
        "merge",
        [
            py,
            str(merge_py),
            str(original_out),
            args.original_prefix,
            str(mutant_out),
            args.mutant_prefix,
            str(merged_out),
        ],
        cwd=workdir,
    )
    if result.returncode != 0:
        print("Merge step failed.", file=sys.stderr)
        return result.returncode

    # 4) Run CPAchecker
    result, _ = run_timed_step(
        "cpachecker",
        [
            str(cpachecker),
            "--32" if args.datamodel == 32 else "--64",
            "--spec",
            "sv-comp-reachability",
            str(merged_out),
        ],
        cwd=workdir,
    )

    combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
    verdict = classify_cpachecker_output(combined_output)

    total_elapsed = time.perf_counter() - total_start
    print(f"[timing] total: {total_elapsed:.3f}s")
    print(f"\nFinal verdict: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
