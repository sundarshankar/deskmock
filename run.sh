#!/usr/bin/env bash
# JobHelm launcher — stops any old instance, starts fresh, and the app opens your browser.
# Usage:  ./run.sh            (port 8899, bundled demo data)
#         JOBHELM_CAREEROPS=/path/to/career-ops ./run.sh   (your real data)
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${JOBHELM_PORT:-8899}"

# --- guardrails ---
command -v python3 >/dev/null 2>&1 || { echo "❌ python3 not found. Install Python 3.9+ (macOS: brew install python), then retry."; exit 1; }
[ -f "${DIR}/jobhelm/mission-control.py" ] || { echo "❌ jobhelm/mission-control.py not found in ${DIR}. Run this from the deskmock checkout."; exit 1; }

echo "-> Stopping any old JobHelm on port ${PORT}..."
lsof -ti:"${PORT}" 2>/dev/null | xargs kill -9 2>/dev/null || true
pkill -f "${DIR}/jobhelm/mission-control.py" 2>/dev/null || true
sleep 1

echo "-> Starting JobHelm at http://127.0.0.1:${PORT}  (Ctrl+C to stop)"
cd "${DIR}"
exec env JOBHELM_PORT="${PORT}" python3 jobhelm/mission-control.py
