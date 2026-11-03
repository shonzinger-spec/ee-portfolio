# Host test of the BMS firmware. `make test` must pass; `make negative` builds six
# deliberately broken BMS builds and passes only if the test FAILS against each one.
CC ?= cc
CFLAGS = -std=c99 -O2 -Wall -Wextra -Werror -I../../src/bms -I.
SRC = ../../src/bms/bms.c plant.c test_bms.c
HDR = ../../src/bms/bms.h ../../src/bms/bms_config.h plant.h
B = build

all: test

$(B)/test_bms: $(SRC) $(HDR)
	@mkdir -p $(B)
	$(CC) $(CFLAGS) -o $@ $(SRC) -lm

test: $(B)/test_bms
	./$(B)/test_bms

# Mutants: M1 raises the OV trip out of reach (overcharge), M2 never starts balancing,
# M3 raises the SCD trip out of reach (only the slower OCD catches the short), M4 allows
# charging below 0 degC, M5 raises the discharge over-temperature trip out of reach,
# M6 removes the cell-level end of charge (the weakest cell is overcharged in CV).
M1 = -DBMS_OV_TRIP_MV=99999
M2 = -DBMS_BAL_START_MV=99999
M3 = -DBMS_SCD_TRIP_MA=999999
M4 = -DBMS_UTC_TRIP_DC=-999
M5 = -DBMS_OTD_TRIP_DC=9999
M6 = -DBMS_CELL_FULL_MV=99999

$(B)/m%: $(SRC) $(HDR)
	@mkdir -p $(B)
	$(CC) $(CFLAGS) $(M$*) -o $@ $(SRC) -lm

negative: $(B)/m1 $(B)/m2 $(B)/m3 $(B)/m4 $(B)/m5 $(B)/m6
	@for m in m1 m2 m3 m4 m5 m6; do \
	  echo "== mutant $$m =="; \
	  ./$(B)/$$m | grep -E '^FAIL|^RESULT' || true; \
	  if ./$(B)/$$m > /dev/null; then echo "NEGATIVE GATE BROKEN: $$m passed"; exit 1; \
	  else echo "mutant $$m: test FAILED as required"; fi; \
	done
	@echo "NEGATIVE GATE: 6/6 mutants caught"

.PHONY: all test negative
