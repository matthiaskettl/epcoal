#!/usr/bin/env python3
"""Generate BenchExec run definitions from a CSV and inject them into template.xml.

CSV format:
  original_path,mutant_path
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


RUN_DEFINITION_TEMPLATE = """  <rundefinition name=\"COUNT\">
  <requiredfiles>benchmark/MUTANT</requiredfiles>
  <option name=\"--mutant\">MUTANT</option>
  <tasks>
    <include>ORIG</include>
  </tasks>
  </rundefinition>"""

DEFINITIONS_MARKER = "  <!-- DEFINITIONS-->"
ORIGINAL_PREFIX = "sv-benchmarks/"


def _normalize_header(name: str) -> str:
  """Normalize a CSV header to a canonical key."""
  if name is None:
    return ""
  return name.strip().lstrip("\ufeff").lower()


def _strip_original_prefix(original_path: str) -> str:
  if original_path.startswith(ORIGINAL_PREFIX):
    return original_path[len(ORIGINAL_PREFIX):]
  return original_path


def find_yaml_for_original(original_path: str) -> str:
  """Find a YAML file in the original's directory that references the original file."""
  relative_original = _strip_original_prefix(original_path)
  original_file = Path(__file__).parent / Path(ORIGINAL_PREFIX) / relative_original
  original_name = original_file.with_suffix("").name

  if not original_file.parent.is_dir():
    raise FileNotFoundError(
      f"Original directory not found for {original_path}: {original_file.parent}"
    )

  # Prefer the usual naming rule: same basename, .yml/.yaml instead of last suffix.
  preferred_candidates = [
    original_file.with_suffix(".yml"),
    original_file.with_suffix(".yaml"),
  ]

  other_candidates = []
  for pattern in ("*.yml", "*.yaml"):
    other_candidates.extend(sorted(original_file.parent.glob(pattern)))

  seen: set[Path] = set()
  candidates = []
  for candidate in preferred_candidates + other_candidates:
    if candidate in seen:
      continue
    seen.add(candidate)
    candidates.append(candidate)

  for candidate in candidates:
    if not candidate.is_file():
      continue

    content = candidate.read_text(encoding="utf-8", errors="ignore")
    
    if original_name in content or relative_original in content:
      rel_candidate = candidate.relative_to( Path(__file__).parent / Path(ORIGINAL_PREFIX))
      return f"{ORIGINAL_PREFIX}{rel_candidate.as_posix()}"

  raise FileNotFoundError(
    "Could not find a .yml/.yaml in the original directory that references "
    f"{original_path}"
  )


def create_run_definitions(csv_path: Path) -> str:
  """Build one XML run definition per CSV row."""
  blocks: list[str] = []

  with csv_path.open(newline="", encoding="utf-8-sig") as handle:
    # Read a sample for dialect sniffing, then reset stream position.
    sample = handle.read(4096)
    handle.seek(0)

    try:
      dialect = csv.Sniffer().sniff(sample, delimiters=",;")
    except csv.Error:
      dialect = csv.excel

    reader = csv.DictReader(handle, dialect=dialect)

    required_columns = {"original_path", "mutant_path"}
    raw_fieldnames = reader.fieldnames or []
    normalized_fieldnames = [_normalize_header(name) for name in raw_fieldnames]

    index_by_name = {
      normalized: idx for idx, normalized in enumerate(normalized_fieldnames) if normalized
    }

    if not required_columns.issubset(index_by_name.keys()):
      raise ValueError(
        "CSV must contain header columns: original_path,mutant_path "
        f"(found: {raw_fieldnames})"
      )

    original_key = raw_fieldnames[index_by_name["original_path"]]
    mutant_key = raw_fieldnames[index_by_name["mutant_path"]]

    for count, row in enumerate(reader, start=1):
      original_path = (row.get(original_key) or "").strip()
      mutant_path = (row.get(mutant_key) or "").strip()

      if not original_path or not mutant_path:
        raise ValueError(
          f"CSV row {count} has an empty original_path or mutant_path"
        )

      if not original_path.startswith(ORIGINAL_PREFIX):
        original_path = f"{ORIGINAL_PREFIX}{original_path}"

      yaml_path = find_yaml_for_original(original_path)

      block = (
        RUN_DEFINITION_TEMPLATE
        .replace("COUNT", f"mutant_{count}")
        .replace("MUTANT", mutant_path)
        .replace("ORIG", yaml_path)
      )
      blocks.append(block)

  return "\n\n".join(blocks)


def inject_into_template(template_content: str, definitions_block: str) -> str:
  """Insert generated run definitions directly after the DEFINITIONS marker."""
  marker_index = template_content.find(DEFINITIONS_MARKER)
  if marker_index == -1:
    raise ValueError(f"Could not find marker in template: {DEFINITIONS_MARKER}")

  insert_at = marker_index + len(DEFINITIONS_MARKER)
  injection = f"\n\n{definitions_block}"
  return template_content[:insert_at] + injection + template_content[insert_at:]


def main() -> None:
  parser = argparse.ArgumentParser(
    description=(
      "Read original_path/mutant_path rows from CSV and inject generated "
      "rundefinition XML entries into a template."
    )
  )
  parser.add_argument("csv", type=Path, help="Input CSV file")
  parser.add_argument(
    "--template",
    type=Path,
    default=Path(__file__).parent / "template.xml",
    help="Template XML file that contains the DEFINITIONS marker",
  )
  parser.add_argument(
    "--output",
    type=Path,
    default=None,
    help="Output XML path (default: overwrite template file)",
  )

  args = parser.parse_args()

  if not args.csv.is_file():
    raise FileNotFoundError(f"CSV not found: {args.csv}")
  if not args.template.is_file():
    raise FileNotFoundError(f"Template not found: {args.template}")

  definitions = create_run_definitions(args.csv)
  template_content = args.template.read_text(encoding="utf-8")
  final_content = inject_into_template(template_content, definitions)

  output_path = args.output if args.output is not None else args.template
  output_path.write_text(final_content, encoding="utf-8")
  print(f"Wrote {output_path}")


if __name__ == "__main__":
  main()
