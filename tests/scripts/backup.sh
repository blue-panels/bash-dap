#!/bin/bash
# a function, a loop, an array, arithmetic
files=("$@")
[ ${#files[@]} -eq 0 ] && files=(notes.txt photo.jpg report.pdf)

size_of () {
    local name=$1
    local size=$(( ${#name} * 100 ))
    echo "$size"
}

total=0
count=0
for f in "${files[@]}"; do
    size=$(size_of "$f")
    total=$(( total + size ))
    count=$(( count + 1 ))
    echo "would copy $f ($size bytes)"
done
declare -A kinds=([txt]="text" [jpg]="image")
echo "$count files, $total bytes"
exit 3
