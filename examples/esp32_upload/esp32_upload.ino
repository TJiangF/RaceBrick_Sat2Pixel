// RaceBrick_Sat2Pixel - ESP32 uploader example
// Connects to WiFi, POSTs a 4-corner GNSS bounding box, retries on failure,
// and receives a <=240x240 satellite image (RGB565 by default).
//
// Dependencies (Arduino Library Manager):
//   - WiFi (built-in)
//   - HTTPClient (built-in)
//   - ArduinoJson by Benoit Blanchon

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

const char* WIFI_SSID = "YOUR_SSID";
const char* WIFI_PASS = "YOUR_PASS";

// Service endpoint (replace with your server address)
const char* SERVER_URL = "http://47.100.182.210:2020/api/v1/satellite";

// Retransmission policy (must match server README)
const int TIMEOUT_MS = 5000;
const int MAX_RETRIES = 3;
const int BACKOFF_MS[3] = {1000, 2000, 4000};

// Device identity + monotonic sequence
const char* DEVICE_ID = "esp32-0001";
uint32_t seq = 0;

// Bounding box: SW, SE, NE, NW
const float CORNERS[4][2] = {
  {121.4325f, 31.1975f},
  {121.4350f, 31.1975f},
  {121.4350f, 31.2000f},
  {121.4325f, 31.2000f},
};

// Output image (RGB565, big-endian bytes: [hi, lo] per pixel)
uint8_t* imgBuf = nullptr;
size_t imgLen = 0;
uint16_t imgW = 0, imgH = 0;

bool uploadOnce(uint32_t s) {
  StaticJsonDocument<512> doc;
  doc["device_id"] = DEVICE_ID;
  doc["seq"] = s;
  JsonArray corners = doc.createNestedArray("corners");
  for (int i = 0; i < 4; i++) {
    JsonObject c = corners.createNestedObject();
    c["lng"] = CORNERS[i][0];
    c["lat"] = CORNERS[i][1];
  }
  doc["max_size"] = 240;
  doc["format"] = "rgb565";   // or "png" / "jpeg"

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.begin(SERVER_URL);
  http.setTimeout(TIMEOUT_MS);
  http.addHeader("Content-Type", "application/json");

  int code = http.POST(body);
  if (code != 200) {
    http.end();
    return false;
  }

  imgW = http.header("X-Sat2Pixel-Width").toInt();
  imgH = http.header("X-Sat2Pixel-Height").toInt();
  imgLen = http.getSize();

  if (imgBuf) free(imgBuf);
  imgBuf = (uint8_t*)malloc(imgLen);
  WiFiClient* stream = http.getStreamPtr();
  stream->readBytes(imgBuf, imgLen);
  http.end();
  return true;
}

void fetchSatellite() {
  uint32_t s = ++seq;
  for (int attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    if (uploadOnce(s)) {
      Serial.printf("[%u] OK %dx%d, %u bytes\n", s, imgW, imgH, imgLen);
      return;
    }
    Serial.printf("[%u] attempt %d failed, retry in %dms\n",
                  s, attempt + 1, attempt < MAX_RETRIES ? BACKOFF_MS[attempt] : 0);
    if (attempt < MAX_RETRIES) delay(BACKOFF_MS[attempt]);
  }
  Serial.printf("[%u] giving up\n", s);
}

// Decode a pixel from the RGB565 buffer (big-endian bytes: [hi, lo] per pixel)
uint16_t pixelAt(uint16_t x, uint16_t y) {
  size_t i = ((size_t)y * imgW + x) * 2;
  return ((uint16_t)imgBuf[i] << 8) | imgBuf[i + 1];
}

// Example: unpack RGB565 -> RGB888 (for SPI displays that want RGB888)
void rgb565To888(uint16_t c, uint8_t& r, uint8_t& g, uint8_t& b) {
  r = (c >> 11) & 0x1F; r = (r << 3) | (r >> 2);
  g = (c >> 5) & 0x3F;  g = (g << 2) | (g >> 4);
  b = c & 0x1F;         b = (b << 3) | (b >> 2);
}

void setup() {
  Serial.begin(115200);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(500);
  Serial.println("WiFi connected");
  fetchSatellite();
}

void loop() {}
