#!/bin/zsh

## set -eu

[[ $1 == "clang" ]] || [[ $1 == "gcc" ]] || { echo "usage: tce.sh clang|gcc -O0|1|2|3|s <file1> <file2>"; exit 1; }
[[ $2 =~ -O[0123s] ]] || { echo "usage: tce.sh clang|gcc -O0|1|2|3|s <file1> <file2>"; exit 1; }
[[ -f $3 ]] || { echo "usage: tce.sh clang|gcc -O0|1|2|3|s <file1> <file2>"; exit 1; }
[[ -f $4 ]] || { echo "usage: tce.sh clang|gcc -O0|1|2|3|s <file1> <file2>"; exit 1; }

compopts="-c"

[[ $2 == "clang" ]] && compopts=$compopts" -w -Wno-error=int-conversion -Wno-error=incompatible-function-pointer-types -Wno-error=implicit-function-declaration -fbracket-depth=1024"

$1 $2 $compopts -o "$3-$1$2".tmp $3 >/dev/null 2>&1
$1 $2 $compopts -o "$4-$1$2".tmp $4 >/dev/null 2>&1
strip "$3-$1$2".tmp >/dev/null 2>&1
strip "$4-$1$2".tmp >/dev/null 2>&1
hash1=$(sha256sum -b "$3-$1$2".tmp | sed -E 's;[[:blank:]]+;,;' | cut -d, -f1)
hash2=$(sha256sum -b "$4-$1$2".tmp | sed -E 's;[[:blank:]]+;,;' | cut -d, -f1)
[[ $hash1 = $hash2 ]] && echo "\033[0;31mTCE equivalent\033[0m" || echo "\033[0;31mnot TCE equivalent\033[0m"
rm "$3-$1$2".tmp "$4-$1$2".tmp
