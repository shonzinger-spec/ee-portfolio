#!/bin/sh
# Run both inverter decks and store their log in docs/.
# Usage, from the 05-bldc-hil-sim folder: sh sim/run_spice.sh
set -e
cd "$(dirname "$0")/.."
NGSPICE=${NGSPICE:-ngspice}
{
  echo "# ngspice -b sim/ngspice/leg_deadtime.cir"
  "$NGSPICE" -b sim/ngspice/leg_deadtime.cir 2> sim/ngspice/ngspice_notes.log | grep -E '^(SWEEP|DELAY)'
  echo "# ngspice -b sim/ngspice/inverter_6step.cir"
  "$NGSPICE" -b sim/ngspice/inverter_6step.cir 2>> sim/ngspice/ngspice_notes.log | grep -E '^(SIXSTEP|BEMF)'
} > docs/sim_ngspice.log
grep -cE '^(SWEEP|DELAY|SIXSTEP|BEMF)' docs/sim_ngspice.log | sed 's/^/result lines: /'
echo "log: docs/sim_ngspice.log"
