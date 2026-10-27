# Host test: real INA219 chip model (sim/wokwi/ina219.chip.c) + real firmware
# driver (src/power_node/Ina219.h, config.h) over mocked Wokwi API and Wire.
#
#   make -C test/host test       build and run the test (exit 1 on any failure)
#   make -C test/host negcheck   build a copy of the chip whose CURRENT ignores CAL
#                                and require the test to FAIL against it
#   make -C test/host clean

HERE  := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
ROOT  := $(abspath $(HERE)../..)
CHIP  := $(ROOT)/sim/wokwi/ina219.chip.c
FW    := $(ROOT)/src/power_node
# One object dir per host arch: on Apple silicon a shell under Rosetta builds
# x86_64 and a native one arm64, and the linker cannot mix the two.
BUILD := $(HERE)build/$(shell uname -m)

# wokwi-api.h comes in through -isystem: its wasm-only import/export attributes
# would otherwise warn on a host compiler. Our own code builds with -Werror.
CFLAGS   := -std=c11 -Wall -Wextra -Werror -isystem $(HERE)
CXXFLAGS := -std=c++17 -Wall -Wextra -Werror -I$(HERE)mock -I$(FW)

OBJS := $(BUILD)/ina219.chip.o $(BUILD)/mock_wokwi.o $(BUILD)/test_ina219.o

.PHONY: test negcheck clean

test: $(BUILD)/test_ina219
	@$(BUILD)/test_ina219

$(BUILD)/test_ina219: $(OBJS)
	$(CXX) -o $@ $(OBJS)

$(BUILD)/ina219.chip.o: $(CHIP) $(HERE)wokwi-api.h | $(BUILD)
	$(CC) $(CFLAGS) -c -o $@ $(CHIP)

$(BUILD)/mock_wokwi.o: $(HERE)mock_wokwi.c $(HERE)mock_wokwi.h $(HERE)wokwi-api.h | $(BUILD)
	$(CC) $(CFLAGS) -c -o $@ $(HERE)mock_wokwi.c

$(BUILD)/test_ina219.o: $(HERE)test_ina219.cpp $(HERE)mock_wokwi.h $(HERE)mock/Arduino.h \
                        $(HERE)mock/Wire.h $(FW)/Ina219.h $(FW)/config.h | $(BUILD)
	$(CXX) $(CXXFLAGS) -c -o $@ $(HERE)test_ina219.cpp

$(BUILD):
	mkdir -p $@

# Mutation: CURRENT = SHUNT * CAL / 4096  ->  CURRENT = SHUNT (CAL ignored).
MUTANT := $(BUILD)/ina219_mutant.chip.c

negcheck: $(BUILD)/mock_wokwi.o $(BUILD)/test_ina219.o
	sed 's|((int64_t)m.shunt \* s->cal) / 4096|(int64_t)m.shunt|' $(CHIP) > $(MUTANT)
	@if cmp -s $(CHIP) $(MUTANT); then echo "negcheck: mutation did not apply"; rm -f $(MUTANT); exit 1; fi
	@diff $(CHIP) $(MUTANT) || true
	$(CC) $(CFLAGS) -c -o $(BUILD)/ina219_mutant.o $(MUTANT)
	$(CXX) -o $(BUILD)/test_mutant $(BUILD)/ina219_mutant.o $(BUILD)/mock_wokwi.o $(BUILD)/test_ina219.o
	rm -f $(MUTANT) $(BUILD)/ina219_mutant.o
	@if $(BUILD)/test_mutant; then rm -f $(BUILD)/test_mutant; \
	  echo "negcheck: FAILED - the test passed against the broken model"; exit 1; \
	else rm -f $(BUILD)/test_mutant; \
	  echo "negcheck: OK - the test failed against the broken model, as it must"; fi

clean:
	rm -rf $(HERE)build
