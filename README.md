# RaceBrick_Sat2Pixel

GNSS 设备（ESP32）上传四个边界点坐标 → 服务端拉取高德（AMap）卫星影像 → 高效压缩到 ≤240×240 并尽可能保留细节后回传。

```
ESP32 ──(WiFi, HTTP POST)──> FastAPI 服务 ──(瓦片拼接)──> 高德卫星图
                                  │
                                  ├──(Lanczos 缩放到 ≤240×240)──> 调试台 /api/preview
                                  └──(RGB565 / JPEG / PNG)──────> 回传设备
```

## 目录结构

```
RaceBrick_Sat2Pixel/
├── server.py               # FastAPI HTTP 服务入口（协议 + 预览台）
├── sat2pixel/
│   ├── mosaic.py           # 高德卫星瓦片拉取 + 拼接 + 裁剪（自动 zoom / 空白降级）
│   ├── preview.py          # 高质量缩放（Lanczos/锐化）+ RGB565/JPEG/PNG 编码
│   ├── quantize.py         # 四色量化 + 2bpp 打包（旧方案，保留）
│   ├── protocol.py         # 协议定义（请求模型 / 二进制格式 / 错误码）
│   └── http.py             # 带 CA 兜底的 TLS 请求
├── static/
│   └── index.html          # 预览台（可拖动地图 + 压缩参数 + 240×240 预览）
├── examples/
│   ├── sample_bbox.json            # 示例请求（学校大小区域）
│   └── esp32_upload/esp32_upload.ino  # ESP32 上传 + 重传示例
└── requirements.txt
```

## 交互协议

### 请求（ESP32 → 服务）

`POST /api/v1/satellite`，`Content-Type: application/json`：

```json
{
  "device_id": "esp32-0001",
  "seq": 1042,
  "corners": [
    {"lng": 121.4325, "lat": 31.1975},
    {"lng": 121.4350, "lat": 31.1975},
    {"lng": 121.4350, "lat": 31.2000},
    {"lng": 121.4325, "lat": 31.2000}
  ],
  "zoom": null,
  "palette": "adaptive",
  "format": "bin"
}
```

| 字段        | 类型     | 必填 | 说明                                                       |
|-------------|----------|------|------------------------------------------------------------|
| `device_id` | string   | 是   | 设备唯一标识，1–64 字符                                     |
| `seq`       | uint32   | 是   | 每设备单调递增，作为幂等键（用于重传去重）                    |
| `corners`   | array[4] | 是   | 四个最边缘点坐标，顺序固定为 **SW, SE, NE, NW**（西南、东南、东北、西北） |
| `zoom`      | int      | 否   | `2–18`，`null` 为自动选择（推荐）                           |
| `palette`   | string   | 否   | 四色策略：`adaptive`（自适应，默认）/ `gray4`（固定四级灰度） |
| `format`    | string   | 否   | 回传格式：`bin`（默认，2bpp 原始）/ `png`（四色索引 PNG）   |

### 响应（服务 → ESP32）

成功返回 `HTTP 200`，响应体为图像数据；关键信息放在响应头，避免 ESP32 解析 JSON：

| 响应头                | 说明                                  |
|-----------------------|---------------------------------------|
| `X-Sat2Pixel-Seq`     | 回显 `seq`                             |
| `X-Sat2Pixel-Width`   | 图像宽（像素）                          |
| `X-Sat2Pixel-Height`  | 图像高（像素）                          |
| `X-Sat2Pixel-Zoom`    | 实际使用的瓦片 zoom                       |
| `X-Sat2Pixel-Tiles`   | 下载的瓦片数量                           |
| `X-Sat2Pixel-Palette` | 四色色板 `#RRGGBB,#RRGGBB,#RRGGBB,#RRGGBB` |

失败返回非 200，响应体为 JSON：

```json
{"code": "TILE_FETCH_FAILED", "message": "..."}
```

错误码：`INVALID_JSON` / `INVALID_CORNERS`（422，附带字段校验信息）/ `TILE_FETCH_FAILED` / `INTERNAL`。

### `bin` 二进制格式（2 bits/pixel）

| 偏移 | 大小 | 字段                                    |
|------|------|-----------------------------------------|
| 0    | 2    | `width`（uint16 大端）                    |
| 2    | 2    | `height`（uint16 大端）                   |
| 4    | 12   | 色板：4 个颜色，每个 RGB888（3 字节）      |
| 16   | N    | 像素：每像素 2 bit，行主序，每行补齐到字节边界 |

`N = height * ceil(width * 2 / 8)`。第 `i` 个像素值（0–3）为该行第 `i/4` 字节中从高位起的 2 bit。

### 失败重传机制

- **超时**：单次请求等待 `TIMEOUT = 5s`。
- **重传**：超时/网络错误时，用**相同的 `seq`** 重发，最多 `MAX_RETRIES = 3` 次（共 4 次）。
- **退避**：`1s / 2s / 4s` 指数退避。
- **幂等去重**：服务端以 `(device_id, seq)` 为键缓存结果，TTL 10 分钟。若上次已处理但响应丢失，重传直接命中缓存返回同一结果，不会重复拉取瓦片、也不会重复扣资源。

## 预览台（交互式网页）

启动后访问 `http://<服务器>:2020/`，可直接：

- 输入**地址**搜索定位，**拖动地图 / 滚轮缩放**；
- 实时把当前视野的卫星图**压缩到 ≤240×240**，叠加显示 + 侧栏像素级预览；
- 调整压缩参数（目标尺寸 / 缩放算法 / 锐化 / 格式 / JPEG 质量），页面实时重算；
- 侧栏对比 **PNG / JPEG / RGB565** 三种编码的体积，可一键下载。

### 压缩方法（保留细节）

1. 瓦片拼接出高分辨率卫星图（比目标尺寸大 2 倍左右，供抗锯齿）；
2. **Lanczos 缩放**到 ≤240×240（保持宽高比，默认最清晰），可选 Box/Bilinear/Nearest；
3. 可选**轻锐化**（unsharp mask）恢复边缘对比度；
4. 编码：
   - `PNG`：无损，约 100 KB；
   - `JPEG`：体积最小，240×240 约 15–20 KB（质量可调）；
   - `RGB565`：每像素 2 字节，MCU 屏幕直接写帧缓冲，无需解码（240×240 = 115 KB）。

### 预览相关接口

| 接口                        | 说明                                        |
|-----------------------------|---------------------------------------------|
| `GET  /api/geocode?address=` | 地址 → 经纬度（需配置 `AMAP_WEB_KEY`）       |
| `POST /api/preview`          | 输入 `{corners, zoom, max_size, method, sharpen, format, quality}`，返回压缩图；响应头带 `X-Preview-Width/Height/Png/Jpeg/Rgb565` |

## 效果示例（学校大小区域，约 250 m × 250 m）

坐标：上海交通大学徐汇校区附近 `(121.4325, 31.1975)–(121.4350, 31.2000)`。

| 项目                 | 值                                            |
|----------------------|-----------------------------------------------|
| 瓦片 zoom            | 18（12 张瓦片拼接）                             |
| 原始卫星图           | 466 × 544，RGB PNG 386 KB                      |
| 压缩输出             | 206 × 240（Lanczos，最长边 ≤240）               |
| PNG（无损）          | 105 KB                                        |
| JPEG（质量 85）      | 17.5 KB                                       |
| RGB565（MCU 直显）   | 98.9 KB（206×240×2 字节）                       |

> 对比图：`docs/sat2pixel_compare.png`（四色压缩，旧方案）。

## 部署

服务端（阿里云轻量应用服务器）单向拉取仓库后运行：

```bash
# 使用 deploy key 拉取（只读）
git clone git@github.com:TJiangF/RaceBrick_Sat2Pixel.git
cd RaceBrick_Sat2Pixel
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 地址搜索需要高德 Web 服务 Key
export AMAP_WEB_KEY=your_amap_web_key

# 调试台（网页）监听 2020
uvicorn server:app --host 0.0.0.0 --port 2020
```

> 注意：需要放行防火墙 2020 端口（`ufw allow 2020/tcp`）；ESP32 生产上传仍走 `/api/v1/satellite`，可与调试台同进程共用。

## 本地验证

```bash
# 生产协议
curl -X POST http://127.0.0.1:2020/api/v1/satellite \
  -H 'Content-Type: application/json' \
  -d @examples/sample_bbox.json \
  -o out.bin -D -

# 调试接口
curl -X POST http://127.0.0.1:2020/api/debug \
  -H 'Content-Type: application/json' \
  -d '{"corners":[{"lng":121.4325,"lat":31.1975},{"lng":121.4350,"lat":31.1975},{"lng":121.4350,"lat":31.2000},{"lng":121.4325,"lat":31.2000}],"zoom":18,"params":{"edge_threshold":15}}' \
  -o debug.png -D -
```

## 许可

MIT
