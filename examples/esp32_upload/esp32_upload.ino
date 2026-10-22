// RaceBrick_Sat2Pixel - ESP32 uploader example
// Connects to WiFi, POSTs a 4-corner GNSS bounding box, retries on failure,
// and receives a 4-color (2-bit) satellite image in .bin format.
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
const char* SERVER_URL = "http://47.100.182.210:8000/api/v1/satellite";

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

uint8_t* imgBuf = nullptr;
size_t imgLen = 0;
uint16_t imgW = 0, imgH = 0;
uint32_t palette[4] = {0};

bool uploadOnce(uint32_t s) {
  // Build request JSON
  StaticJsonDocument<512> doc;
  doc["device_id"] = DEVICE_ID;
  doc["seq"] = s;
  JsonArray corners = doc.createNestedArray("corners");
  for (int i = 0; i < 4; i++) {
    JsonObject c = corners.createNestedObject();
    c["lng"] = CORNERS[i][0];
    c["lat"] = CORNERS[i][1];
  }
  doc["zoom"] = nullptr;   // auto
  doc["palette"] = "adaptive";
  doc["format"] = "bin";

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

  // Parse response headers
  imgW = http.header("X-Sat2Pixel-Width").toInt();
  imgH = http.header("X-Sat2Pixel-Height").toInt();
  String palStr = http.header("X-Sat2Pixel-Palette");  // "#RRGGBB,..."
  for (int i = 0; i < 4; i++) {
    palette[i] = (uint32_t)strtol(palStr.substring(i * 7 + 1, i * 7 + 7).c_str(),
                                  nullptr, 16);
  }

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

// Decode the 2bpp .bin payload (header 16 bytes, then 2 bits/pixel)
uint8_t pixelAt(const uint8_t* buf, uint16_t x, uint16_t y) {
  uint16_t rowBytes = (imgW + 3) / 4;
  const uint8_t* row = buf + 16 + (size_t)y * rowBytes;
  return (row[x / 4] >> (6 - (x % 4) * 2)) & 0x03;
}

void setup() {
  Serial.begin(115200);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(500);
  Serial.println("WiFi connected");
  fetchSatellite();
}

void loop() {}
