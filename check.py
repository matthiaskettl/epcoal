#!/usr/bin/env python3

import argparse
import importlib.util
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


def run_timed_python_step(step_name, fn):
    start = time.perf_counter()
    value = fn()
    elapsed = time.perf_counter() - start
    print(f"[timing] {step_name}: {elapsed:.3f}s")
    return value, elapsed


def classify_cpachecker_output(output_text):
    if "Verification result: TRUE" in output_text:
        return "equivalent"
    if "Verification result: FALSE" in output_text:
        return "not equivalent"
    return "unknown"


def _load_symbol_from_file(module_path, symbol_name):
    spec = importlib.util.spec_from_file_location(module_path.stem, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    symbol = getattr(module, symbol_name, None)
    if symbol is None:
        raise ImportError(f"Symbol `{symbol_name}` not found in {module_path}")
    return symbol


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
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Pass benchmark options (--benchmark --heap 13000M) to CPAchecker",
    )
    args = parser.parse_args()

    workdir = Path(args.workdir).resolve()
    original_in = Path(args.original).resolve()
    mutant_in = Path(args.mutant).resolve()
    cpachecker = Path(args.cpachecker).resolve()

    transformer_py = workdir / "transformer.py"
    merge_py = workdir / "merge.py"
    output_dir = (workdir / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    merged_out = output_dir / "merged.c"

    for required in (transformer_py, merge_py, original_in, mutant_in):
        if not required.exists():
            print(f"Error: required path does not exist: {required}", file=sys.stderr)
            return 2

    if not cpachecker.exists():
        print(f"Error: CPAchecker executable not found at: {cpachecker}", file=sys.stderr)
        return 2

    try:
        Transformer = _load_symbol_from_file(transformer_py, "Transformer")
        Merger = _load_symbol_from_file(merge_py, "Merger")
    except Exception as e:
        print(f"Error loading transformer/merge modules from workdir: {e}", file=sys.stderr)
        return 2

    # 1) Transform original (in-memory AST)
    try:
        original_code = original_in.read_text()
        original_transformer, _ = run_timed_python_step(
            "create transformer original",
            lambda: Transformer(original_code, prefix=args.original_prefix),
        )
        original_ast, _ = run_timed_python_step("transform original", original_transformer.transform)
    except Exception as e:
        print(f"Transformation failed for original program: {e}", file=sys.stderr)
        return 1

    # 2) Transform mutant (in-memory AST)
    try:
        mutant_code = mutant_in.read_text()
        mutant_transformer, _ = run_timed_python_step(
            "create transformer mutant",
            lambda: Transformer(mutant_code, prefix=args.mutant_prefix),
        )
        mutant_ast, _ = run_timed_python_step("transform mutant", mutant_transformer.transform)
    except Exception as e:
        print(f"Transformation failed for mutant program: {e}", file=sys.stderr)
        return 1

    # 3) Merge from ASTs and write only final merged C
    try:
        merger = Merger.from_asts(original_ast, args.original_prefix, mutant_ast, args.mutant_prefix)
        merged_ast, _ = run_timed_python_step("merge", merger.merge)
        merged_code = merger.generate_code(merged_ast)
        merged_out.write_text(merged_code)
    except Exception as e:
        print(f"Merge step failed: {e}", file=sys.stderr)
        return 1

    # 4) Run CPAchecker
    cpachecker_cmd = [str(cpachecker)]
    if args.benchmark:
        cpachecker_cmd.extend(["--benchmark", "--heap", "13000M"])
    cpachecker_cmd.extend(
        [
            "--32" if args.datamodel == 32 else "--64",
            "--spec",
            "sv-comp-reachability",
            str(merged_out),
        ]
    )

    result, _ = run_timed_step("cpachecker", cpachecker_cmd, cwd=workdir)

    combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
    verdict = classify_cpachecker_output(combined_output)

    total_elapsed = time.perf_counter() - total_start
    print(f"[timing] total: {total_elapsed:.3f}s")
    print(f"\nFinal verdict: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
