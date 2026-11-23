#!/bin/zsh
set -e
cd -- "${0:A:h}"

runtime="$HOME/.cache/spx-es-dashboard-venv/bin/python"
if [[ -x "$runtime" ]]; then
  exec "$runtime" app.py "$@"
fi

uv_executable="$(command -v uv || true)"
if [[ -z "$uv_executable" && -x "$HOME/.local/bin/uv" ]]; then
  uv_executable="$HOME/.local/bin/uv"
fi
if [[ -z "$uv_executable" ]]; then
  print -u2 'Install uv or run app.py with Python 3.12 and the packages in requirements.txt.'
  exit 1
fi
exec "$uv_executable" run --no-project --python 3.12 --with-requirements requirements.txt python app.py "$@"
