# Wokwi simulation (on-target run)

The ESP32 firmware (`src/buck_esp32`, LEDC build) runs on a simulated ESP32-DevKitC V4.
The power stage is a custom chip, `buck_plant.chip.c`, wrapped around the host test's
plant model: it `#include`s `test/host/plant.c`, so the on-target run and
`make -C test/host test` use the same averaged RK4 model with the same component values.
The scenario `buck_steps.test.yaml` repeats the host test: soft start at 1 A, a load step
0.5 -> 3 -> 0.5 A, and a 6.25 A overload (0.8 ohm) that must latch the OCP with both gates low.

| File | What it is |
|---|---|
| `buck_plant.chip.c` / `.chip.json` | power-stage chip: pins PWM_H, PWM_L (inputs), VSENSE, ISENSE, VINSENSE (analog outputs), FLT, GND |
| `build_chip.sh` | builds `buck_plant.chip.wasm` with the WASI-SDK clang; `--web` also writes a one-file chip for wokwi.com |
| `diagram.json`, `wokwi.toml` | ESP32 + chip wired per the J3 pin map, serial monitor, red LED on FLT |
| `buck_steps.test.yaml` | the scenario (wait-serial on the firmware's lines, set-control on the load, expect-pin); the load-step checks wait for `STEP_OK`, which the firmware prints only if the step stayed inside dip >= 4.50 V, peak <= 5.50 V and settled within 5 ms (the host test's limits) |
| `buck_stall.test.yaml` | negative scenario for the overrun latch: passes only on a firmware built with `-DFW_TEST_STALL_TICKS=4` |
| `plot_wokwi.py` | serial log + chip log -> `docs/img/wokwi_buck.png` |
| `probe/` | the ADC probe used to measure Wokwi's ADC (see "What Wokwi does differently") |

Wiring (diagram.json): GPIO18 -> PWM_H, GPIO19 -> PWM_L, VSENSE -> GPIO34 (ADC1_CH6),
ISENSE -> GPIO35 (ADC1_CH7), VINSENSE -> GPIO33 (ADC1_CH5). The chip's PWM inputs use
pull-downs, which stand in for R10/R11.

## Chip settings

| Setting | Where | Default |
|---|---|---|
| `loadMa`: load current at 5 V, 0-7000 mA slider (R = 5 V / I) | slider, `set-control`, `diagram.json` | 1000 |
| `vinMv`: input voltage, 0-20000 mV slider | slider, `set-control`, `diagram.json` | 12000 |
| `stepNs`: plant step, simulated ns | `diagram.json` | 6400 |
| `timeScale`: plant time = simulated time / timeScale | `diagram.json` (20 here) | 1 |

## Run it

```sh
# from 02-sync-buck/
arduino-cli compile -b esp32:esp32:esp32 --warnings all --clean --library src/buck_ctrl \
  --build-property "compiler.cpp.extra_flags=-DBUCK_PWM_LEDC" \
  --build-property "compiler.c.extra_flags=-DBUCK_PWM_LEDC" \
  --output-dir sim/wokwi/build src/buck_esp32
# build_chip.sh needs Wokwi's chip API header next to it (kept out of git by the repo .gitignore):
#   https://wokwi.com/api/chips/wokwi-api.h -> sim/wokwi/wokwi-api.h
sim/wokwi/build_chip.sh --web
cd sim/wokwi
export WOKWI_CLI_TOKEN=...          # your own token from the Wokwi CI dashboard
wokwi-cli . --scenario buck_steps.test.yaml --timeout 4000 --serial-log-file ../../docs/wokwi_serial.log
python3 plot_wokwi.py               # -> docs/img/wokwi_buck.png
```

- **VS Code:** open `sim/wokwi`, press F1, run **Wokwi: Start Simulator**, and move the load slider.
- **wokwi.com (not tried in this run):** run `./build_chip.sh --web`. Start a new ESP32
  project, add a custom chip named `buck_plant` (language C), and paste in
  `web/buck_plant.chip.c` and `web/buck_plant.chip.json`. Replace `diagram.json` with
  `web/diagram.json`. Load the firmware with F1, then **Upload Firmware and Start
  Simulation**, and pick `build/buck_esp32.ino.bin`. The web editor cannot pass
  `-DBUCK_PWM_LEDC`, so the sketch is not compiled there.

The chip's `printf` lines appear in the wokwi-cli output with the prefix `[chip-buck_plant]`
(in a browser they go to the JavaScript console):

- `PLANT tp_us=...`: VLOAD, IL, load current and mean duty from the plant's own state. A row
  is printed every 100 us of plant time for 8 ms after a control change, otherwise every 2 ms.
- `PSEG ...`: one row per load or line segment. It gives VLOAD min/max, settle_us (the last
  time VLOAD was outside 5 V +/-1 %, measured from the change) and the end values.
- `PGATE ...`: gate statistics in simulated ns. It gives the dead-time count, min and max,
  the count below 40 ns, a 10 ns histogram, the overlap count and time, the gate periods,
  and the time spent in diode mode.

## What the chip models

- **Gate timing to duty.** PWM_H and PWM_L edges are time-stamped with `get_sim_nanos()`.
  Every `stepNs` (6.4 us, one LEDC period) the plant is stepped once with the window's
  average switch-node voltage:
  - H on: Vin.
  - L on: 0 V.
  - Both off for a normal dead time (gap <= 100 ns): 0 V. This is ideal switching, as in
    `plant.c`.
  - Both off for a longer gap: the body diodes conduct. The node is -0.7 V while IL > 0
    and Vin + 0.7 V while IL < 0. Once IL reaches 0 it is clamped there (DCM).

  A one-period window averages out the ripple whatever its phase. This matches what the
  averaged model assumes. 6.4 us is far below the LC period (about 535 us). `plant.c`
  integrates the 4.05 us anti-alias pole exactly.
- **Sensing.**
  - VSENSE = `plant_t.vaa` (the 10k/6.8k divider + 1 nF filter).
  - ISENSE = 0.5 V/A x load current.
  - VINSENSE = Vin x 10/57.
  - All three are clamped to 0-3.3 V.
- **Gate checks.**
  - Every H-fall -> L-rise and L-fall -> H-rise gap is a dead time.
  - Any interval with H and L both high counts as shoot-through. It also latches FLT
    high; the scenario expects FLT = 0 at the end.
- **Not modelled.** Switching ripple, MOSFET and driver delays, the bootstrap supply, the
  ADC sampling capacitor, the 8 us VINSENSE filter (`plant.c` neglects it too), and hard
  shorts (the product needs a hardware comparator).

## What Wokwi does differently, and the sim-only workarounds

The workarounds apply only when `BUCK_PWM_LEDC` is defined. That flag now also sets
`FW_SIM_WOKWI` (`src/buck_esp32/fw_config.h`). The MCPWM product build is unchanged:
`docs/fw_compile_mcpwm.log` is that build (it compiles; it has never run, see "What ran and what did not").

1. **ADC transfer.** Wokwi returns raw = V x 4095 / 5.0 V:
   0.25 V -> 205, 1.0 V -> 819, 2.0238 V -> 1657, 3.3 V -> 2703, 4.9 V -> 4013, 6 V -> 4095
   (`docs/wokwi_probe_adc.log`). Its simulated eFuse reports a burned Vref, so IDF line
   fitting turns 3.3 V (raw 2703) into 2417 mV. **Sim fix:** convert the raw code with
   Wokwi's 5.0 V transfer (`adc_cal=WOKWI_5V_LINEAR` in the BOOT line).
2. **ADC time.** Three `adc_oneshot_read` calls take 128 us of simulated time. Three
   direct SENS-register conversions take 47 us (`docs/wokwi_probe_adc.log`). The budget is
   20 us. **Sim fix:** run one direct-register conversion per update, pipelined, on the
   schedule V I V I V I V VIN. Each value is held until its channel is read again: V every
   40 us, I every 40 or 80 us, VIN every 160 us.
3. **CPU and interrupt time.** One control update costs 91 us of simulated time
   (`buck_update` alone 15 us). With the 50 kHz gptimer interrupt, an update stretches to
   about 500 us (`docs/wokwi_timing_diag.log`). With the normal overrun limit (3), the
   firmware therefore latches `FAULT OVERRUN` with both gates low, which is its designed
   fail-safe. **Sim fix:** time dilation.
   The loop waits on the CPU cycle counter (no interrupt) for a deadline every
   20 us x `FW_SIM_TIME_SCALE` (20) of simulated time. The chip steps its plant by
   dt / `timeScale` (20). The controller is discrete-time, so the plant still sees one
   update per 20 us of its own time. Firmware times (`t_us`, `settle_us`, `trip_us`) are
   control time (20 us per tick). Gate timing stays in real simulated ns. Wokwi's timing
   says nothing about silicon: whether three conversions plus the update fit in 20 us on
   a real ESP32 is still a bring-up check. The hardware build latches `FAULT OVERRUN` if
   they do not.
4. **LEDC edge timing.** In the passing run (`docs/wokwi_run.log`, last `PGATE` row),
   375364 dead times were measured. Their histogram is 0,0,0,25,20794,353870,470,177,28,0,0
   in 10 ns bins, with min 39 ns, max 86 ns and no overlaps (`flt=0`). 25 were below 40 ns,
   all of them 39 ns. 23 fall in the first 6.6 ms of control time (soft start, duties of a
   few LEDC counts, before READY at 6.94 ms). One more falls in the `PGATE` interval ending
   at tp 67926 us and one in the interval ending at tp 76536 us, so two single 39 ns dead
   times occurred after READY (the plant counts below 40 ns as a violation; the nominal
   value is 50 ns). The H period should be exactly 6400 ns, because H always rises at
   count 0. It measured 13..6401 ns, so Wokwi produced at least one runt H pulse. The L
   period measured 6376..6436 ns. The cause is not established. It may be Wokwi's
   rendering of very short LEDC pulses, or a duty update latching mid-period in Wokwi's
   LEDC model.
5. **LEDC update order and latching.** How the classic ESP32 latches `LEDC_HPOINT` relative
   to the duty (at the next overflow, or at once) is not established here: no TRM page is
   cited and nothing was measured on silicon. `pwm_ledc.cpp` therefore updates so that H and
   L cannot overlap under either behaviour: on a rising duty L is written before H, on a
   falling duty H is written now and L one control tick (400 us of simulated time in this build,
   more than one LEDC period) later. Wokwi's model only shows that Wokwi latches both together.
6. **Stopping the outputs.** Development note, not backed by a kept log: with `ledc_stop()`
   alone, an earlier Wokwi run (the build before this change) left GPIO18 high after the OCP
   latch and `expect-pin` failed. The cause is suspected, not established (the simulated
   channel may keep its last output level when stopped while high). `pwm_off()` now writes
   duty 0 to both channels, waits two LEDC periods, then calls `ledc_stop()`. The passing
   run uses that version.
7. **Sim-only guard.** The LEDC build prints `WARNING SIM_BUILD_NOT_FOR_HARDWARE` on the BOOT
   line, and `pwm_enable()` refuses (firmware fault `FAULT PWM`, gates stay low) unless the
   chip's default MAC is Wokwi's 24:0A:C4:00:01:10. The passing run shows `mac=wokwi`.

## Results (passing run)

See `docs/wokwi_serial.log`, `docs/wokwi_run.log` and `docs/img/wokwi_buck.png`.
The host test numbers come from `docs/host_test.log`.

| Quantity | On target (firmware, 100 us means) | Plant view (chip) | Host test |
|---|---|---|---|
| soft start | first row within 1 %: 5.38 ms; RUN at 6.94 ms | max 5.0125 V | settle 5.26 ms, max 5.0271 V |
| Vout at 1 A | 5.008 V (20 rows, 9-20 ms) | end of segment 5.0081 V | mean 5.0081 V |
| Vout at 0.5 A | 5.002 V (4 rows, 45-60 ms) | end of segment 5.0007 V | - |
| 0.5 -> 3 A step | min 4.646 V, max 5.168 V, settle 1.5 ms (`STEP_OK`) | min 4.5867 V, max 5.1969 V, settle 2.01 ms | min 4.5864 V, recovery 1.30 ms |
| 3 -> 0.5 A release | min 4.729 V, max 5.379 V, settle 2.0 ms (`STEP_OK`) | min 4.7018 V, max 5.4515 V, settle 1.80 ms | max 5.4419 V, recovery 2.10 ms |
| OCP at 6.25 A | trip_us = 40 (3rd sample), gates 0/0 (expect-pin) | - | 3 samples |
| exec per update | mean 67.4-71.9 us, max 220.2 us of simulated time, budget 400 us; 0 overruns | - | - |
| gates | - | 0 overlaps; dead time min 39 ns (see item 4) | - |

Why the columns differ:

- The 100 us means smooth the dip and the peak. The plant view is the true extreme.
- The plant's dip and peak match the host to within 10 mV.
- Settling times differ by up to 710 us (step: 2.01 ms against 1.30 ms; release: 1.80 ms
  against 2.10 ms). Settle is "last time outside +/-1 %", so one
  ringing peak just inside or just outside the 50 mV band changes it by a full ring period.
- The sim build also differs from the host model in four small ways:
  - the sample ages from the ADC schedule;
  - LEDC duty steps of 1/512 instead of 1/400;
  - up to one LEDC period of latch delay;
  - 50 ns dead times (100 ns per period), which the chip counts as ideal.
- The firmware's `i_from_A` is the level two 100 us rows before the trigger row (or the row
  before it when that row is still flat), because the trigger row already holds part of the
  step; `t_us` of a STEP line is the start of the row where the step began. In this run the
  0.5 -> 3 A step logs `i_from_A=0.501` at `t_us=32500`.
- The trace lines `STEP ... STEP_OK` in `docs/wokwi_serial.log` run into the next trace row
  (`STEP_OK36000,5.010,...`): the CLI's serial log drops the newline after the text a
  `wait-serial` step matched. The firmware prints a newline there; the file is as the CLI wrote it.

## What ran and what did not

- Ran: the LEDC variant in Wokwi, at 20x time dilation, with a pipelined single-conversion
  SENS-register ADC and a cycle-counter tick (no 50 kHz interrupt).
- Compiled only: the MCPWM product variant (`docs/fw_compile_mcpwm.log`). The MCPWM backend has
  not executed anywhere: Wokwi has no MCPWM on the classic ESP32. The force / brake /
  dead-time ordering in `pwm_mcpwm.cpp` is argued from the IDF headers. `pwm_enable()` and
  `pwm_off()` return the driver status, so a refused OST recovery latches `FAULT PWM`
  instead of leaving the gates silently off.
- Not measured: whether the product's three `adc_oneshot_read` calls plus one update fit in
  20 us on silicon (Wokwi measured 128 us for the three calls, and 91 us per update). The
  hardware build may latch `FAULT OVERRUN` at bring-up.
- Overrun latch: it counts missed ticks, so one update that stalls for 3 or more ticks
  latches on its own. See the stall run below.

## Negative and fault-path runs

Each run rebuilds the LEDC firmware with one define, runs a scenario, and logs the build and
the CLI output. Afterwards the normal LEDC build is restored. `docs/fw_compile_ledc.log` ends
with a `cmp` against the binary of the passing run: they differ only in the SHA-256 fields
and the build-time string.

| Run (log) | Build define | Scenario | Must | Result |
|---|---|---|---|---|
| OCP off (`docs/wokwi_negative.log`) | `-DBUCK_OCP_TRIP_A=1e9f` | `buck_steps.test.yaml` | fail | both load steps `STEP_OK`, no `FAULT OCP` at 6.25 A; timeout, `EXIT=42` |
| slow integrator (`docs/wokwi_negative_step.log`) | `-DBUCK_KI=100.0f` (Ki 800 -> 100) | `buck_steps.test.yaml` | fail | 0.5 -> 3 A step `settle_us=7000 STEP_FAIL`; the scenario waits for `STEP_OK` and times out, `EXIT=42` |
| stalled update (`docs/wokwi_stall.log`) | `-DFW_TEST_STALL_TICKS=4` | `buck_stall.test.yaml` | pass | `FAULT OVERRUN`, then `expect-pin` GPIO18 = 0 and GPIO19 = 0; `EXIT=0` |

The slow-integrator define was chosen on the host model first. The same define in `test/host`
passes soft start (5.70 ms) and fails both recovery checks: 6.86 ms and 5.62 ms against 5 ms.
Those lines are at the top of `docs/wokwi_negative_step.log`. So the Wokwi failure comes from
the load-step check, not from an earlier step of the scenario.

In the passing run the overload also logs a third `STEP` line (`i_to_A=5.181 ... settle_us=-2
STEP_FAIL`). `-2` means the step was interrupted by the state change to FAULT. The scenario
waits for `STEP_OK` only on the two load steps.
