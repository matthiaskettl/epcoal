# Getting Started

To run Treq on a program and its mutant, execute:

```bash
./check.py orig.c --mutant mutant.c
```

To run TCE on a program and its mutant with gcc or clang and a given optimization, execute:

```bash
./tce.sh gcc|clang -O1|-O2|-O3|-Os orig.c mutant.c
```


---

# Benchmark Setup

### 1. Prepare benchmarks

Create a symbolic link to the SV-Benchmarks repository inside the `benchmark/` directory (or clone the benchmark set to the location):

```bash
ln -s /path/to/sv-benchmarks benchmark/sv-benchmarks
```

---

### 2. Run benchmarks

Execute all benchmarks using:

```bash
make benchmarks
```

---

### 3. Generate result tables

To create a table from a specific set of benchmark results, run:

```bash
./benchmark/create_table.py \
  --pattern "benchmark/results/cor_1000_equivalent_mutants.csv.2026-04-01_12-44-38.*.xml.bz2" \
  --output benchmark/cor_equiv.xml
```

---

### 4. Generate latest tables

To build the most recent equivalent and non-equivalent tables for all prefixes in `benchmark/results`, run:

```bash
./benchmark/create_table.py --latest
```

---

### 5. Analyze results

To analyze generated `*.table.csv` files and produce status-based plots and summary statistics, run:

```bash
make analysis
```

# Artifact: Treq

This artifact accompanies the paper: "Strengths and Weaknesses of Compilation-Based Equivalent Mutant Detection"

It provides a fully reproducible environment using Docker.

---

## Requirements

- Docker (>= 20.x)

---

## Quick Start

### 1. Build the Docker image

```bash
docker build -t treq .
```

---

### 2. Run the example

```bash
docker run --rm treq make example
```

This runs a minimal working example to verify that the setup works.

---

## Running Experiments

### Run lightweight/local experiments

```bash
docker run --rm treq make experiments-local
```

---

### Run full experiments

```bash
docker run --rm treq make experiments
```

Note: Full experiments may take significant time and computational resources.

---

## Project Structure

- `Makefile` — defines all commands
- `requirements.txt` — Python dependencies
- `src/` — source code (if applicable)
- `data/` — input data (if included)

---

## Reproducibility Notes

- All dependencies are installed via `make setup`
- The environment is fully encapsulated in Docker
- Results should be reproducible across systems

---

## Troubleshooting

If build issues occur, try:

```bash
docker build --no-cache -t treq .
```

---
