#!/bin/bash
# Stop the local test stack started by dev_up.sh.
for port in 8099 8199; do
  pid=$(ss -ltnp 2>/dev/null | grep ":$port " | grep -o 'pid=[0-9]*' | cut -d= -f2 | head -1)
  [ -n "$pid" ] && kill "$pid" && echo "stopped :$port"
done
pkill -f "app.camhealth.worker" 2>/dev/null && echo "stopped camera health worker"
exit 0
