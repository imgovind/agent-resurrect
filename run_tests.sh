#!/usr/bin/env bash
# Run the test suite on the macOS system Python (the 3.9 floor) and on python3 from PATH, if different.
# Extra arguments are passed to unittest, e.g. ./run_tests.sh -k Store
set -euo pipefail
cd "$(dirname "$0")"

status=0
seen=""
for py in /usr/bin/python3 python3; do
  command -v "$py" >/dev/null 2>&1 || continue
  real="$("$py" -c 'import sys; print(sys.executable)')"
  case " $seen " in *" $real "*) continue ;; esac
  seen="$seen $real"
  echo "== $("$py" --version 2>&1) ($real)"
  PYTHONPATH=src "$py" -m unittest discover -s tests -t . "$@" || status=1
done
exit "$status"
