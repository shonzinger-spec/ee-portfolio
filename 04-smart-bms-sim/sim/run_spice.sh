#!/bin/sh
# Run the cell-sense deck and store its log in docs/.
# Usage, from the 04-smart-bms-sim folder: sh sim/run_spice.sh
set -e
cd "$(dirname "$0")/.."
NGSPICE=${NGSPICE:-ngspice}
"$NGSPICE" -b sim/ngspice/cell_sense.cir > docs/sim_ngspice.log 2> sim/ngspice/ngspice_notes.log
grep -c '^CORNER' docs/sim_ngspice.log | sed 's/^/corner runs: /'
echo "log: docs/sim_ngspice.log"
