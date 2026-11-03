/* Switch-level plant for the host test: centre-aligned timer with preload, six MOSFETs with
 * turn-on / turn-off delays and body diodes, star-connected BLDC motor with trapezoidal
 * back-EMF, Hall sensors, the DC-link current ADC and the rotor mechanics.
 * The plant advances one timer tick (31.25 ns) per call. main.py reads these constants. */
#ifndef PLANT_H
#define PLANT_H

#include <stdint.h>
#include "bldc.h"

/* Power stage. Switch delays bound sim/ngspice/leg_deadtime.cir (IR2101 propagation + gate
 * charge to Vth): turn-on 196-215 ns, turn-off 326-437 ns. The plant uses the shortest turn-on
 * and the longest turn-off, the pessimistic pair for dead time (main.py check C11). */
#define PL_VBUS        24.0      /* V, stiff supply */
#define PL_RDS_ON      0.010     /* ohm per MOSFET */
#define PL_VF          0.80      /* V, body diode */
#define PL_TD_ON       6         /* ticks (187.5 ns) gate edge -> switch conducting */
#define PL_TD_OFF      14        /* ticks (437.5 ns) gate edge -> switch blocking */
#define PL_R_LOOP      0.020     /* ohm, bus loop seen by a shoot-through */
#define PL_RSHUNT      0.0025    /* ohm, low-side DC-link shunt (INA181A1 gain 20) */

/* Motor: 24 V, 4 pole pairs, about 3270 rpm no-load */
#define PL_R_PH        0.40      /* ohm per phase */
#define PL_L_PH        0.40e-3   /* H per phase (L - M) */
#define PL_KE_PH       0.035     /* V per rad/s, phase back-EMF on the flat top */
#define PL_POLE_PAIRS  4
#define PL_J           3.0e-5    /* kg m^2, rotor plus load */
#define PL_B           2.0e-5    /* N m per rad/s, viscous */
#define PL_TC          0.005     /* N m, Coulomb friction */
#define PL_HALL_ERR_A  2.0       /* electrical degrees, Hall placement error */
#define PL_HALL_ERR_B  (-1.5)
#define PL_HALL_ERR_C  1.0

typedef struct {
    /* timer */
    int      cnt, up;
    uint16_t ccr_h[3], ccr_l[3];     /* active (shadow) compare registers */
    /* switches: gate, conducting, ticks since the gate changed */
    int      gate_h[3], gate_l[3], sw_h[3], sw_l[3], pend_h[3], pend_l[3];
    uint64_t off_h[3], off_l[3];     /* tick of the last turn-off */
    /* electrical and mechanical state */
    double   i[3], vt[3], e[3], vn;
    double   i_shunt, i_src, torque;
    double   w, theta;               /* mechanical rad/s, rad */
    double   t_load;                 /* N m, reactive (opposes motion) */
    uint64_t tick;
    double   t;
    uint8_t  hall;
    uint32_t hall_edge_us;
    /* injected faults */
    int      hold;                   /* speed held at w by the test (w = 0: locked rotor) */
    uint8_t  hall_and, hall_or;      /* applied to the Hall code (wire break: clear a bit) */
    int      ls_stuck_on[3];         /* low-side MOSFET failed short */
    /* measurements */
    uint64_t overlap_ticks;          /* ticks with both switches of a leg conducting */
    double   i_st_peak;              /* largest shoot-through current, A */
    double   t_overlap0;             /* time of the first shoot-through, s */
    uint64_t min_gap_ticks;          /* shortest both-off interval at a leg transition */
    double   e_in, e_mech, e_load;   /* J: from the supply, electromagnetic, into the load */
    uint32_t us_offset;              /* added to the 1 MHz timer (to test its wrap-around) */
} plant_t;

void   plant_init(plant_t *p, bldc_io_t *io);
/* One timer tick. Returns 1 at the update event (counter = 0): the preload has been
 * transferred and io's inputs latched, and the caller runs the ISR next. */
int    plant_tick(plant_t *p, bldc_io_t *io);
double plant_rpm(const plant_t *p);
double plant_trap(double deg);       /* trapezoidal back-EMF shape, +/-1, 120 deg flat tops */

#endif
