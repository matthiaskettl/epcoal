# Getting Started
Run `./check.py orig.c mutant.c`.

# Benchmark:
create symlink to sv-benchmarks in benchmark/ folder.
Execute make benchmarks

./benchmark/create_table.py --pattern "benchmark/results/cor_1000_equivalent_mutants.csv.2026-04-01_12-44-38.*.xml.bz2" --output benchmark/cor_equiv.xml 

To build the newest equivalent and non-equivalent tables for every prefix in `benchmark/results`, run:

./benchmark/create_table.py --latest

To analyze the generated `*.table.csv` files and produce status-based plots plus summary stats, run:

./benchmark/analyze_tables.py