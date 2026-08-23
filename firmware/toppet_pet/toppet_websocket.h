#pragma once

#include <Arduino.h>
#include <WiFi.h>

class TopPetWebSocketClient {
 public:
  bool connectTo(const char *host, uint16_t port, const char *path);
  void close();
  bool connected();

  bool sendText(const String &text);
  bool sendBinary(const uint8_t *data, size_t length);
  bool sendPong(const uint8_t *data, size_t length);

  // Reads one server frame. Payloads larger than capacity are consumed and
  // truncated, which lets the first firmware discard an audio reply safely.
  bool readFrame(
      uint8_t *payload, size_t capacity, size_t &length, uint8_t &opcode,
      uint32_t timeout_ms);

 private:
  bool sendFrame(uint8_t opcode, const uint8_t *data, size_t length);
  bool readExact(uint8_t *data, size_t length, uint32_t timeout_ms);
  bool readByte(uint8_t &value, uint32_t timeout_ms);
  bool waitForHandshake(uint32_t timeout_ms);

  WiFiClient client_;
};
