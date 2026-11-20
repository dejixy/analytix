#!/usr/bin/env bash
# The studies on the server in one short command each (long pasted blocks get garbled in the terminal):
#
#   bash ~/analytix/scripts/run.sh status    what's running, how far each got, which results are ready
#   bash ~/analytix/scripts/run.sh btc       the BTC estimate (a few minutes), printed and kept in /tmp/btc_est.txt
#                                            (extra options pass through: run.sh btc --lam 0.3 --terms etf,mom)
#   bash ~/analytix/scripts/run.sh moves     BTC estimate v2: the size of the next day's move, extreme ETF-flow days
#                                            and Strategy's biggest buys (a few minutes), kept in /tmp/btc_moves.txt
#   bash ~/analytix/scripts/run.sh carry     the funding-carry watcher's rule on Hyperliquid's history (a few minutes)
#   bash ~/analytix/scripts/run.sh analyzer  check each of the Analyzer's sources from this server (a minute)
#   bash ~/analytix/scripts/run.sh bets      check the bets suite's sources from this server (a minute or two)
#   bash ~/analytix/scripts/run.sh link      the bets page's public address (installs and starts the link if needed)
#   bash ~/analytix/scripts/run.sh token     the collector's token for sending odds through the public link (makes one
#                                            the first time and restarts the app), and the command to run on the laptop
#   bash ~/analytix/scripts/run.sh map       (re)start the HYPE map in the background (keeps what it already read)
#   bash ~/analytix/scripts/run.sh top       the biggest wallets in the latest HYPE map, to look up and name
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
ETF_FILE="${ETF_FILE:-}"                                # set it to use a saved Farside page instead of TFTC

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
    echo "BTC moves:    $(running btcMoves), result $(ready /tmp/btc_moves.txt)"
    last /tmp/btc_moves.log 3
    ;;
  btc)
    pkill -f scripts.btcEstimate 2>/dev/null; sleep 1
    [ -n "$ETF_FILE" ] && [ -f "$ETF_FILE" ] && etf=(--etf-file "$ETF_FILE") || etf=()
    nice -n 19 "$PY" -m scripts.btcEstimate "${etf[@]}" "${@:2}" > /tmp/btc_est.txt 2> /tmp/btc_est.log
    cat /tmp/btc_est.txt; echo; echo "(log:)"; tail -n 5 /tmp/btc_est.log
    ;;
  moves)
    pkill -f scripts.btcMoves 2>/dev/null; sleep 1
    [ -n "$ETF_FILE" ] && [ -f "$ETF_FILE" ] && etf=(--etf-file "$ETF_FILE") || etf=()
    nice -n 19 "$PY" -m scripts.btcMoves "${etf[@]}" "${@:2}" > /tmp/btc_moves.txt 2> /tmp/btc_moves.log
    cat /tmp/btc_moves.txt; echo; echo "(log:)"; grep -E "Strategy:|DVOL|ETF flows:|Error|error" /tmp/btc_moves.log | tail -n 6
    ;;
  top)
    "$PY" -m scripts.hypeTop "${@:2}"
    ;;
  token)
    keys=/etc/analytix-keys.env
    t="$(sudo grep -s '^ANALYTIX_BETS_TOKEN=' "$keys" | tail -1 | cut -d= -f2-)"
    if [ -z "$t" ]; then
      t="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
      echo "ANALYTIX_BETS_TOKEN=$t" | sudo tee -a "$keys" >/dev/null && sudo systemctl restart analytix \
        || { echo "couldn't save the token"; exit 1; }
      echo "made a token (kept in $keys) and restarted the app; give it a minute"
    fi
    url="$(sudo journalctl -u analytix-link --no-pager -n 500 | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1)"
    echo
    echo "on the laptop (or phone), once, in the folder with naijaOdds.py (no tunnel needed):"
    echo "  python naijaOdds.py --server ${url:-https://YOUR-LINK.trycloudflare.com} --token $t --save"
    echo "from then on:"
    echo "  python naijaOdds.py --loop"
    echo
    echo "(the token is a password: keep it out of chats and screenshots)"
    ;;
  link)
    if ! command -v cloudflared >/dev/null; then
      arch="$(dpkg --print-architecture)"
      echo "installing cloudflared ($arch) from Cloudflare's GitHub releases"
      curl -fsSL -o /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch.deb" \
        && sudo dpkg -i /tmp/cloudflared.deb || { echo "couldn't install cloudflared"; exit 1; }
    fi
    if ! systemctl is-active --quiet analytix-link; then
      sudo cp /opt/analytix/deploy/analytix-link.service /etc/systemd/system/ && sudo systemctl daemon-reload \
        && sudo systemctl enable --now analytix-link || { echo "couldn't start the link"; exit 1; }
      echo "starting the link..."
    fi
    url=""
    for _ in $(seq 1 30); do
      url="$(sudo journalctl -u analytix-link --no-pager -n 200 | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1)"
      [ -n "$url" ] && break
      sleep 2
    done
    [ -n "$url" ] && echo "the bets page: $url/bets" || echo "no address yet: sudo journalctl -u analytix-link -n 50"
    ;;
  bets)
    "$PY" -m scripts.betsProbe 2>&1 | tee /tmp/bets_probe.txt
    ;;
  analyzer)
    "$PY" -m scripts.analyzerProbe 2>&1 | tee /tmp/analyzer_probe.txt
    ;;
  carry)
    pkill -f scripts.carryStudy 2>/dev/null; sleep 1
    nice -n 19 "$PY" -m scripts.carryStudy "${@:2}" > /tmp/carry.txt 2> /tmp/carry.log
    cat /tmp/carry.txt; echo; echo "(log:)"; tail -n 6 /tmp/carry.log
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
    for f in /tmp/fresh.txt /tmp/hype_probe.txt /tmp/hype_map.txt /tmp/btc_est.txt /tmp/btc_moves.txt /tmp/carry.txt; do
      [ -s "$f" ] && { echo "===== $f"; cat "$f"; echo; }
    done
    ;;
  update)
    git pull --ff-only
    ;;
  *)
    sed -n '2,19p' "$0"
    ;;
esac
