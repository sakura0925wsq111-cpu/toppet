/*
 * ES7210 register setup derived from the ESPRESSIF MIT-licensed ES7210
 * Arduino driver shipped in Waveshare's ESP32-S3-Touch-AMOLED-2.16 example.
 */

#include "es7210_min.h"

#include "pin_config.h"

namespace {

TwoWire *codec_wire = nullptr;
uint8_t clock_off_value = 0;

bool writeRegister(uint8_t address, uint8_t value) {
  codec_wire->beginTransmission(ES7210_I2C_ADDRESS);
  codec_wire->write(address);
  codec_wire->write(value);
  return codec_wire->endTransmission() == 0;
}

uint8_t readRegister(uint8_t address) {
  codec_wire->beginTransmission(ES7210_I2C_ADDRESS);
  codec_wire->write(address);
  if (codec_wire->endTransmission(false) != 0 ||
      codec_wire->requestFrom(ES7210_I2C_ADDRESS, static_cast<size_t>(1)) != 1) {
    return 0xff;
  }
  return codec_wire->read();
}

bool updateRegister(uint8_t address, uint8_t mask, uint8_t value) {
  const uint8_t current = readRegister(address);
  if (current == 0xff) {
    return false;
  }
  return writeRegister(address, (current & ~mask) | (value & mask));
}

bool selectAllMicrophones() {
  bool ok = true;
  ok &= writeRegister(0x4b, 0xff);
  ok &= writeRegister(0x4c, 0xff);

  // MIC1/MIC2 are the onboard microphone pair used by the board example.
  ok &= updateRegister(0x01, 0x0b, 0x00);
  ok &= writeRegister(0x4b, 0x00);
  ok &= updateRegister(0x43, 0x10, 0x10);
  ok &= updateRegister(0x44, 0x10, 0x10);

  // Keep the second ADC pair enabled as in the vendor driver; the I2S
  // receiver below selects the first data slot for the mono PCM stream.
  ok &= updateRegister(0x01, 0x15, 0x00);
  ok &= writeRegister(0x4c, 0x00);
  ok &= updateRegister(0x45, 0x10, 0x10);
  ok &= updateRegister(0x46, 0x10, 0x10);

  // 0 dB gain on all four ADC channels for the first electrical test.
  ok &= updateRegister(0x43, 0x0f, 0x00);
  ok &= updateRegister(0x44, 0x0f, 0x00);
  ok &= updateRegister(0x45, 0x0f, 0x00);
  ok &= updateRegister(0x46, 0x0f, 0x00);
  return ok;
}

}  // namespace

bool es7210Begin(TwoWire &wire) {
  codec_wire = &wire;
  bool ok = true;

  // Reset and power/clock defaults.
  ok &= writeRegister(0x00, 0xff);
  delay(5);
  ok &= writeRegister(0x00, 0x41);
  ok &= writeRegister(0x01, 0x1f);
  ok &= writeRegister(0x09, 0x30);
  ok &= writeRegister(0x0a, 0x30);
  ok &= writeRegister(0x40, 0xc3);
  ok &= writeRegister(0x41, 0x70);
  ok &= writeRegister(0x42, 0x70);

  // MCLK=4.096 MHz (256 x 16 kHz), ADC clock divider and LRCK=16 kHz.
  ok &= writeRegister(0x07, 0x20);
  ok &= writeRegister(0x02, 0xc1);
  ok &= writeRegister(0x04, 0x01);
  ok &= writeRegister(0x05, 0x00);

  // Normal I2S format, 16-bit samples; ADC1/2 are routed to SDOUT1.
  ok &= writeRegister(0x11, 0x60);
  ok &= writeRegister(0x12, 0x00);
  ok &= selectAllMicrophones();

  clock_off_value = readRegister(0x01);
  if (clock_off_value == 0xff) {
    ok = false;
  }
  return ok;
}

bool es7210Start() {
  if (codec_wire == nullptr) {
    return false;
  }
  bool ok = true;
  ok &= writeRegister(0x01, clock_off_value);
  ok &= writeRegister(0x06, 0x00);
  ok &= writeRegister(0x47, 0x00);
  ok &= writeRegister(0x48, 0x00);
  ok &= writeRegister(0x49, 0x00);
  ok &= writeRegister(0x4a, 0x00);
  ok &= writeRegister(0x4b, 0x00);
  ok &= writeRegister(0x4c, 0x00);
  return ok;
}

bool es7210Stop() {
  if (codec_wire == nullptr) {
    return false;
  }
  bool ok = true;
  ok &= writeRegister(0x47, 0xff);
  ok &= writeRegister(0x48, 0xff);
  ok &= writeRegister(0x49, 0xff);
  ok &= writeRegister(0x4a, 0xff);
  ok &= writeRegister(0x4b, 0xff);
  ok &= writeRegister(0x4c, 0xff);
  ok &= writeRegister(0x01, 0x7f);
  ok &= writeRegister(0x06, 0x07);
  return ok;
}

uint8_t es7210ReadRegister(uint8_t address) {
  return readRegister(address);
}
