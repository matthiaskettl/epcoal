#!/usr/bin/env python3

from __future__ import annotations

import argparse
import glob
import difflib
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

sys.path.append(str((Path(__file__).absolute().parent.parent / "lib" / "pip")))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd


SUCCESS_STATUSES = [
    "done (equivalent)",
    "done (not equivalent)",
    "done (equivalent?)",
]

TABLE_NAME_RX = re.compile(r"^(?P<prefix>.+?)_(?P<kind>non_)?equivalent\.table\.csv$")
RUN_ID_RX = re.compile(r"^m(?P<index>\d+)$")
MUTATION_OPERATOR_RX = re.compile(r"\.mutant\.(?P<operator>cor_[^.]+)\.\d+\.[^.]+$")


def collect_table_files(pattern: str, repo_root: Path) -> list[Path]:
    if Path(pattern).is_absolute():
        return sorted(Path(path) for path in glob.glob(pattern))

    return sorted(repo_root.glob(pattern))


def extract_error_messages_from_log_zip(log_zip: Path) -> list[str]:
    """Extract all lines starting with 'Error:' from one .logfiles.zip archive."""
    error_lines: list[str] = []
    with zipfile.ZipFile(log_zip) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            with archive.open(member, "r") as handle:
                for raw_line in handle:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if line.lower().startswith("error:"):
                        error_lines.append(line[6:].split(":", 1)[0].strip())
    return error_lines


def collect_errors_per_logfile(log_zips: list[str | Path]) -> dict[str, list[str]]:
    """Return all Error: lines grouped by logfile zip path.

    Example input filename shape:
    aor_1000_equivalent_mutants.csv.2026-04-07_17-25-46.logfiles.zip
    """
    zip_paths = [Path(path) for path in log_zips]
    if not zip_paths:
        return {}

    max_workers = min(len(zip_paths), os.cpu_count() or 1)
    if max_workers <= 1:
        return {str(path): extract_error_messages_from_log_zip(path) for path in zip_paths}

    collected: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(extract_error_messages_from_log_zip, path): path for path in zip_paths}
        for future in as_completed(futures):
            path = futures[future]
            collected[str(path)] = future.result()
    return collected


def build_logfile_stats(errors_per_logfile: dict[str, list[str]]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for logfile, messages in sorted(errors_per_logfile.items()):
        unique_messages = set(messages)
        top_error = ""
        top_count = 0
        if messages:
            counts: dict[str, int] = {}
            for msg in messages:
                counts[msg] = counts.get(msg, 0) + 1
            top_error, top_count = max(counts.items(), key=lambda item: item[1])

        rows.append(
            {
                "logfile": logfile,
                "error_count": len(messages),
                "unique_error_count": len(unique_messages),
                "has_errors": bool(messages),
                "top_error_count": top_count,
                "top_error": top_error,
            }
        )

    return pd.DataFrame(rows)


def print_logfile_stats(logfile_stats: pd.DataFrame) -> None:
    print("\\nLogfile Error Stats")
    print("=" * 80)
    if logfile_stats.empty:
        print("No logfile stats available")
        return

    name_width = min(60, max(len("logfile"), int(logfile_stats["logfile"].map(len).max())))
    header = f"{'logfile':<{name_width}}  {'errors':>8}  {'unique':>8}"
    print(header)
    print("-" * len(header))

    for _, row in logfile_stats.iterrows():
        logfile = str(row["logfile"])
        if len(logfile) > name_width:
            logfile = "..." + logfile[-(name_width - 3):]
        print(f"{logfile:<{name_width}}  {int(row['error_count']):>8}  {int(row['unique_error_count']):>8}")

    total_files = int(logfile_stats.shape[0])
    files_with_errors = int(logfile_stats["has_errors"].sum())
    total_errors = int(logfile_stats["error_count"].sum())
    total_unique = int(logfile_stats["unique_error_count"].sum())
    print("-" * len(header))
    print(
        f"files={total_files}, files_with_errors={files_with_errors}, total_errors={total_errors}, total_unique_errors={total_unique}"
    )


def write_logfile_stats_text(logfile_stats: pd.DataFrame, output_file: Path) -> None:
    lines: list[str] = ["Logfile Error Stats", "=" * 80]
    if logfile_stats.empty:
        lines.append("No logfile stats available")
        output_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    name_width = min(60, max(len("logfile"), int(logfile_stats["logfile"].map(len).max())))
    header = f"{'logfile':<{name_width}}  {'errors':>8}  {'unique':>8}"
    lines.append(header)
    lines.append("-" * len(header))
    for _, row in logfile_stats.iterrows():
        logfile = str(row["logfile"])
        if len(logfile) > name_width:
            logfile = "..." + logfile[-(name_width - 3):]
        lines.append(f"{logfile:<{name_width}}  {int(row['error_count']):>8}  {int(row['unique_error_count']):>8}")

    total_files = int(logfile_stats.shape[0])
    files_with_errors = int(logfile_stats["has_errors"].sum())
    total_errors = int(logfile_stats["error_count"].sum())
    total_unique = int(logfile_stats["unique_error_count"].sum())
    lines.append("-" * len(header))
    lines.append(
        f"files={total_files}, files_with_errors={files_with_errors}, total_errors={total_errors}, total_unique_errors={total_unique}"
    )

    output_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_table_name(path: Path) -> tuple[str, str]:
    match = TABLE_NAME_RX.match(path.name)
    if not match:
        raise ValueError(
            f"Unexpected table file name: {path.name}. Expected PREFIX_equivalent.table.csv or PREFIX_non_equivalent.table.csv"
        )

    prefix = match.group("prefix")
    kind = "non_equivalent" if match.group("kind") else "equivalent"
    return prefix, kind


def load_table_csv(path: Path) -> pd.DataFrame:
    prefix, kind = parse_table_name(path)
    frame = pd.read_csv(path, sep="\t", skiprows=2, dtype=str, engine="python")
    frame.columns = [str(column).strip() for column in frame.columns]

    rename_map: dict[str, str] = {}
    for column in frame.columns:
        lowered = column.lower()
        if lowered == "status":
            rename_map[column] = "status"
        elif "cputime" in lowered:
            rename_map[column] = "cputime_s"
        elif "walltime" in lowered:
            rename_map[column] = "walltime_s"
        elif "memory" in lowered:
            rename_map[column] = "memory_mb"

    if frame.columns.size:
        rename_map.setdefault(frame.columns[0], "task")

    frame = frame.rename(columns=rename_map)

    run_id_column = infer_run_id_column(frame)

    required = {"status", "cputime_s", "walltime_s", "memory_mb"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"{path} is missing expected columns: {sorted(missing)}")

    selected_columns = ["task", "status", "cputime_s", "walltime_s", "memory_mb"]
    if run_id_column:
        selected_columns.insert(1, run_id_column)

    frame = frame[selected_columns].copy()
    if run_id_column:
        frame = frame.rename(columns={run_id_column: "run_id"})
    else:
        frame["run_id"] = ""

    frame["run_index"] = frame["run_id"].astype(str).str.extract(RUN_ID_RX, expand=False)
    frame["run_index"] = pd.to_numeric(frame["run_index"], errors="coerce")
    frame["source_file"] = path.name
    frame["prefix"] = prefix
    frame["equiv_kind"] = kind
    frame["status"] = frame["status"].fillna("(missing)").astype(str).str.strip()
    for column in ["cputime_s", "walltime_s", "memory_mb"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame["successful"] = frame["status"].isin(SUCCESS_STATUSES)
    frame["status_group"] = frame["status"].where(frame["successful"], other="other")
    frame["status_group"] = pd.Categorical(
        frame["status_group"],
        categories=SUCCESS_STATUSES + ["other"],
        ordered=True,
    )
    return frame


def infer_run_id_column(frame: pd.DataFrame) -> str | None:
    for column in frame.columns:
        lowered = str(column).strip().lower()
        if lowered in {"status", "cputime_s", "walltime_s", "memory_mb", "task", "host"}:
            continue
        values = frame[column].astype(str).str.strip()
        if values.empty:
            continue
        matches = values.str.fullmatch(RUN_ID_RX)
        if matches.mean() >= 0.8:
            return column
    return None


def status_to_predicted_label(status: str) -> str:
    normalized = str(status).strip().lower()
    if "done (not equivalent)" in normalized:
        return "not_equivalent"
    if "done (equivalent)" in normalized or "done (equivalent?)" in normalized:
        return "equivalent"
    return "unknown"


def extract_mutation_operator(mutant_path: str) -> str:
    match = MUTATION_OPERATOR_RX.search(mutant_path)
    if not match:
        return "unknown"
    return match.group("operator")


def build_diff_features(original_file: Path, mutant_file: Path, mutant_path: str) -> dict[str, object]:
    try:
        original_lines = original_file.read_text(encoding="utf-8", errors="replace").splitlines()
        mutant_lines = mutant_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {
            "operator": extract_mutation_operator(mutant_path),
            "pattern_signature": "file_missing",
            "hunk_count": 0,
            "added_lines": 0,
            "removed_lines": 0,
            "changed_line_count": 0,
            "contains_or": False,
            "contains_and": False,
            "contains_not": False,
            "contains_true_false": False,
            "contains_comparison": False,
            "contains_arithmetic": False,
            "diff_excerpt": "",
        }

    diff_lines = list(
        difflib.unified_diff(
            original_lines,
            mutant_lines,
            fromfile=str(original_file),
            tofile=str(mutant_file),
            lineterm="",
        )
    )

    added: list[str] = []
    removed: list[str] = []
    hunks = 0
    excerpts: list[str] = []
    for line in diff_lines:
        if line.startswith("@@"):
            hunks += 1
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            content = line[1:]
            added.append(content)
            if len(excerpts) < 6:
                excerpts.append(f"+ {content}")
        elif line.startswith("-"):
            content = line[1:]
            removed.append(content)
            if len(excerpts) < 6:
                excerpts.append(f"- {content}")

    changed_text = "\n".join(added + removed)
    contains_or = "||" in changed_text
    contains_and = "&&" in changed_text
    contains_not = bool(re.search(r"(?<![=!<>])!(?!=)", changed_text))
    contains_true_false = bool(re.search(r"\b(true|false)\b", changed_text))
    contains_comparison = bool(re.search(r"==|!=|<=|>=|<|>", changed_text))
    contains_arithmetic = bool(re.search(r"[+\-*/%]", changed_text))

    operator = extract_mutation_operator(mutant_path)
    tags = [operator]
    if contains_or:
        tags.append("logic_or")
    if contains_and:
        tags.append("logic_and")
    if contains_not:
        tags.append("logic_not")
    if contains_true_false:
        tags.append("bool_literal")
    if contains_comparison:
        tags.append("comparison")
    if contains_arithmetic:
        tags.append("arithmetic")
    if len(tags) == 1:
        tags.append("other")

    return {
        "operator": operator,
        "pattern_signature": "|".join(tags),
        "hunk_count": hunks,
        "added_lines": len(added),
        "removed_lines": len(removed),
        "changed_line_count": len(added) + len(removed),
        "contains_or": contains_or,
        "contains_and": contains_and,
        "contains_not": contains_not,
        "contains_true_false": contains_true_false,
        "contains_comparison": contains_comparison,
        "contains_arithmetic": contains_arithmetic,
        "diff_excerpt": "\\n".join(excerpts),
    }


def resolve_existing_path(repo_root: Path, relative_path: str) -> Path:
    rel = Path(relative_path)
    candidates = [
        repo_root / "benchmark" / rel,
        repo_root / "benchmark" / "sv-benchmarks" / rel,
        repo_root / rel,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def run_qualitative_misclassification_analysis(repo_root: Path, output_dir: Path) -> None:
    cases = [
        {
            "name": "1000_equivalent_mutants",
            "mapping_csv": repo_root / "benchmark" / "cor_1000_equivalent_mutants.csv",
            "table_csv": repo_root / "benchmark" / "cor_equivalent.table.csv",
            "focus_predicted_label": "not_equivalent",
            "description": "Equivalent mutants classified as not equivalent",
        },
        {
            "name": "1000_non_equivalent_mutants",
            "mapping_csv": repo_root / "benchmark" / "cor_1000_non_equivalent_mutants.csv",
            "table_csv": repo_root / "benchmark" / "cor_non_equivalent.table.csv",
            "focus_predicted_label": "equivalent",
            "description": "Non-equivalent mutants classified as equivalent",
        },
    ]

    qualitative_root = output_dir / "qualitative"
    qualitative_root.mkdir(parents=True, exist_ok=True)
    index_rows: list[dict[str, object]] = []

    for case in cases:
        mapping_csv = case["mapping_csv"]
        table_csv = case["table_csv"]
        if not mapping_csv.exists() or not table_csv.exists():
            continue

        mapping = pd.read_csv(mapping_csv, dtype=str).fillna("")
        if "original_path" not in mapping.columns or "mutant_path" not in mapping.columns:
            continue

        table_frame = load_table_csv(table_csv)
        table_frame = table_frame.copy()
        table_frame["predicted_label"] = table_frame["status"].map(status_to_predicted_label)

        if table_frame["run_index"].notna().any():
            table_subset = table_frame[["run_index", "status", "predicted_label"]].dropna(subset=["run_index"])
            table_subset["run_index"] = table_subset["run_index"].astype(int)
            mapping = mapping.reset_index(drop=True)
            mapping["run_index"] = mapping.index + 1
            joined = mapping.merge(table_subset, on="run_index", how="left")
        else:
            statuses = table_frame[["status", "predicted_label"]].reset_index(drop=True)
            joined = mapping.reset_index(drop=True).join(statuses)

        joined["predicted_label"] = joined["predicted_label"].fillna("unknown")
        joined["status"] = joined["status"].fillna("(missing)")

        focus_predicted_label = str(case["focus_predicted_label"])
        focus = joined[joined["predicted_label"] == focus_predicted_label].copy()

        feature_rows: list[dict[str, object]] = []
        for _, row in focus.iterrows():
            original_path = str(row["original_path"]).strip()
            mutant_path = str(row["mutant_path"]).strip()
            original_file = resolve_existing_path(repo_root, original_path)
            mutant_file = resolve_existing_path(repo_root, mutant_path)
            features = build_diff_features(original_file, mutant_file, mutant_path)

            feature_rows.append(
                {
                    "original_path": original_path,
                    "mutant_path": mutant_path,
                    "status": str(row["status"]),
                    "predicted_label": str(row["predicted_label"]),
                    **features,
                }
            )

        case_output_dir = qualitative_root / str(case["name"])
        case_output_dir.mkdir(parents=True, exist_ok=True)

        feature_frame = pd.DataFrame(feature_rows)
        if feature_frame.empty:
            feature_frame = pd.DataFrame(
                columns=[
                    "original_path",
                    "mutant_path",
                    "status",
                    "predicted_label",
                    "operator",
                    "pattern_signature",
                    "hunk_count",
                    "added_lines",
                    "removed_lines",
                    "changed_line_count",
                    "contains_or",
                    "contains_and",
                    "contains_not",
                    "contains_true_false",
                    "contains_comparison",
                    "contains_arithmetic",
                    "diff_excerpt",
                ]
            )

        pattern_counts = (
            feature_frame["pattern_signature"].value_counts(dropna=False).rename_axis("pattern_signature").reset_index(name="count")
            if not feature_frame.empty
            else pd.DataFrame(columns=["pattern_signature", "count"])
        )

        operator_counts = (
            feature_frame["operator"].value_counts(dropna=False).rename_axis("operator").reset_index(name="count")
            if not feature_frame.empty
            else pd.DataFrame(columns=["operator", "count"])
        )

        feature_frame.to_csv(case_output_dir / "misclassified_cases.csv", index=False)
        pattern_counts.to_csv(case_output_dir / "pattern_counts.csv", index=False)
        operator_counts.to_csv(case_output_dir / "operator_counts.csv", index=False)

        summary_lines = [
            f"Case: {case['name']}",
            f"Description: {case['description']}",
            f"Total benchmark entries: {len(joined)}",
            f"Misclassified entries in scope: {len(feature_frame)}",
            f"Focus predicted label: {focus_predicted_label}",
            "",
            "Top pattern signatures:",
        ]
        if pattern_counts.empty:
            summary_lines.append("  (none)")
        else:
            for _, pattern_row in pattern_counts.head(15).iterrows():
                summary_lines.append(f"  {pattern_row['pattern_signature']}: {int(pattern_row['count'])}")

        summary_lines.append("")
        summary_lines.append("Top operators:")
        if operator_counts.empty:
            summary_lines.append("  (none)")
        else:
            for _, operator_row in operator_counts.head(15).iterrows():
                summary_lines.append(f"  {operator_row['operator']}: {int(operator_row['count'])}")

        (case_output_dir / "summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

        index_rows.append(
            {
                "case": case["name"],
                "description": case["description"],
                "total_entries": len(joined),
                "misclassified_entries": len(feature_frame),
                "focus_predicted_label": focus_predicted_label,
            }
        )

    if index_rows:
        pd.DataFrame(index_rows).to_csv(qualitative_root / "index.csv", index=False)


def load_all_tables(table_files: list[Path]) -> pd.DataFrame:
    max_workers = min(len(table_files), os.cpu_count() or 1)
    if max_workers <= 1:
        frames = [load_table_csv(path) for path in table_files]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            frames = list(executor.map(load_table_csv, table_files))
    if not frames:
        raise ValueError("No table data loaded")

    combined = pd.concat(frames, ignore_index=True)
    combined["status"] = combined["status"].fillna("(missing)").astype(str)
    return combined


def build_status_counts(frame: pd.DataFrame) -> pd.DataFrame:
    counts = frame["status"].value_counts(dropna=False).rename_axis("status").reset_index(name="count")
    counts["share_pct"] = counts["count"] / counts["count"].sum() * 100.0
    return counts


def print_summary(frame: pd.DataFrame, status_counts: pd.DataFrame, table_files: list[Path]) -> None:
    total_rows = len(frame)
    unique_statuses = int(status_counts.shape[0])
    successful_rows = int(frame["successful"].sum())
    successful_pct = successful_rows / total_rows * 100.0 if total_rows else 0.0

    print(f"Loaded {len(table_files)} table files")
    print(f"Total rows: {total_rows}")
    print(f"Unique statuses: {unique_statuses}")
    print(f"Successful rows: {successful_rows} ({successful_pct:.2f}%)")
    print("Top statuses:")
    for _, row in status_counts.head(10).iterrows():
        print(f"  {row['status']}: {int(row['count'])}")


def plot_status_counts(status_counts: pd.DataFrame, output_dir: Path) -> None:
    top_counts = status_counts.head(20).copy()
    if len(status_counts) > 20:
        other_count = int(status_counts.iloc[20:]["count"].sum())
        if other_count > 0:
            top_counts = pd.concat(
                [top_counts, pd.DataFrame([["other", other_count, 0.0]], columns=top_counts.columns)],
                ignore_index=True,
            )

    top_counts = top_counts.sort_values("count", ascending=True)

    fig, ax = plt.subplots(figsize=(12, max(6, 0.35 * len(top_counts) + 2)))
    ax.barh(top_counts["status"], top_counts["count"], color="#3b82f6")
    ax.set_title("Status counts")
    ax.set_xlabel("Count")
    ax.set_ylabel("Status")
    ax.grid(axis="x", linestyle="--", alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_dir / "status_counts.png", dpi=200)
    plt.close(fig)


def plot_metric_by_status(frame: pd.DataFrame, metric: str, ylabel: str, output_dir: Path) -> None:
    data = []
    labels = []
    for status in SUCCESS_STATUSES + ["other"]:
        values = frame.loc[frame["status_group"] == status, metric].dropna()
        values = values[values > 0]
        if not values.empty:
            data.append(values.to_list())
            labels.append(status)

    fig, ax = plt.subplots(figsize=(10, 6))
    if data:
        try:
            box = ax.boxplot(data, tick_labels=labels, patch_artist=True, showfliers=False)
        except TypeError:
            box = ax.boxplot(data, labels=labels, patch_artist=True, showfliers=False)
        for patch in box["boxes"]:
            patch.set_facecolor("#60a5fa")
            patch.set_alpha(0.75)
        for median in box["medians"]:
            median.set_color("#0f172a")
            median.set_linewidth(2)

    ax.set_title(f"{ylabel} by status")
    ax.set_ylabel(ylabel)
    ax.set_yscale("log")
    ax.grid(axis="y", linestyle="--", alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_dir / f"{metric}_by_status.png", dpi=200)
    plt.close(fig)


def write_summary_files(frame: pd.DataFrame, status_counts: pd.DataFrame, output_dir: Path) -> None:
    status_counts.to_csv(output_dir / "status_counts.csv", index=False)

    summary = pd.DataFrame(
        [
            {
                "total_rows": len(frame),
                "unique_statuses": int(status_counts.shape[0]),
                "successful_rows": int(frame["successful"].sum()),
                "successful_pct": frame["successful"].mean() * 100.0 if len(frame) else 0.0,
                "cpu_median_s": frame["cputime_s"].median(),
                "walltime_median_s": frame["walltime_s"].median(),
                "memory_median_mb": frame["memory_mb"].median(),
            }
        ]
    )
    summary.to_csv(output_dir / "summary.csv", index=False)


def run_analysis_for_subset(frame: pd.DataFrame, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    status_counts = build_status_counts(frame)
    write_summary_files(frame, status_counts, output_dir)
    plot_status_counts(status_counts, output_dir)
    plot_metric_by_status(frame, "cputime_s", "CPU time (s)", output_dir)
    plot_metric_by_status(frame, "walltime_s", "Wall time (s)", output_dir)
    plot_metric_by_status(frame, "memory_mb", "Memory (MB)", output_dir)


def run_analysis_job(job: tuple[pd.DataFrame, Path]) -> str:
    frame, output_dir = job
    run_analysis_for_subset(frame, output_dir)
    return str(output_dir)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze BenchExec table CSV files with status-based plots and summary stats."
    )
    parser.add_argument(
        "--input-glob",
        default="benchmark/*equivalent.table.csv",
        help="Glob for table CSV files relative to the repository root (default: %(default)s)",
    )
    parser.add_argument(
        "--output-dir",
        default="benchmark/analysis",
        help="Directory for plots and summary files (default: %(default)s)",
    )
    parser.add_argument(
        "--logfiles",
        nargs="+",
        default=[],
        help="List of *.logfiles.zip files to scan for lines starting with 'Error:'",
    )

    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    table_files = collect_table_files(args.input_glob, repo_root)
    if not table_files:
        print(f"No table CSV files found for pattern: {args.input_glob}")
        return 1

    output_dir = repo_root / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    log_zip_paths = [Path(path) for path in args.logfiles]
    errors_per_logfile = collect_errors_per_logfile(log_zip_paths)
    logfile_stats = build_logfile_stats(errors_per_logfile)

    error_rows: list[dict[str, str]] = []
    for logfile, messages in sorted(errors_per_logfile.items()):
        if not messages:
            error_rows.append({"logfile": logfile, "error": ""})
            continue
        for message in messages:
            error_rows.append({"logfile": logfile, "error": message})

    pd.DataFrame(error_rows).to_csv(output_dir / "log_errors.csv", index=False)
    logfile_stats.to_csv(output_dir / "logfile_stats.csv", index=False)
    write_logfile_stats_text(logfile_stats, output_dir / "logfile_stats.txt")
    with (output_dir / "log_errors.json").open("w", encoding="utf-8") as handle:
        json.dump(errors_per_logfile, handle, indent=2)

    print_logfile_stats(logfile_stats)

    frame = load_all_tables(table_files)
    status_counts = build_status_counts(frame)
    print_summary(frame, status_counts, table_files)

    jobs: list[tuple[pd.DataFrame, Path]] = []

    # 1) Overall analysis across all selected files.
    jobs.append((frame, output_dir / "overall"))

    # 2) Analysis per (prefix, equivalent/non-equivalent).
    by_prefix_kind_root = output_dir / "by_prefix_kind"
    for (prefix, equiv_kind), group in frame.groupby(["prefix", "equiv_kind"], sort=True):
        jobs.append((group.copy(), by_prefix_kind_root / f"{prefix}_{equiv_kind}"))

    # 3) Combined analysis per prefix (equivalent + non-equivalent).
    by_prefix_root = output_dir / "by_prefix"
    for prefix, group in frame.groupby("prefix", sort=True):
        jobs.append((group.copy(), by_prefix_root / prefix))

    max_workers = min(len(jobs), os.cpu_count() or 1)
    if max_workers <= 1:
        for job in jobs:
            run_analysis_job(job)
    else:
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(run_analysis_job, job) for job in jobs]
            for future in as_completed(futures):
                future.result()

    input_files = frame[["prefix", "equiv_kind", "source_file"]].drop_duplicates()
    input_files = input_files.sort_values(["prefix", "equiv_kind", "source_file"])
    input_files.to_csv(output_dir / "input_files.csv", index=False)

    run_qualitative_misclassification_analysis(repo_root, output_dir)

    print(f"Wrote analysis outputs to {output_dir}")
    print(f"  Log errors CSV: {output_dir / 'log_errors.csv'}")
    print(f"  Log errors JSON: {output_dir / 'log_errors.json'}")
    print(f"  Logfile stats CSV: {output_dir / 'logfile_stats.csv'}")
    print(f"  Logfile stats TXT: {output_dir / 'logfile_stats.txt'}")
    print(f"  Overall: {output_dir / 'overall'}")
    print(f"  Per prefix+kind: {output_dir / 'by_prefix_kind'}")
    print(f"  Per prefix combined: {output_dir / 'by_prefix'}")
    print(f"  Qualitative misclassification: {output_dir / 'qualitative'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())