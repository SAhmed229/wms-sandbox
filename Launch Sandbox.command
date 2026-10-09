#!/bin/zsh
set -e
sandbox_dir="${0:A:h}"
cd "$sandbox_dir"
sandbox_python="$sandbox_dir/.venv-sandbox/bin/python"
if [[ ! -x "$sandbox_python" ]]; then
  print "The original optimizer dependencies are not installed."
  print "Create .venv-sandbox with Python 3.12 and install requirements-sandbox.txt."
  exit 1
fi
"$sandbox_python" -m warehouse_sandbox.server --port 8876
