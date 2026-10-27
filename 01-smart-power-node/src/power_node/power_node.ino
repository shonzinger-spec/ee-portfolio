// ============================================================================
//  power_node.ino - ESP32 Smart Power-Monitoring Node (Arduino core + FreeRTOS)
//
//  Libraries : Adafruit GFX Library, Adafruit SSD1306   (INA219 driver is local)
//  Tasks     : sensorTask  prio 3, every 100 ms -> reads INA219
//              displayTask prio 2, every 500 ms -> redraws SSD1306
//  Sync      : g_sampleQueue (len 1, overwrite) = "latest value" mailbox
//              g_i2cMutex                       = serialises the shared I2C bus
// ============================================================================
#include <Arduino.h>
#include <Wire.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#include "config.h"
#include "Ina219.h"

// clkAfter = 400 kHz: Adafruit's default (100 kHz) would silently slow the bus
// for the INA219 after every OLED transfer.
static Adafruit_SSD1306 oled(OLED_W, OLED_H, &Wire, PIN_OLED_RST, 400000UL, 400000UL);
static Ina219           ina;

static SemaphoreHandle_t g_i2cMutex    = nullptr;
static QueueHandle_t     g_sampleQueue = nullptr;

// ------------------------------------------------------------ helpers -------
// RAII wrapper: guarantees the mutex is released on every exit path.
class I2cLock {
public:
  explicit I2cLock(TickType_t timeout)
      : _ok(xSemaphoreTake(g_i2cMutex, timeout) == pdTRUE) {}
  ~I2cLock() { if (_ok) xSemaphoreGive(g_i2cMutex); }
  I2cLock(const I2cLock &)            = delete;
  I2cLock &operator=(const I2cLock &) = delete;
  bool ok() const { return _ok; }
private:
  bool _ok;
};

// GNU attribute, not [[noreturn]]: Arduino auto-prototypes .ino functions, and C++ requires [[noreturn]] on the first declaration.
__attribute__((noreturn)) static void fatal(const char *msg) {
  Serial.printf("FATAL: %s\n", msg);
  for (;;) delay(1000);
}

// ------------------------------------------------------ Task 1: sensor ------
static void sensorTask(void *) {
  const TickType_t period   = pdMS_TO_TICKS(SAMPLE_PERIOD_MS);
  TickType_t       lastWake = xTaskGetTickCount();

  for (;;) {
    PowerSample s;
    {
      I2cLock lock(pdMS_TO_TICKS(I2C_MUTEX_TIMEOUT_MS));
      s.valid = lock.ok() && ina.read(s);
    }
    s.timestamp_ms = millis();

    // Length-1 queue + overwrite: never blocks, consumer always sees the newest sample.
    xQueueOverwrite(g_sampleQueue, &s);

    vTaskDelayUntil(&lastWake, period);   // fixed-rate, no drift
  }
}

// ----------------------------------------------------- Task 2: display ------
// Draws into the SSD1306 RAM buffer only - no I2C traffic, so no mutex needed.
static void renderFrame(const PowerSample &s, bool fresh) {
  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);
  oled.setTextSize(1);
  oled.setCursor(0, 0);
  oled.print(F("POWER MONITOR  5V/2A"));

  oled.setTextSize(2);
  if (!fresh) {
    oled.setCursor(0, 28);
    oled.print(F("SENSOR ERR"));
    return;
  }
  char buf[20];
  snprintf(buf, sizeof buf, "%5.2f V", s.bus_V);               oled.setCursor(0, 12); oled.print(buf);
  snprintf(buf, sizeof buf, "%5.3f A", s.current_mA / 1000.f); oled.setCursor(0, 30); oled.print(buf);
  snprintf(buf, sizeof buf, "%5.2f W", s.power_mW   / 1000.f); oled.setCursor(0, 48); oled.print(buf);
}

static void displayTask(void *) {
  const TickType_t period   = pdMS_TO_TICKS(DISPLAY_PERIOD_MS);
  TickType_t       lastWake = xTaskGetTickCount();

  for (;;) {
    PowerSample s;
    // Peek (not Receive): leaves the sample in the mailbox, never blocks.
    const bool have  = (xQueuePeek(g_sampleQueue, &s, 0) == pdTRUE);
    const bool fresh = have && s.valid &&
                       (millis() - s.timestamp_ms) <= 3 * SAMPLE_PERIOD_MS;

    renderFrame(s, fresh);                 // CPU-only, outside the lock

    {                                      // the ~1 KB frame flush is the only I2C use
      I2cLock lock(pdMS_TO_TICKS(I2C_MUTEX_TIMEOUT_MS));
      if (lock.ok()) oled.display();
    }

    if (fresh)
      Serial.printf("%lu,%.3f,%.2f,%.1f,%.1f\n", (unsigned long)s.timestamp_ms,
                    s.bus_V, s.shunt_mV, s.current_mA, s.power_mW);

    vTaskDelayUntil(&lastWake, period);
  }
}

// ---------------------------------------------------------------- setup -----
void setup() {
  Serial.begin(115200);
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
  Wire.setClock(I2C_FREQ_HZ);

  g_i2cMutex    = xSemaphoreCreateMutex();                // mutex => priority inheritance
  g_sampleQueue = xQueueCreate(1, sizeof(PowerSample));
  if (!g_i2cMutex || !g_sampleQueue) fatal("RTOS object allocation failed");

  // Hardware bring-up happens BEFORE tasks exist, so no locking needed yet.
  if (!ina.begin())                                   fatal("INA219 not responding @0x40");
  if (!oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR))   fatal("SSD1306 not responding @0x3C");
  oled.clearDisplay();
  oled.display();

  if (xTaskCreatePinnedToCore(sensorTask,  "sensor",  STACK_SENSOR,  nullptr,
                              PRIO_SENSOR,  nullptr, TASK_CORE) != pdPASS) fatal("sensor task");
  if (xTaskCreatePinnedToCore(displayTask, "display", STACK_DISPLAY, nullptr,
                              PRIO_DISPLAY, nullptr, TASK_CORE) != pdPASS) fatal("display task");

  Serial.println("ms,bus_V,shunt_mV,current_mA,power_mW");
}

void loop() {
  vTaskDelete(nullptr);   // Arduino's loopTask is not needed; free its stack
}
