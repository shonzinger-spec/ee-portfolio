// probe chip: steps an analog output through known voltages, 10 ms each, after a 600 ms wait
#include "wokwi-api.h"
#include <stdio.h>
#include <stdlib.h>
static const float V[] = {0.25f, 0.5f, 1.0f, 2.0f, 2.0238f, 3.0f, 3.3f, 4.0f, 4.9f, 6.0f};
static pin_t out; static int i;
static void tick(void *u)
{
  (void)u;
  pin_dac_write(out, V[i]);
  printf("VSTEP t_us=%llu v=%.3f\n", (unsigned long long)(get_sim_nanos() / 1000), V[i]);
  if (i < (int)(sizeof V / sizeof V[0]) - 1 && get_sim_nanos() > 600000000ull) i++;
}
void chip_init(void)
{
  out = pin_init("OUT", ANALOG);
  pin_dac_write(out, 0.0f);
  const timer_config_t t = { .user_data = NULL, .callback = tick };
  timer_t tm = timer_init(&t);
  timer_start(tm, 5000, true);
}
