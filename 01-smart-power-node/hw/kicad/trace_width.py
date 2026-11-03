#!/usr/bin/env python3
"""Size the PCB's power traces with the IPC-2221 outer-layer formula and check the traces as drawn.

    I = k * dT^0.44 * A^0.725        k = 0.048 (outer layers), dT in degC, A in mil^2, I in A

Prints the minimum width for the 2 A design load and for the INA219's 3.2 A full scale at a 10 degC
rise, then, for every 2.0 mm trace in power_node.kicad_pcb (VSUPPLY, VLOAD and the ground return):
its length, temperature rise, DC resistance and drop at both currents.
Stdlib only:  python3 hw/kicad/trace_width.py
"""
import math
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PCB = os.path.join(HERE, "power_node.kicad_pcb")

K_OUTER = 0.048
COPPER_MM = 0.035              # 1 oz/ft^2, both outer layers
RHO_CU = 1.72e-8               # ohm m, copper at 20 degC
MIL = 0.0254                   # mm
DT_MAX = 10.0                  # degC rise allowed
CURRENTS = (("design load", 2.0), ("INA219 full scale", 3.2))   # 160 mV / 50 mOhm = 3.2 A


def min_width_mm(amps, dt):
    area_mil2 = (amps / (K_OUTER * dt ** 0.44)) ** (1 / 0.725)
    return area_mil2 / (COPPER_MM / MIL) * MIL


def rise_c(amps, width_mm):
    area_mil2 = (width_mm / MIL) * (COPPER_MM / MIL)
    return (amps / (K_OUTER * area_mil2 ** 0.725)) ** (1 / 0.44)


def mohm_per_mm(width_mm):
    return RHO_CU / (width_mm * 1e-3 * COPPER_MM * 1e-3) * 1e-3 * 1e3


def power_traces():
    """{net: total length in mm} of the locked 2.0 mm tracks."""
    with open(PCB, encoding="utf-8") as f:
        text = f.read()
    out = {}
    for m in re.finditer(r"\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)\s+"
                         r"\(width ([\d.]+)\).*?\(net \"([^\"]+)\"\)", text, re.S):
        x0, y0, x1, y1, width = (float(v) for v in m.groups()[:5])
        if width >= 1.9:
            out[m.group(6)] = out.get(m.group(6), 0.0) + math.hypot(x1 - x0, y1 - y0)
    return out


def main():
    print("IPC-2221 outer layer, k = %.3f, 1 oz copper (%.0f um), allowed rise %.0f degC"
          % (K_OUTER, COPPER_MM * 1000, DT_MAX))
    for name, amps in CURRENTS:
        print("  %-18s %.1f A -> minimum width %.2f mm" % (name, amps, min_width_mm(amps, DT_MAX)))
    width = 2.0
    print("Drawn width %.1f mm: %.3f mOhm/mm" % (width, mohm_per_mm(width)))
    for name, amps in CURRENTS:
        print("  rise at %.1f A: %.1f degC" % (amps, rise_c(amps, width)))
    print("%-10s %9s %9s %14s %14s" % ("net", "length", "R", "drop @ 2.0 A", "drop @ 3.2 A"))
    for net, length in sorted(power_traces().items()):
        r = mohm_per_mm(width) * length
        print("%-10s %6.1f mm %5.2f mOhm %11.1f mV %11.1f mV" % (net, length, r, r * 2.0, r * 3.2))
    print("GND is the trace alone; both GND pours run in parallel with it.")
    print("Shunt for comparison: 50 mOhm -> 100 mV at 2 A. The Kelvin taps sit on R1's sense pads, so these "
          "drops are outside the shunt measurement.")


if __name__ == "__main__":
    main()
