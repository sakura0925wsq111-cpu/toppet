#pragma once

#include <Arduino.h>
#include <Wire.h>

// Minimal ES7210 ADC bring-up for the Waveshare ESP32-S3-Touch-AMOLED-2.16.
// The register sequence follows the first-party Waveshare/ Espressif Arduino
// example, reduced to the fixed 16 kHz / 16-bit slave-I2S configuration used
// by TopPet.
bool es7210Begin(TwoWire &wire);
bool es7210Start();
bool es7210Stop();
uint8_t es7210ReadRegister(uint8_t address);
