#pragma once

#include <Arduino.h>
#include <stdarg.h>

// Diagnostics must never hold up input, UI, capture, or playback. The native
// USB driver otherwise retries a full TX ring up to 20 times (about 2 seconds).
static uint32_t debug_dropped_messages = 0;

inline void beginDebugOutput() {
  Serial.setTxTimeoutMs(0);
  Serial.setTxBufferSize(4096);
}

inline void debugPrint(const char *message) {
  const size_t length = strlen(message);
  if (Serial.availableForWrite() < static_cast<int>(length) ||
      Serial.write(reinterpret_cast<const uint8_t *>(message), length) != length) {
    __atomic_fetch_add(&debug_dropped_messages, 1U, __ATOMIC_RELAXED);
  }
}

inline void debugPrintf(const char *format, ...) {
  char message[384];
  va_list args;
  va_start(args, format);
  vsnprintf(message, sizeof(message), format, args);
  va_end(args);
  debugPrint(message);
}

inline void debugPrintln(const char *message = "") {
  debugPrintf("%s\n", message);
}
