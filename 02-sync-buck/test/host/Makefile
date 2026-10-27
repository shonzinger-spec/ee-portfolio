# Host test of the buck controller. `make test` must pass; `make negative` builds four
# deliberately broken controllers and passes only if the test FAILS against each one.
CC ?= cc
CFLAGS = -std=c99 -O2 -Wall -Wextra -Werror -I../../src/buck_ctrl -I.
SRC = ../../src/buck_ctrl/buck_ctrl.c plant.c test_buck_ctrl.c
HDR = ../../src/buck_ctrl/buck_ctrl.h ../../src/buck_ctrl/buck_config.h plant.h
B = build

all: test

$(B)/test_buck: $(SRC) $(HDR)
	@mkdir -p $(B)
	$(CC) $(CFLAGS) -o $@ $(SRC) -lm

test: $(B)/test_buck
	./$(B)/test_buck

# Mutants: M1 removes the integrator dead band (limit cycle), M2 deletes both anti-windup
# lines (integrator winds up), M3 raises the OCP trip out of reach (no over-current latch),
# M4 sets the DPWM hysteresis to plain rounding (one-LSB chatter pumps the LC resonance).
$(B)/m1: $(SRC) $(HDR)
	@mkdir -p $(B)
	$(CC) $(CFLAGS) -DBUCK_DEADBAND_V=0.0f -o $@ $(SRC) -lm

$(B)/m2_buck_ctrl.c: ../../src/buck_ctrl/buck_ctrl.c
	@mkdir -p $(B)
	sed -e '/AW-COND/s/if (.*)/if (1)/' -e '/AW-CLAMP/d' $< > $@
	@grep -q 'if (1)' $@ && ! grep -q 'AW-CLAMP' $@ || (echo "mutant M2 not applied" && false)

$(B)/m2: $(B)/m2_buck_ctrl.c plant.c test_buck_ctrl.c $(HDR)
	$(CC) $(CFLAGS) -Wno-unused-variable -Wno-unused-function -o $@ $(B)/m2_buck_ctrl.c plant.c test_buck_ctrl.c -lm

$(B)/m3: $(SRC) $(HDR)
	@mkdir -p $(B)
	$(CC) $(CFLAGS) -DBUCK_OCP_TRIP_A=1e9f -o $@ $(SRC) -lm

$(B)/m4: $(SRC) $(HDR)
	@mkdir -p $(B)
	$(CC) $(CFLAGS) -DBUCK_DPWM_HYST_LSB=0.5f -o $@ $(SRC) -lm

negative: $(B)/m1 $(B)/m2 $(B)/m3 $(B)/m4
	@for m in m1 m2 m3 m4; do \
	  echo "== mutant $$m =="; \
	  ./$(B)/$$m | grep -E '^FAIL|^RESULT' || true; \
	  if ./$(B)/$$m > /dev/null; then echo "NEGATIVE GATE BROKEN: $$m passed"; exit 1; \
	  else echo "mutant $$m: test FAILED as required"; fi; \
	done
	@echo "NEGATIVE GATE: 4/4 mutants caught"

.PHONY: all test negative
