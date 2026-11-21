#!/bin/sh
# Slice 0 probe (TOKEN-206). Logs the event, whether the secret is visible, and
# whether the Worker is reachable. Never sends the secret itself, only a short hash.
ev="$1"
if [ -n "$TOKENATLAS_PROBE_SECRET" ]; then
  sh8=$(printf %s "$TOKENATLAS_PROBE_SECRET" | sha256sum | cut -c1-8)
else
  sh8=none
fi
code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' -X POST \
  -A 'tokenatlas-probe/0.1 (slice-0)' \
  -H "X-Probe-Event: $ev" -H "X-Secret-Sha8: $sh8" \
  https://tokenatlas-probe.jc5s2sx2w9.workers.dev/ 2>/dev/null)
echo "$(date -u +%FT%TZ) event=$ev secret=$sh8 worker_http=${code:-none} pwd=$PWD" >> /tmp/hookprobe.log
exit 0
