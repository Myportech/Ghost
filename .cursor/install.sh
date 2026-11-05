#!/usr/bin/env bash
# Idempotent Cursor Cloud bootstrap for Ghost.
# Creates a Linux virtualenv from requirements-cloud.txt.
# Does not install requirements-macos.txt and does not delete an existing
# .venv, including the historical macOS tree.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Python: $(python3 --version)"
python3 -m pip --version

if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv
fi

if [[ -e .venv && ! -x .venv/bin/python ]]; then
  echo "ERROR: .venv exists but .venv/bin/python cannot run." >&2
  echo "This script does not delete it. On a Cloud checkout of this branch," >&2
  echo ".venv is absent and will be created. Move the broken tree aside to continue." >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --disable-pip-version-check --no-input -r requirements-cloud.txt
.venv/bin/python -m pip check

.venv/bin/python - <<'PY'
import ghost
import httpx
import pytest
from ghost.core.protocol import SCHEMAS

assert ghost.__version__ == "2.0.0", ghost.__version__
assert len(SCHEMAS) == 16, len(SCHEMAS)
print(f"imports ok: ghost {ghost.__version__}, httpx {httpx.__version__}, pytest {pytest.__version__}, tools {len(SCHEMAS)}")
PY

# Full suite. test_inline_source skips until the real host file is committed
# as a regular file. requirements-macos.txt is intentionally not installed.
PYTHONPATH=. .venv/bin/python -m pytest tests -q -rs
