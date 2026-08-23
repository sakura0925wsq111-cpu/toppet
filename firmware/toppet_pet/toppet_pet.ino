#include <Arduino.h>
#include <Arduino_GFX_Library.h>
#include <ESP_I2S.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <Wire.h>
#include <WiFi.h>
#include <WebServer.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/queue.h>
#include <freertos/stream_buffer.h>
#include <math.h>
#include <mbedtls/base64.h>

#include "es7210_min.h"
#include "es8311.h"
#include "expression_manifest.h"
#include "net_config.h"
#include "pin_config.h"
#include "toppet_websocket.h"

constexpr uint16_t PET_X = 30;
constexpr uint16_t PET_Y = 140;
constexpr uint16_t PET_WIDTH = 420;
constexpr uint16_t PET_HEIGHT = 340;
constexpr uint16_t TEXT_MASK_HEIGHT = 140;
constexpr uint32_t TEXT_MASK_BYTES = LCD_WIDTH * TEXT_MASK_HEIGHT / 8;
constexpr uint32_t BUBBLE_BUFFER_PIXELS = LCD_WIDTH * TEXT_MASK_HEIGHT;
constexpr size_t SUBTITLE_ENCODED_BUFFER_BYTES = TEXT_MASK_BYTES * 2;
constexpr size_t NETWORK_RESPONSE_BUFFER_BYTES = 32 * 1024;
constexpr uint32_t EXPRESSION_HOLD_MS = 4200;
constexpr uint32_t RLE_BUFFER_BYTES = 1536;
constexpr uint32_t BUTTON_DEBOUNCE_MS = 30;
constexpr uint32_t BUTTON_THINKING_PREVIEW_MS = 1500;
constexpr uint32_t AUDIO_SAMPLE_RATE = 16000;
constexpr uint32_t AUDIO_MAX_RECORDING_MS = 10000;
constexpr uint32_t AUDIO_RECORDING_IO_TIMEOUT_MS = 30;
constexpr uint32_t AUDIO_PLAYBACK_IO_TIMEOUT_MS = 250;
constexpr uint32_t HOST_FRAME_POLL_MS = 20;
constexpr uint32_t AUDIO_MAX_RECORDING_BYTES =
    AUDIO_SAMPLE_RATE * AUDIO_MAX_RECORDING_MS / 1000 * sizeof(int16_t);
// One exact 20 ms stereo DMA block. Capture downmixes its 1280 input bytes to
// one 640-byte mono protocol frame.
constexpr size_t AUDIO_READ_BUFFER_BYTES = 1280;
constexpr size_t PCM_FRAME_BYTES = 640;
constexpr size_t PCM_FRAME_SAMPLES = PCM_FRAME_BYTES / sizeof(int16_t);
constexpr size_t PLAYBACK_RING_BYTES = 16 * 1024;
constexpr size_t PLAYBACK_PREBUFFER_BYTES = 5120;  // 160 ms
constexpr size_t PLAYBACK_LOW_WATER_BYTES = 2560;  // 80 ms
constexpr size_t PLAYBACK_HIGH_WATER_BYTES = 16000;  // 500 ms
constexpr uint32_t HOST_RECONNECT_MS = 1000;
constexpr uint32_t HOST_PING_MS = 5000;

constexpr uint16_t COLOR_BLACK = 0x0000;
constexpr uint16_t COLOR_WHITE = 0xFFFF;
constexpr uint16_t COLOR_BUBBLE_BORDER = 0x8410;

struct __attribute__((packed)) PetAssetHeader {
  char magic[4];
  uint16_t version;
  uint16_t width;
  uint16_t height;
  uint16_t frame_count;
  uint32_t palette_offset;
  uint32_t frame_table_offset;
  uint32_t text_mask_offset;
  uint32_t text_mask_size;
};

struct __attribute__((packed)) PetFrameEntry {
  uint32_t offset;
  uint32_t size;
  uint16_t duration_ms;
  uint16_t reserved;
};

enum class ButtonTestState : uint8_t {
  IDLE,
  LISTENING,
  THINKING,
};

Arduino_DataBus *bus = new Arduino_ESP32QSPI(
    LCD_CS, LCD_SCLK, LCD_SDIO0, LCD_SDIO1, LCD_SDIO2, LCD_SDIO3);

Arduino_CO5300 *gfx = new Arduino_CO5300(
    bus, LCD_RESET, 0, LCD_WIDTH, LCD_HEIGHT, 0, 0, 0, 0);

static uint16_t *frame_buffer = nullptr;
static uint16_t *bubble_buffer = nullptr;
static uint8_t *text_mask = nullptr;
static uint8_t *subtitle_encoded_buffer = nullptr;
static uint8_t *network_response_buffer = nullptr;
static uint8_t *rle_buffer = nullptr;
static uint16_t palette[256];
static PetAssetHeader asset_header;
static PetFrameEntry frame_entries[8];
static File asset_file;
static uint8_t expression_index = 0;
static uint8_t animation_frame = 0;
static uint32_t expression_started_at = 0;
static bool raw_button_pressed = false;
static bool user_button_pressed = false;
static uint32_t raw_button_changed_at = 0;
static ButtonTestState button_test_state = ButtonTestState::IDLE;
static uint32_t button_thinking_started_at = 0;
static int16_t *recording_buffer = nullptr;
static uint32_t recording_bytes = 0;
static bool audio_ready = false;
static bool network_ready = false;
static bool provisioning_active = false;
static Preferences network_preferences;
static WebServer provisioning_server(80);
static String network_ssid;
static String network_password;
static String network_host;
static uint16_t network_host_port = TOPPET_HOST_PORT;
static I2SClass i2s(ES7210_I2S_PORT);
static es8311_handle_t es8311_device = nullptr;
static int16_t audio_read_buffer[AUDIO_READ_BUFFER_BYTES / sizeof(int16_t)];
static int16_t playback_stereo_buffer[1024];

struct PlaybackWavState {
  uint8_t header[44];
  size_t header_bytes = 0;
  bool ready = false;
  bool supported = false;
  uint16_t channels = 0;
  uint32_t sample_rate = 0;
  uint16_t bits_per_sample = 0;
};

static PlaybackWavState playback_wav;
static bool playback_active = false;

enum class CaptureCommandType : uint8_t { START, STOP };
enum class NetworkOutboundType : uint8_t {
  ABORT,
  LISTEN_START,
  MIC_FRAME,
  LISTEN_STOP,
};
enum class PlaybackCommandType : uint8_t { START, END, ABORT };
enum class UiCommandType : uint8_t {
  LISTENING,
  THINKING,
  EMOTION,
  SUBTITLE,
  IDLE,
  CLEAR,
};

struct CaptureCommand {
  CaptureCommandType type;
  char session_id[48];
};

struct NetworkOutbound {
  NetworkOutboundType type;
  char session_id[48];
  uint32_t sequence;
  uint16_t length;
  uint8_t pcm[PCM_FRAME_BYTES];
};

struct PlaybackCommand {
  PlaybackCommandType type;
  char session_id[48];
};

struct PlaybackMarker {
  char session_id[48];
  uint32_t sequence;
  uint32_t stream_offset;
};

struct UiCommand {
  UiCommandType type;
  char session_id[48];
  char emotion[24];
  uint32_t activate_sequence;
  char *json;
};

constexpr EventBits_t EVENT_PLAYBACK_READY = BIT0;
constexpr EventBits_t EVENT_PLAYBACK_ACTIVE = BIT1;
constexpr EventBits_t EVENT_PLAYBACK_LOW_WATER = BIT2;
constexpr EventBits_t EVENT_PLAYBACK_ABORT = BIT3;

static QueueHandle_t capture_command_queue = nullptr;
static QueueHandle_t network_outbound_queue = nullptr;
static QueueHandle_t playback_command_queue = nullptr;
static QueueHandle_t playback_marker_queue = nullptr;
static QueueHandle_t scheduled_ui_queue = nullptr;
static QueueHandle_t ui_command_queue = nullptr;
static StreamBufferHandle_t playback_stream = nullptr;
static EventGroupHandle_t voice_events = nullptr;
static StaticQueue_t network_outbound_queue_control;
static uint8_t *network_outbound_queue_storage = nullptr;
static char ui_active_session[48] = {};
static uint32_t session_counter = 0;
static bool serial_test_pressed = false;
static String serial_test_command;

bool loadExpression(uint8_t index);
uint8_t expressionIndexFor(const char *name);
bool renderCurrentFrame();
void setButtonTestState(ButtonTestState next_state);
bool initializeAudioInput();
void loadNetworkSettings();
bool initializeNetwork();
void startProvisioningPortal();
void serviceProvisioningPortal();
void handleProvisioningRoot();
void handleProvisioningSave();
void serviceRecording();
void finishRecording();
void uploadRecording();
void setHostEmotion(const char *emotion);
void resetPlaybackWav();
void processPlaybackAudio(const uint8_t *data, size_t length);
void finishPlaybackAudio();
bool serviceResponseAnimation();
bool drawSpeechBubble();
bool applyHostSubtitle(const char *json);
bool extractJsonStringField(
    const char *json, const char *field_name, String &value);
bool extractJsonUIntField(
    const char *json, const char *field_name, uint32_t &value);
void audioCaptureTask(void *parameter);
void networkTask(void *parameter);
void audioPlaybackTask(void *parameter);
void serviceUiCommands();
void createVoiceTasks();
void inputTask(void *parameter);
void uiTask(void *parameter);
void enqueueUiCommand(
    UiCommandType type, const char *session_id,
    const char *emotion = nullptr, char *json = nullptr,
    uint32_t activate_sequence = 0, QueueHandle_t destination = nullptr);
void beginVoiceCapture(const char *source, bool capture_hardware = true);
void endVoiceCapture(
    const char *source, bool capture_hardware = true,
    uint32_t injected_sequence = 0);
void serviceSerialTestControl();
void logLatency(
    const char *node, const char *session_id, uint32_t duration_ms = 0,
    int32_t sequence = -1, int32_t buffer_bytes = -1);

void showFatalError(const char *message) {
  Serial.println(message);
  gfx->fillScreen(COLOR_BLACK);
  gfx->setCursor(16, LCD_HEIGHT / 2 - 12);
  gfx->setTextSize(2);
  gfx->setTextColor(RGB565_RED);
  gfx->println(message);
  while (true) {
    delay(1000);
  }
}

void fillBubbleHorizontal(
    int16_t x0, int16_t x1, int16_t y, uint16_t color) {
  if (y < 0 || y >= TEXT_MASK_HEIGHT || x1 < 0 || x0 >= LCD_WIDTH) return;
  if (x0 < 0) x0 = 0;
  if (x1 >= LCD_WIDTH) x1 = LCD_WIDTH - 1;
  uint16_t *destination =
      bubble_buffer + static_cast<uint32_t>(y) * LCD_WIDTH + x0;
  for (int16_t x = x0; x <= x1; ++x) *destination++ = color;
}

void fillBubbleRoundedRectangle(
    int16_t x, int16_t y, int16_t width, int16_t height, int16_t radius,
    uint16_t color) {
  const int32_t radius_squared = radius * radius;
  for (int16_t row = 0; row < height; ++row) {
    int16_t inset = 0;
    if (row < radius || row >= height - radius) {
      const int16_t dy = row < radius
                             ? radius - row - 1
                             : row - (height - radius);
      while (inset < radius &&
             (radius - inset) * (radius - inset) + dy * dy >
                 radius_squared) {
        ++inset;
      }
    }
    fillBubbleHorizontal(x + inset, x + width - inset - 1, y + row, color);
  }
}

void fillBubbleTriangle(
    int16_t left_x, int16_t right_x, int16_t top_y,
    int16_t tip_x, int16_t tip_y, uint16_t color) {
  const int16_t height = tip_y - top_y;
  for (int16_t row = 0; row <= height; ++row) {
    const int16_t left = left_x + (tip_x - left_x) * row / height;
    const int16_t right = right_x + (tip_x - right_x) * row / height;
    fillBubbleHorizontal(left, right, top_y + row, color);
  }
}

bool findTextBounds(
    int16_t &left, int16_t &top, int16_t &right, int16_t &bottom) {
  left = LCD_WIDTH;
  top = TEXT_MASK_HEIGHT;
  right = -1;
  bottom = -1;
  for (int16_t y = 0; y < TEXT_MASK_HEIGHT; ++y) {
    for (int16_t x = 0; x < LCD_WIDTH; ++x) {
      const uint32_t pixel = static_cast<uint32_t>(y) * LCD_WIDTH + x;
      if (text_mask[pixel >> 3] & (0x80 >> (pixel & 7))) {
        if (x < left) left = x;
        if (x > right) right = x;
        if (y < top) top = y;
        if (y > bottom) bottom = y;
      }
    }
  }
  return right >= left && bottom >= top;
}

bool drawSpeechBubble() {
  memset(bubble_buffer, 0, BUBBLE_BUFFER_PIXELS * sizeof(uint16_t));
  int16_t text_left = 0;
  int16_t text_top = 0;
  int16_t text_right = 0;
  int16_t text_bottom = 0;
  if (!findTextBounds(text_left, text_top, text_right, text_bottom)) {
    gfx->fillRect(0, 0, LCD_WIDTH, TEXT_MASK_HEIGHT, COLOR_BLACK);
    return false;
  }

  const int16_t padding_x = 17;
  const int16_t padding_y = 11;
  int16_t bubble_x = text_left - padding_x;
  int16_t bubble_y = text_top - padding_y;
  int16_t bubble_right = text_right + padding_x + 1;
  int16_t bubble_bottom = text_bottom + padding_y + 1;
  if (bubble_x < 12) bubble_x = 12;
  if (bubble_y < 12) bubble_y = 12;
  if (bubble_right > LCD_WIDTH - 12) bubble_right = LCD_WIDTH - 12;
  if (bubble_bottom > TEXT_MASK_HEIGHT - 24) {
    bubble_bottom = TEXT_MASK_HEIGHT - 24;
  }
  const int16_t bubble_width = bubble_right - bubble_x;
  const int16_t bubble_height = bubble_bottom - bubble_y;
  const int16_t tail_x = LCD_WIDTH / 2;

  fillBubbleRoundedRectangle(
      bubble_x, bubble_y, bubble_width, bubble_height, 16,
      COLOR_BUBBLE_BORDER);
  fillBubbleTriangle(
      tail_x - 13, tail_x + 13, bubble_bottom - 2,
      tail_x, bubble_bottom + 20, COLOR_BUBBLE_BORDER);
  fillBubbleRoundedRectangle(
      bubble_x + 2, bubble_y + 2, bubble_width - 4, bubble_height - 4, 14,
      COLOR_WHITE);
  fillBubbleTriangle(
      tail_x - 9, tail_x + 9, bubble_bottom - 3,
      tail_x, bubble_bottom + 15, COLOR_WHITE);

  for (int16_t y = text_top; y <= text_bottom; ++y) {
    for (int16_t x = text_left; x <= text_right; ++x) {
      const uint32_t pixel = static_cast<uint32_t>(y) * LCD_WIDTH + x;
      if (text_mask[pixel >> 3] & (0x80 >> (pixel & 7))) {
        bubble_buffer[pixel] = COLOR_BLACK;
      }
    }
  }
  gfx->draw16bitRGBBitmap(
      0, 0, bubble_buffer, LCD_WIDTH, TEXT_MASK_HEIGHT);
  return true;
}

bool decodeSubtitleRle(const uint8_t *encoded, size_t encoded_length) {
  size_t input = 0;
  size_t output = 0;
  while (input < encoded_length && output < TEXT_MASK_BYTES) {
    const uint8_t header = encoded[input++];
    const size_t count = (header & 0x7f) + 1;
    if (output + count > TEXT_MASK_BYTES) return false;
    if (header & 0x80) {
      memset(text_mask + output, 0, count);
    } else {
      if (input + count > encoded_length) return false;
      memcpy(text_mask + output, encoded + input, count);
      input += count;
    }
    output += count;
  }
  return input == encoded_length && output == TEXT_MASK_BYTES;
}

bool extractJsonStringField(
    const char *json, const char *field_name, String &value) {
  String marker = "\"";
  marker += field_name;
  marker += "\":\"";
  const char *cursor = strstr(json, marker.c_str());
  if (cursor == nullptr) return false;
  cursor += marker.length();
  value = "";
  bool escaped = false;
  while (*cursor != '\0') {
    const char current = *cursor++;
    if (escaped) {
      if (current == 'n' || current == 'r' || current == 't') {
        value += ' ';
      } else {
        value += current;
      }
      escaped = false;
    } else if (current == '\\') {
      escaped = true;
    } else if (current == '"') {
      return true;
    } else {
      value += current;
    }
  }
  return false;
}

bool extractJsonUIntField(
    const char *json, const char *field_name, uint32_t &value) {
  String marker = "\"";
  marker += field_name;
  marker += "\":";
  const char *cursor = strstr(json, marker.c_str());
  if (cursor == nullptr) return false;
  cursor += marker.length();
  while (*cursor == ' ') ++cursor;
  if (*cursor < '0' || *cursor > '9') return false;
  value = 0;
  while (*cursor >= '0' && *cursor <= '9') {
    value = value * 10 + static_cast<uint32_t>(*cursor - '0');
    ++cursor;
  }
  return true;
}

void logLatency(
    const char *node, const char *session_id, uint32_t duration_ms,
    int32_t sequence, int32_t buffer_bytes) {
  Serial.printf(
      "[LATENCY] node=%s session_id=%s device_id=%s monotonic_ms=%llu duration_ms=%lu sequence=%ld buffer_bytes=%ld\n",
      node, session_id == nullptr ? "" : session_id, TOPPET_DEVICE_ID,
      static_cast<unsigned long long>(esp_timer_get_time() / 1000ULL),
      static_cast<unsigned long>(duration_ms), static_cast<long>(sequence),
      static_cast<long>(buffer_bytes));
}

bool applyHostSubtitle(const char *json) {
  String base64_data;
  if (!extractJsonStringField(json, "data", base64_data)) return false;
  size_t decoded_length = 0;
  if (mbedtls_base64_decode(
          subtitle_encoded_buffer, SUBTITLE_ENCODED_BUFFER_BYTES,
          &decoded_length,
          reinterpret_cast<const uint8_t *>(base64_data.c_str()),
          base64_data.length()) != 0 ||
      !decodeSubtitleRle(subtitle_encoded_buffer, decoded_length)) {
    return false;
  }
  return drawSpeechBubble();
}

bool updateUserButton() {
  const bool sampled_pressed =
      digitalRead(USER_BUTTON_PIN) == USER_BUTTON_ACTIVE;
  const uint32_t now = millis();

  if (sampled_pressed != raw_button_pressed) {
    raw_button_pressed = sampled_pressed;
    raw_button_changed_at = now;
  }

  if (sampled_pressed == user_button_pressed ||
      now - raw_button_changed_at < BUTTON_DEBOUNCE_MS) {
    return false;
  }

  user_button_pressed = sampled_pressed;
  if (user_button_pressed) {
    beginVoiceCapture("GPIO18");
  } else {
    endVoiceCapture("GPIO18");
  }
  return true;
}

void beginVoiceCapture(const char *source, bool capture_hardware) {
    if (ui_active_session[0] != '\0') {
      NetworkOutbound abort_message = {};
      abort_message.type = NetworkOutboundType::ABORT;
      strlcpy(
          abort_message.session_id, ui_active_session,
          sizeof(abort_message.session_id));
      xQueueSend(network_outbound_queue, &abort_message, 0);
      PlaybackCommand playback_abort = {};
      playback_abort.type = PlaybackCommandType::ABORT;
      strlcpy(
          playback_abort.session_id, ui_active_session,
          sizeof(playback_abort.session_id));
      xQueueSend(playback_command_queue, &playback_abort, 0);
      xEventGroupSetBits(voice_events, EVENT_PLAYBACK_ABORT);
    }
    const uint64_t chip_id = ESP.getEfuseMac();
    snprintf(
        ui_active_session, sizeof(ui_active_session), "%08lx-%08lx-%08lx",
        static_cast<unsigned long>(chip_id >> 32),
        static_cast<unsigned long>(millis()),
        static_cast<unsigned long>(++session_counter));
    logLatency("button_pressed", ui_active_session);
    if (capture_hardware) {
      CaptureCommand command = {};
      command.type = CaptureCommandType::START;
      strlcpy(command.session_id, ui_active_session, sizeof(command.session_id));
      if (xQueueSend(capture_command_queue, &command, pdMS_TO_TICKS(20)) != pdTRUE) {
        Serial.println("[AUDIO] capture start queue full");
      }
    } else {
      NetworkOutbound start = {};
      start.type = NetworkOutboundType::LISTEN_START;
      strlcpy(start.session_id, ui_active_session, sizeof(start.session_id));
      xQueueSend(network_outbound_queue, &start, pdMS_TO_TICKS(20));
    }
    enqueueUiCommand(
        UiCommandType::LISTENING, ui_active_session, nullptr, nullptr, 0,
        nullptr);
    Serial.printf("[VOICE] source=%s pressed session=%s\n", source, ui_active_session);
}

void endVoiceCapture(
    const char *source, bool capture_hardware,
    uint32_t injected_sequence) {
    if (ui_active_session[0] == '\0') return;
    logLatency("button_released", ui_active_session);
    if (capture_hardware) {
      CaptureCommand command = {};
      command.type = CaptureCommandType::STOP;
      strlcpy(command.session_id, ui_active_session, sizeof(command.session_id));
      if (xQueueSend(capture_command_queue, &command, pdMS_TO_TICKS(20)) != pdTRUE) {
        Serial.println("[AUDIO] capture stop queue full");
      }
    } else {
      NetworkOutbound stop = {};
      stop.type = NetworkOutboundType::LISTEN_STOP;
      strlcpy(stop.session_id, ui_active_session, sizeof(stop.session_id));
      stop.sequence = injected_sequence;
      xQueueSend(network_outbound_queue, &stop, pdMS_TO_TICKS(20));
    }
    enqueueUiCommand(
        UiCommandType::THINKING, ui_active_session, nullptr, nullptr, 0,
        nullptr);
    Serial.printf("[VOICE] source=%s released session=%s\n", source, ui_active_session);
}

void serviceSerialTestControl() {
  while (Serial.available()) {
    const char value = static_cast<char>(Serial.read());
    if (value == '\r') continue;
    if (value != '\n') {
      if (serial_test_command.length() < 32) serial_test_command += value;
      continue;
    }
    serial_test_command.trim();
    if (serial_test_command.startsWith("TEST_PCM ") && !serial_test_pressed) {
      const uint32_t requested_bytes =
          static_cast<uint32_t>(serial_test_command.substring(9).toInt());
      if (requested_bytes == 0 || requested_bytes > AUDIO_MAX_RECORDING_BYTES) {
        Serial.println("[TEST] INVALID TEST_PCM LENGTH");
        serial_test_command = "";
        continue;
      }
      serial_test_pressed = true;
      beginVoiceCapture("USB_PCM", false);
      uint32_t remaining = requested_bytes;
      uint32_t sequence = 0;
      TickType_t frame_wake = xTaskGetTickCount();
      while (remaining > 0) {
        NetworkOutbound frame = {};
        frame.type = NetworkOutboundType::MIC_FRAME;
        strlcpy(frame.session_id, ui_active_session, sizeof(frame.session_id));
        frame.sequence = sequence++;
        frame.length = PCM_FRAME_BYTES;
        const size_t wanted =
            remaining < PCM_FRAME_BYTES ? remaining : PCM_FRAME_BYTES;
        size_t received = 0;
        const uint32_t deadline = millis() + 1000;
        while (received < wanted &&
               static_cast<int32_t>(millis() - deadline) < 0) {
          const int available = Serial.available();
          if (available <= 0) {
            vTaskDelay(1);
            continue;
          }
          const size_t count =
              static_cast<size_t>(available) < wanted - received
                  ? static_cast<size_t>(available)
                  : wanted - received;
          received += Serial.readBytes(
              reinterpret_cast<char *>(frame.pcm + received), count);
        }
        if (received != wanted) {
          Serial.println("[TEST] TEST_PCM TIMEOUT");
          break;
        }
        if (wanted < PCM_FRAME_BYTES) {
          memset(frame.pcm + wanted, 0, PCM_FRAME_BYTES - wanted);
        }
        xQueueSend(network_outbound_queue, &frame, pdMS_TO_TICKS(20));
        remaining -= wanted;
        vTaskDelayUntil(&frame_wake, pdMS_TO_TICKS(20));
      }
      serial_test_pressed = false;
      endVoiceCapture("USB_PCM", false, sequence);
      Serial.printf("[TEST] ACK TEST_PCM bytes=%lu frames=%lu\n",
                    static_cast<unsigned long>(requested_bytes),
                    static_cast<unsigned long>(sequence));
    } else if (serial_test_command == "TEST_PRESS" && !serial_test_pressed) {
      serial_test_pressed = true;
      beginVoiceCapture("USB_TEST");
      Serial.println("[TEST] ACK TEST_PRESS");
    } else if (serial_test_command == "TEST_RELEASE" && serial_test_pressed) {
      serial_test_pressed = false;
      endVoiceCapture("USB_TEST");
      Serial.println("[TEST] ACK TEST_RELEASE");
    } else if (serial_test_command == "TEST_ABORT") {
      if (serial_test_pressed) {
        serial_test_pressed = false;
        endVoiceCapture("USB_TEST");
      }
      if (ui_active_session[0] != '\0') {
        NetworkOutbound abort_message = {};
        abort_message.type = NetworkOutboundType::ABORT;
        strlcpy(abort_message.session_id, ui_active_session,
                sizeof(abort_message.session_id));
        xQueueSend(network_outbound_queue, &abort_message, 0);
        PlaybackCommand playback_abort = {};
        playback_abort.type = PlaybackCommandType::ABORT;
        strlcpy(playback_abort.session_id, ui_active_session,
                sizeof(playback_abort.session_id));
        xQueueSend(playback_command_queue, &playback_abort, 0);
        xEventGroupSetBits(voice_events, EVENT_PLAYBACK_ABORT);
      }
      Serial.println("[TEST] ACK TEST_ABORT");
    } else if (!serial_test_command.isEmpty()) {
      Serial.printf("[TEST] UNKNOWN %s\n", serial_test_command.c_str());
    }
    serial_test_command = "";
  }
}

void loadNetworkSettings() {
  network_preferences.begin("toppet_net", false);
  network_ssid = network_preferences.getString("ssid", TOPPET_WIFI_SSID);
  network_password = network_preferences.getString("password", TOPPET_WIFI_PASSWORD);
  network_host = network_preferences.getString("host", TOPPET_HOST);
  network_host_port = network_preferences.getUShort("port", TOPPET_HOST_PORT);
  if (network_host_port == 0) {
    network_host_port = TOPPET_HOST_PORT;
  }
}

void handleProvisioningRoot() {
  const char html[] = R"HTML(
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TopPet 配网</title></head><body><h2>TopPet 网络配置</h2>
<form method="post" action="/save">
<p>Wi-Fi 名称<br><input name="ssid" required></p>
<p>Wi-Fi 密码<br><input name="password" type="password"></p>
<p>电脑 IP 或主机名<br><input name="host" required></p>
<p>主机端口<br><input name="port" value="8765" inputmode="numeric"></p>
<button type="submit">保存并重启</button></form>
<p>设备会连接 2.4GHz Wi-Fi。电脑端先启动 TopPet 主机服务。</p>
</body></html>)HTML";
  provisioning_server.send(200, "text/html; charset=utf-8", html);
}

void handleProvisioningSave() {
  const String new_ssid = provisioning_server.arg("ssid");
  const String new_password = provisioning_server.arg("password");
  const String new_host = provisioning_server.arg("host");
  const long new_port = provisioning_server.arg("port").toInt();
  if (new_ssid.isEmpty() || new_host.isEmpty() || new_port < 1 || new_port > 65535) {
    provisioning_server.send(400, "text/plain; charset=utf-8",
                             "SSID、电脑地址和端口无效");
    return;
  }

  network_preferences.putString("ssid", new_ssid);
  if (!new_password.isEmpty()) {
    network_preferences.putString("password", new_password);
  }
  network_preferences.putString("host", new_host);
  network_preferences.putUShort("port", static_cast<uint16_t>(new_port));
  provisioning_server.send(
      200, "text/html; charset=utf-8",
      "<html><meta charset='utf-8'><h2>已保存</h2><p>设备正在重启，请重新连接它的网络。</p></html>");
  delay(600);
  ESP.restart();
}

void startProvisioningPortal() {
  if (provisioning_active) {
    return;
  }
  WiFi.mode(WIFI_AP);
  const String ap_name =
      String("TopPet-Setup-") + String(static_cast<uint32_t>(ESP.getEfuseMac()), HEX);
  WiFi.softAP(ap_name.c_str(), "TopPetSetup");
  provisioning_server.on("/", HTTP_GET, handleProvisioningRoot);
  provisioning_server.on("/save", HTTP_POST, handleProvisioningSave);
  provisioning_server.begin();
  provisioning_active = true;
  Serial.printf("[NET] provisioning AP=%s password=TopPetSetup ip=%s\n",
                ap_name.c_str(), WiFi.softAPIP().toString().c_str());
}

void serviceProvisioningPortal() {
  if (provisioning_active) {
    provisioning_server.handleClient();
  }
}

bool initializeNetwork() {
  loadNetworkSettings();
  if (network_ssid.startsWith("YOUR_") || network_host.startsWith("YOUR_")) {
    Serial.println("[NET] no saved network; starting provisioning portal");
    startProvisioningPortal();
    return false;
  }

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(network_ssid.c_str(), network_password.c_str());
  Serial.printf("[NET] connecting to %s", network_ssid.c_str());
  const uint32_t deadline = millis() + TOPPET_WIFI_CONNECT_TIMEOUT_MS;
  while (WiFi.status() != WL_CONNECTED &&
         static_cast<int32_t>(millis() - deadline) < 0) {
    Serial.print(".");
    delay(250);
  }
  Serial.println();
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("[NET] Wi-Fi connect failed status=%d\n", WiFi.status());
    startProvisioningPortal();
    return false;
  }

  network_preferences.putString("ssid", network_ssid);
  network_preferences.putString("password", network_password);
  network_preferences.putString("host", network_host);
  network_preferences.putUShort("port", network_host_port);
  Serial.printf("[NET] Wi-Fi ready ip=%s rssi=%d host=%s:%u\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI(),
                network_host.c_str(), network_host_port);
  return true;
}

uint16_t readLittleEndian16(const uint8_t *data) {
  return static_cast<uint16_t>(data[0]) |
         (static_cast<uint16_t>(data[1]) << 8);
}

uint32_t readLittleEndian32(const uint8_t *data) {
  return static_cast<uint32_t>(data[0]) |
         (static_cast<uint32_t>(data[1]) << 8) |
         (static_cast<uint32_t>(data[2]) << 16) |
         (static_cast<uint32_t>(data[3]) << 24);
}

void resetPlaybackWav() {
  playback_wav = PlaybackWavState();
}

void processPlaybackAudio(const uint8_t *data, size_t length) {
  if (!playback_wav.ready) {
    const size_t header_remaining = sizeof(playback_wav.header) - playback_wav.header_bytes;
    const size_t header_copy = length < header_remaining ? length : header_remaining;
    memcpy(playback_wav.header + playback_wav.header_bytes, data, header_copy);
    playback_wav.header_bytes += header_copy;
    data += header_copy;
    length -= header_copy;
    if (playback_wav.header_bytes < sizeof(playback_wav.header)) {
      return;
    }

    playback_wav.channels = readLittleEndian16(playback_wav.header + 22);
    playback_wav.sample_rate = readLittleEndian32(playback_wav.header + 24);
    playback_wav.bits_per_sample = readLittleEndian16(playback_wav.header + 34);
    playback_wav.ready = true;
    playback_wav.supported =
        memcmp(playback_wav.header, "RIFF", 4) == 0 &&
        memcmp(playback_wav.header + 8, "WAVE", 4) == 0 &&
        readLittleEndian16(playback_wav.header + 20) == 1 &&
        (playback_wav.channels == 1 || playback_wav.channels == 2) &&
        playback_wav.sample_rate == AUDIO_SAMPLE_RATE &&
        playback_wav.bits_per_sample == 16;
    Serial.printf("[AUDIO] WAV reply rate=%lu channels=%u bits=%u %s\n",
                  static_cast<unsigned long>(playback_wav.sample_rate),
                  playback_wav.channels, playback_wav.bits_per_sample,
                  playback_wav.supported ? "supported" : "unsupported");
    if (!playback_wav.supported) {
      return;
    }
  }
  if (!playback_wav.supported || length == 0) {
    return;
  }

  if (playback_wav.channels == 2) {
    const size_t stereo_bytes = length - (length % (sizeof(int16_t) * 2));
    if (i2s.write(data, stereo_bytes) != stereo_bytes) {
      Serial.println("[AUDIO] ES8311 stereo write failed");
    }
    return;
  }

  const size_t sample_count = length / sizeof(int16_t);
  size_t offset = 0;
  while (offset < sample_count) {
    const size_t chunk_samples =
        (sample_count - offset) < (sizeof(playback_stereo_buffer) / sizeof(int16_t) / 2)
            ? (sample_count - offset)
            : (sizeof(playback_stereo_buffer) / sizeof(int16_t) / 2);
    for (size_t index = 0; index < chunk_samples; ++index) {
      const int16_t sample = reinterpret_cast<const int16_t *>(data)[offset + index];
      playback_stereo_buffer[index * 2] = sample;
      playback_stereo_buffer[index * 2 + 1] = sample;
    }
    const size_t stereo_bytes = chunk_samples * sizeof(int16_t) * 2;
    if (i2s.write(
            reinterpret_cast<const uint8_t *>(playback_stereo_buffer),
            stereo_bytes) != stereo_bytes) {
      Serial.println("[AUDIO] ES8311 mono write failed");
      return;
    }
    offset += chunk_samples;
  }
}

void finishPlaybackAudio() {
  // A WebSocket frame may return as soon as its samples are queued. Give the
  // final DMA block enough time to reach the codec before muting the DAC.
  if (playback_active) {
    delay(80);
  }
  if (es8311_device != nullptr) {
    es8311_voice_mute(es8311_device, true);
  }
  if (playback_wav.ready && playback_wav.supported) {
    Serial.println("[AUDIO] ES8311 reply playback finished");
  }
  playback_active = false;
  i2s.setTimeout(AUDIO_RECORDING_IO_TIMEOUT_MS);
}

void setHostEmotion(const char *emotion) {
  if (emotion == nullptr || emotion[0] == '\0') {
    return;
  }
  const uint8_t index = expressionIndexFor(emotion);
  if (!loadExpression(index) || !renderCurrentFrame()) {
    Serial.printf("[PET] cannot show host emotion=%s\n", emotion);
    return;
  }
  Serial.printf("[PET] host emotion=%s\n", emotion);
}

void uploadRecording() {
  if (recording_bytes == 0) {
    return;
  }
  if (WiFi.status() != WL_CONNECTED) {
    network_ready = initializeNetwork();
  }
  if (!network_ready || WiFi.status() != WL_CONNECTED) {
    Serial.println("[NET] recording kept locally; host link unavailable");
    return;
  }

  TopPetWebSocketClient websocket;
  if (!websocket.connectTo(network_host.c_str(), network_host_port, "/ws")) {
    Serial.println("[NET] WebSocket handshake failed");
    return;
  }

  const String session_id = String(static_cast<uint32_t>(millis()), HEX);
  String hello = "{\"type\":\"hello\",\"device_id\":\"";
  hello += TOPPET_DEVICE_ID;
  hello += "\"}";
  String audio_start = "{\"type\":\"audio_start\",\"session_id\":\"";
  audio_start += session_id;
  audio_start += "\",\"sample_rate\":16000,\"channels\":1,\"bits_per_sample\":16,\"content_type\":\"application/octet-stream\"}";

  if (!websocket.sendText(hello) || !websocket.sendText(audio_start)) {
    Serial.println("[NET] WebSocket metadata send failed");
    websocket.close();
    return;
  }

  constexpr size_t NETWORK_AUDIO_CHUNK_BYTES = 2048;
  for (uint32_t offset = 0; offset < recording_bytes;
       offset += NETWORK_AUDIO_CHUNK_BYTES) {
    const uint32_t remaining = recording_bytes - offset;
    const size_t chunk_size = remaining < NETWORK_AUDIO_CHUNK_BYTES
                                  ? remaining
                                  : NETWORK_AUDIO_CHUNK_BYTES;
    if (!websocket.sendBinary(
            reinterpret_cast<const uint8_t *>(recording_buffer) + offset,
            chunk_size)) {
      Serial.printf("[NET] PCM upload failed at %lu/%lu bytes\n",
                    static_cast<unsigned long>(offset),
                    static_cast<unsigned long>(recording_bytes));
      websocket.close();
      return;
    }
  }

  if (!websocket.sendText("{\"type\":\"audio_end\"}")) {
    Serial.println("[NET] audio_end send failed");
    websocket.close();
    return;
  }
  Serial.printf("[NET] uploaded PCM session=%s bytes=%lu\n", session_id.c_str(),
                static_cast<unsigned long>(recording_bytes));

  resetPlaybackWav();
  const uint32_t response_deadline = millis() + TOPPET_HOST_RESPONSE_TIMEOUT_MS;
  while (static_cast<int32_t>(millis() - response_deadline) < 0) {
    if (!serviceResponseAnimation()) {
      Serial.println("[PET] response animation failed");
      break;
    }
    size_t response_length = 0;
    uint8_t opcode = 0;
    if (!websocket.readFrame(
            network_response_buffer, NETWORK_RESPONSE_BUFFER_BYTES,
            response_length, opcode,
            HOST_FRAME_POLL_MS)) {
      if (!websocket.connected()) {
        break;
      }
      continue;
    }
    if (opcode == 0x1) {
      network_response_buffer[
          response_length < NETWORK_RESPONSE_BUFFER_BYTES
                          ? response_length
                          : NETWORK_RESPONSE_BUFFER_BYTES - 1] = '\0';
      Serial.printf("[NET] host: %s\n", network_response_buffer);
      const char *emotion_marker = strstr(
          reinterpret_cast<char *>(network_response_buffer), "\"emotion\":\"");
      if (emotion_marker != nullptr) {
        emotion_marker += strlen("\"emotion\":\"");
        char emotion[24] = {};
        size_t emotion_length = 0;
        while (emotion_marker[emotion_length] != '\0' &&
               emotion_marker[emotion_length] != '"' &&
               emotion_length + 1 < sizeof(emotion)) {
          emotion[emotion_length] = emotion_marker[emotion_length];
          ++emotion_length;
        }
        setHostEmotion(emotion);
      }
      if (strstr(reinterpret_cast<char *>(network_response_buffer),
                 "\"type\":\"subtitle\"")) {
        if (applyHostSubtitle(
                reinterpret_cast<char *>(network_response_buffer))) {
          Serial.println("[PET] dynamic subtitle applied");
        } else {
          Serial.println("[PET] dynamic subtitle decode failed");
        }
      }
      if (strstr(reinterpret_cast<char *>(network_response_buffer),
                 "\"type\":\"audio_start\"")) {
        i2s.setTimeout(AUDIO_PLAYBACK_IO_TIMEOUT_MS);
        playback_active = true;
        if (es8311_device != nullptr) {
          es8311_voice_mute(es8311_device, false);
        }
        resetPlaybackWav();
      }
      if (strstr(reinterpret_cast<char *>(network_response_buffer),
                 "\"type\":\"audio_end\"")) {
        finishPlaybackAudio();
        setButtonTestState(ButtonTestState::IDLE);
        break;
      }
    } else if (opcode == 0x2) {
      processPlaybackAudio(network_response_buffer, response_length);
    } else if (opcode == 0x8) {
      break;
    }
  }
  if (playback_active) {
    finishPlaybackAudio();
  }
  websocket.close();
}

void enqueueUiCommand(
    UiCommandType type, const char *session_id, const char *emotion,
    char *json, uint32_t activate_sequence, QueueHandle_t destination) {
  UiCommand command = {};
  command.type = type;
  strlcpy(command.session_id, session_id == nullptr ? "" : session_id,
          sizeof(command.session_id));
  if (emotion != nullptr) {
    strlcpy(command.emotion, emotion, sizeof(command.emotion));
  }
  command.json = json;
  command.activate_sequence = activate_sequence;
  QueueHandle_t queue = destination == nullptr ? ui_command_queue : destination;
  if (xQueueSend(queue, &command, pdMS_TO_TICKS(20)) != pdTRUE) {
    Serial.println("[UI] command queue full");
    if (json != nullptr) free(json);
  }
}

void audioCaptureTask(void *parameter) {
  (void)parameter;
  bool capturing = false;
  char session_id[48] = {};
  uint32_t sequence = 0;
  uint32_t samples_total = 0;
  uint32_t frame_samples = 0;
  int16_t frame[PCM_FRAME_SAMPLES] = {};
  int16_t minimum = 32767;
  int16_t maximum = -32768;
  uint64_t sum_squares = 0;

  auto send_network = [](NetworkOutbound &message) {
    if (xQueueSend(
            network_outbound_queue, &message,
            pdMS_TO_TICKS(20)) != pdTRUE) {
      Serial.printf("[NET] outbound queue full type=%u session=%s\n",
                    static_cast<unsigned>(message.type), message.session_id);
    }
  };

  auto send_frame = [&]() {
    if (frame_samples == 0) return;
    if (frame_samples < PCM_FRAME_SAMPLES) {
      memset(frame + frame_samples, 0,
             (PCM_FRAME_SAMPLES - frame_samples) * sizeof(int16_t));
    }
    NetworkOutbound message = {};
    message.type = NetworkOutboundType::MIC_FRAME;
    strlcpy(message.session_id, session_id, sizeof(message.session_id));
    message.sequence = sequence++;
    message.length = PCM_FRAME_BYTES;
    memcpy(message.pcm, frame, PCM_FRAME_BYTES);
    send_network(message);
    frame_samples = 0;
  };

  auto stop_capture = [&]() {
    if (!capturing) return;
    send_frame();
    if (audio_ready && !es7210Stop()) {
      Serial.println("[AUDIO] ES7210 stop failed");
    }
    NetworkOutbound stop = {};
    stop.type = NetworkOutboundType::LISTEN_STOP;
    strlcpy(stop.session_id, session_id, sizeof(stop.session_id));
    stop.sequence = sequence;
    send_network(stop);
    const uint32_t mean_square = samples_total == 0
                                     ? 0
                                     : static_cast<uint32_t>(
                                           sum_squares / samples_total);
    Serial.printf(
        "[AUDIO] capture session=%s samples=%lu ms=%lu min=%d max=%d rms=%lu frames=%lu\n",
        session_id, static_cast<unsigned long>(samples_total),
        static_cast<unsigned long>(samples_total * 1000UL / AUDIO_SAMPLE_RATE),
        minimum, maximum,
        static_cast<unsigned long>(sqrtf(static_cast<float>(mean_square))),
        static_cast<unsigned long>(sequence));
    capturing = false;
  };

  while (true) {
    CaptureCommand command = {};
    if (xQueueReceive(
            capture_command_queue, &command,
            capturing ? 0 : portMAX_DELAY) == pdTRUE) {
      if (command.type == CaptureCommandType::START) {
        stop_capture();
        strlcpy(session_id, command.session_id, sizeof(session_id));
        sequence = 0;
        samples_total = 0;
        frame_samples = 0;
        minimum = 32767;
        maximum = -32768;
        sum_squares = 0;
        NetworkOutbound start = {};
        start.type = NetworkOutboundType::LISTEN_START;
        strlcpy(start.session_id, session_id, sizeof(start.session_id));
        send_network(start);
        i2s.setTimeout(AUDIO_RECORDING_IO_TIMEOUT_MS);
        capturing = audio_ready;
        if (audio_ready && !es7210Start()) {
          Serial.println("[AUDIO] ES7210 start failed");
          capturing = false;
        }
      } else if (command.type == CaptureCommandType::STOP &&
                 strcmp(command.session_id, session_id) == 0) {
        stop_capture();
      }
    }
    if (!capturing) continue;

    const size_t bytes_read = i2s.readBytes(
        reinterpret_cast<char *>(audio_read_buffer), sizeof(audio_read_buffer));
    const size_t stereo_samples = bytes_read / (sizeof(int16_t) * 2);
    for (size_t index = 0; index < stereo_samples; ++index) {
      const int16_t sample = audio_read_buffer[index * 2];
      frame[frame_samples++] = sample;
      ++samples_total;
      if (sample < minimum) minimum = sample;
      if (sample > maximum) maximum = sample;
      const int32_t wide_sample = sample;
      sum_squares += static_cast<uint64_t>(wide_sample * wide_sample);
      if (frame_samples == PCM_FRAME_SAMPLES) send_frame();
      if (samples_total >=
          AUDIO_SAMPLE_RATE * AUDIO_MAX_RECORDING_MS / 1000) {
        Serial.println("[AUDIO] recording limit reached (10s)");
        stop_capture();
        break;
      }
    }
    taskYIELD();
  }
}

void networkTask(void *parameter) {
  (void)parameter;
  TopPetWebSocketClient websocket;
  uint32_t reconnect_at = 0;
  uint32_t last_ping_at = 0;
  char network_session[48] = {};
  bool listening = false;
  bool pending_tts_binary = false;
  char pending_tts_session[48] = {};
  uint32_t pending_tts_sequence = 0;
  uint32_t pending_tts_bytes = 0;
  uint32_t expected_tts_sequence = 0;
  uint32_t pending_subtitle_sequence = 0;
  uint32_t stream_written = 0;
  bool first_mic_sent = false;
  bool first_tts_received = false;
  bool high_water_warned = false;

  auto close_socket = [&]() {
    websocket.close();
    if (!listening && network_session[0] != '\0') {
      PlaybackCommand abort = {};
      abort.type = PlaybackCommandType::ABORT;
      strlcpy(abort.session_id, network_session, sizeof(abort.session_id));
      xQueueSend(playback_command_queue, &abort, 0);
      enqueueUiCommand(UiCommandType::IDLE, network_session);
      network_session[0] = '\0';
      pending_tts_binary = false;
    }
    reconnect_at = millis() + HOST_RECONNECT_MS;
  };

  auto connect_host = [&]() -> bool {
    if (WiFi.status() != WL_CONNECTED ||
        static_cast<int32_t>(millis() - reconnect_at) < 0) {
      return false;
    }
    if (!websocket.connectTo(network_host.c_str(), network_host_port, "/ws")) {
      reconnect_at = millis() + HOST_RECONNECT_MS;
      return false;
    }
    String hello = "{\"type\":\"hello\",\"protocol\":\"toppet-host/v2\",\"device_id\":\"";
    hello += TOPPET_DEVICE_ID;
    hello += "\",\"audio\":{\"format\":\"pcm_s16le\",\"sample_rate\":16000,\"channels\":1,\"bits_per_sample\":16,\"frame_ms\":20}}";
    if (!websocket.sendText(hello)) {
      close_socket();
      return false;
    }
    Serial.printf("[NET] v2 connected host=%s:%u rssi=%d\n",
                  network_host.c_str(), network_host_port, WiFi.RSSI());
    last_ping_at = millis();
    if (listening && network_session[0] != '\0') {
      String restart = "{\"type\":\"listen_start\",\"session_id\":\"";
      restart += network_session;
      restart += "\"}";
      if (!websocket.sendText(restart)) {
        close_socket();
        return false;
      }
      expected_tts_sequence = 0;
    }
    return true;
  };

  auto playback_command = [](PlaybackCommandType type, const char *session) {
    PlaybackCommand command = {};
    command.type = type;
    strlcpy(command.session_id, session, sizeof(command.session_id));
    if (type == PlaybackCommandType::START) {
      xEventGroupClearBits(voice_events, EVENT_PLAYBACK_READY);
    }
    xQueueSend(playback_command_queue, &command, pdMS_TO_TICKS(20));
    if (type == PlaybackCommandType::START) {
      xEventGroupWaitBits(
          voice_events, EVENT_PLAYBACK_READY, pdFALSE, pdTRUE,
          pdMS_TO_TICKS(250));
    }
  };

  while (true) {
    if (!websocket.connected()) connect_host();

    NetworkOutbound outbound = {};
    if (websocket.connected() &&
        xQueueReceive(network_outbound_queue, &outbound, 0) == pdTRUE) {
      bool sent = true;
      if (outbound.type == NetworkOutboundType::ABORT) {
        String json = "{\"type\":\"abort\",\"session_id\":\"";
        json += outbound.session_id;
        json += "\"}";
        sent = websocket.sendText(json);
        if (strcmp(network_session, outbound.session_id) == 0) {
          network_session[0] = '\0';
          listening = false;
        }
      } else if (outbound.type == NetworkOutboundType::LISTEN_START) {
        strlcpy(network_session, outbound.session_id, sizeof(network_session));
        listening = true;
        first_mic_sent = false;
        String json = "{\"type\":\"listen_start\",\"session_id\":\"";
        json += network_session;
        json += "\"}";
        sent = websocket.sendText(json);
      } else if (outbound.type == NetworkOutboundType::MIC_FRAME) {
        if (strcmp(network_session, outbound.session_id) != 0 || !listening) {
          continue;
        }
        String json = "{\"type\":\"mic_frame\",\"session_id\":\"";
        json += outbound.session_id;
        json += "\",\"sequence\":";
        json += outbound.sequence;
        json += ",\"bytes\":";
        json += outbound.length;
        json += "}";
        sent = websocket.sendText(json) &&
               websocket.sendBinary(outbound.pcm, outbound.length);
        if (sent && !first_mic_sent) {
          first_mic_sent = true;
          logLatency(
              "first_mic_frame_sent", outbound.session_id, 0,
              outbound.sequence, 0);
        }
      } else if (outbound.type == NetworkOutboundType::LISTEN_STOP) {
        if (strcmp(network_session, outbound.session_id) != 0) continue;
        String json = "{\"type\":\"listen_stop\",\"session_id\":\"";
        json += outbound.session_id;
        json += "\"}";
        sent = websocket.sendText(json);
        listening = false;
        if (sent) {
          logLatency(
              "last_mic_frame_sent", outbound.session_id, 0,
              static_cast<int32_t>(outbound.sequence) - 1, 0);
        }
      }
      if (!sent) {
        xQueueSendToFront(network_outbound_queue, &outbound, 0);
        close_socket();
      }
    }

    if (!websocket.connected()) {
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;
    }
    if (millis() - last_ping_at >= HOST_PING_MS) {
      if (!websocket.sendText("{\"type\":\"ping\"}")) {
        close_socket();
        continue;
      }
      last_ping_at = millis();
    }

    size_t response_length = 0;
    uint8_t opcode = 0;
    if (!websocket.readFrame(
            network_response_buffer, NETWORK_RESPONSE_BUFFER_BYTES - 1,
            response_length, opcode, 2)) {
      if (!websocket.connected()) close_socket();
      vTaskDelay(1);
      continue;
    }
    if (opcode == 0x8) {
      close_socket();
      continue;
    }
    if (opcode == 0x9) {
      if (!websocket.sendPong(network_response_buffer, response_length)) {
        close_socket();
      }
      continue;
    }
    if (opcode == 0xA) continue;
    if (opcode == 0x1) {
      network_response_buffer[response_length] = '\0';
      const char *json = reinterpret_cast<char *>(network_response_buffer);
      String type;
      String event_session;
      if (!extractJsonStringField(json, "type", type)) continue;
      extractJsonStringField(json, "session_id", event_session);
      const bool current = !event_session.isEmpty() &&
                           strcmp(event_session.c_str(), network_session) == 0;
      if (type == "emotion" && current) {
        String emotion;
        if (extractJsonStringField(json, "emotion", emotion)) {
          enqueueUiCommand(
              UiCommandType::EMOTION, event_session.c_str(), emotion.c_str());
        }
      } else if (type == "tts_start" && current) {
        expected_tts_sequence = 0;
        stream_written = 0;
        first_tts_received = false;
        high_water_warned = false;
        playback_command(PlaybackCommandType::START, event_session.c_str());
      } else if (type == "tts_sentence_start" && current) {
        extractJsonUIntField(json, "sequence", pending_subtitle_sequence);
      } else if (type == "subtitle" && current) {
        char *copy = static_cast<char *>(heap_caps_malloc(
            response_length + 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
        if (copy != nullptr) {
          memcpy(copy, json, response_length + 1);
          enqueueUiCommand(
              UiCommandType::SUBTITLE, event_session.c_str(), nullptr, copy,
              pending_subtitle_sequence, scheduled_ui_queue);
        }
      } else if (type == "tts_audio" && current) {
        pending_tts_binary =
            extractJsonUIntField(json, "sequence", pending_tts_sequence) &&
            extractJsonUIntField(json, "bytes", pending_tts_bytes);
        strlcpy(
            pending_tts_session, event_session.c_str(),
            sizeof(pending_tts_session));
      } else if (type == "tts_stop" && current) {
        playback_command(PlaybackCommandType::END, event_session.c_str());
      } else if ((type == "cancelled" || type == "error") && current) {
        playback_command(PlaybackCommandType::ABORT, event_session.c_str());
        enqueueUiCommand(UiCommandType::IDLE, event_session.c_str());
      }
    } else if (opcode == 0x2) {
      if (!pending_tts_binary ||
          strcmp(pending_tts_session, network_session) != 0 ||
          response_length != pending_tts_bytes) {
        Serial.printf(
            "[AUDIO] stale/invalid TTS frame session=%s seq=%lu bytes=%u expected=%lu\n",
            pending_tts_session,
            static_cast<unsigned long>(pending_tts_sequence), response_length,
            static_cast<unsigned long>(pending_tts_bytes));
        pending_tts_binary = false;
        continue;
      }
      pending_tts_binary = false;
      if (pending_tts_sequence != expected_tts_sequence) {
        Serial.printf(
            "[AUDIO] TTS sequence mismatch session=%s expected=%lu got=%lu\n",
            network_session,
            static_cast<unsigned long>(expected_tts_sequence),
            static_cast<unsigned long>(pending_tts_sequence));
        if (pending_tts_sequence < expected_tts_sequence) continue;
      }
      expected_tts_sequence = pending_tts_sequence + 1;
      PlaybackMarker marker = {};
      strlcpy(marker.session_id, network_session, sizeof(marker.session_id));
      marker.sequence = pending_tts_sequence;
      marker.stream_offset = stream_written;
      xQueueSend(playback_marker_queue, &marker, pdMS_TO_TICKS(50));
      size_t offset = 0;
      while (offset < response_length) {
        if (xEventGroupGetBits(voice_events) & EVENT_PLAYBACK_ABORT) break;
        const size_t written = xStreamBufferSend(
            playback_stream, network_response_buffer + offset,
            response_length - offset, pdMS_TO_TICKS(50));
        if (written == 0) {
          Serial.printf(
              "[AUDIO] playback overflow backpressure session=%s seq=%lu water=%u\n",
              network_session,
              static_cast<unsigned long>(pending_tts_sequence),
              xStreamBufferBytesAvailable(playback_stream));
          continue;
        }
        offset += written;
        stream_written += written;
      }
      const size_t water = xStreamBufferBytesAvailable(playback_stream);
      if (!first_tts_received) {
        first_tts_received = true;
        logLatency(
            "first_tts_frame_received", network_session, 0,
            pending_tts_sequence, water);
      }
      if (water > PLAYBACK_HIGH_WATER_BYTES && !high_water_warned) {
        high_water_warned = true;
        Serial.printf("[AUDIO] high water session=%s bytes=%u\n",
                      network_session, water);
      }
    }
  }
}

void audioPlaybackTask(void *parameter) {
  (void)parameter;
  char session_id[48] = {};
  bool session_open = false;
  bool started = false;
  bool input_finished = false;
  bool first_i2s = false;
  uint32_t consumed = 0;
  uint32_t current_sequence = 0;
  uint32_t underruns = 0;
  bool underrun_gap = false;
  uint8_t mono_buffer[PCM_FRAME_BYTES];

  auto drain_scheduled_ui = []() {
    UiCommand command = {};
    while (xQueueReceive(scheduled_ui_queue, &command, 0) == pdTRUE) {
      if (command.json != nullptr) free(command.json);
    }
  };

  auto reset_playback = [&]() {
    xEventGroupSetBits(voice_events, EVENT_PLAYBACK_ABORT);
    xStreamBufferReset(playback_stream);
    xQueueReset(playback_marker_queue);
    drain_scheduled_ui();
    if (es8311_device != nullptr) es8311_voice_mute(es8311_device, true);
    playback_active = false;
    session_open = false;
    started = false;
    input_finished = false;
    first_i2s = false;
    consumed = 0;
    current_sequence = 0;
    underruns = 0;
    underrun_gap = false;
    xEventGroupClearBits(
        voice_events,
        EVENT_PLAYBACK_ACTIVE | EVENT_PLAYBACK_LOW_WATER);
  };

  while (true) {
    PlaybackCommand command = {};
    while (xQueueReceive(playback_command_queue, &command, 0) == pdTRUE) {
      if (command.type == PlaybackCommandType::START) {
        reset_playback();
        strlcpy(session_id, command.session_id, sizeof(session_id));
        session_open = true;
        xEventGroupClearBits(voice_events, EVENT_PLAYBACK_ABORT);
        xEventGroupSetBits(voice_events, EVENT_PLAYBACK_READY);
      } else if (strcmp(command.session_id, session_id) == 0) {
        if (command.type == PlaybackCommandType::END) {
          input_finished = true;
        } else {
          reset_playback();
        }
      }
    }
    if (!session_open) {
      vTaskDelay(pdMS_TO_TICKS(2));
      continue;
    }

    const size_t available = xStreamBufferBytesAvailable(playback_stream);
    if (!started &&
        (available >= PLAYBACK_PREBUFFER_BYTES ||
         (input_finished && available > 0))) {
      started = true;
      playback_active = true;
      i2s.setTimeout(AUDIO_PLAYBACK_IO_TIMEOUT_MS);
      if (es8311_device != nullptr) es8311_voice_mute(es8311_device, false);
      xEventGroupSetBits(voice_events, EVENT_PLAYBACK_ACTIVE);
    }
    if (!started) {
      vTaskDelay(pdMS_TO_TICKS(2));
      continue;
    }

    PlaybackMarker marker = {};
    while (xQueuePeek(playback_marker_queue, &marker, 0) == pdTRUE &&
           marker.stream_offset <= consumed) {
      xQueueReceive(playback_marker_queue, &marker, 0);
      if (strcmp(marker.session_id, session_id) == 0) {
        current_sequence = marker.sequence;
      }
    }
    UiCommand scheduled = {};
    while (xQueuePeek(scheduled_ui_queue, &scheduled, 0) == pdTRUE &&
           scheduled.activate_sequence <= current_sequence) {
      xQueueReceive(scheduled_ui_queue, &scheduled, 0);
      if (strcmp(scheduled.session_id, session_id) == 0) {
        if (xQueueSend(ui_command_queue, &scheduled, pdMS_TO_TICKS(20)) != pdTRUE &&
            scheduled.json != nullptr) {
          free(scheduled.json);
        }
      } else if (scheduled.json != nullptr) {
        free(scheduled.json);
      }
    }

    const size_t received = xStreamBufferReceive(
        playback_stream, mono_buffer, sizeof(mono_buffer),
        pdMS_TO_TICKS(30));
    if (received == 0) {
      if (input_finished) {
        delay(80);
        if (es8311_device != nullptr) es8311_voice_mute(es8311_device, true);
        logLatency(
            "playback_finished", session_id, 0, current_sequence, 0);
        Serial.printf(
            "[AUDIO] playback finished session=%s underruns=%lu\n",
            session_id, static_cast<unsigned long>(underruns));
        enqueueUiCommand(UiCommandType::IDLE, session_id);
        reset_playback();
      } else {
        if (!underrun_gap) {
          ++underruns;
          underrun_gap = true;
          logLatency(
              "playback_underrun", session_id, 0, current_sequence, 0);
        }
      }
      continue;
    }
    underrun_gap = false;

    const size_t sample_count = received / sizeof(int16_t);
    const int16_t *mono = reinterpret_cast<const int16_t *>(mono_buffer);
    size_t sample_offset = 0;
    while (sample_offset < sample_count) {
      const size_t capacity =
          sizeof(playback_stereo_buffer) / sizeof(int16_t) / 2;
      const size_t count =
          sample_count - sample_offset < capacity
              ? sample_count - sample_offset
              : capacity;
      for (size_t index = 0; index < count; ++index) {
        const int16_t sample = mono[sample_offset + index];
        playback_stereo_buffer[index * 2] = sample;
        playback_stereo_buffer[index * 2 + 1] = sample;
      }
      const size_t stereo_bytes = count * sizeof(int16_t) * 2;
      if (i2s.write(
              reinterpret_cast<const uint8_t *>(playback_stereo_buffer),
              stereo_bytes) != stereo_bytes) {
        Serial.printf("[AUDIO] I2S write failed session=%s\n", session_id);
      }
      if (!first_i2s) {
        first_i2s = true;
        logLatency(
            "first_i2s_write", session_id, 0, current_sequence,
            xStreamBufferBytesAvailable(playback_stream));
      }
      sample_offset += count;
    }
    consumed += received;
    const size_t water = xStreamBufferBytesAvailable(playback_stream);
    if (water < PLAYBACK_LOW_WATER_BYTES && !input_finished) {
      xEventGroupSetBits(voice_events, EVENT_PLAYBACK_LOW_WATER);
    } else {
      xEventGroupClearBits(voice_events, EVENT_PLAYBACK_LOW_WATER);
    }
  }
}

void serviceUiCommands() {
  UiCommand command = {};
  while (xQueueReceive(ui_command_queue, &command, 0) == pdTRUE) {
    const bool current = strcmp(command.session_id, ui_active_session) == 0;
    if (current && command.type == UiCommandType::LISTENING) {
      setButtonTestState(ButtonTestState::LISTENING);
    } else if (current && command.type == UiCommandType::THINKING) {
      setButtonTestState(ButtonTestState::THINKING);
    } else if (current && command.type == UiCommandType::EMOTION) {
      setHostEmotion(command.emotion);
    } else if (current && command.type == UiCommandType::SUBTITLE &&
               command.json != nullptr) {
      if (!applyHostSubtitle(command.json)) {
        Serial.println("[PET] dynamic subtitle decode failed");
      }
    } else if (current && command.type == UiCommandType::IDLE) {
      ui_active_session[0] = '\0';
      setButtonTestState(ButtonTestState::IDLE);
    } else if (current && command.type == UiCommandType::CLEAR) {
      memset(text_mask, 0, TEXT_MASK_BYTES);
      drawSpeechBubble();
    }
    if (command.json != nullptr) free(command.json);
  }
}

void inputTask(void *parameter) {
  (void)parameter;
  while (true) {
    serviceSerialTestControl();
    updateUserButton();
    vTaskDelay(pdMS_TO_TICKS(5));
  }
}

void uiTask(void *parameter) {
  (void)parameter;
  while (true) {
    serviceUiCommands();
    const EventBits_t voice_bits = xEventGroupGetBits(voice_events);
    if (voice_bits & (EVENT_PLAYBACK_LOW_WATER | EVENT_PLAYBACK_ACTIVE)) {
      vTaskDelay(pdMS_TO_TICKS(2));
      continue;
    }
    if (asset_header.frame_count <= 1) {
      vTaskDelay(pdMS_TO_TICKS(4));
      continue;
    }
    const uint32_t now = millis();
    const uint16_t duration = frame_entries[animation_frame].duration_ms;
    if (now - expression_started_at < duration) {
      vTaskDelay(pdMS_TO_TICKS(2));
      continue;
    }
    animation_frame = (animation_frame + 1) % asset_header.frame_count;
    expression_started_at = now;
    if (!renderCurrentFrame()) {
      Serial.println("[PET] expression decode failed");
    }
  }
}

void createVoiceTasks() {
  capture_command_queue = xQueueCreate(4, sizeof(CaptureCommand));
  playback_command_queue = xQueueCreate(8, sizeof(PlaybackCommand));
  playback_marker_queue = xQueueCreate(128, sizeof(PlaybackMarker));
  scheduled_ui_queue = xQueueCreate(16, sizeof(UiCommand));
  ui_command_queue = xQueueCreate(16, sizeof(UiCommand));
  playback_stream = xStreamBufferCreate(PLAYBACK_RING_BYTES, PCM_FRAME_BYTES);
  voice_events = xEventGroupCreate();
  constexpr UBaseType_t OUTBOUND_QUEUE_LENGTH = 520;
  network_outbound_queue_storage = static_cast<uint8_t *>(heap_caps_malloc(
      OUTBOUND_QUEUE_LENGTH * sizeof(NetworkOutbound),
      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (network_outbound_queue_storage != nullptr) {
    network_outbound_queue = xQueueCreateStatic(
        OUTBOUND_QUEUE_LENGTH, sizeof(NetworkOutbound),
        network_outbound_queue_storage, &network_outbound_queue_control);
  }
  if (!capture_command_queue || !network_outbound_queue ||
      !playback_command_queue || !playback_marker_queue ||
      !scheduled_ui_queue || !ui_command_queue || !playback_stream ||
      !voice_events) {
    showFatalError("Voice task allocation failed");
  }
  xTaskCreatePinnedToCore(
      audioPlaybackTask, "AudioPlaybackTask", 6144, nullptr, 5, nullptr, 0);
  xTaskCreatePinnedToCore(
      audioCaptureTask, "AudioCaptureTask", 6144, nullptr, 4, nullptr, 0);
  xTaskCreatePinnedToCore(
      networkTask, "NetworkTask", 8192, nullptr, 3, nullptr, 1);
  xTaskCreatePinnedToCore(
      uiTask, "UiTask", 6144, nullptr, 2, nullptr, 1);
  xTaskCreatePinnedToCore(
      inputTask, "InputTask", 4096, nullptr, 4, nullptr, 1);
}

bool initializeAudioInput() {
  i2s.setPins(
      ES7210_BCLK_PIN, ES7210_LRCK_PIN, ES8311_DATA_OUT_PIN,
      ES7210_DATA_IN_PIN, ES7210_MCLK_PIN);
  if (!i2s.begin(
          I2S_MODE_STD, AUDIO_SAMPLE_RATE, I2S_DATA_BIT_WIDTH_16BIT,
          I2S_SLOT_MODE_STEREO, I2S_STD_SLOT_BOTH)) {
    Serial.println("[AUDIO] I2S install failed");
    return false;
  }
  // Keep the GPIO18 press/release state machine responsive. The Arduino
  // Stream default is a 1000 ms read timeout, which feels like a freeze when
  // an I2S DMA block is not ready yet.
  i2s.setTimeout(AUDIO_RECORDING_IO_TIMEOUT_MS);

  pinMode(ES8311_PA_CTRL_PIN, OUTPUT);
  digitalWrite(ES8311_PA_CTRL_PIN, HIGH);

  es8311_device = es8311_create(0, ES8311_I2C_ADDRESS);
  if (es8311_device == nullptr) {
    Serial.println("[AUDIO] ES8311 create failed");
    return false;
  }
  const es8311_clock_config_t es8311_clock = {
      .mclk_inverted = false,
      .sclk_inverted = false,
      .mclk_from_mclk_pin = true,
      .mclk_frequency = static_cast<int>(AUDIO_SAMPLE_RATE * 256),
      .sample_frequency = static_cast<int>(AUDIO_SAMPLE_RATE),
  };
  if (es8311_init(
          es8311_device, &es8311_clock, ES8311_RESOLUTION_16,
          ES8311_RESOLUTION_16) != ESP_OK ||
      es8311_sample_frequency_config(
          es8311_device, es8311_clock.mclk_frequency,
          es8311_clock.sample_frequency) != ESP_OK ||
      es8311_voice_volume_set(es8311_device, 85, nullptr) != ESP_OK ||
      es8311_voice_mute(es8311_device, true) != ESP_OK) {
    Serial.println("[AUDIO] ES8311 init failed");
    return false;
  }

  if (!es7210Begin(Wire) || !es7210Start()) {
    Serial.printf("[AUDIO] ES7210 init failed, reg01=0x%02X\n",
                  es7210ReadRegister(0x01));
    return false;
  }
  if (!es7210Stop()) {
    Serial.println("[AUDIO] ES7210 initial stop failed");
    return false;
  }
  Serial.printf("[AUDIO] ES7210 ready addr=0x%02X rate=%lu bits=16\n",
                ES7210_I2C_ADDRESS, static_cast<unsigned long>(AUDIO_SAMPLE_RATE));
  return true;
}

void serviceRecording() {
  if (!audio_ready || button_test_state != ButtonTestState::LISTENING) {
    return;
  }

  // Rendering a 480x480 frame can take longer than one DMA buffer period.
  // Drain all samples already queued by I2S so the DMA ring does not overflow
  // while the display animation is running.
  // Read one stereo DMA-sized block per service call. Returning to loop()
  // after every block keeps GPIO18 responsive while the user is holding it.
  const size_t bytes_read = i2s.readBytes(
      reinterpret_cast<char *>(audio_read_buffer), sizeof(audio_read_buffer));
  if (bytes_read == 0) {
    return;
  }

  const size_t stereo_sample_count = bytes_read / (sizeof(int16_t) * 2);
  const uint32_t remaining_samples =
      (AUDIO_MAX_RECORDING_BYTES - recording_bytes) / sizeof(int16_t);
  const size_t samples_to_copy = stereo_sample_count < remaining_samples
                                     ? stereo_sample_count
                                     : remaining_samples;
  int16_t *mono_destination = recording_buffer + recording_bytes / sizeof(int16_t);
  for (size_t index = 0; index < samples_to_copy; ++index) {
    mono_destination[index] = audio_read_buffer[index * 2];
  }
  recording_bytes += samples_to_copy * sizeof(int16_t);
  if (samples_to_copy < stereo_sample_count) {
    Serial.println("[AUDIO] recording limit reached (5s)");
    setButtonTestState(ButtonTestState::THINKING);
  }
}

void finishRecording() {
  if (recording_bytes == 0) {
    Serial.println("[AUDIO] recording empty");
    return;
  }

  int16_t minimum = 32767;
  int16_t maximum = -32768;
  uint64_t sum_squares = 0;
  const uint32_t sample_count = recording_bytes / sizeof(int16_t);
  for (uint32_t index = 0; index < sample_count; ++index) {
    const int32_t sample = recording_buffer[index];
    if (sample < minimum) minimum = static_cast<int16_t>(sample);
    if (sample > maximum) maximum = static_cast<int16_t>(sample);
    sum_squares += static_cast<uint64_t>(sample * sample);
  }
  const uint32_t mean_square = sample_count == 0
                                   ? 0
                                   : static_cast<uint32_t>(sum_squares / sample_count);
  Serial.printf("[AUDIO] recording bytes=%lu ms=%lu min=%d max=%d rms=%lu\n",
                static_cast<unsigned long>(recording_bytes),
                static_cast<unsigned long>(sample_count * 1000UL / AUDIO_SAMPLE_RATE),
                minimum, maximum,
                static_cast<unsigned long>(sqrtf(static_cast<float>(mean_square))));
}

bool readExact(void *destination, size_t size) {
  return asset_file.read(static_cast<uint8_t *>(destination), size) == size;
}

bool loadExpression(uint8_t index) {
  if (asset_file) {
    asset_file.close();
  }
  asset_file = LittleFS.open(kExpressionPaths[index], "r");
  if (!asset_file) {
    Serial.printf("Cannot open %s\n", kExpressionPaths[index]);
    return false;
  }
  if (!readExact(&asset_header, sizeof(asset_header)) ||
      memcmp(asset_header.magic, "TPA1", 4) != 0 ||
      asset_header.version != 1 ||
      asset_header.width != 420 ||
      asset_header.height != 340 ||
      asset_header.frame_count == 0 ||
      asset_header.frame_count > 8 ||
      asset_header.text_mask_size != TEXT_MASK_BYTES) {
    Serial.println("Invalid expression asset header");
    return false;
  }

  asset_file.seek(asset_header.palette_offset);
  if (!readExact(palette, sizeof(palette))) {
    return false;
  }
  asset_file.seek(asset_header.frame_table_offset);
  if (!readExact(frame_entries, sizeof(PetFrameEntry) * asset_header.frame_count)) {
    return false;
  }
  asset_file.seek(asset_header.text_mask_offset);
  if (!readExact(text_mask, TEXT_MASK_BYTES)) {
    return false;
  }
  expression_index = index;
  animation_frame = 0;
  expression_started_at = millis();
  Serial.printf("Expression: %s\n", kExpressionNames[index]);
  return true;
}

uint8_t expressionIndexFor(const char *name) {
  for (uint8_t index = 0; index < EXPRESSION_COUNT; ++index) {
    if (strcmp(kExpressionNames[index], name) == 0) {
      return index;
    }
  }
  return 0;
}

void setButtonTestState(ButtonTestState next_state) {
  if (button_test_state == next_state) {
    return;
  }

  button_test_state = next_state;
  const char *expression_name = "NEUTRAL";
  if (next_state == ButtonTestState::LISTENING) {
    expression_name = "LISTENING";
  } else if (next_state == ButtonTestState::THINKING) {
    expression_name = "THINKING";
    button_thinking_started_at = millis();
  }
  if (!loadExpression(expressionIndexFor(expression_name)) ||
      !renderCurrentFrame()) {
    showFatalError("Button state expression failed");
  }
  if (!drawSpeechBubble()) {
    showFatalError("Button state subtitle failed");
  }
  expression_started_at = millis();
  Serial.printf("[VOICE KEY] state=%s\n", expression_name);
}

bool serviceResponseAnimation() {
  // Full-screen display transfers compete with the I2S producer on this first
  // firmware version. Animate while waiting for ASR/LLM/TTS, then keep the
  // selected emotion frame still while audio DMA is active to avoid underruns.
  if (playback_active || asset_header.frame_count <= 1) {
    return true;
  }
  const uint32_t now = millis();
  const uint16_t duration = frame_entries[animation_frame].duration_ms;
  if (now - expression_started_at < duration) {
    return true;
  }
  animation_frame = (animation_frame + 1) % asset_header.frame_count;
  expression_started_at = now;
  return renderCurrentFrame();
}

bool decodePetFrame(uint8_t frame_index) {
  const PetFrameEntry &entry = frame_entries[frame_index];
  if (!asset_file.seek(entry.offset)) {
    return false;
  }

  uint32_t remaining = entry.size;
  uint32_t pet_pixel = 0;
  while (remaining > 0) {
    const uint32_t chunk_size =
        remaining < RLE_BUFFER_BYTES ? remaining : RLE_BUFFER_BYTES;
    if (chunk_size % 3 != 0 || !readExact(rle_buffer, chunk_size)) {
      return false;
    }
    remaining -= chunk_size;

    for (uint32_t offset = 0; offset < chunk_size; offset += 3) {
      uint32_t run_length =
          rle_buffer[offset] | (static_cast<uint16_t>(rle_buffer[offset + 1]) << 8);
      const uint16_t color = palette[rle_buffer[offset + 2]];
      if (run_length == 0 || pet_pixel + run_length > 420UL * 340UL) {
        return false;
      }
      while (run_length > 0) {
        const uint16_t row = pet_pixel / 420;
        const uint16_t column = pet_pixel % 420;
        const uint16_t row_remaining = 420 - column;
        const uint16_t row_run =
            run_length < row_remaining ? run_length : row_remaining;
        uint16_t *destination =
            frame_buffer + static_cast<uint32_t>(row) * PET_WIDTH + column;
        for (uint16_t count = 0; count < row_run; ++count) {
          *destination++ = color;
        }
        pet_pixel += row_run;
        run_length -= row_run;
      }
    }
  }
  return pet_pixel == 420UL * 340UL;
}

bool renderCurrentFrame() {
  const uint32_t started_at = millis();
  memset(frame_buffer, 0, PET_WIDTH * PET_HEIGHT * sizeof(uint16_t));
  if (!decodePetFrame(animation_frame)) {
    return false;
  }
  gfx->draw16bitRGBBitmap(
      PET_X, PET_Y, frame_buffer, PET_WIDTH, PET_HEIGHT);
  const uint32_t elapsed = millis() - started_at;
  if (elapsed > 250) {
    Serial.printf("[PET] slow frame=%lu ms\n", static_cast<unsigned long>(elapsed));
  }
  return true;
}

void setup() {
  Serial.begin(921600);
  delay(100);
  Serial.println("TopPet multi-expression firmware starting");

  pinMode(USER_BUTTON_PIN, INPUT_PULLUP);
  raw_button_pressed =
      digitalRead(USER_BUTTON_PIN) == USER_BUTTON_ACTIVE;
  user_button_pressed = raw_button_pressed;
  raw_button_changed_at = millis();

  Wire.begin(IIC_SDA, IIC_SCL);
  if (!gfx->begin()) {
    while (true) {
      delay(1000);
    }
  }
  bus->writeC8D8(0x36, 0xA0);
  gfx->setBrightness(160);
  gfx->fillScreen(COLOR_BLACK);

  frame_buffer = static_cast<uint16_t *>(heap_caps_malloc(
      PET_WIDTH * PET_HEIGHT * sizeof(uint16_t),
      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  bubble_buffer = static_cast<uint16_t *>(heap_caps_malloc(
      BUBBLE_BUFFER_PIXELS * sizeof(uint16_t),
      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  text_mask = static_cast<uint8_t *>(heap_caps_malloc(
      TEXT_MASK_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  subtitle_encoded_buffer = static_cast<uint8_t *>(heap_caps_malloc(
      SUBTITLE_ENCODED_BUFFER_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  network_response_buffer = static_cast<uint8_t *>(heap_caps_malloc(
      NETWORK_RESPONSE_BUFFER_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  rle_buffer = static_cast<uint8_t *>(heap_caps_malloc(
      RLE_BUFFER_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  if (!frame_buffer || !bubble_buffer || !text_mask ||
      !subtitle_encoded_buffer || !network_response_buffer || !rle_buffer) {
    showFatalError("Memory allocation failed");
  }

  if (!LittleFS.begin(false)) {
    showFatalError("LittleFS mount failed");
  }
  audio_ready = initializeAudioInput();
  if (!audio_ready) {
    Serial.println("[AUDIO] disabled; display and button test continue");
  }
  network_ready = initializeNetwork();
  Serial.printf("LittleFS: %lu/%lu bytes used\n",
                static_cast<unsigned long>(LittleFS.usedBytes()),
                static_cast<unsigned long>(LittleFS.totalBytes()));
  if (!loadExpression(0) || !renderCurrentFrame()) {
    showFatalError("Expression load failed");
  }
  if (!drawSpeechBubble()) {
    showFatalError("Initial subtitle failed");
  }
  // No task may touch I2S, LittleFS, display, or shared queues until the
  // single-threaded hardware/bootstrap rendering above is fully complete.
  createVoiceTasks();
}

void loop() {
  serviceProvisioningPortal();
  delay(10);
}
