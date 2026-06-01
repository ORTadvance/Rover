#!/bin/bash
# Start the ORT base station. Run from any directory.
# Usage: ./start_base.sh [extra args passed to main.py, e.g. --mock]
cd "$(dirname "$0")/.."
source venv/bin/activate
python base_station/main.py "$@"
