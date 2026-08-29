#!/usr/bin/env bash
# JobHelm launcher — stops any old instance, starts fresh, and the app opens your browser.
# Usage:  ./run.sh            (port 8899, bundled demo data)
#         JOBHELM_CAREEROPS=/path/to/career-ops ./run.sh   (your real data)
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${JOBHELM_PORT:-8899}"
echo "-> Stopping any old JobHelm on port ${PORT}..."
lsof -ti:"${PORT}" 2>/dev/null | xargs kill -9 2>/dev/null || true
pkill -f "mission-control.py" 2>/dev/null || true
sleep 1
echo "-> Starting JobHelm at http://127.0.0.1:${PORT}  (Ctrl+C to stop)"
cd "${DIR}"
exec env JOBHELM_PORT="${PORT}" python3 jobhelm/mission-control.py
