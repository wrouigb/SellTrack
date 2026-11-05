#!/usr/bin/env bash
# Runs one step of CI. If it fails, the end of its output is attached to the pull request as an
# annotation, so the reason can be read there without opening the whole log.
set -uo pipefail

log=$(mktemp)
"$@" 2>&1 | tee "$log"
status=${PIPESTATUS[0]}
if [[ $status -ne 0 ]]; then
    # Only what can be read: no colours, and no lines that only draw a table's borders.
    summary=$(sed -E 's/\x1b\[[0-9;]*m//g' "$log" | grep -vE '^[─┌┐└┘├┤┬┴┼│ ]*$' | tail -n 40 | cut -c1-200)
    summary=${summary//'%'/'%25'}
    summary=${summary//$'\r'/'%0D'}
    summary=${summary//$'\n'/'%0A'}
    echo "::error title=$1 failed::$summary"
fi
rm -f "$log"
exit "$status"
