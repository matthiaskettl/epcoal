#!/usr/bin/env python3

import argparse
import importlib.util
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path


logger = logging.getLogger(__name__)


class TimingStats:
    def __init__(self):
        self.steps = []

    def add(self, name, elapsed):
        self.steps.append((name, float(elapsed)))

    def render(self, wall_total):
        lines = ["Timing statistics:"]
        measured_total = 0.0
        for name, elapsed in self.steps:
            measured_total += elapsed
            lines.append(f"- {name} (s): {elapsed:.3f}")
        lines.append(f"- measured total (s): {measured_total:.3f}")
        lines.append(f"- wall total (s): {float(wall_total):.3f}")
        return "\n".join(lines)


def setup_logging(level_name="INFO"):
    level = getattr(logging, str(level_name).upper(), logging.WARNING)
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def run_command(cmd, cwd):
    logger.info("$ %s", " ".join(str(part) for part in cmd))
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )

    return result


def run_timed_step(step_name, cmd, cwd, stats=None):
    start = time.perf_counter()
    result = run_command(cmd, cwd)
    elapsed = time.perf_counter() - start
    if stats is not None:
        stats.add(step_name, elapsed)
    logger.info("[timing] %s: %.3fs", step_name, elapsed)
    return result, elapsed


def run_timed_python_step(step_name, fn, stats=None):
    start = time.perf_counter()
    value = fn()
    elapsed = time.perf_counter() - start
    if stats is not None:
        stats.add(step_name, elapsed)
    logger.info("[timing] %s: %.3fs", step_name, elapsed)
    return value, elapsed


def classify_cpachecker_output(output_text):
    crashed = []
    for line in output_text.splitlines():
        if "Exception in thread" in line:
            crashed.append(line)
    if len(crashed) > 0:
        logger.warning("CPAchecker appears to have crashed. Detected exception lines:")
        for exc in crashed:
            logger.warning("  %s", exc)
    if "Verification result: TRUE" in output_text:
        return "equivalent"
    if "Verification result: FALSE" in output_text:
        return "not equivalent"
    if len(crashed) > 0:
        return "crash"
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
    timing_stats = TimingStats()
    verdict = "unknown"
    skipped_memcmp_sites = 0
    shutdown_signal = None

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
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Logging verbosity",
    )
    parser.add_argument("--original-prefix", default="original_", help="Prefix for original transformation")
    parser.add_argument("--mutant-prefix", default="mutant_", help="Prefix for mutant transformation")
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Pass benchmark options (--benchmark --heap 13000M) to CPAchecker",
    )
    parser.add_argument(
        "--no-memcmp",
        action="store_true",
        help="Skip opaque fallback comparisons that would otherwise use memcmp",
    )
    parser.add_argument(
        "--pointer-policy",
        choices=["strict", "nullness", "ignore-funcptr"],
        default="strict",
        help="Pointer equality policy used in generated comparisons",
    )
    parser.add_argument(
        "--compare-modified-only",
        action="store_true",
        help="Compare only globals that are assigned/updated in either version (heuristic)",
    )
    args = parser.parse_args()
    setup_logging(args.log_level)

    def _handle_shutdown(signum, _frame):
        nonlocal shutdown_signal
        shutdown_signal = signal.Signals(signum).name
        raise KeyboardInterrupt

    prev_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    try:
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
                logger.error("required path does not exist: %s", required)
                return 2

        if not cpachecker.exists():
            logger.error("CPAchecker executable not found at: %s", cpachecker)
            return 2

        try:
            Transformer = _load_symbol_from_file(transformer_py, "Transformer")
            Merger = _load_symbol_from_file(merge_py, "Merger")
        except Exception as e:
            logger.error("Error loading transformer/merge modules from workdir: %s", e)
            return 2

        # 1) Transform original (in-memory AST)
        try:
            original_code = original_in.read_text()
            original_transformer, _ = run_timed_python_step(
                "create transformer original",
                lambda: Transformer(original_code, prefix=args.original_prefix),
                stats=timing_stats,
            )
            original_ast, _ = run_timed_python_step(
                "transform original", original_transformer.transform, stats=timing_stats
            )
        except Exception as e:
            logger.error("Transformation failed for original program: %s", e)
            return 1

        # 2) Transform mutant (in-memory AST)
        try:
            mutant_code = mutant_in.read_text()
            mutant_transformer, _ = run_timed_python_step(
                "create transformer mutant",
                lambda: Transformer(mutant_code, prefix=args.mutant_prefix),
                stats=timing_stats,
            )
            mutant_ast, _ = run_timed_python_step(
                "transform mutant", mutant_transformer.transform, stats=timing_stats
            )
        except Exception as e:
            logger.error("Transformation failed for mutant program: %s", e)
            return 1

        # 3) Merge from ASTs and write only final merged C
        try:
            merger = Merger.from_asts(
                original_ast,
                args.original_prefix,
                mutant_ast,
                args.mutant_prefix,
                no_memcmp=args.no_memcmp,
                pointer_policy=args.pointer_policy,
                compare_modified_only=args.compare_modified_only,
            )
            merged_ast, _ = run_timed_python_step("merge", merger.merge, stats=timing_stats)
            merged_code = merger.generate_code(merged_ast)
            merged_out.write_text(merged_code)
        except Exception as e:
            logger.exception("Merge step failed: %s", e)
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

        result, _ = run_timed_step("cpachecker", cpachecker_cmd, cwd=workdir, stats=timing_stats)

        combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
        verdict = classify_cpachecker_output(combined_output)
        skipped_memcmp_sites = int(getattr(merger, "skipped_memcmp_sites", 0) or 0)
        if skipped_memcmp_sites > 0:
            logger.info(
                "%d opaque comparison site(s) were skipped due to --no-memcmp",
                skipped_memcmp_sites,
            )

        # With --no-memcmp we may skip opaque comparisons. In that case, a TRUE result
        # cannot be considered fully sound, while FALSE remains a safe witness.
        if args.no_memcmp and skipped_memcmp_sites > 0 and verdict == "equivalent":
            verdict = "equivalent?"

        # Keep FALSE as a normal, successful verification outcome.
        if verdict in ("equivalent", "not equivalent", "equivalent?"):
            return 0

        # Unknown/crash should be surfaced to callers and automation.
        return result.returncode if result.returncode != 0 else 1
    except KeyboardInterrupt:
        if shutdown_signal:
            logger.warning("Shutdown requested via %s", shutdown_signal)
        else:
            logger.warning("Interrupted by user")
        verdict = "interrupted"
        return 130
    finally:
        signal.signal(signal.SIGTERM, prev_sigterm)
        total_elapsed = time.perf_counter() - total_start
        print(timing_stats.render(total_elapsed))
        print(f"Final verdict: {verdict}")


if __name__ == "__main__":
    sys.exit(main())
