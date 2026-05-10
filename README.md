# CPM80 Dashboard — 智慧電表能源管理系統

> **AI 驅動的即時電力監控儀表板** — ADTEK CPM-80 多功能電力錶 × 2
> FastAPI + Vue 3 + ECharts 5 + Ollama AI 分析，單一 Python 檔案部署

---

## For Claude Code / AI Agent

> **本段落供另一台機器上的 Claude Code 快速理解本專案。**

### 專案概要

這是一個**單一 Python 檔案**的全端 Web 應用（`cpm80_dashboard.py`，約 3,400 行），內嵌 HTML/CSS/JS。它透過 Modbus TCP 每 2 秒輪詢兩台 ADTEK CPM-80 電表，將量測資料存入 SQLite，並透過 WebSocket 即時推送給瀏覽器端的 Vue 3 SPA。

### 關鍵架構

```
cpm80_dashboard.py 結構（由上到下）：
─────────────────────────────────────────────
  Section 1  : Modbus 常數 & 讀取函式（~100 行）
  Section 2  : argparse CLI 參數解析
  Section 3A : SQLite schema + CRUD 函式
               - db_load_profile()      ← 負載曲線分析（NEW）
               - db_anomaly_detection() ← 統計異常偵測（NEW）
  Section 3B : InfluxDB 整合（選用）
  Section 3C : Ollama AI 分析
               - OLLAMA_SYSTEM_PROMPT
               - build_analysis_prompt()  ← 已增強，含負載/異常/費率（NEW）
               - call_ollama()
  Section 4  : Demo 模式（模擬資料產生器）
  Section 5  : 全域狀態 & 告警閾值
               - TPC_RATES 常數           ← 台電費率（NEW）
               - _calc_tiered_cost()      ← 累進費率計算（NEW）
               - _calc_tou_cost()         ← 時間電價二段式（NEW）
               - _calc_tou3_cost()        ← 時間電價三段式（NEW）
               - db_rate_optimization()   ← 費率比較推薦（NEW）
  Section 6  : FastAPI app + Lifespan + 背景輪詢
  Section 7  : REST API 端點
               - /api/analysis/load-profile      （NEW）
               - /api/analysis/anomaly            （NEW）
               - /api/analysis/rate-optimization  （NEW）
  Section 8  : HTML_PAGE（嵌入式前端）
               - CSS：含 Light/Dark 雙主題（NEW）
               - HTML：6 頁 SPA，AI 分析頁含 KPI 卡片/柱狀圖/異常列表/費率比較表（NEW）
               - Vue 3 setup()：含 fetchLoadProfile/fetchAnomaly/fetchRateOpt（NEW）
  Section 9  : uvicorn 啟動
```

### 修改本專案時請注意

1. **單一檔案** — 所有後端邏輯、前端 HTML/CSS/JS 都在 `cpm80_dashboard.py` 內
2. **HTML 是 Python raw string** — `HTML_PAGE = r"""..."""`，修改前端時請用 Edit 而非 Write
3. **同步兩份檔案** — `CPM80-plugin-model/` 和 `CPM80-MP17-Lab126-main/` 的 `cpm80_dashboard.py` 必須保持一致
4. **不需額外套件** — 新功能僅使用 Python stdlib（`math`, `statistics`, `collections`）
5. **ECharts 主題** — 圖表顏色透過 `getChartTheme()` 動態讀取 CSS 變數，切換主題時自動適配
6. **SQLite schema** — 在 `init_db()` 函式內以 `CREATE TABLE IF NOT EXISTS` 定義，無 migration

---

## 系統架構

```
┌─────────────────────────────────────────────────────────────────┐
│                        使用者層 (Browser)                        │
│  Vue 3 SPA + ECharts 5 — 即時監控 / 趨勢 / 告警 / AI 分析      │
│  WebSocket 即時推送  ·  RWD 自適應  ·  Light / Dark 雙主題       │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP / WebSocket
┌────────────────────────────┴────────────────────────────────────┐
│                     應用層 (FastAPI Server)                       │
│  REST API · WebSocket · SQLite 歷史儲存 · CSV 匯出              │
│  Ollama AI 用電分析 · 告警引擎 · 電費試算 · InfluxDB 整合        │
│  負載曲線分析 · 統計異常偵測 · 台電費率優化                       │
└───────┬──────────────────────┬──────────────────┬───────────────┘
        │ Modbus TCP           │ HTTP              │ HTTP
┌───────┴───────┐    ┌────────┴────────┐  ┌──────┴──────────┐
│  CPM-80 #1    │    │  Ollama Server  │  │  InfluxDB       │
│  10.0.60.21   │    │  10.0.60.180    │  │  (選用)         │
│  (樓上)       │    │  qwen2.5:14b    │  │                 │
├───────────────┤    └─────────────────┘  └─────────────────┘
│  CPM-80 #2    │
│  10.0.60.22   │
│  (B2 停車場)   │
└───────────────┘
```

## 技術棧

| 類別 | 技術 |
|------|------|
| 語言 | Python 3.13 |
| Web 框架 | FastAPI + Uvicorn |
| Modbus 通訊 | pymodbus 3.12.0 (TCP) |
| HTTP 客戶端 | httpx (Ollama API 呼叫) |
| AI 分析 | Ollama (qwen2.5:14b) |
| 時序資料庫 | InfluxDB (選用) |
| 本地儲存 | SQLite (歷史/告警/分析紀錄) |
| 前端框架 | Vue 3 (CDN, 嵌入式 SPA) |
| 圖表引擎 | ECharts 5 |
| 即時通訊 | WebSocket (每 2 秒推送) |

## 硬體環境

### 電表配置

| 項目 | 電表 A（樓上） | 電表 B（B2 停車場） |
|------|---------------|-------------------|
| 型號 | ADTEK CPM-80 | ADTEK CPM-80 |
| IP | `10.0.60.21` | `10.0.60.22` |
| Modbus Port | 502 | 502 |
| Unit ID | 1 | 1 |
| 接線方式 | 1P3W (單相三線) | 1P3W (單相三線) |
| PT Ratio | 600V / 600V | 600V / 600V |
| CT Ratio | 100A / 333mV | 100A / 333mV |

### 網路架構

```
iMac (10.0.60.226) ── Dashboard Server
  │  UniFi 區域網路 (10.0.60.0/24)
  │
  ├── CPM-80 #1 (10.0.60.21) ← 樓上配電盤
  │
  ├── Ollama Server (10.0.60.180) ← AI 分析主機
  │
  └── PLC (樓上, TP-Link AV1000)
        ↓  電力線
      PLC (B2, TP-Link AV1000)
        ↓
      AmpliFi HD AFi-R (10.0.60.186, 橋接模式)
        ↓
      CPM-80 #2 (10.0.60.22) ← B2 停車場配電盤
```

## 功能清單

### 6 大頁面

| 頁面 | 說明 |
|------|------|
| **即時監控** | 雙電表即時數據卡片、合計功率、WebSocket 每 2 秒更新 |
| **趨勢圖表** | 功率 / 電壓 / 電流 / 功率因數歷史趨勢，支援 1h / 6h / 24h / 7d / 30d |
| **告警通知** | 可設定電壓、電流、功率因數閾值，自動偵測與通知 |
| **AI 用電分析** | Ollama AI 分析 + 負載曲線 + 異常偵測 + 台電費率優化（詳見下方） |
| **報表匯出** | 歷史資料 CSV 匯出、電費估算 |
| **系統設定** | 電表連線狀態、告警閾值設定、設備管理 |

### 核心特色

- **Demo / Live 即時切換** — 無需重啟，一鍵切換真實電表與模擬資料
- **Light / Dark 雙主題** — macOS 風格切換，自動偵測系統偏好，localStorage 持久化
- **AI 用電分析** — Ollama LLM 分析，prompt 自動注入負載曲線 / 異常 / 費率比較數據
- **單一檔案部署** — 整個前後端 (`cpm80_dashboard.py`) 約 3,400 行，零 build 步驟
- **InfluxDB 整合** — 可選的時序資料庫支援，長期儲存量測資料
- **WebSocket 即時推送** — 低延遲資料更新，無需輪詢

---

## 進階分析功能（v2 新增）

### 1. 負載曲線分析 (Load Profile)

**後端函式：** `db_load_profile(meter_id, hours=24)`
**API：** `GET /api/analysis/load-profile?meter_id=meter_a&hours=24`

將 2 秒取樣資料以 SQLite `GROUP BY strftime('%Y-%m-%dT%H', ts)` 聚合為**小時桶**，計算以下 KPI：

| KPI | 公式 | 說明 |
|-----|------|------|
| **負載率** Load Factor | avg_power / peak_power | 越接近 1 表示用電越平穩 |
| **尖峰佔比** Peak Ratio | peak_kwh / total_kwh | 台電尖峰定義 07:30-22:30 |
| **需量因數** Demand Factor | peak_power / 設備總額定功率 | 需先在設備管理登錄設備 |
| **總用電量** total_kwh | Σ(avg_power × 1h / 1000) | 小時平均功率積分 |

**前端：** 4 張 KPI 卡片 + ECharts 柱狀圖（尖峰橙色 / 離峰藍色 / 紅色虛線平均線），進入 AI 分析頁時自動載入。

### 2. 統計異常偵測 (Anomaly Detection)

**後端函式：** `db_anomaly_detection(meter_id, baseline_days=7)`
**API：** `GET /api/analysis/anomaly?meter_id=meter_a&baseline_days=7`

三階段偵測流程：

1. **Phase 1 — 基線建立**：取過去 `baseline_days` 天（排除最近 24h）的小時平均功率
2. **Phase 2 — 統計計算**：依 hour_of_day (0-23) 分組，計算 mean / stdev / Q1 / Q3 / IQR
3. **Phase 3 — 異常比對**：最近 24h 每小時與基線比較
   - **Z-score**：|z| > 2.0 標記異常
   - **IQR**：值落在 [Q1-1.5×IQR, Q3+1.5×IQR] 之外標記異常
   - **嚴重度**：Z + IQR 同時觸發 = `high`，單項觸發 = `medium`

某 hour_of_day 基線樣本 < 3 天則跳過。回傳 `baseline_coverage` 比率。

**前端：** 按鈕觸發，顯示嚴重/警告數量 badge + 基準覆蓋率，異常點疊加到負載曲線圖（markPoint）。

### 3. 台電費率優化 (TPC Rate Optimization)

**後端函式：** `db_rate_optimization(meter_id, hours=720)`
**API：** `GET /api/analysis/rate-optimization?meter_id=meter_a&hours=720`

比較三種方案，推薦最省費率：

| 方案 | 常數 key | 說明 |
|------|----------|------|
| **表燈非時間電價** | `tiered` | 累進 6 級距，夏月/非夏月費率不同 |
| **住宅型簡易時間電價二段式** | `tou2` | 尖峰/離峰兩段 + 基本費 75 元 |
| **住宅型簡易時間電價三段式** | `tou3` | 尖峰/半尖峰/離峰三段 + 基本費 75 元 |

費率常數定義在 `TPC_RATES` dict。夏月 = 6-9 月，由 `_get_season()` 自動判斷。
計算函式：`_calc_tiered_cost()`, `_calc_tou_cost()`, `_calc_tou3_cost()`

**前端：** 按鈕觸發，推薦方案綠色 banner + 三方案比較表（月用電量/電力費/基本費/月總費用/平均單價）。

### 4. LLM Prompt 強化

`build_analysis_prompt()` 在原有的即時數據 + 歷史趨勢 + 設備清單之後，自動注入：

- `## 負載曲線分析`：負載率、尖峰功率、平均功率、尖離峰比、總用電量
- `## 統計異常偵測`：severity=high 的異常（最多 5 筆）
- `## 台電費率比較`：各方案預估月費 + 推薦方案

均包在 `try/except` 內，不影響原有分析流程。

### 5. Light / Dark 雙主題

- **CSS**：`:root` 定義深色變數，`[data-theme="light"]` 覆蓋為 macOS 風格淺色（Apple SF Colors）
- **ECharts**：透過 CSS 變數 token（`--chart-tooltip-bg`, `--chart-axis` 等），`getChartTheme()` 動態讀取
- **持久化**：`localStorage.getItem('cpm80_theme')`，首次載入偵測 `prefers-color-scheme`
- **切換**：topbar 右側 toggle 按鈕，帶 sun/moon icon 滑動動畫

---

## REST API 端點

### 系統

| 方法 | 路徑 | 說明 |
|------|------|------|
| `GET` | `/` | Web 儀表板主頁面 |
| `GET` | `/api/config` | 取得系統設定（電表名稱、連線狀態、Demo 模式） |
| `POST` | `/api/demo/toggle` | 切換 Demo / Live 模式 |
| `WebSocket` | `/ws` | 即時資料推送 |

### 量測資料

| 方法 | 路徑 | 說明 |
|------|------|------|
| `GET` | `/api/latest` | 最新量測數據 |
| `GET` | `/api/history?meter_id=meter_a&hours=24` | 歷史資料查詢 |
| `GET` | `/api/export?meter_id=meter_a&hours=24` | 匯出 CSV |
| `GET` | `/api/billing?meter_id=meter_a&hours=24` | 電費估算 |

### 告警

| 方法 | 路徑 | 說明 |
|------|------|------|
| `GET` | `/api/alerts` | 告警列表 |
| `POST` | `/api/alerts/{id}/ack` | 確認單筆告警 |
| `POST` | `/api/alerts/ack-all` | 確認所有告警 |
| `GET` | `/api/alerts/thresholds` | 取得告警閾值 |
| `POST` | `/api/alerts/thresholds` | 設定告警閾值 |

### AI 分析

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/api/analysis` | 執行 AI 用電分析（body: `meter_id`, `note`, `hours`） |
| `GET` | `/api/analysis/history` | AI 分析歷史紀錄 |
| `GET` | `/api/analysis/status` | Ollama 連線狀態 |
| `GET` | `/api/analysis/load-profile?meter_id=&hours=24` | **負載曲線分析** |
| `GET` | `/api/analysis/anomaly?meter_id=&baseline_days=7` | **統計異常偵測** |
| `GET` | `/api/analysis/rate-optimization?meter_id=&hours=720` | **台電費率優化** |

### 設備管理

| 方法 | 路徑 | 說明 |
|------|------|------|
| `GET` | `/api/equipment` | 設備列表 |
| `POST` | `/api/equipment` | 新增設備 |
| `PUT` | `/api/equipment/{id}` | 更新設備 |
| `DELETE` | `/api/equipment/{id}` | 刪除設備 |

## CLI 參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `--host-a` | `10.0.60.21` | 電表 A IP |
| `--host-b` | `10.0.60.22` | 電表 B IP |
| `--modbus-port` | `502` | Modbus TCP Port |
| `--device-id` | `1` | Modbus Unit ID |
| `--name-a` | `樓上` | 電表 A 顯示名稱 |
| `--name-b` | `B2` | 電表 B 顯示名稱 |
| `--port` | `8088` | Web 伺服器 Port |
| `--bind` | `0.0.0.0` | Web 伺服器綁定位址 |
| `--interval` | `2.0` | 輪詢間隔（秒） |
| `--db` | `cpm80_dashboard.db` | SQLite 資料庫路徑 |
| `--demo` | — | 啟動 Demo 模式（模擬資料） |
| `--ollama-url` | `http://10.0.60.180:11434` | Ollama API URL |
| `--ollama-model` | `qwen2.5:14b` | Ollama 模型名稱 |
| `--influxdb-url` | `http://localhost:8086` | InfluxDB URL |
| `--influxdb-token` | — | InfluxDB API Token |
| `--influxdb-org` | `cpm80` | InfluxDB Organization |
| `--influxdb-bucket` | `power_readings` | InfluxDB Bucket |

## 桌面 App (pywebview)

使用 pywebview 將 Dashboard 包裝為 macOS 原生桌面應用程式（WebKit 視窗），不需要外部瀏覽器。

### 安裝

```bash
pip install pywebview
# 或透過 requirements.txt 一次安裝所有依賴
pip install -r requirements.txt
```

### 啟動

```bash
# 預設 Demo 模式（不需要實體電表）
python cpm80_desktop.py

# 連接實體電表
python cpm80_desktop.py --no-demo --host-a 10.0.60.21 --host-b 10.0.60.22

# 指定 port
python cpm80_desktop.py --port 9090
```

### 桌面 App 專用參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `--no-demo` | — | 連接實體電表（預設為 Demo 模式） |
| `--port` | `8088` | 內部 server port |
| `--host-a` | `10.0.60.21` | 電表 A IP |
| `--host-b` | `10.0.60.22` | 電表 B IP |
| `--ollama-url` | `http://10.0.60.180:11434` | Ollama API URL |
| `--ollama-model` | `qwen2.5:14b` | Ollama 模型名稱 |

### 設計特點

- 視窗大小 1280×820，最小 800×600
- macOS 保留系統標題列（紅綠燈按鈕）
- 強制綁定 `127.0.0.1`，不對外開放
- 關閉視窗後 process 完全結束（無殘留）
- 所有 Dashboard 功能完整保留（即時監控、趨勢、AI 分析等）

---

## 安裝與啟動

### 環境需求

- Python 3.10+（本專案使用 3.13）
- 需在**同一區域網路** (`10.0.60.x`) 內才能連接電表
- Ollama Server（AI 分析功能，選用）
- InfluxDB 2.x（時序儲存，選用）

### 安裝

```bash
# 建立虛擬環境
python3 -m venv venv
source venv/bin/activate

# 安裝依賴
pip install -r requirements.txt
```

### 啟動 Dashboard

```bash
# 預設啟動（連接實體電表）
python cpm80_dashboard.py

# 指定參數
python cpm80_dashboard.py --host-a 10.0.60.21 --host-b 10.0.60.22 --port 8088

# Demo 模式（不需要實體電表）
python cpm80_dashboard.py --demo

# 指定 Ollama 主機
python cpm80_dashboard.py --ollama-url http://10.0.60.180:11434 --ollama-model qwen2.5:14b
```

開啟瀏覽器 `http://localhost:8088`（或區網 `http://10.0.60.226:8088`）即可使用。

### 舊版 CLI Reader

```bash
# 雙電表同時讀取
python cpm80_reader_all_wireFun_duo.py --once
```

## 檔案結構

```
CPM80-plugin-model/
├── cpm80_dashboard.py              # 主儀表板（~3,400 行，全端單一檔案）
│                                   #   含：Modbus 讀取、SQLite CRUD、
│                                   #   負載曲線分析、異常偵測、費率優化、
│                                   #   AI 分析、Demo 模式、Light/Dark 主題、
│                                   #   HTML/CSS/Vue 3/ECharts 前端
├── cpm80_desktop.py                # pywebview 桌面啟動器（macOS 原生視窗）
├── cpm80_reader_all_wireFun_duo.py # CLI — 雙電表並排顯示 + 合計
├── requirements.txt                # Python 依賴套件
├── docs/                           # 原廠文件與現場照片
│   ├── CPM-80 操作手冊_*.pdf
│   ├── CPM-80 通訊位址表_*.pdf
│   └── *.jpeg / *.jpg              # 電表設定截圖
└── README.md                       # 本文件
```

## 量測參數

| 參數 | 說明 |
|------|------|
| Freq | 系統頻率 (Hz) |
| V / Uavg | 電壓平均 (V) |
| Ia / Ib / Ic | 各相電流 (A) |
| Psum | 總有功功率 (W) |
| PFavg | 平均功率因數 |
| S | 視在功率 (kVA) |
| Q | 無功功率 (kVAr) |

## 變更紀錄

### v2 — 2026-03-28（進階分析 + 雙主題）

- **負載曲線分析**：小時桶聚合 + 負載率 / 尖峰佔比 / 需量因數 KPI + ECharts 柱狀圖
- **統計異常偵測**：Z-score + IQR 雙重檢驗，基線覆蓋率指標
- **台電費率優化**：累進 / 二段式 / 三段式三方案比較，自動推薦最省方案
- **LLM Prompt 強化**：自動注入負載曲線 / 異常 / 費率數據至 AI 分析
- **Light / Dark 雙主題**：macOS 風格切換，系統偏好自動偵測，ECharts 動態適配
- **新增 3 個 API 端點**：`load-profile`, `anomaly`, `rate-optimization`

### v1 — 2026-02-25（AI 分析增強）

- AI 分析新增時間範圍選擇（1h / 6h / 24h / 自訂）
- 設備管理 CRUD + AI prompt 自動注入設備清單
- Demo/Live 一鍵切換

### v0 — 2026-02-20（初始版本）

- 雙電表即時監控 + WebSocket
- 趨勢圖表 + 告警 + CSV 匯出 + 電費試算
- Ollama AI 用電分析

## License

MIT
