# Wokwi simulation

The unmodified firmware runs on a simulated ESP32-DevKitC V4 with the SSD1306
OLED and an INA219. Wokwi has no INA219 part, so `ina219.chip.c` /
`ina219.chip.json` model it as a custom chip at 0x40. A slider sets the load
current, and every register read is recomputed from it (TI SBOS448 register map).

| Chip setting | Where it is set | Default |
|---|---|---|
| `currentMa`: load current, 0-3200 mA slider, step 10 | slider while running; start value in `diagram.json` attrs | 1000 |
| `shuntMohm`: shunt resistance in milliohms | `diagram.json` attrs of part `ina` | 50 |
| `supplyMv`: supply voltage in mV, before the shunt | `diagram.json` attrs of part `ina` | 5000 |

The chip.json format has no section for attributes. Defaults live in the
`attr_init()` calls in `ina219.chip.c`, and `diagram.json` can override them.
The official chip schema only accepts camelCase ids (`^[a-zA-Z][a-zA-Z0-9]*$`),
so the names are `currentMa`, `shuntMohm` and `supplyMv`.

## On wokwi.com (free, no install)

1. Run `./make_web_bundle.sh`. It copies every file the web editor needs into `web/`.
2. Open <https://wokwi.com/projects/new/esp32>.
3. In the diagram, click **+**, choose **Custom Chip**, name it `ina219`, and pick
   language **C**. Replace the generated `ina219.chip.json` and `ina219.chip.c`
   with the files from `web/`.
4. Replace `sketch.ino` and `diagram.json` with the versions in `web/`. Use the
   file-tab menu to add `config.h`, `Ina219.h` and `libraries.txt`, and paste
   each file's contents in.
5. Press play. The OLED shows V / A / W. The Serial Monitor prints a CSV line
   every 500 ms: `ms,bus_V,shunt_mV,current_mA,power_mW`.
6. Click the INA219 chip and move the **Load current (mA)** slider. Its top
   value, 3200 mA, is exactly the ±160 mV PGA full scale. Above that the
   shunt register saturates. With `"currentMa": "3300"` in `diagram.json`,
   the display stays at 3.200 A and shows no error. SBOS448 sets OVF only when
   the CURRENT or POWER calculation overflows, and with CAL = 8192 that cannot
   happen, so the real part does not flag an over-range load either. To detect
   it, the firmware would have to check |shunt| >= 160 mV itself. The exact
   over-range code is not in the datasheet. The model assumes saturation at
   full scale.

## With Wokwi for VS Code or wokwi-cli

`wokwi.toml` in this folder points at an arduino-cli build and at the chip:

```sh
# from the project root
arduino-cli compile -b esp32:esp32:esp32 --output-dir sim/wokwi/build src/power_node
cd sim/wokwi
wokwi-cli chip compile ina219.chip.c -o ina219.chip.wasm   # builds the custom chip
```

- **VS Code:** open `sim/wokwi`, press F1, then run **Wokwi: Start Simulator**.
- **wokwi-cli:** the simulation runs in Wokwi's cloud, so it needs an API token.
  Create a free token yourself on the Wokwi CI dashboard
  (<https://docs.wokwi.com/wokwi-ci/cli-usage>) and run:

  ```sh
  export WOKWI_CLI_TOKEN=wok_...      # your own token
  wokwi-cli . --timeout 10000 --expect-text "ms,bus_V"
  ```

## Host test (no simulator)

`make -C test/host test` builds the real chip model and the real firmware
driver (`Ina219.h`) against host mocks of the Wokwi API and `Wire`. It checks
the power-on and reset register values, CAL bit 0, CURRENT = 0 until CAL is
written, the BUS flag bits, the CONFIG/CAL read-back after `begin()`, readings
at 0-3000 mA, the saturation at 3300 mA, and the OVF rule. OVF must be set by a
CURRENT overflow and must stay clear for a POWER value above 32767.
`make -C test/host negcheck` breaks the CURRENT formula in a copy of the chip
and confirms that the test then fails.
