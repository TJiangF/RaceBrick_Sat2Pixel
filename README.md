# RaceBrick_Sat2Pixel

GNSS 设备（ESP32-S3）通过 WiFi 上传坐标 → 服务端拉取高德（AMap）卫星影像 → 按请求尺寸渲染 / 压缩后回传（支持可变尺寸与 zoom）。支持两种使用场景：

| 场景 | 输入 | 输出 | 接口 |
|------|------|------|------|
| **1. 轨迹四角** | 一段轨迹算出的最外侧四点（bbox） | 覆盖该区域的 240×240 卫星图（RGB565） | `POST /api/v1/satellite` |
| **2. 实时导航** | 当前坐标（+ 朝向 + zoom），可变地面范围 | 可变尺寸（16–512）**16 色 + RLE** 导航图 | `POST /api/v1/nav` |

```
ESP32-S3 ──(WiFi, HTTP POST JSON)──> FastAPI ──> 高德/天地图瓦片 ──> 按 size/zoom 缩放裁剪
                                        │
  场景1: 4 角 bbox ────────────────────┼──> RGB565 原始帧 (≈115 KB)
  场景2: 中心点+朝向 ───────────────────┴──> 16 色 + RLE 压缩包 (≈30 KB)
```

**坐标系**：GNSS 输出 WGS-84，高德使用 GCJ-02，服务端自动转换（请求里 `coord:"wgs84"`）。

## 目录结构

```
RaceBrick_Sat2Pixel/
├── server.py               # FastAPI 入口（场景1/2 + 预览台）
├── sat2pixel/
│   ├── mosaic.py           # 高德卫星瓦片拉取 + 拼接 + 裁剪
│   ├── preview.py          # Lanczos 缩放 + RGB565/JPEG/PNG 编码
│   ├── coords.py           # WGS-84 <-> GCJ-02 坐标转换
│   ├── nav.py              # 导航视角几何（中心点/范围/朝向 -> 图像）
│   ├── navpacket.py        # 16 色中位切分 + RLE（导航图压缩）
│   ├── protocol.py         # 请求模型 / 错误码 / 重传策略
│   └── http.py             # 带 CA 兜底的 TLS 请求
├── static/index.html       # 预览台（拖动地图 + 可变尺寸预览）
├── examples/
│   ├── sample_bbox.json
│   └── esp32_upload/esp32_upload.ino   # 两种场景 + 重传 + 包解码示例
└── requirements.txt
```

## 坐标系（`coord`）

所有接口都接受 `coord` 字段：

| 值 | 含义 |
|----|------|
| `gcj02`（默认） | 输入已是高德坐标（网页/高德取点用这个） |
| `wgs84` | 输入是原始 GNSS/GPS 坐标，服务端先转 GCJ-02 再用 |

转换实现见 `sat2pixel/coords.py`（`wgs84_to_gcj02` / `gcj02_to_wgs84`）。

## 图像类型（`layer`）

底图可在 API 上切换，所有接口都接受 `layer` 字段。不同来源**坐标系不同**，服务端按 `layer` 自动转换（`coord` 是输入坐标系）：

| 值 | 底图 | 坐标系 | 说明 |
|----|------|--------|------|
| `satellite` | 高德卫星图（`webst0x…style=6`） | GCJ-02 | 场景 1 默认 |
| `road` | **天地图矢量路网**（`vec_w`，无文字标注） | WGS-84 | 场景 2 默认，像素风最干净 |
| `amap_road` | 高德路网（`webrd0x…style=8`，带路名） | GCJ-02 | 与天地图对照用 |
| `tdt_satellite` | 天地图卫星图（`img_w`） | WGS-84 | 卫星源可选 |

> 天地图提供卫星图（`img_w`）；本项目卫星默认仍走高德，`tdt_satellite` 可选。
> 天地图密钥通过环境变量 `TIANDITU_KEY` 配置，且**必须在该 key 的 IP 白名单里加入服务器公网 IP**，否则返回 `301008 IP不匹配`。天地图 `vec_w` 不含文字标注，像素风更干净（文字需另叠 `cva_w`，本项目未用）。

## 风格（`style`）与主题（`theme`）

`style` 决定渲染方式，`theme` 决定像素风的配色（MotoNav 固件 `MapTheme` 表，RGB565）：

| `style` | 说明 |
|---------|------|
| `pixel`（默认） | **MotoNav 像素风**：4 类语义分类 + 主题平涂色板 |
| `original` | 原色：保留高德原始观感（导航用 16 色加权中位切分 / 框选用 Lanczos 高清） |

| `theme` | 名称 | 街区 / 道路 / 绿地 / 水面 |
|---------|------|----------------------------|
| `gameboy`（默认） | 掌机像素 | `#8CC373` / `#E7FBD6` / `#316952` / `#081821` |
| `original` | 原版 | `#DEDFDE` / `#313031` / `#5ABE5A` / `#4A92DE` |
| `vice` | 罪恶都市 | `#E7DBCE` / `#313039` / `#73C36B` / `#4AC7DE` |
| `gta` | GTA5 | `#394142` / `#9CA2AD` / `#426539` / `#29517B` |
| `cyber` | 赛博霓虹 | `#000408` / `#0096B5` / `#002018` / `#180839` |
| `paper` | 复古纸 | `#EFE3C6` / `#F7BA6B` / `#C6D79C` / `#9CC7D6` |
| `minimal` | 极简黑白 | `#000000` / `#848284` / `#000000` / `#000000` |
| `blueprint` | 蓝图 | `#08285A` / `#DEEBFF` / `#184984` / `#2965AD` |
| `radar` / `compass` / `satellite_alt` | 雷达 / 寻宝罗盘 / 卫星替代 | 见 `pixelstyle.py` |

**像素风算法**（对齐 MotoNav `MapGeo.classifyPixel` / `cleanRaster` / `dilateRoads` + 固件 `MapTheme`）：

1. `backgroundLum`：低饱和像素里出现最多的亮度 = 街区底色；
2. HSV 分类：水体（蓝相 s>0.12）→ 绿地（绿相 s>0.10）→ 道路（黄橙相 s>0.18 或比底色亮 6 以上）→ 其余为街区；
3. 去噪：删除 <40px（道路 <60px）的孤立碎块（图标/文字残留），再 3×3 多数投票平滑；
4. 道路加粗 1px（向街区扩张），小屏更清晰；
5. 用主题色板平涂（GameBoy 主题即 `#8CC373` 底 + 浅绿道路，呈现掌机像素风）。

像素风是大色块，RLE 极其高效（240×240 路网图 **≈1.7 KB**，对比原色 16 色 ≈17 KB、RGB565 115 KB）。

---

## 场景 1：轨迹四角 → 图像

设备把一段轨迹的**最外侧四点**（外接 bbox）发来，服务端回传该区域的图。

`POST /api/v1/satellite`：

```json
{
  "device_id": "esp32s3-0001",
  "seq": 1042,
  "corners": [
    {"lng": 121.4325, "lat": 31.1975},
    {"lng": 121.4350, "lat": 31.1975},
    {"lng": 121.4350, "lat": 31.2000},
    {"lng": 121.4325, "lat": 31.2000}
  ],
  "max_size": 240,
  "format": "rgb565",
  "coord": "wgs84"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `device_id` | string | 是 | 设备唯一标识（幂等键之一） |
| `seq` | uint32 | 是 | 单调递增（幂等键之一，重传去重） |
| `corners` | array[4] | 是 | 四点坐标（SW/SE/NE/NW，任意顺序均可，取 min/max） |
| `max_size` | int | 否 | 输出边长，默认 240 |
| `format` | string | 否 | `rgb565`（默认）/ `png` / `jpeg` |
| `coord` | string | 否 | `gcj02`（默认）/ `wgs84` |
| `layer` | string | 否 | `satellite`（默认）/ `road` |

> 为适配方形屏幕，服务端会把 bbox **外接成以中心为原点的正方形**（边长取宽/高中的较大者），保证四点一定在画面内，再缩放为 240×240。

**响应**：`HTTP 200`，body 为图像数据，关键信息在响应头：

| 响应头 | 说明 |
|--------|------|
| `X-Sat2Pixel-Seq` | 回显 seq |
| `X-Sat2Pixel-Width` / `Height` | 图像尺寸 |
| `X-Sat2Pixel-Size` | 字节数 |

`rgb565` 格式：每像素 2 字节**大端**，`R5 G6 B5`：`[RRRRRGGG][GGGBBBBB]`。

---

## 场景 2：实时导航 → 压缩图

设备发送**当前坐标 + 目标尺寸 + zoom**，服务端截取以该点为中心的正方形并压缩回传。输出尺寸可变（方形 `size`，非方形 `width`+`height`，各 16–512），`zoom` 只改变**地面覆盖范围**（有效宽度 = `size_m / zoom`，最大 8,000 km），不改变像素尺寸。设备全屏导航会请求 `320×240`，最小 zoom 档约 6,553 km 宽，可看中国全图。支持三种图：**风格化路网 / 卫星 / 融合**，用 `mode` 一步选定。完整协议见 [docs/API.md](docs/API.md)。

`POST /api/v1/nav`：

```json
{
  "device_id": "esp32s3-0001",
  "seq": 2050,
  "lng": 121.43375,
  "lat": 31.19875,
  "size_m": 300,
  "heading": 45,
  "size": 240,
  "width": 240,
  "height": 240,
  "zoom": 1.5,
  "format": "pal16",
  "coord": "wgs84"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `device_id` / `seq` | | 是 | 同上（幂等重传） |
| `lng` / `lat` | float | 是 | 当前坐标 |
| `size_m` | float | 否 | 基准地面边长（米），默认 300；实际宽度 = `size_m / zoom` |
| `heading` | float | 否 | 朝向（度，正北顺时针）；`0`=正北朝上，其它值旋转成「朝向朝上」 |
| `size` | int | 否 | **可变**方形输出边长（16–512），默认 240 |
| `width` / `height` | int | 否 | 可选非方形输出（各 16–512）；全屏用 `320×240` |
| `zoom` | float | 否 | 缩放倍率（1e-6–8.0），>1 放大、<1 缩小；默认 1.0；最小档可看中国全图 |
| `format` | string | 否 | `pal16`（默认，压缩）/ `rgb565` / `png` / `jpeg` |
| `coord` | string | 否 | `gcj02`（默认）/ `wgs84` |
| `layer` | string | 否 | `road`（默认）/ `satellite` |
| `style` | string | 否 | `pixel`（默认，像素风）/ `original` |
| `theme` | string | 否 | 像素风主题，默认 `gameboy`（掌机像素） |
| `mode` | string | 否 | `auto`（默认，用 layer/style）/ `road`（风格化路网）/ `satellite`（卫星）/ `blend`（融合）|
| `sat_opacity` | int | 否 | 融合时卫星透明度 0–100（0=路网，100=卫星），默认 100 |

**响应头**（`pal16`）：`X-Nav-Width/Height`、`X-Nav-Zoom`（内部瓦片级）、`X-Nav-Zoom-Factor`（请求倍率）、`X-Nav-Span-M`（实际地面宽度）、`X-Nav-Size`、`X-Nav-Palette`（调色板 RGB565）、`X-Nav-Raw-Rgb565`、`X-Nav-Style`。客户端应按 `Width/Height` 解码，不要写死 240。

### 导航图压缩算法（参考 zyn950929/MotoNav）

完全对齐参考项目的 **`OriginalMapColors.java`（加权中位切分）** + **`MapGeo.encodeRaster`（RLE）**：

1. **量化直方图**：每像素 RGB 量化到 5-5-5（15 bit），统计 32768 色直方图。
2. **加权中位切分**：初始 1 个色盒；反复挑「得分 = 色域跨度 × √(像素数)」最大的盒子，沿其跨度最大的通道排序，在**加权中位数**处切成两半；直到 16 个盒子。
3. **调色板**：每个盒子取加权平均色，打包为 16 个 **RGB565**。
4. **索引**：每个像素映射到所属盒子编号（0–15）。
5. **RLE**：按行游程编码，`1 字节 = (run-1)<<4 | 色号`；`run-1 == 15` 时后跟一字节表示 `run-16`（最长 271）。

优点：只用 16 色即可保留导航底图主要结构，RLE 进一步压缩；设备端**无需任何图像解码库**，展开调色板即可上屏。

**压缩包 `pal16` 二进制格式（小端）**：

| 偏移 | 大小 | 字段 |
|------|------|------|
| 0 | 2 | width（uint16 LE） |
| 2 | 2 | height（uint16 LE） |
| 4 | 2 | palette count = 16（uint16 LE） |
| 6 | 32 | 16 × RGB565（uint16 LE） |
| 38 | N | RLE 数据 |

**实测**（240×240，`size_m=300`，`pal16` vs RGB565 115 KB）：路网图 ≈ **17 KB（≈6.8×）**，卫星图 ≈ **30 KB（≈3.8×）**。尺寸越大包越大；服务端最多按 1024 px 拉源图再缩放，避免大尺寸请求拖垮服务。

---

## 失败重传机制（两个接口通用）

- **超时**：单次等待 `5s`（建议设备端设 12s，首次拉瓦片较慢）。
- **重传**：超时/网络错误时用**相同 `seq`** 重发，最多 3 次（共 4 次），退避 `1s/2s/4s`。
- **幂等去重**：服务端以 `(device_id, seq)` 缓存结果 10 分钟，重传直接命中缓存，不重复拉瓦片。

错误返回 JSON：`{"code":"TILE_FETCH_FAILED","message":"..."}`，错误码 `INVALID_JSON` / `INVALID_CORNERS`(422) / `TILE_FETCH_FAILED`。

## 预览台（交互式网页）

访问 `http://<服务器>:2020/`，两种场景都能直接调试：

- **图像类型**：卫星图 / 路网图一键切换（底图与预览同步切换）。
- **风格**：像素风（4 色平涂，MotoNav 观感）/ 原色（保留高德原始观感）实时对比。
- **场景 ①「框选区域」**：拖动地图，白色方框内区域实时压缩成 240×240；可调最大边长/缩放算法/锐化/格式/JPEG 质量，地图上叠加结果并可调透明度。
- **场景 ②「实时导航」**：地图中心十字=当前位置，方框=导航范围；可调地面边长 `size_m`、朝向 `heading`、输出尺寸、格式（PAL16/RGB565/PNG/JPEG）；侧栏实时显示 16 色+RLE 压缩结果与压缩比（页面内置 PAL16/RGB565 解码）。
- `+/-` 按钮连续缩放；地址搜索带联想。

## 效果示例（学校区域 ~250 m）

| 项目 | 值 |
|------|-----|
| 瓦片 zoom | 18 |
| 场景1 输出（RGB565） | 240×240，115,200 B（卫星/路网同尺寸） |
| 场景2 输出 `pal16`（路网·像素风） | 240×240，**≈1.7 KB（≈68×）** |
| 场景2 输出 `pal16`（路网·原色） | 240×240，≈17 KB（≈6.8×） |
| 场景2 输出 `pal16`（卫星·原色） | 240×240，≈30 KB（≈3.8×） |

## 部署

```bash
git clone git@github.com:TJiangF/RaceBrick_Sat2Pixel.git
cd RaceBrick_Sat2Pixel
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export AMAP_WEB_KEY=your_amap_web_key
export TIANDITU_KEY=your_tianditu_key   # 路网图(天地图)需要；key 的 IP 白名单要含本机公网 IP
uvicorn server:app --host 0.0.0.0 --port 2020
```

> 放行防火墙 2020；两个设备接口与预览台同进程共用。

## 本地验证

```bash
# 场景1：轨迹四角（WGS-84）
curl -X POST http://127.0.0.1:2020/api/v1/satellite -H 'Content-Type: application/json' \
  -d '{"device_id":"d","seq":1,"corners":[{"lng":121.4325,"lat":31.1975},{"lng":121.4350,"lat":31.1975},{"lng":121.4350,"lat":31.2000},{"lng":121.4325,"lat":31.2000}],"format":"rgb565","coord":"wgs84"}' \
  -o s1.rgb565 -D -

# 场景2：实时导航（16 色 RLE）
curl -X POST http://127.0.0.1:2020/api/v1/nav -H 'Content-Type: application/json' \
  -d '{"device_id":"d","seq":2,"lng":121.43375,"lat":31.19875,"size_m":300,"size":240,"zoom":1.5,"heading":45,"format":"pal16","coord":"wgs84"}' \
  -o nav.pal16 -D -
```

## 许可

MIT
