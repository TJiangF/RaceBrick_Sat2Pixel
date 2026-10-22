# RaceBrick_Sat2Pixel

GNSS 设备（ESP32）上传四个边界点坐标 → 服务端拉取高德（AMap）卫星影像 → 四色（2-bit）压缩后回传。

```
ESP32 ──(WiFi, HTTP POST)──> FastAPI 服务 ──(瓦片拼接)──> 高德卫星图 ──(四色量化)──> 2-bit 图像回传
```

## 目录结构

```
RaceBrick_Sat2Pixel/
├── server.py               # FastAPI HTTP 服务入口
├── sat2pixel/
│   ├── mosaic.py           # 高德卫星瓦片拉取 + 拼接 + 裁剪
│   ├── quantize.py         # 四色量化 + 2bpp 打包
│   └── protocol.py         # 协议定义（请求模型 / 二进制格式 / 错误码）
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

## 四色压缩

卫星图先量化到 4 色（每像素 2 bit），两种策略：

- **`adaptive`**：对图像自身主色做中位切割（median-cut），保留最大信息量，适合区分植被/建筑/道路/水体。
- **`gray4`**：固定四级灰度 `#FFFFFF / #AAAAAA / #555555 / #000000`，适合墨水屏类显示。

## 效果示例（学校大小区域，约 250 m × 250 m）

坐标：上海交通大学徐汇校区附近 `(121.4325, 31.1975)–(121.4350, 31.2000)`。

| 项目                 | 值                                            |
|----------------------|-----------------------------------------------|
| 自动 zoom            | 18（12 张瓦片拼接）                             |
| 原始卫星图           | 466 × 544，RGB PNG 386 KB（原始 RGB ≈ 760 KB） |
| 四色自适应色板       | `#556151 #28352E #2F562E #50553A`              |
| 四色 PNG             | 26.9 KB（≈ 14:1）                              |
| 2bpp `bin`（ESP32 用）| 63.6 KB（≈ 12:1，无 PNG 解码开销）              |

> 参考对比图：[docs/sat2pixel_compare.png](docs/sat2pixel_compare.png)（左：原始卫星图，右：四色压缩图）。

## 部署

服务端（阿里云轻量应用服务器）单向拉取仓库后运行：

```bash
# 使用 deploy key 拉取（只读）
git clone git@github.com:TJiangF/RaceBrick_Sat2Pixel.git
cd RaceBrick_Sat2Pixel
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 监听 8000 端口（与 ESP32 上传端口一致）
uvicorn server:app --host 0.0.0.0 --port 8000
```

> 注意：需要放行防火墙 8000 端口（`ufw allow 8000/tcp`）。

## 本地验证

```bash
curl -X POST http://127.0.0.1:8000/api/v1/satellite \
  -H 'Content-Type: application/json' \
  -d @examples/sample_bbox.json \
  -o out.bin -D -
```

## 许可

MIT
