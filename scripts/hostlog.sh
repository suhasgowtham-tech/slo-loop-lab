#!/usr/bin/env bash
# make hostlog: every HOSTLOG_EVERY_S seconds (default 30) append ONE line describing how busy the machine is to results/hostlog.csv.
# Run it in a second tmux window during long runs (Ctrl+b then c opens a window; Ctrl+C stops the logger).
#
# Why: the same lab was 100 % good at one hour and 90 % good (median TTFT 4x higher) at another, with nothing changed in the lab.
# `make health` joins this log with every run, so a slow stretch can be tied to a cause:
#   cpu_steal_pct high  -> Windows (the hypervisor) is taking CPU away from WSL: Windows Update, Defender, thermal/power limits
#   cpu_busy_pct high while no run is measuring -> something inside WSL/Docker is burning CPU (top_container names the busiest one)
#   mem_avail_mb low / swap_used_mb high -> memory pressure
# Columns: epoch,load1,cpu_busy_pct,cpu_steal_pct,mem_avail_mb,swap_used_mb,psi_cpu_avg10,top_container
# All WSL distros share one kernel, so /proc/stat here covers the whole WSL VM (including Docker Desktop's containers).
set -u
cd "$(dirname "$0")/.." || exit 1

OUT="${HOSTLOG_FILE:-results/hostlog.csv}"
EVERY="${HOSTLOG_EVERY_S:-30}"
SAMPLE="${HOSTLOG_SAMPLE_S:-5}"
ONCE="${HOSTLOG_ONCE:-0}"

mkdir -p "$(dirname "$OUT")"
[ -s "$OUT" ] || echo "epoch,load1,cpu_busy_pct,cpu_steal_pct,mem_avail_mb,swap_used_mb,psi_cpu_avg10,top_container" > "$OUT"

cpu_counters() { awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9, $5+$6, $9}' /proc/stat; }

sample() {
  local a b pct busy steal load mem swap psi top
  a=$(cpu_counters)
  sleep "$SAMPLE"
  b=$(cpu_counters)
  pct=$(awk -v a="$a" -v b="$b" 'BEGIN {
    split(a, x, " "); split(b, y, " "); dt = y[1] - x[1]
    if (dt <= 0) { print "NA NA"; exit }
    printf "%.1f %.1f", 100 * (dt - (y[2] - x[2])) / dt, 100 * (y[3] - x[3]) / dt }')
  busy=${pct% *}
  steal=${pct#* }
  load=$(awk '{print $1}' /proc/loadavg)
  mem=$(awk '/^MemAvailable:/{printf "%d", $2/1024}' /proc/meminfo)
  swap=$(awk '/^SwapTotal:/{t=$2} /^SwapFree:/{f=$2} END{printf "%d", (t-f)/1024}' /proc/meminfo)
  psi=""
  if [ -r /proc/pressure/cpu ]; then
    psi=$(awk '/^some/{for (i = 1; i <= NF; i++) if ($i ~ /^avg10=/) {sub("avg10=", "", $i); print $i}}' /proc/pressure/cpu)
  fi
  top=""
  if command -v docker >/dev/null 2>&1; then
    top=$(timeout 20 docker stats --no-stream --format '{{.Name}} {{.CPUPerc}}' 2>/dev/null \
      | awk '{gsub("%", "", $2); if ($2 + 0 > m) {m = $2 + 0; n = $1}} END {if (n != "") printf "%s %.0f%%", n, m}' | tr -d ',')
  fi
  echo "$(date +%s),$load,$busy,$steal,$mem,$swap,$psi,$top"
}

echo "hostlog: appending to $OUT every ${EVERY}s (Ctrl+C stops)" >&2
while true; do
  sample >> "$OUT"
  [ "$ONCE" = "1" ] && break
  sleep "$EVERY"
done
