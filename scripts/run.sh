#!/usr/bin/env bash
# The studies on the server in one short command each (long pasted blocks get garbled in the terminal):
#
#   bash ~/analytix/scripts/run.sh status    what's running, how far each got, which results are ready
#   bash ~/analytix/scripts/run.sh btc       the BTC estimate (a few minutes), printed and kept in /tmp/btc_est.txt
#   bash ~/analytix/scripts/run.sh map       (re)start the HYPE map in the background (keeps what it already read)
#   bash ~/analytix/scripts/run.sh fresh     (re)start fresh money in the background (store build, then the study)
#   bash ~/analytix/scripts/run.sh show      every finished result
#   bash ~/analytix/scripts/run.sh update    pull the latest code
#
# Settings that should outlast a login (the SEC contact, say) go in ~/.analytix-env, e.g.
#   export ANALYTIX_SEC_UA="your name your@email"
set -u
cd "$(dirname "$0")/.."
export ANALYTIX_STORE_DIR="${ANALYTIX_STORE_DIR:-$HOME/analytix-store}"
PY="${PY:-/opt/analytix/.venv/bin/python}"
[ -f "$HOME/.analytix-env" ] && . "$HOME/.analytix-env"
ETF_FILE="${ETF_FILE:-$HOME/farside.html}"

running() { pgrep -f "scripts.$1" >/dev/null && echo "running" || echo "not running"; }
last() { [ -s "$1" ] && tail -n "${2:-2}" "$1" | sed 's/^/    /' || echo "    (nothing yet)"; }
ready() { [ -s "$1" ] && echo "ready ($1)" || echo "not yet"; }

case "${1:-status}" in
  status)
    echo "fresh money:  build $(running buildStore), study $(running freshMoney), result $(ready /tmp/fresh.txt)"
    last /tmp/fresh.log
    echo "HYPE map:     $(running hypeMap), result $(ready /tmp/hype_map.txt)"
    last /tmp/hype_map.log
    echo "BTC estimate: $(running btcEstimate), result $(ready /tmp/btc_est.txt)"
    last /tmp/btc_est.log 3
    ;;
  btc)
    pkill -f scripts.btcEstimate 2>/dev/null; sleep 1
    [ -f "$ETF_FILE" ] && etf=(--etf-file "$ETF_FILE") || etf=()
    nice -n 19 "$PY" -m scripts.btcEstimate "${etf[@]}" "${@:2}" > /tmp/btc_est.txt 2> /tmp/btc_est.log
    cat /tmp/btc_est.txt; echo; echo "(log:)"; tail -n 5 /tmp/btc_est.log
    ;;
  map)
    pkill -f scripts.hypeMap 2>/dev/null; sleep 2
    nohup nice -n 19 "$PY" -m scripts.hypeMap "${@:2}" > /tmp/hype_map.txt 2> /tmp/hype_map.log < /dev/null &
    echo "HYPE map started in the background; check with: bash scripts/run.sh status"
    ;;
  fresh)
    pkill -f scripts.freshMoney 2>/dev/null; pkill -f scripts.buildStore 2>/dev/null; sleep 2
    nohup sh -c "nice -n 19 '$PY' -m scripts.buildStore /opt/analytix/data/recordings --jobs 1 && nice -n 19 '$PY' -m scripts.freshMoney" \
      > /tmp/fresh.txt 2> /tmp/fresh.log < /dev/null &
    echo "fresh money started in the background; check with: bash scripts/run.sh status"
    ;;
  show)
    for f in /tmp/fresh.txt /tmp/hype_probe.txt /tmp/hype_map.txt /tmp/btc_est.txt; do
      [ -s "$f" ] && { echo "===== $f"; cat "$f"; echo; }
    done
    ;;
  update)
    git pull --ff-only
    ;;
  *)
    sed -n '2,13p' "$0"
    ;;
esac
