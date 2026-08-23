#include <Arduino.h>
#include <Arduino_GFX_Library.h>
#include <ESP_I2S.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <Wire.h>
#include <WiFi.h>
#include <WebServer.h>
#include <esp_heap_caps.h>
#include <math.h>

#include "es7210_min.h"
#include "es8311.h"
#include "expression_manifest.h"
#include "net_config.h"
#include "pin_config.h"
#include "toppet_websocket.h"

constexpr uint16_t PET_X = 30;
constexpr uint16_t PET_Y = 140;
constexpr uint16_t TEXT_MASK_HEIGHT = 140;
constexpr uint32_t TEXT_MASK_BYTES = LCD_WIDTH * TEXT_MASK_HEIGHT / 8;
constexpr uint32_t EXPRESSION_HOLD_MS = 4200;
constexpr uint32_t RLE_BUFFER_BYTES = 1536;
constexpr uint32_t BUTTON_DEBOUNCE_MS = 30;
constexpr uint32_t BUTTON_THINKING_PREVIEW_MS = 1500;
constexpr uint32_t AUDIO_SAMPLE_RATE = 16000;
constexpr uint32_t AUDIO_MAX_RECORDING_MS = 5000;
constexpr uint32_t AUDIO_MAX_RECORDING_BYTES =
    AUDIO_SAMPLE_RATE * AUDIO_MAX_RECORDING_MS / 1000 * sizeof(int16_t);
constexpr size_t AUDIO_READ_BUFFER_BYTES = 2048;

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
static uint8_t *text_mask = nullptr;
static uint8_t *rle_buffer = nullptr;
static uint16_t palette[256];
static PetAssetHeader asset_header;
static PetFrameEntry frame_entries[8];
static File asset_file;
static uint8_t expression_index = 0;
static uint8_t animation_frame = 0;
static uint32_t expression_started_at = 0;
static int16_t text_left = 0;
static int16_t text_top = 0;
static int16_t text_right = 0;
static int16_t text_bottom = 0;
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

inline void setPixel(int16_t x, int16_t y, uint16_t color) {
  if (x >= 0 && x < LCD_WIDTH && y >= 0 && y < LCD_HEIGHT) {
    frame_buffer[static_cast<uint32_t>(y) * LCD_WIDTH + x] = color;
  }
}

void fillHorizontal(int16_t x0, int16_t x1, int16_t y, uint16_t color) {
  if (y < 0 || y >= LCD_HEIGHT || x1 < 0 || x0 >= LCD_WIDTH) {
    return;
  }
  if (x0 < 0) {
    x0 = 0;
  }
  if (x1 >= LCD_WIDTH) {
    x1 = LCD_WIDTH - 1;
  }
  uint16_t *destination = frame_buffer + static_cast<uint32_t>(y) * LCD_WIDTH + x0;
  for (int16_t x = x0; x <= x1; ++x) {
    *destination++ = color;
  }
}

void fillRoundedRectangle(
    int16_t x, int16_t y, int16_t width, int16_t height, int16_t radius,
    uint16_t color) {
  const int32_t radius_squared = radius * radius;
  for (int16_t row = 0; row < height; ++row) {
    int16_t inset = 0;
    if (row < radius) {
      const int16_t dy = radius - row - 1;
      while (inset < radius &&
             (radius - inset) * (radius - inset) + dy * dy > radius_squared) {
        ++inset;
      }
    } else if (row >= height - radius) {
      const int16_t dy = row - (height - radius);
      while (inset < radius &&
             (radius - inset) * (radius - inset) + dy * dy > radius_squared) {
        ++inset;
      }
    }
    fillHorizontal(x + inset, x + width - inset - 1, y + row, color);
  }
}

void fillTriangle(
    int16_t left_x, int16_t right_x, int16_t top_y,
    int16_t tip_x, int16_t tip_y, uint16_t color) {
  const int16_t height = tip_y - top_y;
  for (int16_t row = 0; row <= height; ++row) {
    const int16_t left = left_x + (tip_x - left_x) * row / height;
    const int16_t right = right_x + (tip_x - right_x) * row / height;
    fillHorizontal(left, right, top_y + row, color);
  }
}

bool findTextBounds() {
  text_left = LCD_WIDTH;
  text_top = TEXT_MASK_HEIGHT;
  text_right = -1;
  text_bottom = -1;
  for (int16_t y = 0; y < TEXT_MASK_HEIGHT; ++y) {
    for (int16_t x = 0; x < LCD_WIDTH; ++x) {
      const uint32_t pixel = static_cast<uint32_t>(y) * LCD_WIDTH + x;
      if (text_mask[pixel >> 3] & (0x80 >> (pixel & 7))) {
        if (x < text_left) text_left = x;
        if (x > text_right) text_right = x;
        if (y < text_top) text_top = y;
        if (y > text_bottom) text_bottom = y;
      }
    }
  }
  return text_right >= text_left && text_bottom >= text_top;
}

void drawSpeechBubble() {
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

  fillRoundedRectangle(
      bubble_x, bubble_y, bubble_width, bubble_height, 16, COLOR_BUBBLE_BORDER);
  fillTriangle(
      tail_x - 13, tail_x + 13, bubble_bottom - 2,
      tail_x, bubble_bottom + 20, COLOR_BUBBLE_BORDER);
  fillRoundedRectangle(
      bubble_x + 2, bubble_y + 2, bubble_width - 4, bubble_height - 4,
      14, COLOR_WHITE);
  fillTriangle(
      tail_x - 9, tail_x + 9, bubble_bottom - 3,
      tail_x, bubble_bottom + 15, COLOR_WHITE);

  for (int16_t y = text_top; y <= text_bottom; ++y) {
    for (int16_t x = text_left; x <= text_right; ++x) {
      const uint32_t pixel = static_cast<uint32_t>(y) * LCD_WIDTH + x;
      if (text_mask[pixel >> 3] & (0x80 >> (pixel & 7))) {
        frame_buffer[pixel] = COLOR_BLACK;
      }
    }
  }
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
  Serial.printf(
      "[KEY3/GPIO18] %s at %lu ms\n",
      user_button_pressed ? "pressed" : "released",
      static_cast<unsigned long>(now));
  setButtonTestState(
      user_button_pressed ? ButtonTestState::LISTENING : ButtonTestState::THINKING);
  if (user_button_pressed) {
    recording_bytes = 0;
    if (audio_ready && !es7210Start()) {
      Serial.println("[AUDIO] ES7210 start failed");
    }
  } else {
    finishRecording();
    if (audio_ready && !es7210Stop()) {
      Serial.println("[AUDIO] ES7210 stop failed");
    }
    uploadRecording();
  }
  return true;
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
  if (es8311_device != nullptr) {
    es8311_voice_mute(es8311_device, true);
  }
  if (playback_wav.ready && playback_wav.supported) {
    Serial.println("[AUDIO] ES8311 reply playback finished");
  }
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

  uint8_t response_buffer[2048];
  resetPlaybackWav();
  const uint32_t response_deadline = millis() + TOPPET_HOST_RESPONSE_TIMEOUT_MS;
  while (static_cast<int32_t>(millis() - response_deadline) < 0) {
    size_t response_length = 0;
    uint8_t opcode = 0;
    if (!websocket.readFrame(
            response_buffer, sizeof(response_buffer), response_length, opcode,
            1000)) {
      if (!websocket.connected()) {
        break;
      }
      continue;
    }
    if (opcode == 0x1) {
      response_buffer[response_length < sizeof(response_buffer)
                          ? response_length
                          : sizeof(response_buffer) - 1] = '\0';
      Serial.printf("[NET] host: %s\n", response_buffer);
      const char *emotion_marker = strstr(
          reinterpret_cast<char *>(response_buffer), "\"emotion\":\"");
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
      if (strstr(reinterpret_cast<char *>(response_buffer),
                 "\"type\":\"audio_start\"")) {
        if (es8311_device != nullptr) {
          es8311_voice_mute(es8311_device, false);
        }
        resetPlaybackWav();
      }
      if (strstr(reinterpret_cast<char *>(response_buffer),
                 "\"type\":\"audio_end\"")) {
        finishPlaybackAudio();
        setButtonTestState(ButtonTestState::IDLE);
        break;
      }
    } else if (opcode == 0x2) {
      processPlaybackAudio(response_buffer, response_length);
    } else if (opcode == 0x8) {
      break;
    }
  }
  websocket.close();
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
  i2s.setTimeout(1);

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
  if (!findTextBounds()) {
    Serial.println("Empty subtitle mask");
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

  if (!loadExpression(expressionIndexFor(expression_name))) {
    showFatalError("Button state expression failed");
  }
  Serial.printf("[VOICE KEY] state=%s\n", expression_name);
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
            frame_buffer + static_cast<uint32_t>(PET_Y + row) * LCD_WIDTH + PET_X + column;
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
  memset(frame_buffer, 0, LCD_WIDTH * LCD_HEIGHT * sizeof(uint16_t));
  if (!decodePetFrame(animation_frame)) {
    return false;
  }
  drawSpeechBubble();
  gfx->draw16bitRGBBitmap(0, 0, frame_buffer, LCD_WIDTH, LCD_HEIGHT);
  return true;
}

void setup() {
  Serial.begin(115200);
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
      LCD_WIDTH * LCD_HEIGHT * sizeof(uint16_t),
      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  text_mask = static_cast<uint8_t *>(heap_caps_malloc(
      TEXT_MASK_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  rle_buffer = static_cast<uint8_t *>(heap_caps_malloc(
      RLE_BUFFER_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  if (!frame_buffer || !text_mask || !rle_buffer) {
    showFatalError("Memory allocation failed");
  }

  recording_buffer = static_cast<int16_t *>(heap_caps_malloc(
      AUDIO_MAX_RECORDING_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (!recording_buffer) {
    showFatalError("Audio buffer allocation failed");
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
}

void loop() {
  const uint32_t started_at = millis();
  serviceProvisioningPortal();
  updateUserButton();
  serviceRecording();
  if (button_test_state == ButtonTestState::THINKING &&
      millis() - button_thinking_started_at >= BUTTON_THINKING_PREVIEW_MS) {
    setButtonTestState(ButtonTestState::IDLE);
  }

  // Keep the listening animation moving while the voice key is held.
  // THINKING intentionally remains on its preview frame until the host
  // responds, while IDLE and LISTENING advance through their frame timings.
  if (button_test_state == ButtonTestState::IDLE ||
      button_test_state == ButtonTestState::LISTENING) {
    animation_frame = (animation_frame + 1) % asset_header.frame_count;
  }

  if (!renderCurrentFrame()) {
    showFatalError("Expression decode failed");
  }
  const uint16_t duration = frame_entries[animation_frame].duration_ms;
  const uint32_t render_time = millis() - started_at;
  if (render_time < duration) {
    const uint32_t frame_deadline = started_at + duration;
    while (static_cast<int32_t>(millis() - frame_deadline) < 0) {
      if (updateUserButton()) {
        break;
      }
      serviceRecording();
      delay(1);
    }
  }
}
