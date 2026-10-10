#!/bin/bash
set -eu
. "$(dirname "$0")/lib.sh"
greet world
printf 'a\nb\nc\n' | while read -r letter; do
    echo "got $letter"
done
echo "end"
