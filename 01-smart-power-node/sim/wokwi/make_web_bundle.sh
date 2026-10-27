#!/usr/bin/env bash
# Collects everything a wokwi.com ESP32 project needs into sim/wokwi/web/.
# The web editor wants the sketch at the root as sketch.ino, so the firmware's
# power_node.ino is copied under that name next to its two local headers.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fw="$here/../../src/power_node"
out="$here/web"

mkdir -p "$out"
cp "$fw/power_node.ino"        "$out/sketch.ino"
cp "$fw/config.h"              "$out/config.h"
cp "$fw/Ina219.h"              "$out/Ina219.h"
cp "$here/diagram.json"        "$out/diagram.json"
cp "$here/libraries.txt"       "$out/libraries.txt"
cp "$here/ina219.chip.c"       "$out/ina219.chip.c"
cp "$here/ina219.chip.json"    "$out/ina219.chip.json"

echo "wokwi.com bundle written to $out:"
ls -1 "$out"
