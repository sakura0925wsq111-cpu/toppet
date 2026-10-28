#pragma once

#include <Arduino.h>

// RAM-only physical-edge measurements survive a closed serial monitor. No
// audio, text, network credentials, or flash writes are involved.
struct ButtonUiSample {
  uint32_t id;
  uint32_t edge_ms;
  uint32_t input_ms;
  uint32_t ui_ms;
  bool pressed;
  bool rendered;
};

constexpr size_t BUTTON_SAMPLE_COUNT = 32;
static portMUX_TYPE button_metrics_mux = portMUX_INITIALIZER_UNLOCKED;
static ButtonUiSample button_samples[BUTTON_SAMPLE_COUNT] = {};
static uint32_t button_sample_count = 0;
static uint32_t button_press_count = 0;
static uint32_t button_release_count = 0;
static uint32_t button_press_rendered = 0;
static uint32_t button_release_rendered = 0;
static uint32_t button_press_max_ms = 0;
static uint32_t button_release_max_ms = 0;

inline uint32_t recordButtonInput(bool pressed, uint32_t edge_ms) {
  const uint32_t now = millis();
  portENTER_CRITICAL(&button_metrics_mux);
  const uint32_t id = ++button_sample_count;
  button_samples[(id - 1) % BUTTON_SAMPLE_COUNT] =
      {id, edge_ms, now, 0, pressed, false};
  if (pressed) ++button_press_count;
  else ++button_release_count;
  portEXIT_CRITICAL(&button_metrics_mux);
  return id;
}

inline void recordButtonRendered(uint32_t id) {
  const uint32_t now = millis();
  portENTER_CRITICAL(&button_metrics_mux);
  ButtonUiSample &sample = button_samples[(id - 1) % BUTTON_SAMPLE_COUNT];
  if (sample.id == id && !sample.rendered) {
    sample.ui_ms = now;
    sample.rendered = true;
    const uint32_t elapsed = now - sample.edge_ms;
    if (sample.pressed) {
      ++button_press_rendered;
      if (elapsed > button_press_max_ms) button_press_max_ms = elapsed;
    } else {
      ++button_release_rendered;
      if (elapsed > button_release_max_ms) button_release_max_ms = elapsed;
    }
  }
  portEXIT_CRITICAL(&button_metrics_mux);
}
