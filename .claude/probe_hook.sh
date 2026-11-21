#!/bin/sh
# Slice 0 probe (TOKEN-206). Logs the event, whether the secret is visible (yes/no only,
# nothing derived from its value), and whether the Worker is reachable.
ev="$1"
if [ -n "$TOKENATLAS_PROBE_SECRET" ]; then s=yes; else s=no; fi
code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' -X POST \
  -A 'tokenatlas-probe/0.1 (slice-0)' \
  -H "X-Probe-Event: $ev" -H "X-Secret-Present: $s" \
  https://tokenatlas-probe.jc5s2sx2w9.workers.dev/ 2>/dev/null)
echo "$(date -u +%FT%TZ) event=$ev secret_present=$s worker_http=${code:-none} pwd=$PWD" >> /tmp/hookprobe.log
exit 0
