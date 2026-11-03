# Host test of the BLDC firmware. `make test` must pass; `make negative` builds seven
# deliberately broken controllers and passes only if the test FAILS against each one.
CC ?= cc
CFLAGS = -std=c99 -O2 -Wall -Wextra -Werror -I../../src/bldc -I.
SRC = ../../src/bldc/bldc.c plant.c test_bldc.c
HDR = ../../src/bldc/bldc.h ../../src/bldc/bldc_config.h plant.h
B = build

all: test

$(B)/test_bldc: $(SRC) $(HDR)
	@mkdir -p $(B)
	$(CC) $(CFLAGS) -o $@ $(SRC) -lm

test: $(B)/test_bldc
	./$(B)/test_bldc --trace ../../docs

# Mutants: M1 removes the dead time (compare pair collapses to one value), M2 commutates one
# sector early, M3 raises the over-current trip out of reach, M4 removes the invalid-Hall
# check, M5 removes the stall timeout, M6 removes the current limit, M7 removes the minimum
# duty that keeps the current sample valid.
M1 = -DBLDC_DT_NS=0
M2 = -DBLDC_COMM_ADVANCE=1
M3 = -DBLDC_OC_TRIP_A=1e9f
M4 = -DBLDC_HALL_CHECK=0
M5 = -DBLDC_STALL_MS=100000
M6 = -DBLDC_I_LIMIT_A=1e9f
M7 = -DBLDC_DUTY_MIN_SAMPLE=0.0f

$(B)/m%: $(SRC) $(HDR)
	@mkdir -p $(B)
	$(CC) $(CFLAGS) $(M$*) -o $@ $(SRC) -lm

negative: $(B)/m1 $(B)/m2 $(B)/m3 $(B)/m4 $(B)/m5 $(B)/m6 $(B)/m7
	@for m in m1 m2 m3 m4 m5 m6 m7; do \
	  echo "== mutant $$m =="; \
	  ./$(B)/$$m | grep -E '^FAIL|^RESULT' || true; \
	  if ./$(B)/$$m > /dev/null; then echo "NEGATIVE GATE BROKEN: $$m passed"; exit 1; \
	  else echo "mutant $$m: test FAILED as required"; fi; \
	done
	@echo "NEGATIVE GATE: 7/7 mutants caught"

# Design record, not a gate: the same test with both speed-loop gains doubled (see README,
# design notes): the gain margin. Prints the speed lines; failures are expected.
$(B)/gains2x: $(SRC) $(HDR)
	@mkdir -p $(B)
	$(CC) $(CFLAGS) -DBLDC_SPD_KP=0.016f -DBLDC_SPD_KI=0.24f -o $@ $(SRC) -lm

gains: $(B)/gains2x
	@echo "== speed-loop gains x2 (KP 0.016 A/rpm, KI 0.24 A/rpm/s) =="
	@./$(B)/gains2x | grep -E 'S1 (within|overshoot|speed ripple)|S2 (speed dip|back within)|^RESULT' || true

.PHONY: all test negative gains
