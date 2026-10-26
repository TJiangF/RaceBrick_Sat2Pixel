# RaceBrick_Sat2Pixel — API / 协议手册

- 仓库：https://github.com/TJiangF/RaceBrick_Sat2Pixel
- 服务：`http://47.100.182.210:2020`（直连）或 `https://racebrick.tfworks.cn`（nginx 443 → 127.0.0.1:2020）
- 调试台：`GET /`
- 鉴权：目前无。HTTPS 场景 ESP32 需内置 CA `ISRG Root X1`。

## 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/satellite` | 场景①：轨迹四角 bbox → 图像 |
| POST | `/api/v1/nav` | 场景②：当前位置 → 导航图（路网/卫星/融合） |
| POST | `/api/preview` | 调试台框选预览（同参数风格） |
| GET | `/api/geocode?address=&datum=` | 地址 → 经纬度 |
| GET | `/api/inputtips?keywords=&datum=` | 地址联想 |
| GET | `/api/tile?layer=&z=&x=&y=` | 瓦片代理（网页用） |
| GET | `/api/convert?lng=&lat=&src=&dst=` | GCJ-02 ↔ WGS-84 |
| GET | `/healthz` | 健康检查 |

## 场景②：`POST /api/v1/nav`（三种图，可变尺寸 + zoom）

输出尺寸可变：方形用 `size`（16–512 px），非方形用 `width` + `height`
（各 16–512）。设备普通导航请求 240×240，全屏导航请求 320×240。
`zoom` 控制**地面覆盖范围**，画面像素尺寸不变：

- `zoom = 1.0`：地面宽度 = `size_m`
- `zoom > 1.0`：放大（地面范围变小、细节更大）
- `zoom < 1.0`：缩小（地面范围变大）

有效地面宽度 = `size_m / zoom`，服务端夹到 **20 m – 8,000 km**。`zoom` 范围
`1e-6 – 8.0`：`>1` 放大，`<1` 缩小。设备端使用一组离散档位，最小档约
`6.1e-5`（`size_m=400` 时约 **6,553 km** 宽，足够看到中国全图）。

`mode` 一步选定图像类型（推荐设备用这个）：

| `mode` | 输出 | 相关参数 |
|--------|------|----------|
| `road` | 风格化路网图（像素风） | `theme` 生效，`sat_opacity` 忽略 |
| `satellite` | 卫星图（原图，不做像素风） | 忽略 `theme`/`style` |
| `blend` | 卫星图按 `sat_opacity` 叠在路网图上 | `sat_opacity`：100=纯卫星，0=纯路网 |
| `auto`（默认） | 用 `layer`+`style`+`sat_opacity` 旧方式 | 兼容旧协议 |

请求（通用）：
```json
{
  "device_id": "esp32s3-1",
  "seq": 2050,
  "lng": 121.43375,
  "lat": 31.19875,
  "size_m": 300,
  "heading": 0,
  "size": 240,
  "zoom": 1.5,
  "format": "pal16",
  "mode": "blend",
  "sat_opacity": 60,
  "theme": "gameboy",
  "coord": "wgs84"
}
```

三种调用的最小差异：
```jsonc
// 1) 风格化路网
{"mode":"road","theme":"gameboy"}
// 2) 卫星
{"mode":"satellite"}
// 3) 融合（60% 卫星 / 30% 卫星）
{"mode":"blend","sat_opacity":60}
{"mode":"blend","sat_opacity":30}
```

字段：

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `device_id` | string | — | 设备标识（幂等键） |
| `seq` | uint32 | — | 单调递增（幂等键；重传复用） |
| `lng`,`lat` | float | — | 当前位置 |
| `size_m` | float | 300 | 基准地面边长（米）20–10000；实际宽度再除以 `zoom` |
| `heading` | float | 0 | 朝向（正北顺时针），0=北朝上 |
| `size` | int | 240 | **可变**方形输出边长，16–512 |
| `width` | int | — | 可选非方形输出宽，16–512；与 `height` 一起用 |
| `height` | int | — | 可选非方形输出高，16–512 |
| `zoom` | float | 1.0 | 缩放倍率，1e-6–8.0；>1 放大、<1 缩小 |
| `format` | string | `pal16` | `pal16` / `rgb565` / `png` / `jpeg` |
| `mode` | string | `auto` | `road` / `satellite` / `blend` / `auto` |
| `sat_opacity` | int | 100 | 融合时卫星透明度 0–100（0=路网，100=卫星）|
| `theme` | string | `gameboy` | 像素风主题（仅 road）|
| `style` | string | `pixel` | `pixel`/`original`（仅 auto 模式生效）|
| `process` | bool | true | 是否做像素风处理（仅 road）|
| `coord` | string | `gcj02` | 输入坐标系；**设备传 `wgs84`** |

响应头：
```
X-Nav-Seq, X-Nav-Width, X-Nav-Height,
X-Nav-Zoom（内部瓦片级）, X-Nav-Zoom-Factor（请求的 zoom）,
X-Nav-Span-M（实际地面宽度，米）,
X-Nav-Size, X-Nav-Raw-Rgb565, X-Nav-Style,
X-Nav-Palette（pal16 时，如 #8E0E,#E7DA,#334A,#08C4）
```

客户端应以 `Width/Height` 为准分配/校验图像，不要假定 240。`X-Nav-Zoom-Factor`
和 `X-Nav-Span-M` 可用于 UI 显示与调试。非方形请求下 `size_m` 仍表示**宽度**方向
的地面米数，高度按 `height/width` 比例折算。

实测体积（240×240，`size_m=300`，`format=pal16`）：

| 类型 | 风格 | 大小 |
|------|------|------|
| `road` | GameBoy 像素风(4色) | ≈1.5 KB |
| `satellite` | 原图(16色) | ≈35 KB |
| `blend` 60% | 原图(16色) | ≈36 KB |

> 若需要更小的卫星/融合包，用 `format=jpeg`（设备端需 JPEG 解码）；`rgb565` 为无损原始帧（115,200 B）。

## 场景①：`POST /api/v1/satellite`

```json
{"device_id":"d","seq":1042,
 "corners":[{"lng":121.4325,"lat":31.1975},{"lng":121.4350,"lat":31.1975},
            {"lng":121.4350,"lat":31.2000},{"lng":121.4325,"lat":31.2000}],
 "max_size":240,"format":"rgb565","coord":"wgs84","layer":"satellite"}
```
- 四点顺序任意；服务端外接成**中心正方形**再缩放（保证四角在内）。
- `layer`：`satellite`（高德卫星）/`road`（天地图路网，可像素风），**单图层，不做融合**。
- 响应头：`X-Sat2Pixel-Seq/Width/Height/Size`。

## 二进制格式

- **rgb565**：每像素 2 字节**大端** `R5G6B5`，`[RRRRRGGG][GGGBBBBB]`，长度 `w*h*2`。
- **pal16**（小端）：
  ```
  0   2  width      (u16 LE)
  2   2  height     (u16 LE)
  4   2  paletteCnt (u16 LE)
  6   2*N palette    (N×RGB565 u16 LE)
  ..     RLE：byte=(run-1)<<4 | idx ; (run-1)==15 时下一字节 = run-16（run≤271）
  ```
  - 像素风 road：`paletteCnt=4`；satellite/blend（原图）：`paletteCnt=16`。

## 坐标系（重要）

| 来源 | 基准 |
|------|------|
| 高德（卫星） | GCJ-02 |
| 天地图（路网） | WGS-84 |

设备 GNSS 输出 WGS-84，**一律传 `coord:"wgs84"`**，服务端按 `mode`/`layer` 自动换算，切勿自行转换。融合时服务端会分别按 GCJ/WGS 取图再对齐混合。

## 重传 / 幂等

- 服务端按 `(device_id, seq)` 缓存 **600s**。
- **不同请求用新 `seq`；重传必须复用同一 `seq`**。
- 建议超时 12s，重试 3 次，退避 1/2/4s。

## 错误

统一 JSON：`{"code":"...","message":"..."}`

| code | HTTP | 含义 |
|------|------|------|
| `INVALID_JSON` | 400 | body 非合法 JSON |
| (pydantic) | 422 | 字段校验失败（附字段信息）|
| `TILE_FETCH_FAILED` | 502 | 取瓦片失败 |
| `TILE_FAILED` | 502 | 瓦片代理失败 |
| `NO_KEY` | 500 | 未配置高德 key |
| `GEOCODE_FAILED` / `NOT_FOUND` | 502/404 | 地理编码 |

## 注意事项

1. **天地图 key 的 IP 白名单**：key 必须在天地图控制台把**服务器公网 IP** 加入白名单，否则 `301008 IP不匹配`（本地开发机通常未加）。
2. 天地图路网 `vec_w` **无文字标注**（只有道路/水系/绿地）；要路名需叠 `cva_w`（未启用）。
3. 卫星图层**永远是原图**，`style/theme/process` 对其无效。
4. `blend` 需要天地图（会额外拉路网瓦片）；`sat_opacity=100` 时等同卫星，不拉路网。
5. 朝向 `heading≠0` 时图像旋转成「朝向朝上」；为避免黑角，服务端 1.5× 超采样后中心裁切。
6. `size` / `width` / `height` 可变（16–512），服务端最多按 1024 px 拉取源图再缩放，防止大图请求拖垮服务器。全屏设备请求 `width=320,height=240`。
7. `zoom` 是**绝对倍率**（不是增量）。设备端每次放大/缩小后，用新的 `zoom` + 新 `seq` 请求；重传必须复用同一 `seq` 和 `zoom`。
   - 设备档位：`4 / 2 / 1 / 0.5 / 0.25 / 0.0625 / 0.015625 / 0.00390625 / 0.0009765625 / 0.000244140625 / 0.00006103515625`。
   - 最小档约 6,553 km 宽，覆盖中国全图；服务端硬上限 8,000 km。
8. 服务端内置瓦片缓存（600 s），同一区域连续移动/缩放时不会反复拉上游瓦片。
9. `Content-Length` 已设置，按字节读满；读取到 `-1`(chunked) 需兜底。
10. 端口 2020 目前对公网开放；如需关闭，改用域名 `https://racebrick.tfworks.cn`（nginx 反代到 127.0.0.1:2020）。

## curl 示例

```bash
B=http://47.100.182.210:2020/api/v1/nav
# 风格化路网(GameBoy)
curl -X POST $B -H 'Content-Type: application/json' -d '{"device_id":"d","seq":1,"lng":121.43375,"lat":31.19875,"size_m":300,"size":240,"zoom":1.0,"format":"pal16","mode":"road","theme":"gameboy","coord":"wgs84"}' -o road.pal16 -D -
# 卫星
curl -X POST $B -H 'Content-Type: application/json' -d '{"device_id":"d","seq":2,"lng":121.43375,"lat":31.19875,"size_m":300,"size":240,"zoom":2.0,"format":"pal16","mode":"satellite","coord":"wgs84"}' -o sat.pal16 -D -
# 融合 60% 卫星
curl -X POST $B -H 'Content-Type: application/json' -d '{"device_id":"d","seq":3,"lng":121.43375,"lat":31.19875,"size_m":300,"size":320,"zoom":0.5,"format":"pal16","mode":"blend","sat_opacity":60,"coord":"wgs84"}' -o blend60.pal16 -D -
# 全屏 320x240
curl -X POST $B -H 'Content-Type: application/json' -d '{"device_id":"d","seq":4,"lng":121.43375,"lat":31.19875,"size_m":400,"width":320,"height":240,"zoom":1.0,"format":"pal16","mode":"road","theme":"gameboy","coord":"wgs84"}' -o full.pal16 -D -
```
