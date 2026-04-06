#!/usr/bin/env python3

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
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


def collect_table_files(pattern: str, repo_root: Path) -> list[Path]:
    if Path(pattern).is_absolute():
        return sorted(Path(path) for path in glob.glob(pattern))

    return sorted(repo_root.glob(pattern))


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

    required = {"status", "cputime_s", "walltime_s", "memory_mb"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"{path} is missing expected columns: {sorted(missing)}")

    frame = frame[["task", "status", "cputime_s", "walltime_s", "memory_mb"]].copy()
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
        box = ax.boxplot(data, tick_labels=labels, patch_artist=True, showfliers=False)
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

    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    table_files = collect_table_files(args.input_glob, repo_root)
    if not table_files:
        print(f"No table CSV files found for pattern: {args.input_glob}")
        return 1

    output_dir = repo_root / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

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

    print(f"Wrote analysis outputs to {output_dir}")
    print(f"  Overall: {output_dir / 'overall'}")
    print(f"  Per prefix+kind: {output_dir / 'by_prefix_kind'}")
    print(f"  Per prefix combined: {output_dir / 'by_prefix'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())