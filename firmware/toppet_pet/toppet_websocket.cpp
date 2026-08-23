#include "toppet_websocket.h"

#include <esp_system.h>
#include <mbedtls/base64.h>
#include <mbedtls/sha1.h>

namespace {

constexpr char kWebSocketGuid[] = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
constexpr uint32_t kHandshakeTimeoutMs = 5000;

String makeClientKey() {
  uint8_t random_bytes[16];
  for (uint8_t index = 0; index < sizeof(random_bytes); ++index) {
    random_bytes[index] = static_cast<uint8_t>(esp_random());
  }
  uint8_t encoded[32] = {};
  size_t encoded_length = 0;
  if (mbedtls_base64_encode(
          encoded, sizeof(encoded), &encoded_length,
          random_bytes, sizeof(random_bytes)) != 0) {
    return String();
  }
  encoded[encoded_length] = '\0';
  return String(reinterpret_cast<char *>(encoded));
}

}  // namespace

bool TopPetWebSocketClient::connectTo(
    const char *host, uint16_t port, const char *path) {
  close();
  if (!client_.connect(host, port)) {
    return false;
  }
  client_.setTimeout(kHandshakeTimeoutMs / 1000);

  const String key = makeClientKey();
  if (key.isEmpty()) {
    close();
    return false;
  }
  client_.printf("GET %s HTTP/1.1\r\n", path);
  client_.printf("Host: %s:%u\r\n", host, port);
  client_.print("Upgrade: websocket\r\n");
  client_.print("Connection: Upgrade\r\n");
  client_.printf("Sec-WebSocket-Key: %s\r\n", key.c_str());
  client_.print("Sec-WebSocket-Version: 13\r\n\r\n");
  client_.flush();
  return waitForHandshake(kHandshakeTimeoutMs);
}

void TopPetWebSocketClient::close() {
  if (client_.connected()) {
    sendFrame(0x8, nullptr, 0);
  }
  client_.stop();
}

bool TopPetWebSocketClient::connected() {
  return client_.connected();
}

bool TopPetWebSocketClient::sendText(const String &text) {
  return sendFrame(0x1, reinterpret_cast<const uint8_t *>(text.c_str()), text.length());
}

bool TopPetWebSocketClient::sendBinary(const uint8_t *data, size_t length) {
  return sendFrame(0x2, data, length);
}

bool TopPetWebSocketClient::sendPong(const uint8_t *data, size_t length) {
  return sendFrame(0xA, data, length);
}

bool TopPetWebSocketClient::sendFrame(
    uint8_t opcode, const uint8_t *data, size_t length) {
  if (!client_.connected() || length > 0x7fffffffUL) {
    return false;
  }

  uint8_t header[14] = {};
  size_t header_length = 0;
  header[header_length++] = static_cast<uint8_t>(0x80 | (opcode & 0x0f));
  if (length <= 125) {
    header[header_length++] = static_cast<uint8_t>(0x80 | length);
  } else if (length <= 0xffff) {
    header[header_length++] = 0x80 | 126;
    header[header_length++] = static_cast<uint8_t>(length >> 8);
    header[header_length++] = static_cast<uint8_t>(length);
  } else {
    header[header_length++] = 0x80 | 127;
    for (int8_t shift = 7; shift >= 0; --shift) {
      header[header_length++] = static_cast<uint8_t>(length >> (shift * 8));
    }
  }

  uint8_t mask[4];
  for (uint8_t index = 0; index < sizeof(mask); ++index) {
    mask[index] = static_cast<uint8_t>(esp_random());
    header[header_length++] = mask[index];
  }
  if (client_.write(header, header_length) != header_length) {
    return false;
  }

  uint8_t masked_block[512];
  size_t offset = 0;
  while (offset < length) {
    const size_t block_length =
        length - offset < sizeof(masked_block)
            ? length - offset
            : sizeof(masked_block);
    for (size_t index = 0; index < block_length; ++index) {
      masked_block[index] =
          (data == nullptr ? 0 : data[offset + index]) ^
          mask[(offset + index) & 3];
    }
    if (client_.write(masked_block, block_length) != block_length) {
      return false;
    }
    offset += block_length;
  }
  return true;
}

bool TopPetWebSocketClient::readByte(uint8_t &value, uint32_t timeout_ms) {
  const uint32_t deadline = millis() + timeout_ms;
  while (!client_.available()) {
    if (!client_.connected() || static_cast<int32_t>(millis() - deadline) >= 0) {
      return false;
    }
    delay(1);
  }
  value = static_cast<uint8_t>(client_.read());
  return true;
}

bool TopPetWebSocketClient::readExact(
    uint8_t *data, size_t length, uint32_t timeout_ms) {
  size_t offset = 0;
  const uint32_t deadline = millis() + timeout_ms;
  while (offset < length) {
    const int available = client_.available();
    if (available <= 0) {
      if (!client_.connected() ||
          static_cast<int32_t>(millis() - deadline) >= 0) {
        return false;
      }
      delay(1);
      continue;
    }
    const size_t wanted =
        static_cast<size_t>(available) < length - offset
            ? static_cast<size_t>(available)
            : length - offset;
    const int received = client_.read(data + offset, wanted);
    if (received <= 0) {
      return false;
    }
    offset += static_cast<size_t>(received);
  }
  return true;
}

bool TopPetWebSocketClient::waitForHandshake(uint32_t timeout_ms) {
  String response;
  const uint32_t deadline = millis() + timeout_ms;
  while (response.indexOf("\r\n\r\n") < 0) {
    if (!client_.connected() || static_cast<int32_t>(millis() - deadline) >= 0) {
      return false;
    }
    while (client_.available()) {
      response += static_cast<char>(client_.read());
      if (response.length() > 2048) {
        return false;
      }
    }
    delay(1);
  }
  return response.startsWith("HTTP/1.1 101") || response.startsWith("HTTP/1.0 101");
}

bool TopPetWebSocketClient::readFrame(
    uint8_t *payload, size_t capacity, size_t &length, uint8_t &opcode,
    uint32_t timeout_ms) {
  length = 0;
  opcode = 0;
  uint8_t first = 0;
  uint8_t second = 0;
  if (!readByte(first, timeout_ms) || !readByte(second, 250)) {
    return false;
  }
  opcode = first & 0x0f;
  uint64_t frame_length = second & 0x7f;
  if (frame_length == 126) {
    uint8_t extended[2];
    if (!readExact(extended, sizeof(extended), 250)) {
      return false;
    }
    frame_length = (static_cast<uint16_t>(extended[0]) << 8) | extended[1];
  } else if (frame_length == 127) {
    uint8_t extended[8];
    if (!readExact(extended, sizeof(extended), 250) || extended[0] != 0) {
      return false;
    }
    frame_length = 0;
    for (uint8_t index = 0; index < sizeof(extended); ++index) {
      frame_length = (frame_length << 8) | extended[index];
    }
  }

  const bool masked = (second & 0x80) != 0;
  uint8_t mask[4] = {};
  if (masked && !readExact(mask, sizeof(mask), 250)) {
    return false;
  }

  uint8_t block[512];
  uint64_t offset = 0;
  while (offset < frame_length) {
    const size_t block_length =
        frame_length - offset < sizeof(block)
            ? static_cast<size_t>(frame_length - offset)
            : sizeof(block);
    // Once a frame header has arrived, TCP may still split its payload across
    // several packets. The short caller timeout is only for polling a new
    // frame header; allow the in-progress payload to finish without losing
    // WebSocket framing.
    if (!readExact(block, block_length, 2000)) {
      return false;
    }
    for (size_t index = 0; index < block_length; ++index) {
      uint8_t value = block[index];
      if (masked) value ^= mask[(offset + index) & 3];
      if (offset + index < capacity && payload != nullptr) {
        payload[offset + index] = value;
        ++length;
      }
    }
    offset += block_length;
  }
  return true;
}
