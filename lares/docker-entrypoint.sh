#!/bin/sh
set -e

# Collectors run as background processes alongside the API in this single
# container. Container teardown kills every process in the container's PID
# namespace together regardless of individual signal delivery, so a stop
# loses at most one flush interval's worth of buffered metrics (tens of
# seconds) rather than getting each collector's own graceful SIGTERM flush.
# Acceptable for a home-lab tool; not worth a signal-forwarding supervisor
# for that small a gap.
python -m backend.collectors.system &
python -m backend.collectors.disk &
python -m backend.collectors.containers &

exec uvicorn backend.main:app --host 0.0.0.0 --port 8000
