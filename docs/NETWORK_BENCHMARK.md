# VPS network-only benchmark

The authenticated `PUT /v1/bench/sink` endpoint measures Mac-to-VPS HTTPS request-body delivery without involving the R2 cache, filesystem writes, fsync, R2 worker, or execution polling. The request body is streamed and discarded. It requires Content-Length and rejects bodies larger than `R2_RELAY_BENCH_MAX_BYTES` (default 32 MiB). Concurrent sink handlers are bounded by `R2_RELAY_BENCH_MAX_CONCURRENT` (default 4). The endpoint is not a general-purpose upload/storage API.

The endpoint is added to the existing FastAPI app on the existing TLS listener; no additional port is required. It is protected by the same Bearer token as the other API routes. Do not expose the token in shell history, command output, or logs.

## Mac setup

Use the VPS CA file and an environment variable containing the existing relay token:

```bash
export VPS_URL='https://104.224.168.192:8443'
export VPS_CA="$HOME/r2-relay-root.crt"
# Set securely from the same protected token source used by the relay client.
export R2_RELAY_TOKEN="$(cat ~/.config/r2-relay/token)"

dd if=/dev/urandom of=/tmp/netbench-01.bin bs=1m count=20 2>/dev/null
dd if=/dev/urandom of=/tmp/netbench-02.bin bs=1m count=20 2>/dev/null

test "$(wc -c < /tmp/netbench-01.bin)" -eq 20971520
test "$(wc -c < /tmp/netbench-02.bin)" -eq 20971520
```

## One connection

```bash
curl --noproxy '*' --cacert "$VPS_CA" \\
  -H "Authorization: Bearer $R2_RELAY_TOKEN" \\
  --data-binary @/tmp/netbench-01.bin -X PUT \\
  -w '\nHTTP=%{http_code} bytes=%{size_upload} total=%{time_total}s speed=%{speed_upload}B/s\n' \\
  "$VPS_URL/v1/bench/sink"
```

## Two simultaneous connections

```bash
set -euo pipefail
T0=$(python3 -c 'import time; print(time.monotonic())')
for n in 01 02; do
  curl --noproxy '*' --cacert "$VPS_CA" \\
    -H "Authorization: Bearer $R2_RELAY_TOKEN" \\
    --data-binary @"/tmp/netbench-$n.bin" -X PUT \\
    -o "/tmp/netbench-$n.json" \\
    -w "request_$n HTTP=%{http_code} bytes=%{size_upload} time=%{time_total}s speed=%{speed_upload}B/s\\n" \\
    "$VPS_URL/v1/bench/sink" > "/tmp/netbench-$n.result" 2> "/tmp/netbench-$n.error" &
done
wait
T1=$(python3 -c 'import time; print(time.monotonic())')
cat /tmp/netbench-01.result /tmp/netbench-02.result
python3 - "$T0" "$T1" <<'PY'
import sys
elapsed=float(sys.argv[2])-float(sys.argv[1])
print(f'wall_time={elapsed:.3f}s aggregate_mbps={40*1024*1024*8/elapsed/1e6:.2f}')
PY
cat /tmp/netbench-01.json /tmp/netbench-02.json
```

Run one-connection and two-connection tests several times, alternating order. Compare the client-side aggregate throughput and the server-reported per-request receive throughput. The server's `server_elapsed_ms` starts after the bounded concurrency slot is acquired; client wall time includes connection/TLS and queueing. This isolates the VPS request-body receive path from cache disk I/O, but still includes TLS, TCP, VPS kernel/network processing and application streaming overhead. A direct call to this endpoint does not pass through the Mac's 18585 MITM proxy; it is specifically the clean Mac-to-VPS network baseline.
