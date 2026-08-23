#pragma once

#define LCD_SDIO0 4
#define LCD_SDIO1 5
#define LCD_SDIO2 6
#define LCD_SDIO3 7
#define LCD_SCLK 38
#define LCD_RESET 39
#define LCD_CS 12
#define LCD_WIDTH 480
#define LCD_HEIGHT 480

#define IIC_SDA 15
#define IIC_SCL 14

// The factory Key3 button is pulled high and shorts GPIO18 to GND when pressed.
#define USER_BUTTON_PIN 18
#define USER_BUTTON_ACTIVE LOW

// Onboard ES7210 microphone ADC, shared I2S clock with ES8311.
#define ES7210_I2S_PORT I2S_NUM_1
#define ES7210_MCLK_PIN 42
#define ES7210_BCLK_PIN 9
#define ES7210_LRCK_PIN 45
#define ES7210_DATA_IN_PIN 10
#define ES7210_I2C_ADDRESS 0x40
#define ES8311_DATA_OUT_PIN 8
#define ES8311_PA_CTRL_PIN 46
#define ES8311_I2C_ADDRESS 0x18
