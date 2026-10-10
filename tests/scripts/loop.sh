#!/bin/bash
n=0
while true; do
    n=$((n + 1))
    sleep 0.05
    [ "$n" -ge 200 ] && break
done
echo "n=$n"
