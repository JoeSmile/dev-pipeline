#!/usr/bin/env bash
# Run the repo test gate inside the Docker image. No hidden config-root default.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

python -m pytest
ruff check src tests
mypy
