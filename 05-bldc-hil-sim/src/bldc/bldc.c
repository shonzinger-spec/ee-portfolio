/* Sensored 6-step BLDC controller. See bldc.h for the hardware contract. */
#include "bldc.h"

#define PI_F 3.14159265f
#define DT_ISR (1.0f / (float)BLDC_PWM_HZ)
#define DT_SPD ((float)BLDC_SPEED_DIV / (float)BLDC_PWM_HZ)
#define STALL_PERIODS ((uint32_t)((long)BLDC_STALL_MS * BLDC_PWM_HZ / 1000L))

/* Sectors in electrical order, 60 degrees each, starting where phase A's back-EMF flat top
 * begins (30 deg). Each sector drives the two phases whose back-EMF is flat: A+ B-, A+ C-,
 * B+ C-, B+ A-, C+ A-, C+ B-. Hall codes in the same order: 1, 3, 2, 6, 4, 5. */
const int bldc_sector_high[6] = {0, 0, 1, 1, 2, 2};
const int bldc_sector_low[6]  = {1, 2, 2, 0, 0, 1};
static const int hall_to_sector[8] = {-1, 0, 2, 1, 4, 5, 3, -1};

int bldc_hall_sector(uint8_t hall)
{
    return hall_to_sector[hall & 7];
}

static float clampf(float v, float lo, float hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

static void outputs_off(bldc_io_t *io)
{
    int x;
    io->moe = 0;
    for (x = 0; x < 3; x++) {
        io->ccr_h[x] = BLDC_CCR_NEVER;   /* high side never on */
        io->ccr_l[x] = 0;                /* low side never on */
    }
}

static void trip(bldc_t *m, bldc_io_t *io, unsigned fault)
{
    m->faults |= fault;
    m->state = BLDC_FAULT;
    m->i_ref = 0.0f;
    m->duty = 0.0f;
    outputs_off(io);
}

void bldc_init(bldc_t *m, bldc_io_t *io)
{
    int k;
    m->state = BLDC_IDLE;
    m->faults = 0;
    m->rpm_cmd = m->rpm_ref = m->rpm = 0.0f;
    m->i_ref = m->i_meas = m->duty = m->vbus = 0.0f;
    m->duty_out = m->duty_prev = 0.0f;
    m->spd_int = m->cur_int = 0.0f;
    m->hall_last = io->hall;
    m->hall_bad = 0;
    m->sector = -1;
    m->edge_last_us = 0;
    for (k = 0; k < 6; k++)
        m->period_us[k] = 0;
    m->n_periods = -1;           /* -1: no edge seen yet, 0: one edge, no interval yet */
    m->ticks = 0;
    m->speed_div = 0;
    m->stall_periods = 0;
    outputs_off(io);
}

void bldc_set_speed(bldc_t *m, float rpm)
{
    m->rpm_cmd = rpm < 0.0f ? 0.0f : rpm;
}

/* Speed from the last six Hall intervals (one electrical revolution, so the placement error of
 * each sensor cancels), or fewer at low speed, where six would span tens of milliseconds and
 * lag a speed that is still changing: only intervals inside the last 10 ms are used. The time
 * since the latest edge is a lower bound on the next interval, so the estimate falls as soon as
 * the rotor slows or stops. */
static void measure_speed(bldc_t *m, const bldc_io_t *io)
{
    /* Signed difference of two 32-bit microsecond timestamps: correct across the timer wrap,
     * and a capture taken just after the now_us snapshot counts as zero, not as 71 minutes. */
    int32_t since = (int32_t)(io->now_us - m->edge_last_us);
    uint32_t sum = 0, longest = 0;
    float t_us;
    int k, n = m->n_periods < 6 ? m->n_periods : 6;

    if (since < 0)
        since = 0;
    if (n <= 0 || since > BLDC_SPEED_TIMEOUT_MS * 1000L) {
        if (n > 0)
            m->n_periods = -1;                    /* stale: start again from the next edge */
        m->rpm = 0.0f;
        return;
    }
    for (k = 0; k < n; k++) {
        if (k > 0 && sum + m->period_us[k] > BLDC_SPEED_WINDOW_US)
            break;
        sum += m->period_us[k];
        if (m->period_us[k] > longest)
            longest = m->period_us[k];
    }
    t_us = (float)sum / (float)k;
    /* Only once the wait is longer than any recent interval: Hall placement error makes some
     * intervals a few per cent longer than the average, and bounding inside one of those would
     * dip the estimate for one sample and kick the speed loop. */
    if ((uint32_t)since > longest && (float)since > t_us)
        t_us = (float)since;
    m->rpm = 60.0e6f / (t_us * 6.0f * (float)BLDC_POLE_PAIRS);
}

static void hall_edge(bldc_t *m, const bldc_io_t *io)
{
    if (m->n_periods >= 0) {
        int k;
        for (k = 5; k > 0; k--)
            m->period_us[k] = m->period_us[k - 1];
        m->period_us[0] = io->hall_edge_us - m->edge_last_us;
        if (m->n_periods < 6)
            m->n_periods++;
    } else {
        m->n_periods = 0;
    }
    m->edge_last_us = io->hall_edge_us;
    m->stall_periods = 0;
}

/* Duty -> compare pair for the PWM leg. The counter crosses ccr_l and ccr_h on each slope, so
 * the low side is off for DT_TICKS before the high side turns on and again after it turns off:
 * the dead time is inserted here, in firmware, symmetrically about the duty edge. */
static void set_pwm_leg(bldc_io_t *io, int x, float duty)
{
    int c = BLDC_TIM_ARR - (int)(duty * (float)BLDC_TIM_ARR + 0.5f);
    int h = c + BLDC_DT_TICKS / 2;
    int l = c - (BLDC_DT_TICKS - BLDC_DT_TICKS / 2);
    io->ccr_h[x] = (uint16_t)(h > BLDC_TIM_ARR ? BLDC_CCR_NEVER : h);
    io->ccr_l[x] = (uint16_t)(l < 0 ? 0 : l);
}

static void commutate(bldc_t *m, bldc_io_t *io)
{
    int s = (m->sector + BLDC_COMM_ADVANCE) % 6;
    int hi = bldc_sector_high[s], lo = bldc_sector_low[s], x;
    for (x = 0; x < 3; x++) {
        if (x == hi) {
            set_pwm_leg(io, x, m->duty);
        } else if (x == lo) {
            io->ccr_h[x] = BLDC_CCR_NEVER;   /* low side on for the whole period */
            io->ccr_l[x] = BLDC_CCR_NEVER;
        } else {
            io->ccr_h[x] = BLDC_CCR_NEVER;   /* floating */
            io->ccr_l[x] = 0;
        }
    }
    io->moe = 1;
}

void bldc_pwm_isr(bldc_t *m, bldc_io_t *io)
{
    float w;

    m->ticks++;
    m->vbus = (float)io->adc_vbus * (float)BLDC_ADC_VREF_MV * (float)BLDC_VBUS_DIV
              / ((float)BLDC_ADC_FULL * 1000.0f);
    /* The shunt carries the motor current only while the high side conducts. The sample read
     * now was taken in the period that ran the registers written two ISRs ago (preload); if
     * that ON pulse was too short to straddle the sample point, keep the previous reading. */
    if (m->duty_prev >= BLDC_DUTY_MIN_SAMPLE)
        m->i_meas = ((float)io->adc_ibus - (float)BLDC_ISENSE_ZERO) * (float)BLDC_ADC_VREF_MV
                    / ((float)BLDC_ADC_FULL * (float)BLDC_ISENSE_MV_PER_A);
    m->duty_prev = m->duty_out;
    m->duty_out = 0.0f;                           /* set again below if a leg is driven */

    if (io->hall != m->hall_last) {
        if (bldc_hall_sector(io->hall) >= 0 && bldc_hall_sector(m->hall_last) >= 0)
            hall_edge(m, io);
        m->hall_last = io->hall;
    }
    m->sector = bldc_hall_sector(io->hall);
    measure_speed(m, io);

    if (m->state == BLDC_FAULT)
        return;                                   /* latched: outputs stay off */

    /* Protection, checked every period */
    if (m->state == BLDC_RUN && m->i_meas > BLDC_OC_TRIP_A) {
        trip(m, io, BLDC_F_OC);
        return;
    }
    if (m->sector < 0) {
        if (++m->hall_bad >= 2 && BLDC_HALL_CHECK) {
            trip(m, io, BLDC_F_HALL);
            return;
        }
    } else {
        m->hall_bad = 0;
    }

    if (m->state == BLDC_IDLE) {
        if (m->rpm_cmd <= 0.0f)
            return;
        m->state = BLDC_RUN;
        m->spd_int = m->cur_int = 0.0f;
        m->rpm_ref = 0.0f;
        m->stall_periods = 0;
    }

    if (m->i_ref <= 0.0f)
        m->stall_periods = 0;
    else if (++m->stall_periods >= STALL_PERIODS) {
        trip(m, io, BLDC_F_STALL);
        return;
    }

    /* Speed loop, 1 kHz: ramped reference, PI to a current reference in [0, I_LIMIT] */
    if (++m->speed_div >= BLDC_SPEED_DIV) {
        float err, step = BLDC_RAMP_RPM_S * DT_SPD;
        m->speed_div = 0;
        if (m->rpm_ref < m->rpm_cmd)
            m->rpm_ref = m->rpm_ref + step < m->rpm_cmd ? m->rpm_ref + step : m->rpm_cmd;
        else
            m->rpm_ref = m->rpm_ref - step > m->rpm_cmd ? m->rpm_ref - step : m->rpm_cmd;
        err = m->rpm_ref - m->rpm;
        m->spd_int = clampf(m->spd_int + BLDC_SPD_KI * err * DT_SPD, 0.0f, BLDC_I_LIMIT_A);
        m->i_ref = clampf(BLDC_SPD_KP * err + m->spd_int, 0.0f, BLDC_I_LIMIT_A);
        if (m->rpm_cmd <= 0.0f && m->rpm_ref <= 0.0f) {
            /* Stop: the reference has ramped to zero, so release the motor and let it coast.
             * Staying in RUN would keep the minimum duty pulse on, which creeps or brakes. */
            m->state = BLDC_IDLE;
            m->i_ref = 0.0f;
            m->duty = m->duty_out = 0.0f;
            outputs_off(io);
            return;
        }
    }

    /* Current loop, 20 kHz: back-EMF feed-forward plus PI on the sampled DC-link current */
    {
        float err = m->i_ref - m->i_meas, ff = 0.0f;
        w = m->rpm * 2.0f * PI_F / 60.0f;
        if (m->vbus > 1.0f)
            ff = BLDC_KE_LL * w / m->vbus;
        float u = ff + BLDC_CUR_KP * err + m->cur_int;
        /* Integrate only while the output is not pinned at a limit in the direction of the error
         * (no wind-up while the duty is saturated). */
        if (!((u >= BLDC_DUTY_MAX && err > 0.0f) || (u <= BLDC_DUTY_MIN_SAMPLE && err < 0.0f)))
            m->cur_int = clampf(m->cur_int + BLDC_CUR_KI * err * DT_ISR, -BLDC_DUTY_MAX, BLDC_DUTY_MAX);
        /* Never below the shortest pulse the shunt can sample: at duty 0 the loop would be blind
         * (the held reading would freeze it at 0). Below the back-EMF the minimum pulse brakes,
         * and the loop sees that current too. */
        m->duty = clampf(ff + BLDC_CUR_KP * err + m->cur_int, BLDC_DUTY_MIN_SAMPLE, BLDC_DUTY_MAX);
    }

    if (m->sector < 0) {
        outputs_off(io);                          /* one bad sample: coast for this period */
        return;
    }
    commutate(m, io);
    m->duty_out = m->duty;
}
