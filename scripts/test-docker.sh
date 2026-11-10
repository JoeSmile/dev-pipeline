#!/usr/bin/env bash
# Run the Docker test gate. Use --with-temporal to also start Temporal on :7233
# and run live History tests.
set -euo pipefail

WITH_TEMPORAL=0
if [[ "${1:-}" == "--with-temporal" ]]; then
  WITH_TEMPORAL=1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

docker compose build test
docker compose run --rm test

if [[ "${WITH_TEMPORAL}" -eq 1 ]]; then
  docker compose --profile temporal run --rm test-history
  docker compose --profile temporal down
fi
