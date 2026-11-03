// Wokwi ADC probe v2: raw transfer vs a known chip voltage, cost of adc_oneshot_read vs a direct
// SENS-register conversion (hal/adc_ll.h) on the classic ESP32.
#include <Arduino.h>
#include "esp_adc/adc_oneshot.h"
#include "hal/adc_ll.h"
#include "esp_cpu.h"

static adc_oneshot_unit_handle_t h;

static int ll_read(int ch)
{
    adc_oneshot_ll_set_channel(ADC_UNIT_1, ch);
    adc_oneshot_ll_start(ADC_UNIT_1);
    while (!adc_oneshot_ll_get_event(ADC_LL_EVENT_ADC1_ONESHOT_DONE)) {}
    return (int)adc_oneshot_ll_get_raw_result(ADC_UNIT_1);
}

void setup()
{
    Serial.begin(921600);
    adc_oneshot_unit_init_cfg_t u = {};
    u.unit_id = ADC_UNIT_1;
    adc_oneshot_new_unit(&u, &h);
    adc_oneshot_chan_cfg_t c = {};
    c.atten = ADC_ATTEN_DB_12;
    c.bitwidth = ADC_BITWIDTH_12;
    adc_oneshot_config_channel(h, ADC_CHANNEL_6, &c);
    adc_oneshot_config_channel(h, ADC_CHANNEL_7, &c);
    adc_oneshot_config_channel(h, ADC_CHANNEL_5, &c);
    Serial.printf("PROBE2 start\n");
}

void loop()
{
    int r6 = 0, r7 = 0, r5 = 0;
    const uint32_t c0 = esp_cpu_get_cycle_count();
    adc_oneshot_read(h, ADC_CHANNEL_6, &r6);
    adc_oneshot_read(h, ADC_CHANNEL_7, &r7);
    adc_oneshot_read(h, ADC_CHANNEL_5, &r5);
    const uint32_t c1 = esp_cpu_get_cycle_count();
    const int l6 = ll_read(6), l7 = ll_read(7), l5 = ll_read(5);
    const uint32_t c2 = esp_cpu_get_cycle_count();
    Serial.printf("PROBE2 t_us=%lld os=%d,%d,%d ll=%d,%d,%d os3_us=%.2f ll3_us=%.2f\n",
                  esp_timer_get_time(), r6, r7, r5, l6, l7, l5, (c1 - c0) / 240.0f, (c2 - c1) / 240.0f);
    delay(2);
}
