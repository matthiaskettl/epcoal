#!/bin/bash

set -e

PROG1="$1"
PROG2="$2"
COMPILER="$3"
OPT="$4"

if [[ -z "$PROG1" || -z "$PROG2" || -z "$COMPILER" || -z "$OPT" ]]; then
    echo "Usage: $0 <program1> <program2> <gcc|clang> <optimization>"
    echo "Example: $0 a.c b.c gcc -Os"
    exit 1
fi

# ----------------------------
# 1. Run Python checker
# ----------------------------
PY_OUTPUT=$(./check.py "$PROG1" --mutant "$PROG2")

echo "=== Python output ==="
echo "$PY_OUTPUT"

if echo "$PY_OUTPUT" | grep -q "Final verdict: equivalent"; then
    PY_VERDICT="equivalent"
elif echo "$PY_OUTPUT" | grep -q "Final verdict: not equivalent"; then
    PY_VERDICT="not equivalent"
else
    PY_VERDICT="other"
fi

# ----------------------------
# 2. Run TCE script
# ----------------------------
# Pass compiler + optimization as separate args
TCE_OUTPUT=$(./tce.sh "$COMPILER" "$OPT" "$PROG1" "$PROG2")

echo "=== TCE output ==="
echo "$TCE_OUTPUT"

if echo "$TCE_OUTPUT" | grep -q "not TCE equivalent"; then
    TCE_VERDICT="not equivalent"
elif echo "$TCE_OUTPUT" | grep -q "TCE equivalent"; then
    TCE_VERDICT="equivalent"
else
    TCE_VERDICT="other"
fi

# ----------------------------
# 3. Compare results
# ----------------------------
if [[ "$PY_VERDICT" == "equivalent" && "$TCE_VERDICT" == "equivalent" ]]; then
    echo "AGREEMENT: equivalent"
elif [[ "$PY_VERDICT" == "not equivalent" && "$TCE_VERDICT" == "not equivalent" ]]; then
    echo "AGREEMENT: not equivalent"
else
    echo "DISAGREEMENT: TCE $TCE_VERDICT but Treq $PY_VERDICT"
fi
