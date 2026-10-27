#!/bin/sh
# Run both SPICE decks and store their logs in docs/ (LTspice writes UTF-16; converted here).
# Usage, from the 02-sync-buck folder: sh sim/run_spice.sh
set -e
cd "$(dirname "$0")/.."
LTSPICE=${LTSPICE:-/Applications/LTspice.app/Contents/MacOS/LTspice}
NGSPICE=${NGSPICE:-ngspice}
"$NGSPICE" -b sim/ngspice/buck_sync_ngspice.cir > docs/sim_ngspice.log 2>&1 &
NGPID=$!
"$LTSPICE" -b "$PWD/sim/buck_sync.cir"   # LTspice needs an absolute path
iconv -f UTF-16LE -t UTF-8 sim/buck_sync.log | tr -d '\r' > docs/sim_ltspice.log
wait $NGPID
echo "logs: docs/sim_ltspice.log docs/sim_ngspice.log"
