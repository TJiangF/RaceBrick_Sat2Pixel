// RaceBrick_Sat2Pixel - ESP32-S3 uploader example
// Two scenarios:
//   1) Track bbox  : a series of GNSS points -> outermost 4 corners -> image
//   2) Live nav    : current position (+heading) -> compressed navigation image
//
// Board: ESP32-S3 with PSRAM (needed for the 240x240 RGB565 buffer).
// Dependencies (Arduino Library Manager):
//   - WiFi, HTTPClient (built-in)
//   - ArduinoJson by Benoit Blanchon

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

const char* WIFI_SSID = "YOUR_SSID";
const char* WIFI_PASS = "YOUR_PASS";
const char* SERVER    = "http://47.100.182.210:2020";

const int    TIMEOUT_MS = 12000;   // first tile fetch can take a few seconds
const int    MAX_RETRIES = 3;
const int    BACKOFF_MS[3] = {1000, 2000, 4000};
const char*  DEVICE_ID = "esp32s3-0001";
uint32_t     seq = 0;

// ------------------------------------------------------------------ buffers
uint16_t* img = nullptr;            // RGB565 framebuffer (PSRAM)
uint16_t  imgW = 0, imgH = 0;

// ------------------------------------------------------- shared HTTP helper
// Returns true and fills `body` with the binary response on HTTP 200.
static bool postJson(const String& url, const String& payload, uint8_t*& body, size_t& len,
                     HTTPClient& http) {
  http.begin(url);
  http.setTimeout(TIMEOUT_MS);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(payload);
  if (code != 200) { http.end(); return false; }
  len = http.getSize();                       // Content-Length (FastAPI sets it)
  if (len == 0 || len == (size_t)-1) { http.end(); return false; }
  body = (uint8_t*)malloc(len);
  if (!body) { http.end(); return false; }
  WiFiClient* s = http.getStreamPtr();
  size_t got = s->readBytes(body, len);
  http.end();
  return got == len;
}

// =====================================================================
// Scenario 1: track -> outermost 4 corners -> satellite image (RGB565)
// =====================================================================
struct Pt { float lng, lat; };

void bboxCorners(const Pt* pts, int n, float out[4][2]) {
  float mnL = pts[0].lng, mxL = pts[0].lng, mnA = pts[0].lat, mxA = pts[0].lat;
  for (int i = 1; i < n; i++) {
    mnL = min(mnL, pts[i].lng); mxL = max(mxL, pts[i].lng);
    mnA = min(mnA, pts[i].lat); mxA = max(mxA, pts[i].lat);
  }
  out[0][0] = mnL; out[0][1] = mnA;   // SW
  out[1][0] = mxL; out[1][1] = mnA;   // SE
  out[2][0] = mxL; out[2][1] = mxA;   // NE
  out[3][0] = mnL; out[3][1] = mxA;   // NW
}

bool uploadOnce(uint32_t s, const float corners[4][2], bool wgs84) {
  StaticJsonDocument<768> doc;
  doc["device_id"] = DEVICE_ID;
  doc["seq"] = s;
  JsonArray arr = doc.createNestedArray("corners");
  for (int i = 0; i < 4; i++) {
    JsonObject c = arr.createNestedObject();
    c["lng"] = corners[i][0];
    c["lat"] = corners[i][1];
  }
  doc["max_size"] = 240;
  doc["format"] = "rgb565";
  doc["coord"] = wgs84 ? "wgs84" : "gcj02";   // GNSS is WGS-84 -> server converts
  String payload; serializeJson(doc, payload);

  HTTPClient http;
  uint8_t* body = nullptr; size_t len = 0;
  bool ok = postJson(String(SERVER) + "/api/v1/satellite", payload, body, len, http);
  if (!ok) return false;

  imgW = http.header("X-Sat2Pixel-Width").toInt();
  imgH = http.header("X-Sat2Pixel-Height").toInt();
  // body is big-endian RGB565 bytes; unpack into uint16 framebuffer
  if (img) free(img);
  img = (uint16_t*)malloc((size_t)imgW * imgH * 2);
  for (size_t i = 0; i < (size_t)imgW * imgH; i++)
    img[i] = ((uint16_t)body[i * 2] << 8) | body[i * 2 + 1];
  free(body);
  return true;
}

void fetchByTrack(const Pt* pts, int n, bool wgs84 = true) {
  float corners[4][2];
  bboxCorners(pts, n, corners);
  uint32_t s = ++seq;
  for (int a = 0; a <= MAX_RETRIES; a++) {
    if (uploadOnce(s, corners, wgs84)) {
      Serial.printf("[bbox %u] OK %ux%u\n", s, imgW, imgH);
      return;
    }
    if (a < MAX_RETRIES) delay(BACKOFF_MS[a]);
  }
  Serial.printf("[bbox %u] failed\n", s);
}

// =====================================================================
// Scenario 2: current position -> compressed nav image (16-color RLE)
// =====================================================================
// Decode the pal16 packet into an RGB565 framebuffer.
//   [w u16 LE][h u16 LE][palCnt u16 LE][palette palCnt*u16 LE][RLE...]
//   RLE: byte = (run-1)<<4 | index; if (run-1)==15 -> next byte = run-16
bool decodeNavPacket(const uint8_t* pkt, size_t len) {
  if (len < 6) return false;
  imgW = pkt[0] | (pkt[1] << 8);
  imgH = pkt[2] | (pkt[3] << 8);
  uint16_t palCnt = pkt[4] | (pkt[5] << 8);
  size_t off = 6;
  if (off + (size_t)palCnt * 2 > len) return false;
  uint16_t pal[16];
  for (uint16_t i = 0; i < palCnt && i < 16; i++, off += 2)
    pal[i] = pkt[off] | (pkt[off + 1] << 8);

  size_t total = (size_t)imgW * imgH, idx = 0;
  if (img) free(img);
  img = (uint16_t*)malloc(total * 2);
  if (!img) return false;
  while (off < len && idx < total) {
    uint8_t v = pkt[off++];
    size_t run = (v >> 4) + 1;
    if ((v >> 4) == 15) { if (off >= len) break; run = 16 + pkt[off++]; }
    uint16_t c = pal[v & 15];
    for (size_t k = 0; k < run && idx < total; k++) img[idx++] = c;
  }
  return idx == total;
}

bool navOnce(uint32_t s, double lng, double lat, float sizeM, float heading, bool wgs84) {
  StaticJsonDocument<512> doc;
  doc["device_id"] = DEVICE_ID;
  doc["seq"] = s;
  doc["lng"] = lng;
  doc["lat"] = lat;
  doc["size_m"] = sizeM;
  doc["heading"] = heading;
  doc["size"] = 240;
  doc["format"] = "pal16";
  doc["coord"] = wgs84 ? "wgs84" : "gcj02";
  String payload; serializeJson(doc, payload);

  HTTPClient http;
  uint8_t* body = nullptr; size_t len = 0;
  bool ok = postJson(String(SERVER) + "/api/v1/nav", payload, body, len, http);
  if (!ok) return false;
  bool good = decodeNavPacket(body, len);
  Serial.printf("[nav %u] %s %ux%u, packet %u bytes\n", s, good ? "OK" : "DECODE-FAIL", imgW, imgH, (unsigned)len);
  free(body);
  return good;
}

void fetchNav(double lng, double lat, float sizeM = 300, float heading = 0, bool wgs84 = true) {
  uint32_t s = ++seq;
  for (int a = 0; a <= MAX_RETRIES; a++) {
    if (navOnce(s, lng, lat, sizeM, heading, wgs84)) return;
    if (a < MAX_RETRIES) delay(BACKOFF_MS[a]);
  }
  Serial.printf("[nav %u] failed\n", s);
}

// =====================================================================
void setup() {
  Serial.begin(115200);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(500);
  Serial.println("WiFi connected");

  // Scenario 1: a sampled track (WGS-84)
  Pt track[] = {
    {121.4325f, 31.1975f}, {121.4350f, 31.1975f},
    {121.4350f, 31.2000f}, {121.4325f, 31.2000f},
  };
  fetchByTrack(track, sizeof(track) / sizeof(track[0]));

  // Scenario 2: live position, heading-up
  fetchNav(121.43375, 31.19875, 300, 45);
}

void loop() {}
