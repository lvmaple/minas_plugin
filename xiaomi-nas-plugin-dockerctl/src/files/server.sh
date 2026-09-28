#!/bin/sh
# Compatibility wrapper — lifecycle logic lives in scripts/control.
exec "$(cd "$(dirname "$0")/../.." && pwd)/scripts/control" "$@"
