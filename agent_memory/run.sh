#!/bin/sh
# Options are read from /data/options.json; the database, exports and audit live under /data.
set -eu

echo "[agent-memory] serving on :8765"
exec python3 /opt/agent_memory/server.py --options /data/options.json --data /data --port 8765
