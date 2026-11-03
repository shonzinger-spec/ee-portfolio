#!/usr/bin/env python3
"""Run the KiCad checks of the schematic and the board, and show that the board checks can fail.

Run it with KiCad 10's Python, like gen_pcb.py:  <KiCad python3> hw/kicad/check_pcb.py

1. ERC on the schematic, and check_netlist.py.
2. DRC with schematic parity, all severities, on the committed board: as saved and with
   --refill-zones (which only proves the saved fills are current; the file is not rewritten).
3. trace_width.py on the committed board.
4. Sabotaged copies of the board, each made in its own temporary directory, which the checks
   must reject:
     Freerouting's SDA tracks and via removed -> DRC reports an unconnected pad
     R4's value changed to 4.7k               -> DRC reports a schematic parity error
     the VSUPPLY trace narrowed to 1.0 mm     -> DRC fires the Power net class width rule
     the ground return narrowed to 0.5 mm     -> DRC fires the GND_RETURN width rule,
                                                 and trace_width.py fails
     the ground return replaced by new        -> the same two (the rule goes by the rule area,
       0.5 mm tracks on the same path            not by which tracks the script drew)
     the ground return removed                -> trace_width.py fails (DRC stays clean: the
                                                 pours still join J1 pin 2 and J2 pin 2)
Prints PASS/FAIL per check; exit 0 only if every check passes.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pcbnew

from gen_pcb import HERE, KICAD_CLI, PCB, PROJECT, SCH

PROJECT_FILES = [PROJECT + ext for ext in (".kicad_pro", ".kicad_dru", ".kicad_sch", ".kicad_sym")] + [
    "sym-lib-table", "fp-lib-table"]
results = []


def check(name, ok, detail):
    results.append(ok)
    print("%s  %-52s %s" % ("PASS" if ok else "FAIL", name, detail))


def drc(board_path, *extra):
    """kicad-cli DRC with schematic parity; returns (exit code, counts, report text)."""
    report = os.path.join(os.path.dirname(board_path), "drc.rpt")
    r = subprocess.run([KICAD_CLI, "pcb", "drc", "--schematic-parity", "--severity-all",
                        "--exit-code-violations"] + list(extra) + ["-o", report, board_path],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    with open(report, encoding="utf-8") as f:
        text = f.read()
    counts = [int(n) for n in re.findall(r"\*\* Found (\d+) ", text)]   # violations, unconnected, parity
    return r.returncode, counts, text


def summary(code, counts):
    return "%d violations, %d unconnected, %d parity errors (exit %d)" % (counts[0], counts[1], counts[2], code)


def sabotaged(tmp, case):
    """Copy the project into tmp and write the board with CASES[case] applied there. Each copy is
    made in a fresh process: a second LoadBoard() after a save elsewhere is not reliable in one."""
    for f in PROJECT_FILES:
        shutil.copy(os.path.join(HERE, f), tmp)
    shutil.copytree(os.path.join(HERE, PROJECT + ".pretty"), os.path.join(tmp, PROJECT + ".pretty"))
    path = os.path.join(tmp, PROJECT + ".kicad_pcb")
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--sabotage", case, path],
                       stdout=subprocess.PIPE, text=True, check=True)
    return path, r.stdout.strip().splitlines()[-1]


def tracks(board):
    items = board.Tracks()       # indexed: the SWIG iterator of GetTracks() fails under Python 3.14
    return [items[i] for i in range(len(items)) if items[i].GetClass() == "PCB_TRACK"]


def delete_sda(board):
    items = board.Tracks()
    routed = [items[i] for i in range(len(items)) if items[i].GetNetname() == "/SDA" and not items[i].IsLocked()]
    for t in routed:
        board.Delete(t)
    return "SDA routing removed (%d tracks and vias)" % len(routed)


def change_r4(board):
    fps = board.Footprints()
    [fps[i] for i in range(len(fps)) if fps[i].GetReference() == "R4"][0].SetValue("4.7k")
    return "R4 value 2.2k -> 4.7k"


def ground_return(board):
    return [t for t in tracks(board) if t.GetNetname() == "GND" and t.GetWidth() >= pcbnew.FromMM(1.9)]


def redraw_return(board):
    old = ground_return(board)
    for t in old:
        new = pcbnew.PCB_TRACK(board)
        new.SetStart(t.GetStart())
        new.SetEnd(t.GetEnd())
        new.SetLayer(t.GetLayer())
        new.SetNet(t.GetNet())
        new.SetWidth(pcbnew.FromMM(0.5))
        board.Add(new)
        board.Delete(t)
    return "GND return redrawn, new 0.5 mm tracks (%d)" % len(old)


def remove_return(board):
    old = ground_return(board)
    for t in old:
        board.Delete(t)
    return "GND return removed (%d tracks)" % len(old)


def narrow(net, width):
    def change(board):
        hits = [t for t in tracks(board) if t.GetNetname() == net and t.GetWidth() >= pcbnew.FromMM(1.9)]
        for t in hits:
            t.SetWidth(pcbnew.FromMM(width))
        return "%s 2.0 -> %.1f mm (%d segment%s)" % (net.strip("/"), width, len(hits), "s" * (len(hits) != 1))
    return change


def main():
    version = subprocess.run([KICAD_CLI, "version"], stdout=subprocess.PIPE, text=True).stdout.strip()
    print("KiCad %s, board %s" % (version, os.path.relpath(PCB, os.path.join(HERE, "..", ".."))))
    with tempfile.TemporaryDirectory() as tmp:
        report = os.path.join(tmp, "erc.rpt")
        r = subprocess.run([KICAD_CLI, "sch", "erc", "--exit-code-violations", "-o", report, SCH],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(report, encoding="utf-8") as f:
            line = re.search(r"\*\* ERC messages: .*", f.read()).group(0)
        check("ERC", r.returncode == 0 and "Errors 0  Warnings 0" in line, line.strip("* ") + " (exit %d)" % r.returncode)
    r = subprocess.run([sys.executable, os.path.join(HERE, "check_netlist.py")], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, env=dict(os.environ, KICAD_CLI=KICAD_CLI))
    check("check_netlist.py", r.returncode == 0 and "RESULT: PASS" in r.stdout, r.stdout.strip().splitlines()[-1])

    with tempfile.TemporaryDirectory() as tmp:
        for f in PROJECT_FILES + [PROJECT + ".kicad_pcb"]:
            shutil.copy(os.path.join(HERE, f), tmp)
        shutil.copytree(os.path.join(HERE, PROJECT + ".pretty"), os.path.join(tmp, PROJECT + ".pretty"))
        board = os.path.join(tmp, PROJECT + ".kicad_pcb")
        code, counts, _ = drc(board)
        check("DRC, board as committed", code == 0 and counts == [0, 0, 0], summary(code, counts))
        code, counts, _ = drc(board, "--refill-zones")
        check("DRC with --refill-zones", code == 0 and counts == [0, 0, 0], summary(code, counts))
    r = subprocess.run([sys.executable, os.path.join(HERE, "trace_width.py")], stdout=subprocess.PIPE, text=True)
    check("trace_width.py", r.returncode == 0, r.stdout.strip().splitlines()[-1])

    ground_rule = (lambda c, t: "rule 'The ground return" in t, "GND_RETURN width rule")
    cases = [  # sabotage, what DRC must report (None: DRC not expected to object), run trace_width.py
        ("sda", (lambda c, t: c[1] >= 1, "unconnected pad"), False),
        ("r4", (lambda c, t: c[2] >= 1, "parity error"), False),
        ("vsupply", (lambda c, t: "rule 'Power traces" in t, "Power width rule"), False),
        ("gnd", ground_rule, True),
        ("redraw", ground_rule, True),
        ("noreturn", None, True)]
    for case, drc_expect, width_check in cases:
        with tempfile.TemporaryDirectory() as tmp:
            path, what = sabotaged(tmp, case)
            if drc_expect:
                expected, label = drc_expect
                code, counts, text = drc(path)
                seen = expected(counts, text)
                check("DRC rejects: " + what, code != 0 and seen,
                      "%s; %s%s reported" % (summary(code, counts), "" if seen else "no ", label))
            if width_check:
                r = subprocess.run([sys.executable, os.path.join(HERE, "trace_width.py"), path],
                                   stdout=subprocess.PIPE, text=True)
                check("trace_width.py rejects: " + what, r.returncode != 0,
                      "%s (exit %d)" % (r.stdout.strip().splitlines()[-1], r.returncode))
    print("RESULT: %s (%d/%d)" % ("PASS" if all(results) else "FAIL", sum(results), len(results)))
    return 0 if all(results) else 1


CASES = {"sda": delete_sda, "r4": change_r4, "vsupply": narrow("/VSUPPLY", 1.0), "gnd": narrow("GND", 0.5),
         "redraw": redraw_return, "noreturn": remove_return}

if __name__ == "__main__":
    if sys.argv[1:2] == ["--sabotage"]:              # child process of sabotaged()
        board = pcbnew.LoadBoard(PCB)
        print(CASES[sys.argv[2]](board))
        pcbnew.SaveBoard(sys.argv[3], board)
        sys.exit(0)
    sys.exit(main())
