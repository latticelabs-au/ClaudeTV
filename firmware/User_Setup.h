// User_Setup.h — GeekMagic SmallTV-Ultra (ESP8266 / ESP-12F, ST7789V 240x240)
// ESP8266 hardware SPI: MOSI=GPIO13, SCLK=GPIO14 (fixed, not defined here).

#define USER_SETUP_ID 999

#define ST7789_DRIVER
#define TFT_WIDTH  240
#define TFT_HEIGHT 240
#define TFT_INVERSION_ON          // IPS panel needs inversion (matches our working black bg)

#define TFT_CS   15
#define TFT_DC    0
#define TFT_RST   2
// Backlight (GPIO5) is PWM'd by the sketch — not managed by TFT_eSPI.

#define LOAD_GLCD
#define LOAD_FONT2
#define LOAD_FONT4
#define LOAD_GFXFF
#define SMOOTH_FONT

#define SPI_FREQUENCY 27000000
