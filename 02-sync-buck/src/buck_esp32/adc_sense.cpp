// adc_sense.cpp - ESP32 ADC1 oneshot + adc_cali line fitting.
//
// Scale: buck_config.h defines counts against a calibrated 3.3 V full scale, so every read is
// turned into millivolts first and then into counts = mV x 4096 / 3300, clamped to 0..4095.
// Range note: at 12 dB the classic ESP32 ADC stops being linear near 3.1 V. The operating
// points sit well below that (VSENSE 2.02 V at 5 V, ISENSE 2.25 V at the 4.5 A OCP trip,
// VINSENSE 2.11 V at 12 V); a 6.25 A overload (3.13 V) reads at or near full scale, which is
// above the trip either way.
#include "adc_sense.h"
#include "fw_config.h"
#include "esp_err.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

#define ADC_CH_VSENSE   ADC_CHANNEL_6   // GPIO34
#define ADC_CH_ISENSE   ADC_CHANNEL_7   // GPIO35
#define ADC_CH_VIN      ADC_CHANNEL_5   // GPIO33
#define ADC_FS_MV       3300
#define CTRL_COUNTS     4096

#ifndef FW_SIM_WOKWI

static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali;        // nullptr -> raw fallback
static const char *s_cal_name = "RAW";

void adc_sense_init(void)
{
    adc_oneshot_unit_init_cfg_t ucfg = {};
    ucfg.unit_id = ADC_UNIT_1;
    ucfg.ulp_mode = ADC_ULP_MODE_DISABLE;
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&ucfg, &s_adc));

    adc_oneshot_chan_cfg_t ccfg = {};
    ccfg.atten = ADC_ATTEN_DB_12;
    ccfg.bitwidth = ADC_BITWIDTH_12;
    ESP_ERROR_CHECK(adc_oneshot_config_channel(s_adc, ADC_CH_VSENSE, &ccfg));
    ESP_ERROR_CHECK(adc_oneshot_config_channel(s_adc, ADC_CH_ISENSE, &ccfg));
    ESP_ERROR_CHECK(adc_oneshot_config_channel(s_adc, ADC_CH_VIN, &ccfg));

    // Line fitting would also "succeed" with the 1100 mV default Vref when the eFuse is blank
    // (e.g. a simulator); that is not a calibration, so it takes the raw path instead.
    adc_cali_line_fitting_efuse_val_t ef;
    if (adc_cali_scheme_line_fitting_check_efuse(&ef) == ESP_OK &&
        ef != ADC_CALI_LINE_FITTING_EFUSE_VAL_DEFAULT_VREF) {
        adc_cali_line_fitting_config_t kcfg = {};
        kcfg.unit_id = ADC_UNIT_1;
        kcfg.atten = ADC_ATTEN_DB_12;
        kcfg.bitwidth = ADC_BITWIDTH_12;
        kcfg.default_vref = 1100;
        if (adc_cali_create_scheme_line_fitting(&kcfg, &s_cali) == ESP_OK)
            s_cal_name = (ef == ADC_CALI_LINE_FITTING_EFUSE_VAL_EFUSE_TP) ? "EFUSE_TP" : "EFUSE_VREF";
        else
            s_cali = nullptr;
    }
}

const char *adc_sense_cal_name(void) { return s_cal_name; }

static uint16_t read_one(adc_channel_t ch, uint16_t *raw_out)
{
    int raw = 0, mv;
    (void)adc_oneshot_read(s_adc, ch, &raw);
    if (s_cali == nullptr || adc_cali_raw_to_voltage(s_cali, raw, &mv) != ESP_OK)
        mv = raw * ADC_FS_MV / 4095;
    int counts = mv * CTRL_COUNTS / ADC_FS_MV;
    if (counts < 0) counts = 0;
    if (counts > CTRL_COUNTS - 1) counts = CTRL_COUNTS - 1;
    *raw_out = (uint16_t)raw;
    return (uint16_t)counts;
}

void adc_sense_read(adc_sample_t *s)
{
    s->v = read_one(ADC_CH_VSENSE, &s->raw_v);
    s->i = read_one(ADC_CH_ISENSE, &s->raw_i);
    s->vin = read_one(ADC_CH_VIN, &s->raw_vin);
}

#else  // FW_SIM_WOKWI ------------------------------------------------------------------------
// Simulator-only path (LEDC / Wokwi build). Measured in Wokwi (sim/wokwi/README_WOKWI.md):
//   - three adc_oneshot_read calls take 128 us, three direct SENS-register conversions 47 us
//     (15.7 us each), so three conversions cannot fit in one 20 us control period there;
//   - the raw code is round(V x 4095 / 5.0 V) (0.25 V -> 205, 2.0238 V -> 1657, 3.3 V -> 2703,
//     4.9 V -> 4013), while the simulated eFuse reports a burned Vref, so line fitting would
//     turn 2.105 V into 1.593 V.
// So here: one direct-register conversion per control period, pipelined (started at the end of
// this read, collected at the next period), channel schedule V I V I V I V VIN, each value
// held until its next conversion (V and I every 40 us, VIN every 160 us), and the raw code
// converted with Wokwi's 5.0 V transfer. Not for hardware: no SAR power management.
// The register sequence of hal/adc_ll.h (adc_oneshot_ll_set_channel / _start / _get_event /
// _get_raw_result for ADC_UNIT_1) written out on the SENS registers, because that header does not
// compile warning-free as C++ (-Wmissing-field-initializers in an unrelated function).
#include "soc/sens_struct.h"
#include "hal/misc.h"            // HAL_FORCE_READ_U32_REG_FIELD: 32-bit access, as adc_ll.h does

#define WOKWI_FS_MV     5000

static adc_oneshot_unit_handle_t s_adc;
static const adc_channel_t k_ch[3] = {ADC_CH_VSENSE, ADC_CH_ISENSE, ADC_CH_VIN};
static const uint8_t k_sched[8] = {0, 1, 0, 1, 0, 1, 0, 2};   // index into k_ch
static uint16_t s_raw[3];
static uint8_t s_slot;

static void ll_start(int k)
{
    SENS.sar_meas_start1.sar1_en_pad = 1u << k_ch[k];          // one channel selected
    while (HAL_FORCE_READ_U32_REG_FIELD(SENS.sar_slave_addr1, meas_status) != 0) {}
    SENS.sar_meas_start1.meas1_start_sar = 0;
    SENS.sar_meas_start1.meas1_start_sar = 1;
}

static uint16_t ll_collect(void)
{
    while (!SENS.sar_meas_start1.meas1_done_sar) {}
    return (uint16_t)HAL_FORCE_READ_U32_REG_FIELD(SENS.sar_meas_start1, meas1_data_sar);
}

static uint16_t to_counts(uint16_t raw)
{
    // counts = raw x 5000 mV / 4095 x 4096 / 3300 mV, rounded
    const uint64_t num = (uint64_t)raw * WOKWI_FS_MV * CTRL_COUNTS;
    const uint64_t den = (uint64_t)4095 * ADC_FS_MV;
    uint32_t c = (uint32_t)((num + den / 2) / den);
    return (uint16_t)(c > CTRL_COUNTS - 1 ? CTRL_COUNTS - 1 : c);
}

void adc_sense_init(void)
{
    adc_oneshot_unit_init_cfg_t ucfg = {};
    ucfg.unit_id = ADC_UNIT_1;
    ucfg.ulp_mode = ADC_ULP_MODE_DISABLE;
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&ucfg, &s_adc));
    adc_oneshot_chan_cfg_t ccfg = {};
    ccfg.atten = ADC_ATTEN_DB_12;
    ccfg.bitwidth = ADC_BITWIDTH_12;
    for (int k = 0; k < 3; k++) {
        ESP_ERROR_CHECK(adc_oneshot_config_channel(s_adc, k_ch[k], &ccfg));
        int raw = 0;
        ESP_ERROR_CHECK(adc_oneshot_read(s_adc, k_ch[k], &raw));   // sets up the unit, primes
        s_raw[k] = (uint16_t)raw;
    }
    s_slot = 0;
    ll_start(k_sched[0]);
}

const char *adc_sense_cal_name(void) { return "WOKWI_5V_LINEAR"; }

void adc_sense_read(adc_sample_t *s)
{
    s_raw[k_sched[s_slot]] = ll_collect();
    s_slot = (uint8_t)((s_slot + 1) & 7);
    ll_start(k_sched[s_slot]);
    s->raw_v = s_raw[0];
    s->raw_i = s_raw[1];
    s->raw_vin = s_raw[2];
    s->v = to_counts(s_raw[0]);
    s->i = to_counts(s_raw[1]);
    s->vin = to_counts(s_raw[2]);
}
#endif  // FW_SIM_WOKWI
