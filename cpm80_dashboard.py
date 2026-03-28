#!/usr/bin/env python3
"""
CPM80 Dashboard — 智慧電表能源管理系統
======================================
全功能 Web 儀表板：即時監控、趨勢圖表、告警通知、電費計算、報表匯出。
FastAPI 後端 + 嵌入式 Vue 3 / ECharts 前端，單一檔案部署。

啟動:
    python cpm80_dashboard.py
    python cpm80_dashboard.py --host-a 10.0.60.21 --host-b 10.0.60.22 --port 8080

瀏覽器:
    http://localhost:8080
"""

import argparse
import asyncio
import csv
import io
import json
import logging
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

# ─── pymodbus ────────────────────────────────────────────────────
try:
    from pymodbus.client import ModbusTcpClient
    HAS_MODBUS = True
except ImportError:
    HAS_MODBUS = False

# ─── httpx（Ollama 呼叫）────────────────────────────────────────
try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

# ─── InfluxDB ────────────────────────────────────────────────────
try:
    from influxdb_client import InfluxDBClient, Point
    from influxdb_client.client.write_api import SYNCHRONOUS
    HAS_INFLUXDB = True
except ImportError:
    HAS_INFLUXDB = False

# ─── Logging ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cpm80_dashboard")

# ═══════════════════════════════════════════════════════════════════
# Section 1: Modbus 常數 & 函式（參考 cpm80_reader_all_wireFun_duo.py）
# ═══════════════════════════════════════════════════════════════════

REG_WIRE_FUNC   = 0x0000
REG_PT_PRIMARY  = 0x0001
REG_PT_SECOND   = 0x0003
REG_CT_PRIMARY  = 0x0004
REG_CT_SEC_SEL  = 0x000A

REG_FREQ        = 0x1000
REG_VOLTAGE_A   = 0x100A
REG_VOLTAGE_B   = 0x100C
REG_VOLTAGE_C   = 0x100E
REG_VOLTAGE_AVG = 0x1010
REG_CURRENT_A   = 0x1012
REG_CURRENT_B   = 0x1014
REG_CURRENT_C   = 0x1016
REG_CURRENT_AVG = 0x1018
REG_POWER_TOTAL = 0x1022
REG_PF_AVG      = 0x1036
REG_YEAR        = 0x01A6

WIRE_FUNC_MAP = {
    0: ("1P2W",    "單相二線"),
    1: ("1P3W",    "單相三線"),
    2: ("3P3W1CT", "三相三線 1CT"),
    3: ("3P3W2CT", "三相三線 2CT"),
    4: ("3P3W3CT", "三相三線 3CT"),
    5: ("3P4W1CT", "三相四線 1CT"),
    6: ("3P4W3CT", "三相四線 3CT"),
}
CT_SEC_MAP = {0: "5A", 1: "1A", 2: "333mV"}


def read_signed_int16(value):
    return value - 0x10000 if value >= 0x8000 else value


def read_system_config(client, device_id):
    cfg = {}
    resp = client.read_holding_registers(REG_WIRE_FUNC, count=1, device_id=device_id)
    if resp.isError():
        raise RuntimeError("無法讀取接線設定")
    wire_val = resp.registers[0]
    code, name = WIRE_FUNC_MAP.get(wire_val, (f"未知({wire_val})", "未知"))
    cfg["wire_raw"] = wire_val
    cfg["wire_code"] = code
    cfg["wire_name"] = name

    resp = client.read_holding_registers(REG_PT_PRIMARY, count=4, device_id=device_id)
    if resp.isError():
        raise RuntimeError("無法讀取 PT/CT 設定")
    regs = resp.registers
    cfg["pt_primary"] = (regs[0] << 16) | regs[1]
    cfg["pt_secondary"] = regs[2]
    cfg["ct_primary"] = regs[3]

    resp = client.read_holding_registers(REG_CT_SEC_SEL, count=1, device_id=device_id)
    if resp.isError():
        cfg["ct_secondary"] = "讀取失敗"
    else:
        cfg["ct_secondary"] = CT_SEC_MAP.get(resp.registers[0], f"未知({resp.registers[0]})")
    return cfg


def read_time(client, device_id):
    try:
        t = client.read_holding_registers(REG_YEAR, count=6, device_id=device_id)
        if t.isError():
            return "時間讀取錯誤"
        r = t.registers
        return f"{r[0]:04d}-{r[1]:02d}-{r[2]:02d} {r[3]:02d}:{r[4]:02d}:{r[5]:02d}"
    except Exception:
        return "時間讀取錯誤"


def read_block(client, start, count, device_id):
    data = []
    remaining = count
    addr = start
    while remaining > 0:
        step = min(remaining, 120)
        resp = client.read_holding_registers(addr, count=step, device_id=device_id)
        if resp.isError():
            raise RuntimeError(f"暫存器讀取錯誤: 0x{addr:04X}")
        data.extend(resp.registers)
        addr += step
        remaining -= step
    return data


def decode_main(data, base, wire_code):
    def idx(reg):
        return reg - base

    freq  = data[idx(REG_FREQ)] * 0.01
    v_a   = data[idx(REG_VOLTAGE_A)] * 0.1
    v_b   = data[idx(REG_VOLTAGE_B)] * 0.1
    v_c   = data[idx(REG_VOLTAGE_C)] * 0.1
    v_avg = data[idx(REG_VOLTAGE_AVG)] * 0.1
    i_a   = data[idx(REG_CURRENT_A)] * 0.001
    i_b   = data[idx(REG_CURRENT_B)] * 0.001
    i_c   = data[idx(REG_CURRENT_C)] * 0.001
    i_avg = data[idx(REG_CURRENT_AVG)] * 0.001
    p_sum = read_signed_int16(data[idx(REG_POWER_TOTAL)])
    pf    = read_signed_int16(data[idx(REG_PF_AVG)]) * 0.001

    p_kw = p_sum / 1000.0
    if wire_code.startswith("1P"):
        s_kva = (v_avg * i_avg) / 1000.0 if v_avg > 0 and i_avg > 0 else 0.0
    elif wire_code.startswith("3P"):
        s_kva = (v_avg * i_avg * 1.732) / 1000.0 if v_avg > 0 and i_avg > 0 else 0.0
    else:
        s_kva = 0.0

    q_kvar = (max(s_kva**2 - p_kw**2, 0.0) ** 0.5) if s_kva > 0 else 0.0

    return {
        "freq": freq,
        "v_a": v_a, "v_b": v_b, "v_c": v_c, "v_avg": v_avg,
        "i_a": i_a, "i_b": i_b, "i_c": i_c, "i_avg": i_avg,
        "p_sum": p_sum, "pf": pf,
        "s_kva": s_kva, "q_kvar": q_kvar,
    }


def read_meter(client, device_id, wire_code):
    ts = read_time(client, device_id)
    block = read_block(client, 0x1000, 0xA0, device_id)
    decoded = decode_main(block, 0x1000, wire_code)
    return ts, decoded


# ═══════════════════════════════════════════════════════════════════
# Section 2: CLI 參數
# ═══════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description="CPM-80 Dashboard — 智慧電表能源管理系統")
    p.add_argument("--host-a", default="10.0.60.21", help="電表 A IP")
    p.add_argument("--host-b", default="10.0.60.22", help="電表 B IP")
    p.add_argument("--modbus-port", type=int, default=502, help="Modbus TCP Port")
    p.add_argument("--device-id", type=int, default=1, help="Modbus Unit ID")
    p.add_argument("--name-a", default="樓上", help="電表 A 名稱")
    p.add_argument("--name-b", default="B2", help="電表 B 名稱")
    p.add_argument("--port", type=int, default=8088, help="Web 伺服器 Port")
    p.add_argument("--bind", default="0.0.0.0", help="Web 伺服器綁定位址")
    p.add_argument("--interval", type=float, default=2.0, help="輪詢間隔（秒）")
    p.add_argument("--db", default="cpm80_dashboard.db", help="SQLite 資料庫路徑")
    p.add_argument("--demo", action="store_true", help="Demo 模式（使用模擬資料）")
    # Ollama AI 分析
    p.add_argument("--ollama-url", default="http://10.0.60.180:11434", help="Ollama API URL")
    p.add_argument("--ollama-model", default="qwen2.5:14b", help="Ollama 模型名稱")
    # InfluxDB 時序資料庫
    p.add_argument("--influxdb-url", default="http://localhost:8086", help="InfluxDB URL")
    p.add_argument("--influxdb-token", default="", help="InfluxDB API Token")
    p.add_argument("--influxdb-org", default="cpm80", help="InfluxDB Organization")
    p.add_argument("--influxdb-bucket", default="power_readings", help="InfluxDB Bucket")
    return p.parse_args()

args = parse_args()

# ═══════════════════════════════════════════════════════════════════
# Section 3: SQLite 資料庫
# ═══════════════════════════════════════════════════════════════════

DB_PATH = Path(__file__).resolve().parent / args.db


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            meter_id TEXT NOT NULL,
            ts       TEXT NOT NULL,
            meter_ts TEXT,
            freq     REAL, v_avg REAL, v_a REAL, v_b REAL, v_c REAL,
            i_avg    REAL, i_a REAL, i_b REAL, i_c REAL,
            p_sum    REAL, pf REAL, s_kva REAL, q_kvar REAL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_readings_meter_ts
        ON readings(meter_id, ts)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            meter_id  TEXT NOT NULL,
            ts        TEXT NOT NULL,
            level     TEXT NOT NULL,
            category  TEXT NOT NULL,
            message   TEXT NOT NULL,
            value     REAL,
            threshold REAL,
            acked     INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_alerts_ts
        ON alerts(ts DESC)
    """)
    # ─── 設備清單（非時序）───
    conn.execute("""
        CREATE TABLE IF NOT EXISTS equipment_profiles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            meter_id      TEXT NOT NULL,
            name          TEXT NOT NULL,
            rated_watts   REAL,
            description   TEXT,
            typical_hours REAL
        )
    """)
    # ─── AI 分析歷史（非時序）───
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analysis_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ts             TEXT NOT NULL,
            meter_id       TEXT,
            prompt_summary TEXT,
            response       TEXT,
            model          TEXT
        )
    """)
    conn.commit()
    conn.close()


def db_insert(meter_id, ts, meter_ts, data):
    conn = get_db()
    conn.execute(
        """INSERT INTO readings
           (meter_id, ts, meter_ts, freq, v_avg, v_a, v_b, v_c,
            i_avg, i_a, i_b, i_c, p_sum, pf, s_kva, q_kvar)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            meter_id, ts, meter_ts,
            data.get("freq"), data.get("v_avg"),
            data.get("v_a"), data.get("v_b"), data.get("v_c"),
            data.get("i_avg"), data.get("i_a"), data.get("i_b"), data.get("i_c"),
            data.get("p_sum"), data.get("pf"),
            data.get("s_kva"), data.get("q_kvar"),
        ),
    )
    conn.commit()
    conn.close()


def db_insert_alert(meter_id, level, category, message, value=None, threshold=None):
    conn = get_db()
    ts = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO alerts (meter_id, ts, level, category, message, value, threshold) VALUES (?,?,?,?,?,?,?)",
        (meter_id, ts, level, category, message, value, threshold),
    )
    conn.commit()
    conn.close()


def db_query_history(meter_id, hours, downsample=None):
    conn = get_db()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    if downsample and downsample > 0:
        rows = conn.execute(
            """SELECT * FROM (
                 SELECT *, ROW_NUMBER() OVER (ORDER BY ts) AS rn
                 FROM readings WHERE meter_id=? AND ts>=?
               ) WHERE (rn - 1) % ? = 0 ORDER BY ts""",
            (meter_id, cutoff, downsample),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM readings WHERE meter_id=? AND ts>=? ORDER BY ts",
            (meter_id, cutoff),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_query_alerts(limit=50, unacked_only=False):
    conn = get_db()
    if unacked_only:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE acked=0 ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_ack_alert(alert_id):
    conn = get_db()
    conn.execute("UPDATE alerts SET acked=1 WHERE id=?", (alert_id,))
    conn.commit()
    conn.close()


def db_ack_all_alerts():
    conn = get_db()
    conn.execute("UPDATE alerts SET acked=1 WHERE acked=0")
    conn.commit()
    conn.close()


def db_billing_kwh(meter_id, hours):
    """估算 kWh：以 p_sum (W) 的時間積分近似。"""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT ts, p_sum FROM readings WHERE meter_id=? AND ts>=? ORDER BY ts",
        (meter_id, cutoff),
    ).fetchall()
    conn.close()
    if len(rows) < 2:
        return 0.0
    total_wh = 0.0
    for i in range(1, len(rows)):
        try:
            t0 = datetime.fromisoformat(rows[i - 1]["ts"])
            t1 = datetime.fromisoformat(rows[i]["ts"])
            dt_h = (t1 - t0).total_seconds() / 3600.0
            avg_w = ((rows[i - 1]["p_sum"] or 0) + (rows[i]["p_sum"] or 0)) / 2.0
            total_wh += avg_w * dt_h
        except Exception:
            continue
    return total_wh / 1000.0  # kWh


# ─── 負載曲線分析 ────────────────────────────────────────────────

def db_load_profile(meter_id, hours=24):
    """負載曲線分析：將 2 秒取樣資料聚合為小時桶並計算 KPI。"""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    rows = conn.execute("""
        SELECT strftime('%Y-%m-%dT%H', ts) AS hour_bucket,
               AVG(p_sum) AS avg_power,
               MAX(p_sum) AS max_power,
               MIN(p_sum) AS min_power,
               AVG(v_avg) AS avg_voltage,
               AVG(i_avg) AS avg_current,
               AVG(pf) AS avg_pf,
               COUNT(*) AS sample_count
        FROM readings
        WHERE meter_id=? AND ts>=?
        GROUP BY hour_bucket
        ORDER BY hour_bucket
    """, (meter_id, cutoff)).fetchall()
    conn.close()

    if not rows:
        return {"hourly": [], "kpi": {}, "total_kwh": 0}

    hourly = []
    total_kwh = 0.0
    peak_power = 0.0
    total_power_sum = 0.0
    peak_kwh = 0.0
    offpeak_kwh = 0.0

    for r in rows:
        bucket = dict(r)
        avg_w = bucket["avg_power"] or 0
        max_w = bucket["max_power"] or 0
        kwh = avg_w / 1000.0  # avg_power(W) × 1h / 1000
        bucket["kwh"] = round(kwh, 4)

        # 判斷尖離峰（台電定義：07:30-22:30 為尖峰）
        hour_str = bucket["hour_bucket"]  # format: YYYY-MM-DDTHH
        try:
            hh = int(hour_str.split("T")[1])
        except (IndexError, ValueError):
            hh = 12
        is_peak = 8 <= hh <= 21  # 近似 07:30-22:30
        bucket["is_peak"] = is_peak

        if is_peak:
            peak_kwh += kwh
        else:
            offpeak_kwh += kwh

        total_kwh += kwh
        total_power_sum += avg_w
        if max_w > peak_power:
            peak_power = max_w

        for k in ("avg_power", "max_power", "min_power", "avg_voltage", "avg_current", "avg_pf"):
            if bucket[k] is not None:
                bucket[k] = round(bucket[k], 2)

        hourly.append(bucket)

    avg_power = total_power_sum / len(rows) if rows else 0
    load_factor = avg_power / peak_power if peak_power > 0 else 0
    peak_ratio = peak_kwh / total_kwh if total_kwh > 0 else 0

    # 需量因數：peak_power / 設備總額定功率
    equips = db_get_equipment(meter_id)
    total_rated = sum(e.get("rated_watts", 0) or 0 for e in equips)
    demand_factor = peak_power / total_rated if total_rated > 0 else None

    kpi = {
        "load_factor": round(load_factor, 4),
        "peak_power_w": round(peak_power, 2),
        "avg_power_w": round(avg_power, 2),
        "peak_ratio": round(peak_ratio, 4),
        "demand_factor": round(demand_factor, 4) if demand_factor is not None else None,
        "total_rated_w": total_rated,
        "total_kwh": round(total_kwh, 4),
        "peak_kwh": round(peak_kwh, 4),
        "offpeak_kwh": round(offpeak_kwh, 4),
        "hours_analyzed": len(hourly),
    }

    return {"hourly": hourly, "kpi": kpi, "total_kwh": round(total_kwh, 4)}


# ─── 統計異常偵測 ────────────────────────────────────────────────

def db_anomaly_detection(meter_id, baseline_days=7):
    """統計異常偵測：以過去 baseline_days 建立基線，比對最近 24h。"""
    conn = get_db()
    now = datetime.now()
    baseline_end = (now - timedelta(hours=24)).isoformat()
    baseline_start = (now - timedelta(days=baseline_days + 1)).isoformat()
    recent_start = baseline_end

    # Phase 1: 基線資料（排除最近 24h）
    baseline_rows = conn.execute("""
        SELECT strftime('%H', ts) AS hour_of_day,
               AVG(p_sum) AS avg_power,
               strftime('%Y-%m-%d', ts) AS date_str
        FROM readings
        WHERE meter_id=? AND ts>=? AND ts<?
        GROUP BY date_str, hour_of_day
        ORDER BY date_str, hour_of_day
    """, (meter_id, baseline_start, baseline_end)).fetchall()

    # Phase 2: 基線統計（按 hour_of_day 分組）
    hourly_baselines = defaultdict(list)
    for r in baseline_rows:
        hod = r["hour_of_day"]
        val = r["avg_power"] or 0
        hourly_baselines[hod].append(val)

    baseline_stats = {}
    valid_hours = 0
    for hod in range(24):
        key = f"{hod:02d}"
        vals = hourly_baselines.get(key, [])
        if len(vals) >= 3:
            valid_hours += 1
            mean_val = statistics.mean(vals)
            stdev_val = statistics.stdev(vals) if len(vals) > 1 else 0
            sorted_vals = sorted(vals)
            n = len(sorted_vals)
            q1 = sorted_vals[n // 4] if n >= 4 else sorted_vals[0]
            q3 = sorted_vals[(3 * n) // 4] if n >= 4 else sorted_vals[-1]
            iqr = q3 - q1
            baseline_stats[key] = {
                "mean": mean_val,
                "stdev": stdev_val,
                "q1": q1,
                "q3": q3,
                "iqr": iqr,
                "sample_days": len(vals),
                "lower_fence": q1 - 1.5 * iqr,
                "upper_fence": q3 + 1.5 * iqr,
            }

    baseline_coverage = valid_hours / 24.0

    # Phase 3: 最近 24h 資料
    recent_rows = conn.execute("""
        SELECT strftime('%Y-%m-%dT%H', ts) AS hour_bucket,
               strftime('%H', ts) AS hour_of_day,
               AVG(p_sum) AS avg_power
        FROM readings
        WHERE meter_id=? AND ts>=?
        GROUP BY hour_bucket
        ORDER BY hour_bucket
    """, (meter_id, recent_start)).fetchall()
    conn.close()

    anomalies = []
    recent_hourly = []
    for r in recent_rows:
        hod = r["hour_of_day"]
        actual = r["avg_power"] or 0
        entry = {
            "hour_bucket": r["hour_bucket"],
            "hour_of_day": hod,
            "actual_power": round(actual, 2),
        }

        if hod in baseline_stats:
            bs = baseline_stats[hod]
            entry["baseline_mean"] = round(bs["mean"], 2)
            entry["baseline_stdev"] = round(bs["stdev"], 2)

            # Z-score
            z_score = (actual - bs["mean"]) / bs["stdev"] if bs["stdev"] > 0 else 0
            entry["z_score"] = round(z_score, 2)
            z_flag = abs(z_score) > 2.0

            # IQR
            iqr_flag = actual < bs["lower_fence"] or actual > bs["upper_fence"]
            entry["iqr_flag"] = iqr_flag

            if z_flag and iqr_flag:
                severity = "high"
            elif z_flag or iqr_flag:
                severity = "medium"
            else:
                severity = None

            if severity:
                direction = "偏高" if actual > bs["mean"] else "偏低"
                anomalies.append({
                    "hour_bucket": r["hour_bucket"],
                    "hour_of_day": hod,
                    "severity": severity,
                    "actual_power": round(actual, 2),
                    "baseline_mean": round(bs["mean"], 2),
                    "z_score": round(z_score, 2),
                    "direction": direction,
                    "deviation_pct": round((actual - bs["mean"]) / bs["mean"] * 100, 1) if bs["mean"] > 0 else 0,
                })

        recent_hourly.append(entry)

    return {
        "anomalies": anomalies,
        "anomaly_count": {
            "high": sum(1 for a in anomalies if a["severity"] == "high"),
            "medium": sum(1 for a in anomalies if a["severity"] == "medium"),
        },
        "baseline_coverage": round(baseline_coverage, 2),
        "baseline_days": baseline_days,
        "recent_hourly": recent_hourly,
        "baseline_stats": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in baseline_stats.items()},
    }


def db_cleanup(days=30):
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    cur = conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
    deleted = cur.rowcount
    conn.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
    conn.commit()
    conn.close()
    return deleted


def db_export_csv(meter_id, hours):
    conn = get_db()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT ts, meter_ts, freq, v_avg, v_a, v_b, v_c, i_avg, i_a, i_b, i_c, p_sum, pf, s_kva, q_kvar FROM readings WHERE meter_id=? AND ts>=? ORDER BY ts",
        (meter_id, cutoff),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Equipment CRUD ──────────────────────────────────────────────

def db_get_equipment(meter_id=None):
    conn = get_db()
    if meter_id:
        rows = conn.execute(
            "SELECT * FROM equipment_profiles WHERE meter_id=? ORDER BY name", (meter_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM equipment_profiles ORDER BY meter_id, name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_add_equipment(meter_id, name, rated_watts=None, description=None, typical_hours=None):
    conn = get_db()
    conn.execute(
        "INSERT INTO equipment_profiles (meter_id, name, rated_watts, description, typical_hours) VALUES (?,?,?,?,?)",
        (meter_id, name, rated_watts, description, typical_hours),
    )
    conn.commit()
    last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return last_id


def db_update_equipment(eq_id, **kwargs):
    conn = get_db()
    fields = []
    values = []
    for k in ("meter_id", "name", "rated_watts", "description", "typical_hours"):
        if k in kwargs and kwargs[k] is not None:
            fields.append(f"{k}=?")
            values.append(kwargs[k])
    if fields:
        values.append(eq_id)
        conn.execute(f"UPDATE equipment_profiles SET {','.join(fields)} WHERE id=?", values)
        conn.commit()
    conn.close()


def db_delete_equipment(eq_id):
    conn = get_db()
    conn.execute("DELETE FROM equipment_profiles WHERE id=?", (eq_id,))
    conn.commit()
    conn.close()


# ─── Analysis History ────────────────────────────────────────────

def db_save_analysis(meter_id, prompt_summary, response, model):
    conn = get_db()
    ts = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO analysis_history (ts, meter_id, prompt_summary, response, model) VALUES (?,?,?,?,?)",
        (ts, meter_id, prompt_summary, response, model),
    )
    conn.commit()
    conn.close()


def db_get_analysis_history(meter_id=None, limit=20):
    conn = get_db()
    if meter_id:
        rows = conn.execute(
            "SELECT * FROM analysis_history WHERE meter_id=? ORDER BY ts DESC LIMIT ?",
            (meter_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM analysis_history ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════
# Section 3B: InfluxDB 時序資料庫
# ═══════════════════════════════════════════════════════════════════

influx_client = None
influx_write_api = None
influx_query_api = None


def init_influxdb():
    """初始化 InfluxDB 連線。有 token 且有套件才啟用。"""
    global influx_client, influx_write_api, influx_query_api
    if not HAS_INFLUXDB or not args.influxdb_token:
        return False
    try:
        influx_client = InfluxDBClient(
            url=args.influxdb_url,
            token=args.influxdb_token,
            org=args.influxdb_org,
        )
        influx_write_api = influx_client.write_api(write_options=SYNCHRONOUS)
        influx_query_api = influx_client.query_api()
        # 測試連線
        influx_client.ping()
        log.info("InfluxDB 連線成功: %s", args.influxdb_url)
        return True
    except Exception as e:
        log.warning("InfluxDB 連線失敗: %s（退回 SQLite）", e)
        influx_client = None
        influx_write_api = None
        influx_query_api = None
        return False


def influx_write(meter_id, data):
    """寫入一筆時序資料到 InfluxDB。"""
    if influx_write_api is None:
        return
    try:
        p = (
            Point("power_reading")
            .tag("meter_id", meter_id)
            .field("freq", float(data.get("freq") or 0))
            .field("v_avg", float(data.get("v_avg") or 0))
            .field("v_a", float(data.get("v_a") or 0))
            .field("v_b", float(data.get("v_b") or 0))
            .field("v_c", float(data.get("v_c") or 0))
            .field("i_avg", float(data.get("i_avg") or 0))
            .field("i_a", float(data.get("i_a") or 0))
            .field("i_b", float(data.get("i_b") or 0))
            .field("i_c", float(data.get("i_c") or 0))
            .field("p_sum", float(data.get("p_sum") or 0))
            .field("pf", float(data.get("pf") or 0))
            .field("s_kva", float(data.get("s_kva") or 0))
            .field("q_kvar", float(data.get("q_kvar") or 0))
        )
        influx_write_api.write(bucket=args.influxdb_bucket, record=p)
    except Exception as e:
        log.warning("InfluxDB 寫入失敗: %s", e)


def influx_query_history(meter_id, hours):
    """從 InfluxDB 查詢歷史資料。"""
    if influx_query_api is None:
        return None
    try:
        q = f'''
        from(bucket: "{args.influxdb_bucket}")
          |> range(start: -{int(hours)}h)
          |> filter(fn: (r) => r._measurement == "power_reading" and r.meter_id == "{meter_id}")
          |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> sort(columns: ["_time"])
        '''
        tables = influx_query_api.query(q, org=args.influxdb_org)
        rows = []
        for table in tables:
            for record in table.records:
                rows.append({
                    "ts": record.get_time().isoformat(),
                    "freq": record.values.get("freq"),
                    "v_avg": record.values.get("v_avg"),
                    "v_a": record.values.get("v_a"),
                    "v_b": record.values.get("v_b"),
                    "v_c": record.values.get("v_c"),
                    "i_avg": record.values.get("i_avg"),
                    "i_a": record.values.get("i_a"),
                    "i_b": record.values.get("i_b"),
                    "i_c": record.values.get("i_c"),
                    "p_sum": record.values.get("p_sum"),
                    "pf": record.values.get("pf"),
                    "s_kva": record.values.get("s_kva"),
                    "q_kvar": record.values.get("q_kvar"),
                })
        return rows
    except Exception as e:
        log.warning("InfluxDB 查詢失敗: %s", e)
        return None


def influx_query_kwh(meter_id, hours):
    """從 InfluxDB 計算 kWh。"""
    if influx_query_api is None:
        return None
    try:
        q = f'''
        from(bucket: "{args.influxdb_bucket}")
          |> range(start: -{int(hours)}h)
          |> filter(fn: (r) => r._measurement == "power_reading" and r.meter_id == "{meter_id}" and r._field == "p_sum")
          |> integral(unit: 1h)
        '''
        tables = influx_query_api.query(q, org=args.influxdb_org)
        for table in tables:
            for record in table.records:
                return record.get_value() / 1000.0  # W·h → kWh
        return 0.0
    except Exception as e:
        log.warning("InfluxDB kWh 查詢失敗: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════
# Section 3C: Ollama AI 分析
# ═══════════════════════════════════════════════════════════════════

OLLAMA_SYSTEM_PROMPT = """你是一位專業的電力系統分析師，專門分析智慧電表數據。請使用**繁體中文**回覆。

你的任務：
1. **用電分析**：解讀即時數據（電壓、電流、功率、功率因數），說明用電狀態
2. **設備推測**：根據功率大小與波動模式，推測可能運行的設備類型
3. **異常偵測**：識別電壓異常、功率因數過低、過載風險等問題
4. **節能建議**：提供具體可行的節能措施與改善方向
5. **趨勢解讀**：如有歷史數據，分析用電趨勢變化

輸出格式（使用 Markdown）：
## 即時用電概況
（簡述目前用電狀態）

## 設備推測
（根據功率推測可能的設備）

## 異常與風險
（列出發現的異常或潛在風險，無則說明正常）

## 節能建議
（具體可行的改善措施）

注意：
- 功率單位注意 W（瓦）和 kW（千瓦）的換算
- 功率因數低於 0.85 應提醒改善
- 電壓應在 110V±10% 或 220V±10% 範圍內
- 請保持專業但易懂的語氣
"""


def build_analysis_prompt(meter_id, user_note="", hours=1):
    """組裝使用者提示詞：即時數據 + 歷史趨勢 + 設備清單 + 使用者補充。"""
    parts = []

    # 即時數據
    data = latest_data.get(meter_id)
    if data:
        parts.append(f"## 即時電表數據 [{data.get('name', meter_id)}]")
        parts.append(f"- 時間: {data.get('ts', '--')}")
        parts.append(f"- 電壓 (平均): {data.get('v_avg', '--')} V")
        parts.append(f"- 電壓 A/B/C: {data.get('v_a','--')}/{data.get('v_b','--')}/{data.get('v_c','--')} V")
        parts.append(f"- 電流 (平均): {data.get('i_avg', '--')} A")
        parts.append(f"- 電流 A/B/C: {data.get('i_a','--')}/{data.get('i_b','--')}/{data.get('i_c','--')} A")
        parts.append(f"- 有功功率: {data.get('p_sum', '--')} W ({(data.get('p_sum',0) or 0)/1000:.3f} kW)")
        parts.append(f"- 功率因數: {data.get('pf', '--')}")
        parts.append(f"- 視在功率: {data.get('s_kva', '--')} kVA")
        parts.append(f"- 無功功率: {data.get('q_kvar', '--')} kVAr")
        parts.append(f"- 頻率: {data.get('freq', '--')} Hz")
        wire = meter_configs.get(meter_id, {})
        if wire:
            parts.append(f"- 接線方式: {wire.get('wire_code', '--')} ({wire.get('wire_name', '--')})")
            parts.append(f"- CT: {wire.get('ct_primary', '--')}A")
    else:
        parts.append(f"## 電表 {meter_id} — 目前無即時數據")

    # 簡短歷史趨勢
    try:
        ds = max(10, hours * 10)  # 時間越長取樣越疏
        hist = db_query_history(meter_id, hours, downsample=ds)
        if hist:
            p_vals = [r["p_sum"] for r in hist if r.get("p_sum") is not None]
            v_vals = [r["v_avg"] for r in hist if r.get("v_avg") is not None]
            if p_vals:
                parts.append(f"\n## 最近 {hours} 小時趨勢")
                parts.append(f"- 功率範圍: {min(p_vals):.0f}W ~ {max(p_vals):.0f}W (平均 {sum(p_vals)/len(p_vals):.0f}W)")
            if v_vals:
                parts.append(f"- 電壓範圍: {min(v_vals):.1f}V ~ {max(v_vals):.1f}V")
    except Exception:
        pass

    # 負載曲線分析
    try:
        lp = db_load_profile(meter_id, max(hours, 24))
        kpi = lp.get("kpi", {})
        if kpi and kpi.get("total_kwh", 0) > 0:
            parts.append(f"\n## 負載曲線分析")
            parts.append(f"- 負載率 (Load Factor): {kpi.get('load_factor', 0):.1%}")
            parts.append(f"- 尖峰功率: {kpi.get('peak_power_w', 0):.0f} W ({kpi.get('peak_power_w', 0)/1000:.3f} kW)")
            parts.append(f"- 平均功率: {kpi.get('avg_power_w', 0):.0f} W")
            parts.append(f"- 尖離峰用電比: {kpi.get('peak_ratio', 0):.1%} 尖峰 / {1 - kpi.get('peak_ratio', 0):.1%} 離峰")
            parts.append(f"- 總用電量: {kpi.get('total_kwh', 0):.2f} kWh")
            if kpi.get("demand_factor") is not None:
                parts.append(f"- 需量因數: {kpi['demand_factor']:.1%}（設備總額定 {kpi.get('total_rated_w', 0)} W）")
    except Exception:
        pass

    # 統計異常偵測
    try:
        anom = db_anomaly_detection(meter_id)
        high_anomalies = [a for a in anom.get("anomalies", []) if a["severity"] == "high"]
        if high_anomalies:
            parts.append(f"\n## 統計異常偵測（最近 24h）")
            parts.append(f"- 基線覆蓋率: {anom.get('baseline_coverage', 0):.0%}")
            for a in high_anomalies[:5]:
                parts.append(f"- {a['hour_bucket']}時 功率 {a['actual_power']:.0f}W（基線 {a['baseline_mean']:.0f}W, Z={a['z_score']:.1f}, {a['direction']} {abs(a['deviation_pct']):.0f}%）")
    except Exception:
        pass

    # 台電費率比較
    try:
        rate = db_rate_optimization(meter_id, max(hours, 168))
        plans = rate.get("plans", [])
        if plans:
            parts.append(f"\n## 台電費率比較（{rate.get('season', '')}）")
            for p in plans:
                tag = " ★推薦" if p["id"] == rate.get("recommended") else ""
                parts.append(f"- {p['name']}: 月費 NT${p['total_cost']:.0f}（均價 {p['avg_price']:.2f} 元/kWh）{tag}")
            if rate.get("monthly_savings", 0) > 0:
                parts.append(f"- 最佳方案每月可省 NT${rate['monthly_savings']:.0f}")
    except Exception:
        pass

    # 設備清單
    try:
        equips = db_get_equipment(meter_id)
        if equips:
            parts.append(f"\n## 已知設備清單")
            for eq in equips:
                w = f"{eq['rated_watts']}W" if eq.get("rated_watts") else "未知功率"
                h = f"，每日約 {eq['typical_hours']}h" if eq.get("typical_hours") else ""
                desc = f"（{eq['description']}）" if eq.get("description") else ""
                parts.append(f"- {eq['name']}: {w}{h} {desc}")
    except Exception:
        pass

    # 使用者補充說明
    if user_note:
        parts.append(f"\n## 使用者補充說明\n{user_note}")

    return "\n".join(parts)


async def call_ollama(meter_id, user_note="", hours=1):
    """呼叫 Ollama 進行 AI 分析。"""
    if not HAS_HTTPX:
        return {"ok": False, "error": "未安裝 httpx 套件。請執行 pip install httpx"}

    prompt = build_analysis_prompt(meter_id, user_note, hours)
    url = f"{args.ollama_url}/api/chat"
    payload = {
        "model": args.ollama_model,
        "messages": [
            {"role": "system", "content": OLLAMA_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = data.get("message", {}).get("content", "")
            # 儲存分析紀錄
            summary = (user_note or meter_id)[:100]
            await asyncio.to_thread(db_save_analysis, meter_id, summary, content, args.ollama_model)
            return {"ok": True, "content": content, "model": args.ollama_model}
    except httpx.TimeoutException:
        return {"ok": False, "error": "Ollama 回應逾時（120 秒），模型可能正在載入中，請稍後再試"}
    except httpx.ConnectError:
        return {"ok": False, "error": f"無法連線到 Ollama ({args.ollama_url})，請確認服務已啟動"}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"Ollama 回傳錯誤 HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": f"分析失敗: {str(e)}"}


async def check_ollama_status():
    """檢查 Ollama 連線狀態與可用模型。"""
    if not HAS_HTTPX:
        return {"online": False, "error": "未安裝 httpx"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # 檢查連線
            resp = await client.get(f"{args.ollama_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            model_ready = args.ollama_model in models
            return {
                "online": True,
                "url": args.ollama_url,
                "model": args.ollama_model,
                "model_ready": model_ready,
                "available_models": models[:20],
            }
    except Exception as e:
        return {"online": False, "url": args.ollama_url, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════
# Section 4: WebSocket 管理
# ═══════════════════════════════════════════════════════════════════

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        log.info("WebSocket +1 (共 %d)", len(self.active))

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        log.info("WebSocket -1 (共 %d)", len(self.active))

    async def broadcast(self, data: dict):
        msg = json.dumps(data, ensure_ascii=False)
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.active:
                self.active.remove(ws)

manager = ConnectionManager()

# ═══════════════════════════════════════════════════════════════════
# Section 5: 全域狀態 & 告警閾值
# ═══════════════════════════════════════════════════════════════════

meter_configs = {}
latest_data = {}
clients = {}
METERS = []
demo_mode = False  # 執行時期 demo 切換旗標

alert_thresholds = {
    "v_high": 250.0,
    "v_low": 190.0,
    "i_high": 80.0,
    "pf_low": 0.5,
    "p_high": 15000,
}
alert_cooldown = {}  # (meter_id, category) -> last_alert_time

# ─── 台電費率常數 ────────────────────────────────────────────────

TPC_RATES = {
    "tiered": {
        "name": "表燈非時間電價（累進費率）",
        "summer": [  # 夏月 6/1-9/30
            (120, 1.68), (330, 2.45), (500, 3.13),
            (700, 4.19), (1000, 5.03), (float("inf"), 6.03),
        ],
        "non_summer": [
            (120, 1.68), (330, 2.12), (500, 2.71),
            (700, 3.37), (1000, 3.97), (float("inf"), 4.60),
        ],
    },
    "tou2": {
        "name": "住宅型簡易時間電價二段式",
        "summer": {"peak": 4.71, "offpeak": 1.85, "basic": 75.0},
        "non_summer": {"peak": 4.37, "offpeak": 1.78, "basic": 75.0},
    },
    "tou3": {
        "name": "住宅型簡易時間電價三段式",
        "summer": {"peak": 5.42, "half_peak": 3.85, "offpeak": 1.62, "basic": 75.0},
        "non_summer": {"peak": 5.05, "half_peak": 3.52, "offpeak": 1.56, "basic": 75.0},
    },
}


def _get_season():
    """判斷當前是夏月或非夏月。"""
    month = datetime.now().month
    return "summer" if 6 <= month <= 9 else "non_summer"


def _calc_tiered_cost(total_kwh, season=None):
    """累進費率計算。"""
    if season is None:
        season = _get_season()
    tiers = TPC_RATES["tiered"][season]
    cost = 0.0
    remaining = total_kwh
    prev_limit = 0
    details = []
    for limit, rate in tiers:
        band = min(remaining, limit - prev_limit)
        if band <= 0:
            break
        band_cost = band * rate
        cost += band_cost
        lbl = f"{prev_limit + 1}-{int(limit)}" if limit != float("inf") else f"{prev_limit + 1}+"
        details.append({"range": lbl, "kwh": round(band, 2), "rate": rate, "cost": round(band_cost, 2)})
        remaining -= band
        prev_limit = limit
    return {
        "total_cost": round(cost, 2), "basic_fee": 0, "energy_cost": round(cost, 2),
        "avg_price": round(cost / total_kwh, 2) if total_kwh > 0 else 0, "details": details,
    }


def _calc_tou_cost(hourly_data, season=None):
    """時間電價計算（二段式）。"""
    if season is None:
        season = _get_season()
    rates = TPC_RATES["tou2"][season]
    peak_kwh = 0.0
    offpeak_kwh = 0.0
    for h in hourly_data:
        kwh = h.get("kwh", 0) or 0
        if h.get("is_peak", False):
            peak_kwh += kwh
        else:
            offpeak_kwh += kwh
    peak_cost = peak_kwh * rates["peak"]
    offpeak_cost = offpeak_kwh * rates["offpeak"]
    energy = peak_cost + offpeak_cost
    basic = rates["basic"]
    total = energy + basic
    total_kwh = peak_kwh + offpeak_kwh
    return {
        "total_cost": round(total, 2), "basic_fee": basic, "energy_cost": round(energy, 2),
        "peak_kwh": round(peak_kwh, 2), "offpeak_kwh": round(offpeak_kwh, 2),
        "avg_price": round(total / total_kwh, 2) if total_kwh > 0 else 0,
    }


def _calc_tou3_cost(hourly_data, season=None):
    """時間電價計算（三段式）。"""
    if season is None:
        season = _get_season()
    rates = TPC_RATES["tou3"][season]
    peak_kwh = 0.0
    half_peak_kwh = 0.0
    offpeak_kwh = 0.0
    for h in hourly_data:
        kwh = h.get("kwh", 0) or 0
        try:
            hh = int(h.get("hour_bucket", "T12").split("T")[1])
        except (IndexError, ValueError):
            hh = 12
        # 三段式：尖峰 16-22, 半尖峰 07:30-16 + 22-22:30, 離峰 22:30-07:30
        if 16 <= hh <= 21:
            peak_kwh += kwh
        elif 8 <= hh <= 15:
            half_peak_kwh += kwh
        else:
            offpeak_kwh += kwh
    peak_cost = peak_kwh * rates["peak"]
    half_cost = half_peak_kwh * rates["half_peak"]
    offpeak_cost = offpeak_kwh * rates["offpeak"]
    energy = peak_cost + half_cost + offpeak_cost
    basic = rates["basic"]
    total = energy + basic
    total_kwh = peak_kwh + half_peak_kwh + offpeak_kwh
    return {
        "total_cost": round(total, 2), "basic_fee": basic, "energy_cost": round(energy, 2),
        "peak_kwh": round(peak_kwh, 2), "half_peak_kwh": round(half_peak_kwh, 2),
        "offpeak_kwh": round(offpeak_kwh, 2),
        "avg_price": round(total / total_kwh, 2) if total_kwh > 0 else 0,
    }


def db_rate_optimization(meter_id, hours=720):
    """台電費率優化：比較各方案費用並推薦最佳方案。"""
    profile = db_load_profile(meter_id, hours)
    hourly = profile.get("hourly", [])
    total_kwh = profile.get("total_kwh", 0)

    if total_kwh <= 0:
        return {"error": "無足夠用電資料進行費率比較", "plans": []}

    # 月換算因子
    actual_hours = len(hourly)
    monthly_factor = 720.0 / actual_hours if actual_hours > 0 else 1
    monthly_kwh = total_kwh * monthly_factor

    season = _get_season()

    tiered = _calc_tiered_cost(monthly_kwh, season)

    tou2 = _calc_tou_cost(hourly, season)
    tou2_energy_monthly = round(tou2["energy_cost"] * monthly_factor, 2)
    tou2_monthly = {
        **tou2,
        "energy_cost": tou2_energy_monthly,
        "total_cost": round(tou2_energy_monthly + tou2["basic_fee"], 2),
        "avg_price": round((tou2_energy_monthly + tou2["basic_fee"]) / monthly_kwh, 2) if monthly_kwh > 0 else 0,
    }

    tou3 = _calc_tou3_cost(hourly, season)
    tou3_energy_monthly = round(tou3["energy_cost"] * monthly_factor, 2)
    tou3_monthly = {
        **tou3,
        "energy_cost": tou3_energy_monthly,
        "total_cost": round(tou3_energy_monthly + tou3["basic_fee"], 2),
        "avg_price": round((tou3_energy_monthly + tou3["basic_fee"]) / monthly_kwh, 2) if monthly_kwh > 0 else 0,
    }

    plans = [
        {"id": "tiered", "name": TPC_RATES["tiered"]["name"], **tiered, "monthly_kwh": round(monthly_kwh, 2)},
        {"id": "tou2", "name": TPC_RATES["tou2"]["name"], **tou2_monthly, "monthly_kwh": round(monthly_kwh, 2)},
        {"id": "tou3", "name": TPC_RATES["tou3"]["name"], **tou3_monthly, "monthly_kwh": round(monthly_kwh, 2)},
    ]

    best = min(plans, key=lambda p: p["total_cost"])
    worst = max(plans, key=lambda p: p["total_cost"])
    savings = round(worst["total_cost"] - best["total_cost"], 2)

    return {
        "plans": plans,
        "recommended": best["id"],
        "recommended_name": best["name"],
        "monthly_savings": savings,
        "season": "夏月" if season == "summer" else "非夏月",
        "monthly_kwh": round(monthly_kwh, 2),
        "actual_hours": actual_hours,
    }


def check_alerts(meter_id, data):
    now = datetime.now()
    def fire(level, category, msg, value, threshold):
        key = (meter_id, category)
        last = alert_cooldown.get(key)
        if last and (now - last).total_seconds() < 300:
            return  # 5 分鐘冷卻
        alert_cooldown[key] = now
        db_insert_alert(meter_id, level, category, msg, value, threshold)
        log.warning("告警 [%s] %s: %s", meter_id, category, msg)

    v = data.get("v_avg", 0)
    if v > alert_thresholds["v_high"]:
        fire("warning", "voltage_high",
             f"電壓過高: {v:.1f}V > {alert_thresholds['v_high']}V",
             v, alert_thresholds["v_high"])
    elif 0 < v < alert_thresholds["v_low"]:
        fire("critical", "voltage_low",
             f"電壓過低: {v:.1f}V < {alert_thresholds['v_low']}V",
             v, alert_thresholds["v_low"])

    i = data.get("i_avg", 0)
    if i > alert_thresholds["i_high"]:
        fire("critical", "current_high",
             f"電流過高: {i:.2f}A > {alert_thresholds['i_high']}A",
             i, alert_thresholds["i_high"])

    pf = data.get("pf", 0)
    p = data.get("p_sum", 0)
    if 0 < abs(pf) < alert_thresholds["pf_low"] and abs(p) > 100:
        fire("warning", "pf_low",
             f"功率因數過低: {pf:.3f} < {alert_thresholds['pf_low']}",
             pf, alert_thresholds["pf_low"])

    if abs(p) > alert_thresholds["p_high"]:
        fire("warning", "power_high",
             f"功率過高: {p}W > {alert_thresholds['p_high']}W",
             p, alert_thresholds["p_high"])


# ═══════════════════════════════════════════════════════════════════
# Section 6: Modbus 連線 & 背景任務
# ═══════════════════════════════════════════════════════════════════

def connect_meter(meter_id, host, port):
    if meter_id in clients:
        try:
            clients[meter_id].close()
        except Exception:
            pass
    client = ModbusTcpClient(host, port=port)
    if client.connect():
        clients[meter_id] = client
        log.info("電表 %s (%s) 連線成功", meter_id, host)
        return True
    else:
        log.warning("電表 %s (%s) 連線失敗", meter_id, host)
        return False


def read_meter_sync(meter_id, device_id, wire_code):
    client = clients.get(meter_id)
    if client is None:
        return None
    try:
        ts, data = read_meter(client, device_id, wire_code)
        return {"meter_ts": ts, "data": data}
    except Exception as e:
        log.warning("電表 %s 讀取錯誤: %s", meter_id, e)
        return None


def read_config_sync(meter_id, device_id):
    client = clients.get(meter_id)
    if client is None:
        return None
    try:
        return read_system_config(client, device_id)
    except Exception as e:
        log.warning("電表 %s 設定讀取錯誤: %s", meter_id, e)
        return None


# ─── Demo 模式（模擬資料）────────────────────────────────────────

import random
import math

_demo_t = 0

def demo_reading(meter_id):
    global _demo_t
    _demo_t += 1
    t = _demo_t
    base_v = 223.0 if meter_id == "meter_a" else 221.0
    base_i = 8.0 if meter_id == "meter_a" else 32.0
    base_pf = 0.88 if meter_id == "meter_a" else 0.99

    v_avg = base_v + math.sin(t * 0.05) * 3 + random.uniform(-0.5, 0.5)
    i_avg = base_i + math.sin(t * 0.03) * 2 + random.uniform(-0.3, 0.3)
    pf = min(1.0, max(0.3, base_pf + math.sin(t * 0.02) * 0.05 + random.uniform(-0.01, 0.01)))
    p_sum = int(v_avg * i_avg * pf)
    s_kva = (v_avg * i_avg) / 1000.0
    q_kvar = max(0, (s_kva**2 - (p_sum / 1000.0)**2) ** 0.5)
    freq = 60.0 + random.uniform(-0.02, 0.02)

    return {
        "meter_ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": {
            "freq": round(freq, 2),
            "v_a": round(v_avg + random.uniform(-1, 1), 1),
            "v_b": round(v_avg + random.uniform(-1, 1), 1),
            "v_c": round(v_avg + random.uniform(-1, 1), 1),
            "v_avg": round(v_avg, 1),
            "i_a": round(i_avg * 0.9 + random.uniform(-0.1, 0.1), 3),
            "i_b": round(i_avg * 1.05 + random.uniform(-0.1, 0.1), 3),
            "i_c": round(i_avg * 1.05 + random.uniform(-0.1, 0.1), 3),
            "i_avg": round(i_avg, 3),
            "p_sum": p_sum,
            "pf": round(pf, 3),
            "s_kva": round(s_kva, 3),
            "q_kvar": round(q_kvar, 3),
        },
    }


# ─── 背景輪詢 ────────────────────────────────────────────────────

async def poll_meters():
    reconnect_countdown = {}
    while True:
        for m in METERS:
            mid = m["id"]

            if demo_mode:
                result = demo_reading(mid)
            else:
                if mid not in clients or clients[mid] is None:
                    cd = reconnect_countdown.get(mid, 0)
                    if cd > 0:
                        reconnect_countdown[mid] = cd - 1
                        continue
                    ok = await asyncio.to_thread(connect_meter, mid, m["host"], args.modbus_port)
                    if ok:
                        cfg = await asyncio.to_thread(read_config_sync, mid, args.device_id)
                        if cfg:
                            meter_configs[mid] = cfg
                        reconnect_countdown[mid] = 0
                    else:
                        reconnect_countdown[mid] = 5
                        continue

                wire_code = meter_configs.get(mid, {}).get("wire_code", "1P3W")
                result = await asyncio.to_thread(read_meter_sync, mid, args.device_id, wire_code)

                if result is None:
                    log.warning("電表 %s 讀取失敗", mid)
                    try:
                        clients[mid].close()
                    except Exception:
                        pass
                    clients[mid] = None
                    reconnect_countdown[mid] = 2
                    continue

            now = datetime.now().isoformat(timespec="seconds")
            latest_data[mid] = {
                "meter_id": mid,
                "name": m["name"],
                "ts": now,
                "meter_ts": result["meter_ts"],
                "connected": True,
                **result["data"],
            }
            await asyncio.to_thread(db_insert, mid, now, result["meter_ts"], result["data"])
            if influx_write_api:
                await asyncio.to_thread(influx_write, mid, result["data"])
            check_alerts(mid, result["data"])

        if latest_data:
            # 取得未確認告警數量
            unacked = db_query_alerts(limit=100, unacked_only=True)
            await manager.broadcast({
                "type": "update",
                "meters": latest_data,
                "alert_count": len(unacked),
            })

        await asyncio.sleep(args.interval)


async def periodic_cleanup():
    while True:
        await asyncio.sleep(3600)
        deleted = await asyncio.to_thread(db_cleanup, 30)
        if deleted > 0:
            log.info("清理 %d 筆過期資料", deleted)


# ═══════════════════════════════════════════════════════════════════
# Section 7: FastAPI 應用
# ═══════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    global METERS
    init_db()
    log.info("SQLite: %s", DB_PATH)
    init_influxdb()

    METERS = [
        {"id": "meter_a", "name": args.name_a, "host": args.host_a},
        {"id": "meter_b", "name": args.name_b, "host": args.host_b},
    ]

    global demo_mode
    demo_mode = args.demo  # CLI 指定的初始值

    if not demo_mode and HAS_MODBUS:
        for m in METERS:
            ok = connect_meter(m["id"], m["host"], args.modbus_port)
            if ok:
                cfg = read_config_sync(m["id"], args.device_id)
                if cfg:
                    meter_configs[m["id"]] = cfg
    if demo_mode:
        _apply_demo_configs()
        log.info("Demo 模式啟動（模擬資料）")

    poll_task = asyncio.create_task(poll_meters())
    cleanup_task = asyncio.create_task(periodic_cleanup())
    log.info("背景輪詢啟動 (間隔 %.1f 秒)", args.interval)
    yield
    poll_task.cancel()
    cleanup_task.cancel()
    for c in clients.values():
        try:
            c.close()
        except Exception:
            pass

app = FastAPI(title="CPM-80 Dashboard", lifespan=lifespan)


def _apply_demo_configs():
    """切到 demo 時補上模擬設定。"""
    for m in METERS:
        if m["id"] not in meter_configs:
            meter_configs[m["id"]] = {
                "wire_code": "1P3W", "wire_name": "單相三線",
                "pt_primary": 600, "pt_secondary": 600,
                "ct_primary": 100, "ct_secondary": "333mV",
            }


# ─── REST API ─────────────────────────────────────────────────────

@app.get("/api/config")
async def api_config():
    result = {"_meta": {"demo": demo_mode}}
    for m in METERS:
        mid = m["id"]
        cfg = meter_configs.get(mid)
        connected = (mid in clients and clients[mid] is not None) or demo_mode
        result[mid] = {"name": m["name"], "host": m["host"], "connected": connected, "config": cfg}
    return JSONResponse(result)


@app.post("/api/demo/toggle")
async def api_demo_toggle():
    global demo_mode
    demo_mode = not demo_mode
    if demo_mode:
        _apply_demo_configs()
    log.info("Demo 模式: %s", "開啟" if demo_mode else "關閉")
    return JSONResponse({"demo": demo_mode})


@app.get("/api/latest")
async def api_latest():
    return JSONResponse(latest_data)


@app.get("/api/history")
async def api_history(
    meter_id: str = Query(default="meter_a"),
    hours: float = Query(default=1.0),
    downsample: int = Query(default=0),
):
    rows = await asyncio.to_thread(
        db_query_history, meter_id, hours, downsample if downsample > 0 else None
    )
    return JSONResponse(rows)


@app.get("/api/alerts")
async def api_alerts(
    limit: int = Query(default=50),
    unacked: bool = Query(default=False),
):
    rows = await asyncio.to_thread(db_query_alerts, limit, unacked)
    return JSONResponse(rows)


@app.post("/api/alerts/{alert_id}/ack")
async def api_ack_alert(alert_id: int):
    await asyncio.to_thread(db_ack_alert, alert_id)
    return JSONResponse({"ok": True})


@app.post("/api/alerts/ack-all")
async def api_ack_all():
    await asyncio.to_thread(db_ack_all_alerts)
    return JSONResponse({"ok": True})


@app.get("/api/alerts/thresholds")
async def api_get_thresholds():
    return JSONResponse(alert_thresholds)


@app.post("/api/alerts/thresholds")
async def api_set_thresholds(body: dict = None):
    if body:
        for k in alert_thresholds:
            if k in body:
                alert_thresholds[k] = float(body[k])
    return JSONResponse(alert_thresholds)


@app.get("/api/billing")
async def api_billing(
    meter_id: str = Query(default="meter_a"),
    hours: float = Query(default=24),
    rate: float = Query(default=3.5),
):
    kwh = await asyncio.to_thread(db_billing_kwh, meter_id, hours)
    return JSONResponse({
        "meter_id": meter_id,
        "hours": hours,
        "kwh": round(kwh, 2),
        "rate": rate,
        "cost": round(kwh * rate, 2),
    })


@app.get("/api/export")
async def api_export(
    meter_id: str = Query(default="meter_a"),
    hours: float = Query(default=24),
):
    rows = await asyncio.to_thread(db_export_csv, meter_id, hours)
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    output.seek(0)
    filename = f"cpm80_{meter_id}_{int(hours)}h_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ─── AI 分析 API ──────────────────────────────────────────────────

@app.post("/api/analysis")
async def api_analysis(body: dict = None):
    body = body or {}
    meter_id = body.get("meter_id", "meter_a")
    user_note = body.get("note", "")
    hours = max(1, min(720, int(body.get("hours", 1))))
    result = await call_ollama(meter_id, user_note, hours)
    return JSONResponse(result)


@app.get("/api/analysis/history")
async def api_analysis_history(
    meter_id: str = Query(default=None),
    limit: int = Query(default=20),
):
    rows = await asyncio.to_thread(db_get_analysis_history, meter_id, limit)
    return JSONResponse(rows)


@app.get("/api/analysis/status")
async def api_analysis_status():
    status = await check_ollama_status()
    return JSONResponse(status)


@app.get("/api/analysis/load-profile")
async def api_load_profile(
    meter_id: str = Query(default="meter_a"),
    hours: int = Query(default=24),
):
    result = await asyncio.to_thread(db_load_profile, meter_id, hours)
    return JSONResponse(result)


@app.get("/api/analysis/anomaly")
async def api_anomaly(
    meter_id: str = Query(default="meter_a"),
    baseline_days: int = Query(default=7),
):
    result = await asyncio.to_thread(db_anomaly_detection, meter_id, baseline_days)
    return JSONResponse(result)


@app.get("/api/analysis/rate-optimization")
async def api_rate_optimization(
    meter_id: str = Query(default="meter_a"),
    hours: int = Query(default=720),
):
    result = await asyncio.to_thread(db_rate_optimization, meter_id, hours)
    return JSONResponse(result)


# ─── 設備管理 API ─────────────────────────────────────────────────

@app.get("/api/equipment")
async def api_equipment(meter_id: str = Query(default=None)):
    rows = await asyncio.to_thread(db_get_equipment, meter_id)
    return JSONResponse(rows)


@app.post("/api/equipment")
async def api_add_equipment(body: dict = None):
    body = body or {}
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "名稱不可為空"}, status_code=400)
    eq_id = await asyncio.to_thread(
        db_add_equipment,
        body.get("meter_id", "meter_a"),
        name,
        body.get("rated_watts"),
        body.get("description", ""),
        body.get("typical_hours"),
    )
    return JSONResponse({"ok": True, "id": eq_id})


@app.put("/api/equipment/{eq_id}")
async def api_update_equipment(eq_id: int, body: dict = None):
    body = body or {}
    await asyncio.to_thread(
        db_update_equipment, eq_id,
        meter_id=body.get("meter_id"),
        name=body.get("name"),
        rated_watts=body.get("rated_watts"),
        description=body.get("description"),
        typical_hours=body.get("typical_hours"),
    )
    return JSONResponse({"ok": True})


@app.delete("/api/equipment/{eq_id}")
async def api_delete_equipment(eq_id: int):
    await asyncio.to_thread(db_delete_equipment, eq_id)
    return JSONResponse({"ok": True})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    if latest_data:
        unacked = db_query_alerts(limit=100, unacked_only=True)
        await ws.send_text(json.dumps({
            "type": "update", "meters": latest_data, "alert_count": len(unacked),
        }, ensure_ascii=False))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ═══════════════════════════════════════════════════════════════════
# Section 8: 前端頁面
# ═══════════════════════════════════════════════════════════════════

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>CPM-80 智慧電表能源管理系統</title>
<script src="https://cdn.jsdelivr.net/npm/vue@3/dist/vue.global.prod.js"></script>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
/* ═══ CSS Reset & Variables ═══ */
:root {
  --bg-0: #0b0f19;
  --bg-1: #111827;
  --bg-2: #1a2332;
  --bg-3: #243044;
  --border: #2a3a50;
  --border-light: #334766;
  --blue: #3b82f6;
  --blue-dim: rgba(59,130,246,0.15);
  --orange: #f59e0b;
  --orange-dim: rgba(245,158,11,0.12);
  --green: #10b981;
  --green-dim: rgba(16,185,129,0.12);
  --red: #ef4444;
  --red-dim: rgba(239,68,68,0.12);
  --purple: #8b5cf6;
  --cyan: #06b6d4;
  --text-0: #f1f5f9;
  --text-1: #cbd5e1;
  --text-2: #94a3b8;
  --text-3: #64748b;
  --sidebar-w: 220px;
  --header-h: 56px;
  /* ECharts theme tokens (dark) */
  --chart-tooltip-bg: #1e293b;
  --chart-tooltip-border: #334155;
  --chart-tooltip-text: #f1f5f9;
  --chart-axis: #334155;
  --chart-label: #64748b;
  --chart-split: #1e293b;
}

/* ═══ Light Theme (macOS-inspired) ═══ */
[data-theme="light"] {
  --bg-0: #f5f5f7;
  --bg-1: #ffffff;
  --bg-2: #f0f0f5;
  --bg-3: #e5e5ea;
  --border: #d1d1d6;
  --border-light: #c7c7cc;
  --blue: #007aff;
  --blue-dim: rgba(0,122,255,0.10);
  --orange: #ff9500;
  --orange-dim: rgba(255,149,0,0.10);
  --green: #34c759;
  --green-dim: rgba(52,199,89,0.10);
  --red: #ff3b30;
  --red-dim: rgba(255,59,48,0.10);
  --purple: #af52de;
  --cyan: #32ade6;
  --text-0: #1d1d1f;
  --text-1: #3a3a3c;
  --text-2: #636366;
  --text-3: #8e8e93;
  --chart-tooltip-bg: #ffffff;
  --chart-tooltip-border: #d1d1d6;
  --chart-tooltip-text: #1d1d1f;
  --chart-axis: #d1d1d6;
  --chart-label: #8e8e93;
  --chart-split: #f0f0f5;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  background: var(--bg-0);
  color: var(--text-0);
  overflow-x: hidden;
}
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: var(--bg-1); }
::-webkit-scrollbar-thumb { background: var(--bg-3); border-radius: 3px; }
[data-theme="light"] ::-webkit-scrollbar-track { background: #f0f0f5; }
[data-theme="light"] ::-webkit-scrollbar-thumb { background: #c7c7cc; }

/* ─── Theme Toggle ─── */
.theme-toggle {
  display: flex; align-items: center; gap: 6px; cursor: pointer;
  background: var(--bg-2); border: 1px solid var(--border); border-radius: 20px;
  padding: 4px 10px; font-size: 12px; color: var(--text-2);
  transition: all 0.3s; user-select: none;
}
.theme-toggle:hover { border-color: var(--blue); color: var(--text-1); }
.theme-toggle-track {
  position: relative; width: 36px; height: 20px;
  background: var(--bg-3); border-radius: 10px;
  transition: background 0.3s;
}
.theme-toggle-track .knob {
  position: absolute; top: 2px; left: 2px;
  width: 16px; height: 16px; border-radius: 50%;
  background: #f59e0b; transition: all 0.3s;
  display: flex; align-items: center; justify-content: center;
  font-size: 10px; line-height: 1;
}
.theme-toggle-track.light .knob {
  left: 18px; background: #007aff;
}
[data-theme="light"] .theme-toggle-track {
  background: #d1d1d6;
}

/* ═══ Layout ═══ */
.app-layout { display: flex; min-height: 100vh; }

/* ─── Sidebar ─── */
.sidebar {
  width: var(--sidebar-w); background: var(--bg-1);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  position: fixed; top: 0; left: 0; height: 100vh; z-index: 100;
  transition: transform 0.3s;
}
.sidebar-brand {
  padding: 18px 20px; border-bottom: 1px solid var(--border);
}
.sidebar-brand h1 {
  font-size: 17px; font-weight: 700; color: var(--blue);
  letter-spacing: 0.5px;
}
.sidebar-brand p {
  font-size: 11px; color: var(--text-3); margin-top: 2px;
}
.sidebar-nav { flex: 1; padding: 12px 8px; }
.nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 14px; border-radius: 8px; cursor: pointer;
  color: var(--text-2); font-size: 14px; font-weight: 500;
  transition: all 0.15s; margin-bottom: 2px; position: relative;
  user-select: none;
}
.nav-item:hover { background: var(--bg-2); color: var(--text-0); }
.nav-item.active {
  background: var(--blue-dim); color: var(--blue);
}
.nav-item .nav-icon { font-size: 18px; width: 22px; text-align: center; }
.nav-badge {
  position: absolute; right: 10px; top: 50%; transform: translateY(-50%);
  background: var(--red); color: #fff; font-size: 10px; font-weight: 700;
  padding: 1px 6px; border-radius: 10px; min-width: 18px; text-align: center;
}
.sidebar-footer {
  padding: 14px 16px; border-top: 1px solid var(--border);
  font-size: 11px; color: var(--text-3);
}
.sidebar-footer .ws-dot {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 5px;
}
.ws-dot.on { background: var(--green); box-shadow: 0 0 6px var(--green); }
.ws-dot.off { background: var(--red); box-shadow: 0 0 6px var(--red); }

/* ─── Main ─── */
.main-content {
  flex: 1; margin-left: var(--sidebar-w);
  display: flex; flex-direction: column; min-height: 100vh;
}
.topbar {
  height: var(--header-h); background: var(--bg-1);
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 24px; position: sticky; top: 0; z-index: 50;
}
.topbar-title { font-size: 15px; font-weight: 600; color: var(--text-1); }
.topbar-right { display: flex; align-items: center; gap: 16px; }
.topbar-time { font-size: 12px; color: var(--text-3); font-family: 'SF Mono', monospace; }
.data-mode-badge {
  font-size: 11px; font-weight: 600; padding: 3px 10px;
  border-radius: 20px; letter-spacing: 0.3px;
  cursor: pointer; user-select: none; transition: all 0.2s;
}
.data-mode-badge:hover { filter: brightness(1.3); transform: scale(1.05); }
.data-mode-badge.live { background: var(--green-dim); color: var(--green); border: 1px solid var(--green); }
.data-mode-badge.demo { background: var(--orange-dim); color: var(--orange); border: 1px solid var(--orange); }
.mobile-menu-btn {
  display: none; background: none; border: none; color: var(--text-1);
  font-size: 22px; cursor: pointer; padding: 4px;
}

.page-content { flex: 1; padding: 20px 24px; }

/* ═══ Dashboard Page ═══ */
/* KPI Cards */
.kpi-grid {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 14px; margin-bottom: 20px;
}
.kpi-card {
  background: var(--bg-1); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px;
  transition: border-color 0.2s;
}
.kpi-card:hover { border-color: var(--border-light); }
.kpi-label { font-size: 11px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
.kpi-value { font-size: 28px; font-weight: 700; font-family: 'SF Mono', 'Menlo', monospace; }
.kpi-unit { font-size: 13px; color: var(--text-2); font-weight: 400; margin-left: 4px; }
.kpi-sub { font-size: 11px; color: var(--text-3); margin-top: 4px; }
.kpi-card.accent-green .kpi-value { color: var(--green); }
.kpi-card.accent-blue .kpi-value { color: var(--blue); }
.kpi-card.accent-orange .kpi-value { color: var(--orange); }
.kpi-card.accent-purple .kpi-value { color: var(--purple); }

/* Meter Cards */
.meters-row {
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 16px; margin-bottom: 20px;
}
.meter-card {
  background: var(--bg-1); border: 1px solid var(--border);
  border-radius: 10px; overflow: hidden;
}
.meter-card.color-a { border-top: 3px solid var(--blue); }
.meter-card.color-b { border-top: 3px solid var(--orange); }
.meter-card.color-demo { border-top: 3px solid var(--purple); }
.color-demo .meter-name { color: var(--purple) !important; }
.meter-head {
  display: flex; justify-content: space-between; align-items: center;
  padding: 14px 18px; border-bottom: 1px solid var(--border);
}
.meter-name { font-size: 15px; font-weight: 600; }
.color-a .meter-name { color: var(--blue); }
.color-b .meter-name { color: var(--orange); }
.meter-status {
  display: flex; align-items: center; gap: 6px;
  font-size: 11px; color: var(--text-3);
}
.meter-status .dot {
  width: 7px; height: 7px; border-radius: 50%;
}
.dot.on { background: var(--green); box-shadow: 0 0 5px var(--green); }
.dot.off { background: var(--red); }
.meter-body { padding: 14px 18px; }
.meter-grid {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
}
.m-item { padding: 8px 10px; background: var(--bg-2); border-radius: 6px; }
.m-label { font-size: 10px; color: var(--text-3); text-transform: uppercase; margin-bottom: 3px; }
.m-val { font-size: 18px; font-weight: 600; font-family: 'SF Mono', monospace; color: var(--text-0); }
.m-unit { font-size: 11px; color: var(--text-2); }

/* Charts */
.charts-row {
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 16px; margin-bottom: 20px;
}
.chart-card {
  background: var(--bg-1); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px;
}
.chart-title { font-size: 12px; color: var(--text-2); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; display: flex; align-items: center; gap: 8px; }
.demo-tag {
  font-size: 9px; font-weight: 700; text-transform: uppercase;
  background: var(--purple); color: #fff; padding: 1px 6px;
  border-radius: 8px; letter-spacing: 0.5px;
}
.chart-box { width: 100%; height: 240px; }

.range-bar {
  display: flex; gap: 6px; margin-bottom: 16px; align-items: center;
}
.range-bar span { font-size: 12px; color: var(--text-3); margin-right: 4px; }
.range-btn {
  background: var(--bg-2); border: 1px solid var(--border); color: var(--text-2);
  padding: 5px 14px; border-radius: 6px; cursor: pointer;
  font-size: 12px; font-family: inherit; transition: all 0.15s;
}
.range-btn:hover { border-color: var(--blue); color: var(--text-0); }
.range-btn.active { background: var(--blue); border-color: var(--blue); color: #fff; }

/* ═══ Alerts Page ═══ */
.alert-list { display: flex; flex-direction: column; gap: 8px; }
.alert-row {
  display: flex; align-items: center; gap: 14px;
  background: var(--bg-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 16px;
  transition: background 0.15s;
}
.alert-row:hover { background: var(--bg-2); }
.alert-row.unacked { border-left: 3px solid var(--red); }
.alert-dot {
  width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
}
.alert-dot.critical { background: var(--red); box-shadow: 0 0 6px var(--red); }
.alert-dot.warning { background: var(--orange); box-shadow: 0 0 6px var(--orange); }
.alert-dot.info { background: var(--blue); }
.alert-content { flex: 1; min-width: 0; }
.alert-msg { font-size: 13px; color: var(--text-0); }
.alert-meta { font-size: 11px; color: var(--text-3); margin-top: 2px; }
.alert-ack-btn {
  background: var(--bg-3); border: none; color: var(--text-2);
  padding: 5px 12px; border-radius: 5px; cursor: pointer;
  font-size: 11px; font-family: inherit; transition: all 0.15s;
}
.alert-ack-btn:hover { background: var(--blue); color: #fff; }
.alert-toolbar {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 14px;
}
.alert-toolbar h3 { font-size: 16px; color: var(--text-1); }
.btn-outline {
  background: transparent; border: 1px solid var(--border);
  color: var(--text-2); padding: 6px 14px; border-radius: 6px;
  cursor: pointer; font-size: 12px; font-family: inherit; transition: all 0.15s;
}
.btn-outline:hover { border-color: var(--blue); color: var(--blue); }

/* ═══ Reports Page ═══ */
.report-grid {
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.report-card {
  background: var(--bg-1); border: 1px solid var(--border);
  border-radius: 10px; padding: 20px;
}
.report-card h3 { font-size: 15px; color: var(--text-1); margin-bottom: 16px; }
.form-group { margin-bottom: 14px; }
.form-label { display: block; font-size: 12px; color: var(--text-3); margin-bottom: 5px; }
.form-select, .form-input {
  width: 100%; background: var(--bg-2); border: 1px solid var(--border);
  color: var(--text-0); padding: 8px 12px; border-radius: 6px;
  font-size: 13px; font-family: inherit; outline: none;
}
.form-select:focus, .form-input:focus { border-color: var(--blue); }
.form-select option { background: var(--bg-2); }
.btn-primary {
  background: var(--blue); border: none; color: #fff;
  padding: 8px 20px; border-radius: 6px; cursor: pointer;
  font-size: 13px; font-weight: 600; font-family: inherit;
  transition: background 0.15s;
}
.btn-primary:hover { background: #2563eb; }
.billing-result {
  background: var(--bg-2); border-radius: 8px;
  padding: 16px; margin-top: 14px;
}
.billing-row {
  display: flex; justify-content: space-between; align-items: center;
  padding: 6px 0; font-size: 13px;
}
.billing-row .label { color: var(--text-2); }
.billing-row .value { color: var(--text-0); font-weight: 600; font-family: 'SF Mono', monospace; }
.billing-total {
  border-top: 1px solid var(--border); padding-top: 10px; margin-top: 6px;
}
.billing-total .value { font-size: 22px; color: var(--green); }

/* ═══ Settings Page ═══ */
.settings-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
}
.threshold-row {
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 0; border-bottom: 1px solid var(--border);
}
.threshold-row:last-child { border-bottom: none; }
.threshold-label { font-size: 13px; color: var(--text-1); }
.threshold-input {
  width: 100px; background: var(--bg-2); border: 1px solid var(--border);
  color: var(--text-0); padding: 6px 10px; border-radius: 5px;
  font-size: 13px; font-family: 'SF Mono', monospace; text-align: right; outline: none;
}
.threshold-input:focus { border-color: var(--blue); }

/* ═══ AI Analysis Page ═══ */
.ai-layout {
  display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
}
.ai-panel {
  background: var(--bg-1); border: 1px solid var(--border);
  border-radius: 10px; padding: 20px;
}
.ai-panel h3 { font-size: 15px; color: var(--text-1); margin-bottom: 16px; }
.ai-status {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 14px; border-radius: 8px; margin-bottom: 16px;
  font-size: 13px;
}
.ai-status.online { background: var(--green-dim); color: var(--green); border: 1px solid rgba(16,185,129,0.3); }
.ai-status.offline { background: var(--red-dim); color: var(--red); border: 1px solid rgba(239,68,68,0.3); }
.ai-status.checking { background: var(--blue-dim); color: var(--blue); border: 1px solid rgba(59,130,246,0.3); }
.ai-status .st-dot {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
}
.ai-status .st-dot.on { background: var(--green); box-shadow: 0 0 6px var(--green); }
.ai-status .st-dot.off { background: var(--red); }
.ai-status .st-dot.spin {
  background: var(--blue); animation: pulse 1s infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
.ai-result {
  background: var(--bg-2); border-radius: 8px; padding: 16px;
  margin-top: 14px; font-size: 13px; line-height: 1.8;
  color: var(--text-1); max-height: 500px; overflow-y: auto;
}
.ai-result h2 { font-size: 15px; color: var(--blue); margin: 14px 0 6px 0; }
.ai-result h2:first-child { margin-top: 0; }
.ai-result ul, .ai-result ol { padding-left: 20px; margin: 6px 0; }
.ai-result li { margin: 3px 0; }
.ai-result strong { color: var(--text-0); }
.ai-result code { background: var(--bg-3); padding: 1px 5px; border-radius: 3px; font-size: 12px; }
.ai-loading {
  display: flex; align-items: center; gap: 10px;
  padding: 20px; color: var(--text-2); font-size: 13px;
}
.ai-spinner {
  width: 20px; height: 20px; border: 2px solid var(--bg-3);
  border-top-color: var(--blue); border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.ai-error {
  background: var(--red-dim); border: 1px solid rgba(239,68,68,0.3);
  border-radius: 8px; padding: 12px 16px; margin-top: 14px;
  font-size: 13px; color: var(--red);
}
.ai-history-item {
  padding: 10px 0; border-bottom: 1px solid var(--border);
  cursor: pointer; transition: background 0.15s;
}
.ai-history-item:hover { background: var(--bg-2); margin: 0 -12px; padding: 10px 12px; border-radius: 6px; }
.ai-history-item:last-child { border-bottom: none; }
.ai-history-meta { font-size: 11px; color: var(--text-3); margin-bottom: 3px; }
.ai-history-summary { font-size: 12px; color: var(--text-2); }
.eq-table {
  width: 100%; border-collapse: collapse; font-size: 13px;
}
.eq-table th {
  text-align: left; padding: 8px 10px; font-size: 11px;
  color: var(--text-3); text-transform: uppercase; letter-spacing: 0.5px;
  border-bottom: 1px solid var(--border);
}
.eq-table td {
  padding: 8px 10px; border-bottom: 1px solid var(--border);
  color: var(--text-1);
}
.eq-table tr:hover td { background: var(--bg-2); }
.eq-actions { display: flex; gap: 6px; }
.eq-actions button {
  background: var(--bg-3); border: none; color: var(--text-2);
  padding: 3px 8px; border-radius: 4px; cursor: pointer;
  font-size: 11px; font-family: inherit; transition: all 0.15s;
}
.eq-actions button:hover { background: var(--blue); color: #fff; }
.eq-actions button.del:hover { background: var(--red); }
.form-textarea {
  width: 100%; background: var(--bg-2); border: 1px solid var(--border);
  color: var(--text-0); padding: 8px 12px; border-radius: 6px;
  font-size: 13px; font-family: inherit; outline: none;
  resize: vertical; min-height: 60px;
}
.form-textarea:focus { border-color: var(--blue); }
.btn-success {
  background: var(--green); border: none; color: #fff;
  padding: 8px 20px; border-radius: 6px; cursor: pointer;
  font-size: 13px; font-weight: 600; font-family: inherit;
  transition: background 0.15s;
}
.btn-success:hover { background: #059669; }
.btn-sm {
  padding: 5px 12px; font-size: 12px;
}
.btn-danger {
  background: var(--red); border: none; color: #fff;
  padding: 5px 12px; border-radius: 6px; cursor: pointer;
  font-size: 12px; font-family: inherit;
}
.ai-auto-bar {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 16px; border-radius: 8px; margin-bottom: 16px;
  border: 1px solid var(--border);
}
.ai-auto-bar.active {
  background: rgba(139,92,246,0.08); border-color: rgba(139,92,246,0.4);
}
.ai-auto-bar.inactive { background: var(--bg-2); }
.auto-toggle {
  position: relative; width: 44px; height: 24px;
  background: var(--bg-3); border-radius: 12px; cursor: pointer;
  transition: background 0.2s; flex-shrink: 0;
}
.auto-toggle.on { background: var(--purple); }
.auto-toggle .knob {
  position: absolute; top: 2px; left: 2px;
  width: 20px; height: 20px; background: #fff;
  border-radius: 50%; transition: transform 0.2s;
}
.auto-toggle.on .knob { transform: translateX(20px); }
.ai-live-dot {
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--purple); flex-shrink: 0;
  animation: live-pulse 1.5s ease-in-out infinite;
}
@keyframes live-pulse {
  0%,100% { box-shadow: 0 0 0 0 rgba(139,92,246,0.6); }
  50% { box-shadow: 0 0 0 8px rgba(139,92,246,0); }
}
.interval-select {
  background: var(--bg-1); border: 1px solid var(--border);
  color: var(--text-0); padding: 4px 8px; border-radius: 5px;
  font-size: 12px; font-family: inherit; outline: none;
}
.interval-select:focus { border-color: var(--purple); }
.ai-counter {
  font-size: 11px; color: var(--text-3); font-family: 'SF Mono', monospace;
}

/* ═══ Empty state ═══ */
.empty-state {
  text-align: center; padding: 60px 20px; color: var(--text-3);
}
.empty-state .icon { font-size: 40px; margin-bottom: 12px; }
.empty-state p { font-size: 14px; }

/* ═══ Mobile Overlay ═══ */
.sidebar-overlay {
  display: none; position: fixed; top: 0; left: 0;
  width: 100%; height: 100%; background: rgba(0,0,0,0.5);
  z-index: 99;
}

/* ═══ RWD ═══ */
@media (max-width: 1024px) {
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
  .meters-row { grid-template-columns: 1fr; }
  .charts-row { grid-template-columns: 1fr; }
  .report-grid { grid-template-columns: 1fr; }
  .settings-grid { grid-template-columns: 1fr; }
  .ai-layout { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
  .sidebar { transform: translateX(-100%); }
  .sidebar.open { transform: translateX(0); }
  .sidebar-overlay.show { display: block; }
  .main-content { margin-left: 0; }
  .mobile-menu-btn { display: block; }
  .page-content { padding: 14px; }
  .kpi-grid { grid-template-columns: 1fr 1fr; gap: 10px; }
  .kpi-value { font-size: 22px; }
  .meter-grid { grid-template-columns: repeat(2, 1fr); }
  .m-val { font-size: 15px; }
}
@media (max-width: 480px) {
  .kpi-grid { grid-template-columns: 1fr; }
}

/* ═══ Load Profile / Analysis KPI ═══ */
.analysis-kpi-grid {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px;
}
.analysis-kpi-card {
  background: var(--bg-1); border: 1px solid var(--border);
  border-left: 4px solid var(--blue); border-radius: 8px; padding: 14px;
}
.analysis-kpi-card.green { border-left-color: var(--green); }
.analysis-kpi-card.orange { border-left-color: var(--orange); }
.analysis-kpi-card.purple { border-left-color: #9b59b6; }
.analysis-kpi-label { font-size: 11px; color: var(--text-3); margin-bottom: 4px; }
.analysis-kpi-value { font-size: 20px; font-weight: 700; color: var(--text-1); }
.analysis-kpi-sub { font-size: 11px; color: var(--text-3); margin-top: 2px; }
.analysis-section { margin-top: 16px; }
.analysis-section h3 { margin-bottom: 12px; }
.anomaly-badge {
  display: inline-block; padding: 2px 10px; border-radius: 12px;
  font-size: 12px; font-weight: 600; margin-right: 6px;
}
.anomaly-badge.high { background: rgba(231,76,60,0.15); color: var(--red); }
.anomaly-badge.medium { background: rgba(241,196,15,0.15); color: var(--orange); }
.rate-recommend {
  background: rgba(46,204,113,0.1); border: 1px solid var(--green);
  border-radius: 8px; padding: 14px; margin-bottom: 12px;
}
.rate-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.rate-table th, .rate-table td {
  padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--border);
}
.rate-table th { text-align: left; font-weight: 600; color: var(--text-2); font-size: 12px; }
.rate-table td:first-child, .rate-table th:first-child { text-align: left; }
.rate-table tr.best { background: rgba(46,204,113,0.08); font-weight: 600; }
@media (max-width: 1024px) {
  .analysis-kpi-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 480px) {
  .analysis-kpi-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<div id="app">
  <!-- Sidebar Overlay (mobile) -->
  <div class="sidebar-overlay" :class="{show: sidebarOpen}" @click="sidebarOpen=false"></div>

  <div class="app-layout">
    <!-- ─── Sidebar ─── -->
    <aside class="sidebar" :class="{open: sidebarOpen}">
      <div class="sidebar-brand">
        <h1>CPM-80</h1>
        <p>智慧電表能源管理系統</p>
      </div>
      <nav class="sidebar-nav">
        <div class="nav-item" :class="{active: page==='dashboard'}" @click="go('dashboard')">
          <span class="nav-icon">&#9671;</span> 即時監控
        </div>
        <div class="nav-item" :class="{active: page==='trends'}" @click="go('trends')">
          <span class="nav-icon">&#9681;</span> 趨勢圖表
        </div>
        <div class="nav-item" :class="{active: page==='alerts'}" @click="go('alerts')">
          <span class="nav-icon">&#9888;</span> 告警通知
          <span class="nav-badge" v-if="alertCount>0">{{ alertCount }}</span>
        </div>
        <div class="nav-item" :class="{active: page==='analysis'}" @click="go('analysis')">
          <span class="nav-icon">&#9733;</span> 用電分析
          <span style="font-size:9px;background:var(--purple);color:#fff;padding:1px 5px;border-radius:8px;margin-left:2px">AI</span>
        </div>
        <div class="nav-item" :class="{active: page==='reports'}" @click="go('reports')">
          <span class="nav-icon">&#9783;</span> 報表匯出
        </div>
        <div class="nav-item" :class="{active: page==='settings'}" @click="go('settings')">
          <span class="nav-icon">&#9881;</span> 系統設定
        </div>
      </nav>
      <div class="sidebar-footer">
        <span class="ws-dot" :class="wsOn ? 'on' : 'off'"></span>
        {{ wsOn ? '即時連線中' : '已斷線' }}
        <br><span style="margin-left:12px">{{ lastTime }}</span>
      </div>
    </aside>

    <!-- ─── Main ─── -->
    <main class="main-content">
      <div class="topbar">
        <div style="display:flex;align-items:center;gap:12px">
          <button class="mobile-menu-btn" @click="sidebarOpen=!sidebarOpen">&#9776;</button>
          <span class="topbar-title">{{ pageTitle }}</span>
        </div>
        <div class="topbar-right">
          <div class="theme-toggle" @click="toggleTheme()" :title="darkMode ? '切換日間模式' : '切換夜間模式'">
            <div class="theme-toggle-track" :class="{ light: !darkMode }">
              <div class="knob">{{ darkMode ? '&#9790;' : '&#9728;' }}</div>
            </div>
            <span>{{ darkMode ? '深色' : '淺色' }}</span>
          </div>
          <span class="data-mode-badge" :class="isDemo ? 'demo' : 'live'" @click="toggleDemo()" :title="isDemo ? '點擊切換為真實資料' : '點擊切換為模擬資料'">{{ isDemo ? 'DEMO 模擬資料' : 'LIVE 即時資料' }}</span>
          <span class="topbar-time">{{ clock }}</span>
        </div>
      </div>

      <div class="page-content">

        <!-- ══════ Dashboard ══════ -->
        <template v-if="page==='dashboard'">
          <!-- KPI -->
          <div class="kpi-grid">
            <div class="kpi-card accent-green">
              <div class="kpi-label">合計功率</div>
              <div class="kpi-value">{{ totalP }}<span class="kpi-unit">kW</span></div>
              <div class="kpi-sub">{{ mA?.name }}: {{ fmtP(mA?.p_sum) }} + {{ mB?.name }}: {{ fmtP(mB?.p_sum) }}</div>
            </div>
            <div class="kpi-card accent-blue">
              <div class="kpi-label">合計電流</div>
              <div class="kpi-value">{{ totalI }}<span class="kpi-unit">A</span></div>
              <div class="kpi-sub">{{ mA?.name }}: {{ fmt(mA?.i_avg,2) }}A + {{ mB?.name }}: {{ fmt(mB?.i_avg,2) }}A</div>
            </div>
            <div class="kpi-card accent-orange">
              <div class="kpi-label">合計視在功率</div>
              <div class="kpi-value">{{ totalS }}<span class="kpi-unit">kVA</span></div>
            </div>
            <div class="kpi-card accent-purple">
              <div class="kpi-label">系統頻率</div>
              <div class="kpi-value">{{ fmt(mA?.freq || mB?.freq, 2) }}<span class="kpi-unit">Hz</span></div>
            </div>
          </div>

          <!-- Meters -->
          <div class="meters-row">
            <div class="meter-card" :class="isDemo ? 'color-demo' : 'color-a'">
              <div class="meter-head">
                <span class="meter-name">{{ cfg.meter_a?.name || '電表 A' }}{{ isDemo ? ' (Demo)' : '' }}</span>
                <div class="meter-status">
                  <span class="dot" :class="cfg.meter_a?.connected ? 'on' : 'off'"></span>
                  {{ mA?.meter_ts || '--' }}
                </div>
              </div>
              <div class="meter-body">
                <div class="meter-grid" v-if="mA">
                  <div class="m-item"><div class="m-label">電壓 Vavg</div><div class="m-val">{{ fmt(mA.v_avg,1) }}<span class="m-unit"> V</span></div></div>
                  <div class="m-item"><div class="m-label">電流 Iavg</div><div class="m-val">{{ fmt(mA.i_avg,2) }}<span class="m-unit"> A</span></div></div>
                  <div class="m-item"><div class="m-label">功率 P</div><div class="m-val">{{ fmtP(mA.p_sum) }}<span class="m-unit"> kW</span></div></div>
                  <div class="m-item"><div class="m-label">功率因數</div><div class="m-val">{{ fmt(mA.pf,3) }}</div></div>
                  <div class="m-item"><div class="m-label">視在功率 S</div><div class="m-val">{{ fmt(mA.s_kva,3) }}<span class="m-unit"> kVA</span></div></div>
                  <div class="m-item"><div class="m-label">無功 Q</div><div class="m-val">{{ fmt(mA.q_kvar,3) }}<span class="m-unit"> kVAr</span></div></div>
                </div>
                <div v-else class="empty-state"><p>等待資料...</p></div>
              </div>
            </div>
            <div class="meter-card" :class="isDemo ? 'color-demo' : 'color-b'">
              <div class="meter-head">
                <span class="meter-name">{{ cfg.meter_b?.name || '電表 B' }}{{ isDemo ? ' (Demo)' : '' }}</span>
                <div class="meter-status">
                  <span class="dot" :class="cfg.meter_b?.connected ? 'on' : 'off'"></span>
                  {{ mB?.meter_ts || '--' }}
                </div>
              </div>
              <div class="meter-body">
                <div class="meter-grid" v-if="mB">
                  <div class="m-item"><div class="m-label">電壓 Vavg</div><div class="m-val">{{ fmt(mB.v_avg,1) }}<span class="m-unit"> V</span></div></div>
                  <div class="m-item"><div class="m-label">電流 Iavg</div><div class="m-val">{{ fmt(mB.i_avg,2) }}<span class="m-unit"> A</span></div></div>
                  <div class="m-item"><div class="m-label">功率 P</div><div class="m-val">{{ fmtP(mB.p_sum) }}<span class="m-unit"> kW</span></div></div>
                  <div class="m-item"><div class="m-label">功率因數</div><div class="m-val">{{ fmt(mB.pf,3) }}</div></div>
                  <div class="m-item"><div class="m-label">視在功率 S</div><div class="m-val">{{ fmt(mB.s_kva,3) }}<span class="m-unit"> kVA</span></div></div>
                  <div class="m-item"><div class="m-label">無功 Q</div><div class="m-val">{{ fmt(mB.q_kvar,3) }}<span class="m-unit"> kVAr</span></div></div>
                </div>
                <div v-else class="empty-state"><p>等待資料...</p></div>
              </div>
            </div>
          </div>

          <!-- Mini Charts -->
          <div class="range-bar">
            <span>趨勢：</span>
            <button class="range-btn" v-for="r in ranges" :key="r.h"
              :class="{active: selRange===r.h}" @click="changeRange(r.h)">{{ r.label }}</button>
          </div>
          <div class="charts-row">
            <div class="chart-card">
              <div class="chart-title">功率 Power (kW)<span v-if="isDemo" class="demo-tag">Demo</span></div>
              <div class="chart-box" ref="cPower"></div>
            </div>
            <div class="chart-card">
              <div class="chart-title">電壓 Voltage (V)<span v-if="isDemo" class="demo-tag">Demo</span></div>
              <div class="chart-box" ref="cVoltage"></div>
            </div>
            <div class="chart-card">
              <div class="chart-title">電流 Current (A)<span v-if="isDemo" class="demo-tag">Demo</span></div>
              <div class="chart-box" ref="cCurrent"></div>
            </div>
            <div class="chart-card">
              <div class="chart-title">功率因數 PF<span v-if="isDemo" class="demo-tag">Demo</span></div>
              <div class="chart-box" ref="cPF"></div>
            </div>
          </div>
        </template>

        <!-- ══════ Trends ══════ -->
        <template v-if="page==='trends'">
          <div class="range-bar">
            <span>時間範圍：</span>
            <button class="range-btn" v-for="r in ranges" :key="r.h"
              :class="{active: selRange===r.h}" @click="changeRange(r.h)">{{ r.label }}</button>
          </div>
          <div style="display:flex;flex-direction:column;gap:16px">
            <div class="chart-card"><div class="chart-title">功率 Power (kW)<span v-if="isDemo" class="demo-tag">Demo</span></div><div class="chart-box" style="height:300px" ref="tPower"></div></div>
            <div class="chart-card"><div class="chart-title">電壓 Voltage (V)<span v-if="isDemo" class="demo-tag">Demo</span></div><div class="chart-box" style="height:300px" ref="tVoltage"></div></div>
            <div class="chart-card"><div class="chart-title">電流 Current (A)<span v-if="isDemo" class="demo-tag">Demo</span></div><div class="chart-box" style="height:300px" ref="tCurrent"></div></div>
            <div class="chart-card"><div class="chart-title">功率因數 PF<span v-if="isDemo" class="demo-tag">Demo</span></div><div class="chart-box" style="height:300px" ref="tPF"></div></div>
          </div>
        </template>

        <!-- ══════ Alerts ══════ -->
        <template v-if="page==='alerts'">
          <div class="alert-toolbar">
            <h3>告警通知 ({{ alerts.length }})</h3>
            <div style="display:flex;gap:8px">
              <button class="btn-outline" @click="loadAlerts()">重新整理</button>
              <button class="btn-outline" @click="ackAll()">全部確認</button>
            </div>
          </div>
          <div class="alert-list" v-if="alerts.length>0">
            <div class="alert-row" v-for="a in alerts" :key="a.id"
                 :class="{unacked: !a.acked}">
              <span class="alert-dot" :class="a.level"></span>
              <div class="alert-content">
                <div class="alert-msg">{{ a.message }}</div>
                <div class="alert-meta">{{ a.meter_id }} &middot; {{ a.ts }} &middot; {{ a.category }}</div>
              </div>
              <button class="alert-ack-btn" v-if="!a.acked" @click="ackAlert(a.id)">確認</button>
              <span v-else style="font-size:11px;color:var(--text-3)">已確認</span>
            </div>
          </div>
          <div class="empty-state" v-else>
            <div class="icon">&#10003;</div>
            <p>目前沒有告警</p>
          </div>
        </template>

        <!-- ══════ AI Analysis ══════ -->
        <template v-if="page==='analysis'">
          <!-- AI Status -->
          <div class="ai-status" :class="aiStatus.online===true?'online':(aiStatus.online===false?'offline':'checking')">
            <span class="st-dot" :class="aiStatus.online===true?'on':(aiStatus.online===false?'off':'spin')"></span>
            <span v-if="aiStatus.online===true">Ollama 連線正常 — {{ aiStatus.model }} <span v-if="!aiStatus.model_ready" style="color:var(--orange)">(模型未載入)</span></span>
            <span v-else-if="aiStatus.online===false">Ollama 離線 — {{ aiStatus.error || aiStatus.url }}</span>
            <span v-else>檢查 Ollama 狀態中...</span>
          </div>

          <!-- 自動分析控制列 -->
          <div class="ai-auto-bar" :class="ai.auto ? 'active' : 'inactive'">
            <div class="auto-toggle" :class="{on: ai.auto}" @click="toggleAutoAnalysis()">
              <div class="knob"></div>
            </div>
            <div style="flex:1;min-width:0">
              <div style="font-size:13px;font-weight:600" :style="{color: ai.auto ? 'var(--purple)' : 'var(--text-2)'}">
                {{ ai.auto ? '即時自動分析中' : '自動分析（關閉）' }}
              </div>
              <div style="font-size:11px;color:var(--text-3)">
                {{ ai.auto ? '每隔指定時間自動呼叫 LLM 分析最新數據' : '開啟後 LLM 會持續分析，會消耗 Token' }}
              </div>
            </div>
            <div v-if="ai.auto" class="ai-live-dot"></div>
            <div style="display:flex;align-items:center;gap:8px">
              <span style="font-size:11px;color:var(--text-3)">間隔</span>
              <select class="interval-select" v-model.number="ai.autoInterval" @change="onIntervalChange()">
                <option :value="10">10 秒</option>
                <option :value="30">30 秒</option>
                <option :value="60">1 分鐘</option>
                <option :value="120">2 分鐘</option>
                <option :value="300">5 分鐘</option>
              </select>
            </div>
            <span class="ai-counter" v-if="ai.autoCount>0">已分析 {{ ai.autoCount }} 次</span>
          </div>

          <div class="ai-layout">
            <!-- 左欄：分析操作 -->
            <div>
              <div class="ai-panel">
                <h3>AI 用電分析</h3>
                <div class="form-group">
                  <label class="form-label">選擇電表</label>
                  <select class="form-select" v-model="ai.meter">
                    <option value="meter_a">{{ cfg.meter_a?.name || '電表 A' }}</option>
                    <option value="meter_b">{{ cfg.meter_b?.name || '電表 B' }}</option>
                  </select>
                </div>
                <div class="form-group">
                  <label class="form-label">補充說明（選填）</label>
                  <textarea class="form-textarea" v-model="ai.note" placeholder="例如：這是辦公室用電，最近新增了一台冷氣..."></textarea>
                </div>
                <div class="form-group">
                  <label class="form-label">分析時間範圍</label>
                  <div class="range-bar">
                    <button class="range-btn" v-for="r in aiRanges" :key="r.h"
                      :class="{active: r.h !== 0 ? ai.hours===r.h && !ai.customInput : ai.customInput}"
                      @click="if(r.h){ai.hours=r.h;ai.customInput=false}else{ai.customInput=true}">{{ r.label }}</button>
                  </div>
                  <div v-if="ai.customInput" style="display:flex;align-items:center;gap:8px;margin-top:6px">
                    <input type="number" min="1" max="720" v-model.number="ai.hours"
                      class="form-select" style="width:120px" placeholder="小時數">
                    <span style="font-size:12px;color:var(--text-3)">小時（1~720）</span>
                  </div>
                </div>
                <button class="btn-primary" style="width:100%" @click="runAnalysis()" :disabled="ai.loading || !aiStatus.online">
                  {{ ai.loading ? '分析中...' : '手動分析一次' }}
                </button>

                <!-- Loading -->
                <div class="ai-loading" v-if="ai.loading">
                  <div class="ai-spinner"></div>
                  AI 正在分析用電數據，請稍候（約 5~15 秒）...
                </div>

                <!-- Error -->
                <div class="ai-error" v-if="ai.error">{{ ai.error }}</div>

                <!-- Result -->
                <div class="ai-result" v-if="ai.result" v-html="renderMd(ai.result)"></div>
              </div>

              <!-- History -->
              <div class="ai-panel" style="margin-top:16px">
                <h3>分析歷史紀錄</h3>
                <div v-if="ai.history.length>0">
                  <div class="ai-history-item" v-for="h in ai.history" :key="h.id" @click="ai.result=h.response">
                    <div class="ai-history-meta">{{ h.ts }} &middot; {{ h.meter_id }} &middot; {{ h.model }}</div>
                    <div class="ai-history-summary">{{ h.prompt_summary }}</div>
                  </div>
                </div>
                <div v-else class="empty-state" style="padding:20px">
                  <p>尚無分析紀錄</p>
                </div>
              </div>
            </div>

            <!-- 右欄：設備管理 -->
            <div>
              <div class="ai-panel">
                <h3>設備清單管理</h3>
                <!-- Add/Edit Form -->
                <div style="background:var(--bg-2);border-radius:8px;padding:14px;margin-bottom:16px">
                  <div style="font-size:12px;color:var(--text-2);margin-bottom:10px;font-weight:600">
                    {{ eq.editId ? '編輯設備' : '新增設備' }}
                  </div>
                  <div class="form-group">
                    <label class="form-label">所屬電表</label>
                    <select class="form-select" v-model="eq.form.meter_id">
                      <option value="meter_a">{{ cfg.meter_a?.name || '電表 A' }}</option>
                      <option value="meter_b">{{ cfg.meter_b?.name || '電表 B' }}</option>
                    </select>
                  </div>
                  <div class="form-group">
                    <label class="form-label">設備名稱 *</label>
                    <input class="form-input" v-model="eq.form.name" placeholder="例如：冷氣機">
                  </div>
                  <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
                    <div class="form-group">
                      <label class="form-label">額定功率 (W)</label>
                      <input class="form-input" type="number" v-model.number="eq.form.rated_watts" placeholder="例如：1200">
                    </div>
                    <div class="form-group">
                      <label class="form-label">每日使用時數</label>
                      <input class="form-input" type="number" step="0.5" v-model.number="eq.form.typical_hours" placeholder="例如：8">
                    </div>
                  </div>
                  <div class="form-group">
                    <label class="form-label">描述</label>
                    <input class="form-input" v-model="eq.form.description" placeholder="選填說明">
                  </div>
                  <div style="display:flex;gap:8px">
                    <button class="btn-success btn-sm" @click="saveEquipment()">{{ eq.editId ? '更新' : '新增' }}</button>
                    <button class="btn-outline btn-sm" v-if="eq.editId" @click="cancelEditEq()">取消</button>
                  </div>
                </div>

                <!-- Equipment Table -->
                <table class="eq-table" v-if="eq.list.length>0">
                  <thead>
                    <tr><th>名稱</th><th>電表</th><th>額定 W</th><th>時數</th><th></th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="e in eq.list" :key="e.id">
                      <td>{{ e.name }}<br><span style="font-size:11px;color:var(--text-3)">{{ e.description }}</span></td>
                      <td>{{ e.meter_id }}</td>
                      <td>{{ e.rated_watts || '--' }}</td>
                      <td>{{ e.typical_hours || '--' }}</td>
                      <td class="eq-actions">
                        <button @click="editEquipment(e)">編輯</button>
                        <button class="del" @click="deleteEquipment(e.id)">刪除</button>
                      </td>
                    </tr>
                  </tbody>
                </table>
                <div v-else class="empty-state" style="padding:20px">
                  <p>尚無設備資料</p>
                  <p style="font-size:12px;margin-top:4px;color:var(--text-3)">新增設備可幫助 AI 更準確分析</p>
                </div>
              </div>
            </div>
          </div>

          <!-- ═══ 負載曲線分析 ═══ -->
          <div class="ai-panel analysis-section" v-if="loadProfile.data || loadProfile.loading">
            <h3>負載曲線分析（24h）</h3>
            <div v-if="loadProfile.loading" class="ai-loading"><div class="ai-spinner"></div> 載入中...</div>
            <template v-if="loadProfile.data && loadProfile.data.kpi">
              <div class="analysis-kpi-grid">
                <div class="analysis-kpi-card">
                  <div class="analysis-kpi-label">負載率 Load Factor</div>
                  <div class="analysis-kpi-value">{{ (loadProfile.data.kpi.load_factor * 100).toFixed(1) }}%</div>
                  <div class="analysis-kpi-sub">平均 / 尖峰功率比</div>
                </div>
                <div class="analysis-kpi-card orange">
                  <div class="analysis-kpi-label">尖峰功率</div>
                  <div class="analysis-kpi-value">{{ (loadProfile.data.kpi.peak_power_w / 1000).toFixed(2) }} kW</div>
                  <div class="analysis-kpi-sub">{{ loadProfile.data.kpi.peak_power_w.toFixed(0) }} W</div>
                </div>
                <div class="analysis-kpi-card green">
                  <div class="analysis-kpi-label">尖峰佔比</div>
                  <div class="analysis-kpi-value">{{ (loadProfile.data.kpi.peak_ratio * 100).toFixed(1) }}%</div>
                  <div class="analysis-kpi-sub">尖峰 {{ loadProfile.data.kpi.peak_kwh }} / 總 {{ loadProfile.data.kpi.total_kwh }} kWh</div>
                </div>
                <div class="analysis-kpi-card purple">
                  <div class="analysis-kpi-label">需量因數</div>
                  <div class="analysis-kpi-value">{{ loadProfile.data.kpi.demand_factor != null ? (loadProfile.data.kpi.demand_factor * 100).toFixed(1) + '%' : 'N/A' }}</div>
                  <div class="analysis-kpi-sub">{{ loadProfile.data.kpi.demand_factor != null ? '額定 ' + loadProfile.data.kpi.total_rated_w + 'W' : '需登錄設備' }}</div>
                </div>
              </div>
              <div ref="cLoadProfile" style="width:100%;height:300px"></div>
            </template>
          </div>

          <!-- ═══ 異常偵測 ═══ -->
          <div class="ai-panel analysis-section">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
              <h3 style="margin:0">統計異常偵測</h3>
              <button class="btn-outline btn-sm" @click="fetchAnomaly()" :disabled="anomaly.loading">
                {{ anomaly.loading ? '偵測中...' : '執行異常偵測' }}
              </button>
            </div>
            <div v-if="anomaly.loading" class="ai-loading"><div class="ai-spinner"></div> 分析基線資料中...</div>
            <template v-if="anomaly.data">
              <div style="margin-bottom:10px">
                <span class="anomaly-badge high" v-if="anomaly.data.anomaly_count.high">嚴重 {{ anomaly.data.anomaly_count.high }}</span>
                <span class="anomaly-badge medium" v-if="anomaly.data.anomaly_count.medium">警告 {{ anomaly.data.anomaly_count.medium }}</span>
                <span v-if="!anomaly.data.anomaly_count.high && !anomaly.data.anomaly_count.medium"
                  style="color:var(--green);font-size:13px;font-weight:600">未偵測到異常</span>
                <span style="font-size:12px;color:var(--text-3);margin-left:8px">基準覆蓋率: {{ (anomaly.data.baseline_coverage * 100).toFixed(0) }}%</span>
              </div>
              <div v-if="anomaly.data.baseline_coverage < 0.5" style="font-size:12px;color:var(--orange);margin-bottom:8px">
                基線資料不足（需至少運行 3 天以上），結果可能不準確
              </div>
              <div v-for="a in anomaly.data.anomalies" :key="a.hour_bucket" class="alert-row" style="cursor:default"
                :style="{borderLeftColor: a.severity==='high' ? 'var(--red)' : 'var(--orange)'}">
                <div><strong>{{ a.hour_bucket.split('T')[1] }}:00</strong>
                  <span :style="{color: a.severity==='high' ? 'var(--red)' : 'var(--orange)'}"> {{ a.direction }}</span>
                </div>
                <div style="font-size:12px;color:var(--text-3)">
                  實際 {{ a.actual_power.toFixed(0) }}W vs 基線 {{ a.baseline_mean.toFixed(0) }}W（Z={{ a.z_score.toFixed(1) }}, {{ a.deviation_pct > 0 ? '+' : '' }}{{ a.deviation_pct.toFixed(0) }}%）
                </div>
              </div>
            </template>
          </div>

          <!-- ═══ 費率優化 ═══ -->
          <div class="ai-panel analysis-section">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
              <h3 style="margin:0">台電費率優化</h3>
              <button class="btn-outline btn-sm" @click="fetchRateOpt()" :disabled="rateOpt.loading">
                {{ rateOpt.loading ? '計算中...' : '計算費率比較' }}
              </button>
            </div>
            <div v-if="rateOpt.loading" class="ai-loading"><div class="ai-spinner"></div> 計算各方案費率中...</div>
            <template v-if="rateOpt.data && rateOpt.data.plans && rateOpt.data.plans.length">
              <div class="rate-recommend">
                <div style="font-weight:600;color:var(--green);margin-bottom:4px">推薦方案：{{ rateOpt.data.recommended_name }}</div>
                <div style="font-size:13px;color:var(--text-2)">
                  {{ rateOpt.data.season }} · 月用電量 {{ rateOpt.data.monthly_kwh }} kWh
                  <span v-if="rateOpt.data.monthly_savings > 0"> · 每月可省 NT${{ rateOpt.data.monthly_savings }}</span>
                </div>
              </div>
              <table class="rate-table">
                <thead>
                  <tr><th>方案</th><th>月用電量</th><th>電力費</th><th>基本費</th><th>月總費用</th><th>均價</th></tr>
                </thead>
                <tbody>
                  <tr v-for="p in rateOpt.data.plans" :key="p.id" :class="{best: p.id === rateOpt.data.recommended}">
                    <td>{{ p.name }}</td>
                    <td>{{ p.monthly_kwh }} kWh</td>
                    <td>NT${{ p.energy_cost }}</td>
                    <td>NT${{ p.basic_fee }}</td>
                    <td>NT${{ p.total_cost }}</td>
                    <td>{{ p.avg_price }} 元/kWh</td>
                  </tr>
                </tbody>
              </table>
            </template>
            <div v-if="rateOpt.data && rateOpt.data.error" style="font-size:13px;color:var(--orange);margin-top:8px">
              {{ rateOpt.data.error }}
            </div>
          </div>

        </template>

        <!-- ══════ Reports ══════ -->
        <template v-if="page==='reports'">
          <div class="report-grid">
            <!-- Billing -->
            <div class="report-card">
              <h3>電費估算</h3>
              <div class="form-group">
                <label class="form-label">電表</label>
                <select class="form-select" v-model="bill.meter">
                  <option value="meter_a">{{ cfg.meter_a?.name || '電表 A' }}</option>
                  <option value="meter_b">{{ cfg.meter_b?.name || '電表 B' }}</option>
                </select>
              </div>
              <div class="form-group">
                <label class="form-label">時間範圍</label>
                <select class="form-select" v-model="bill.hours">
                  <option :value="1">1 小時</option>
                  <option :value="6">6 小時</option>
                  <option :value="24">24 小時</option>
                  <option :value="168">7 天</option>
                  <option :value="720">30 天</option>
                </select>
              </div>
              <div class="form-group">
                <label class="form-label">電價 (元/kWh)</label>
                <input class="form-input" type="number" step="0.1" v-model.number="bill.rate">
              </div>
              <button class="btn-primary" @click="calcBill()" style="width:100%">計算</button>
              <div class="billing-result" v-if="bill.result">
                <div class="billing-row"><span class="label">統計期間</span><span class="value">{{ bill.result.hours }} 小時</span></div>
                <div class="billing-row"><span class="label">用電量</span><span class="value">{{ bill.result.kwh }} kWh</span></div>
                <div class="billing-row"><span class="label">電價</span><span class="value">{{ bill.result.rate }} 元/kWh</span></div>
                <div class="billing-row billing-total"><span class="label">預估電費</span><span class="value">NT$ {{ bill.result.cost }}</span></div>
              </div>
            </div>
            <!-- Export -->
            <div class="report-card">
              <h3>資料匯出</h3>
              <div class="form-group">
                <label class="form-label">電表</label>
                <select class="form-select" v-model="exp.meter">
                  <option value="meter_a">{{ cfg.meter_a?.name || '電表 A' }}</option>
                  <option value="meter_b">{{ cfg.meter_b?.name || '電表 B' }}</option>
                </select>
              </div>
              <div class="form-group">
                <label class="form-label">時間範圍</label>
                <select class="form-select" v-model="exp.hours">
                  <option :value="1">1 小時</option>
                  <option :value="6">6 小時</option>
                  <option :value="24">24 小時</option>
                  <option :value="168">7 天</option>
                  <option :value="720">30 天</option>
                </select>
              </div>
              <a class="btn-primary" style="display:block;text-align:center;text-decoration:none;margin-top:20px"
                 :href="`/api/export?meter_id=${exp.meter}&hours=${exp.hours}`">
                下載 CSV
              </a>
            </div>
          </div>
        </template>

        <!-- ══════ Settings ══════ -->
        <template v-if="page==='settings'">
          <div class="settings-grid">
            <div class="report-card">
              <h3>告警閾值設定</h3>
              <div class="threshold-row">
                <span class="threshold-label">電壓上限 (V)</span>
                <input class="threshold-input" type="number" step="1" v-model.number="thresholds.v_high">
              </div>
              <div class="threshold-row">
                <span class="threshold-label">電壓下限 (V)</span>
                <input class="threshold-input" type="number" step="1" v-model.number="thresholds.v_low">
              </div>
              <div class="threshold-row">
                <span class="threshold-label">電流上限 (A)</span>
                <input class="threshold-input" type="number" step="1" v-model.number="thresholds.i_high">
              </div>
              <div class="threshold-row">
                <span class="threshold-label">功率上限 (W)</span>
                <input class="threshold-input" type="number" step="100" v-model.number="thresholds.p_high">
              </div>
              <div class="threshold-row">
                <span class="threshold-label">功率因數下限</span>
                <input class="threshold-input" type="number" step="0.01" v-model.number="thresholds.pf_low">
              </div>
              <button class="btn-primary" @click="saveThresholds()" style="width:100%;margin-top:16px">儲存設定</button>
            </div>
            <div class="report-card">
              <h3>電表資訊</h3>
              <template v-for="(c, mid) in cfg" :key="mid">
                <div style="margin-bottom:16px;padding:12px;background:var(--bg-2);border-radius:6px">
                  <div style="font-weight:600;margin-bottom:8px;color:var(--text-1)">{{ c.name }} ({{ c.host }})</div>
                  <div style="font-size:12px;color:var(--text-2);line-height:1.8" v-if="c.config">
                    接線方式: {{ c.config.wire_code }} ({{ c.config.wire_name }})<br>
                    PT: {{ c.config.pt_primary }}V / {{ c.config.pt_secondary }}V<br>
                    CT: {{ c.config.ct_primary }}A / {{ c.config.ct_secondary }}<br>
                    連線狀態: <span :style="{color: c.connected ? 'var(--green)' : 'var(--red)'}">{{ c.connected ? '正常' : '離線' }}</span>
                  </div>
                  <div v-else style="font-size:12px;color:var(--text-3)">未讀取到設定</div>
                </div>
              </template>
            </div>
          </div>
        </template>

      </div>
    </main>
  </div>
</div>

<script>
const { createApp, ref, reactive, computed, onMounted, onBeforeUnmount, nextTick, watch } = Vue;
createApp({
  setup() {
    // ─── State ───
    const page = ref('dashboard');
    const sidebarOpen = ref(false);
    const wsOn = ref(false);
    const lastTime = ref('--');
    const clock = ref('');
    const meters = ref({});
    const cfg = ref({});
    const alertCount = ref(0);
    const isDemo = ref(false);
    const darkMode = ref(true);
    const alerts = ref([]);
    const selRange = ref(1);
    const ranges = [{label:'1h',h:1},{label:'6h',h:6},{label:'24h',h:24},{label:'7d',h:168},{label:'30d',h:720}];
    const bill = reactive({meter:'meter_a',hours:24,rate:3.5,result:null});
    const exp = reactive({meter:'meter_a',hours:24});
    const thresholds = reactive({v_high:250,v_low:190,i_high:80,p_high:15000,pf_low:0.5});

    // AI Analysis state
    const aiStatus = reactive({online:null, url:'', model:'', model_ready:false, error:''});
    const ai = reactive({
      meter:'meter_a', note:'', loading:false, result:null, error:null, history:[],
      auto:false, autoInterval:30, autoCount:0, autoTimer:null,
      hours:1, customInput:false
    });
    const aiRanges = [
      {label:'1 小時',h:1}, {label:'6 小時',h:6}, {label:'24 小時',h:24}, {label:'自訂',h:0}
    ];
    const eq = reactive({
      list: [],
      editId: null,
      form: {meter_id:'meter_a', name:'', rated_watts:null, description:'', typical_hours:null}
    });

    // Load Profile / Anomaly / Rate Optimization
    const loadProfile = reactive({ data: null, loading: false });
    const anomaly = reactive({ data: null, loading: false });
    const rateOpt = reactive({ data: null, loading: false });
    const cLoadProfile = ref(null);

    // Chart refs — Dashboard
    const cPower = ref(null), cVoltage = ref(null), cCurrent = ref(null), cPF = ref(null);
    // Chart refs — Trends
    const tPower = ref(null), tVoltage = ref(null), tCurrent = ref(null), tPF = ref(null);
    let charts = {};
    let ws = null, reconTimer = null, histTimer = null, clockTimer = null, aiAutoTimer = null;

    const pageTitles = {dashboard:'即時監控',trends:'趨勢圖表',alerts:'告警通知',analysis:'AI 用電分析',reports:'報表匯出',settings:'系統設定'};
    const pageTitle = computed(() => pageTitles[page.value] || '');

    const mA = computed(() => meters.value.meter_a);
    const mB = computed(() => meters.value.meter_b);

    const fmt = (v,d) => v!=null ? Number(v).toFixed(d) : '--';
    const fmtP = (v) => v!=null ? (v/1000).toFixed(3) : '--';
    const totalP = computed(() => { const a=mA.value?.p_sum||0, b=mB.value?.p_sum||0; return ((a+b)/1000).toFixed(3); });
    const totalI = computed(() => { const a=mA.value?.i_avg||0, b=mB.value?.i_avg||0; return (a+b).toFixed(2); });
    const totalS = computed(() => { const a=mA.value?.s_kva||0, b=mB.value?.s_kva||0; return (a+b).toFixed(3); });

    function go(p) {
      page.value = p; sidebarOpen.value = false;
      nextTick(()=>{ initPageCharts(); loadHistory(); });
      if(p==='alerts') loadAlerts();
      if(p==='analysis') { checkAiStatus(); loadAnalysisHistory(); loadEquipment(); fetchLoadProfile(); }
    }

    // ─── WebSocket ───
    function connectWS() {
      const proto = location.protocol==='https:'?'wss:':'ws:';
      ws = new WebSocket(`${proto}//${location.host}/ws`);
      ws.onopen = () => { wsOn.value=true; if(reconTimer){clearTimeout(reconTimer);reconTimer=null;} };
      ws.onclose = () => { wsOn.value=false; reconTimer=setTimeout(connectWS,3000); };
      ws.onerror = () => ws.close();
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if(msg.type==='update') {
          meters.value = msg.meters;
          alertCount.value = msg.alert_count || 0;
          lastTime.value = new Date().toLocaleTimeString();
        }
      };
    }

    // ─── Charts ───
    function getChartTheme() {
      const s = getComputedStyle(document.documentElement);
      const g = (v) => s.getPropertyValue(v).trim();
      return {
        tooltip: { backgroundColor: g('--chart-tooltip-bg'), borderColor: g('--chart-tooltip-border'), textStyle:{color: g('--chart-tooltip-text'), fontFamily:'monospace', fontSize:12} },
        legend: { textStyle:{color: g('--text-2'), fontSize:11}, top:0 },
        grid: { left:50, right:16, top:30, bottom:24 },
        xAxis: { type:'time', axisLine:{lineStyle:{color: g('--chart-axis')}}, axisLabel:{color: g('--chart-label'), fontSize:10}, splitLine:{show:false} },
        yAxis: { type:'value', axisLine:{lineStyle:{color: g('--chart-axis')}}, axisLabel:{color: g('--chart-label'), fontSize:10}, splitLine:{lineStyle:{color: g('--chart-split')}} },
      };
    }
    // backward-compat alias
    const chartTheme = {
      tooltip: { backgroundColor:'#1e293b', borderColor:'#334155', textStyle:{color:'#f1f5f9',fontFamily:'monospace',fontSize:12} },
      legend: { textStyle:{color:'#94a3b8',fontSize:11}, top:0 },
      grid: { left:50, right:16, top:30, bottom:24 },
      xAxis: { type:'time', axisLine:{lineStyle:{color:'#334155'}}, axisLabel:{color:'#64748b',fontSize:10}, splitLine:{show:false} },
      yAxis: { type:'value', axisLine:{lineStyle:{color:'#334155'}}, axisLabel:{color:'#64748b',fontSize:10}, splitLine:{lineStyle:{color:'#1e293b'}} },
    };

    // 真實 vs Demo 配色
    const COLORS_LIVE = {
      a: '#3b82f6', a_area: 'rgba(59,130,246,0.06)',
      b: '#f59e0b', b_area: 'rgba(245,158,11,0.06)',
    };
    const COLORS_DEMO = {
      a: '#8b5cf6', a_area: 'rgba(139,92,246,0.08)',
      b: '#ec4899', b_area: 'rgba(236,72,153,0.08)',
    };

    function makeOpt(yName) {
      const demo = isDemo.value;
      const tag = demo ? ' (Demo)' : '';
      const nameA = (cfg.value.meter_a?.name || '電表A') + tag;
      const nameB = (cfg.value.meter_b?.name || '電表B') + tag;
      const c = demo ? COLORS_DEMO : COLORS_LIVE;
      const dash = demo ? [6,3] : null; // Demo 用虛線
      const ct = getChartTheme();
      const s = getComputedStyle(document.documentElement);
      const labelClr = s.getPropertyValue('--chart-label').trim() || '#64748b';
      return {
        ...ct,
        legend: { ...ct.legend, data:[nameA, nameB] },
        yAxis: { ...ct.yAxis, name:yName, nameTextStyle:{color:labelClr,fontSize:10} },
        series: [
          { name:nameA, type:'line', smooth:true, symbol:'none', lineStyle:{width:2, color:c.a, type: dash?'dashed':'solid'}, itemStyle:{color:c.a}, areaStyle:{color:c.a_area}, data:[] },
          { name:nameB, type:'line', smooth:true, symbol:'none', lineStyle:{width:2, color:c.b, type: dash?'dashed':'solid'}, itemStyle:{color:c.b}, areaStyle:{color:c.b_area}, data:[] },
        ],
        animation: false,
      };
    }

    function initChart(el, yName) {
      if(!el) return null;
      const c = echarts.init(el);
      c.setOption(makeOpt(yName));
      return c;
    }

    function initPageCharts() {
      // Dispose old charts
      Object.values(charts).forEach(c => { try{c.dispose();}catch(e){} });
      charts = {};
      if(page.value==='dashboard') {
        if(cPower.value) charts.dP = initChart(cPower.value, 'kW');
        if(cVoltage.value) charts.dV = initChart(cVoltage.value, 'V');
        if(cCurrent.value) charts.dI = initChart(cCurrent.value, 'A');
        if(cPF.value) charts.dPF = initChart(cPF.value, '');
      } else if(page.value==='trends') {
        if(tPower.value) charts.tP = initChart(tPower.value, 'kW');
        if(tVoltage.value) charts.tV = initChart(tVoltage.value, 'V');
        if(tCurrent.value) charts.tI = initChart(tCurrent.value, 'A');
        if(tPF.value) charts.tPF = initChart(tPF.value, '');
      }
    }

    async function loadHistory() {
      const h = selRange.value;
      let ds = 0;
      if(h>=720) ds=60; else if(h>=168) ds=30; else if(h>=24) ds=10; else if(h>=6) ds=3;
      try {
        const [rA, rB] = await Promise.all([
          fetch(`/api/history?meter_id=meter_a&hours=${h}&downsample=${ds}`).then(r=>r.json()),
          fetch(`/api/history?meter_id=meter_b&hours=${h}&downsample=${ds}`).then(r=>r.json()),
        ]);
        const toS = (rows, key) => rows.map(r=>[r.ts, r[key]]);
        const pA = toS(rA,'p_sum').map(([t,v])=>[t,v!=null?v/1000:null]);
        const pB = toS(rB,'p_sum').map(([t,v])=>[t,v!=null?v/1000:null]);

        const update = (c, seriesA, seriesB) => {
          if(!c) return;
          c.setOption({ series:[{data:seriesA},{data:seriesB}] });
        };
        // Dashboard charts
        update(charts.dP, pA, pB);
        update(charts.dV, toS(rA,'v_avg'), toS(rB,'v_avg'));
        update(charts.dI, toS(rA,'i_avg'), toS(rB,'i_avg'));
        update(charts.dPF, toS(rA,'pf'), toS(rB,'pf'));
        // Trends charts
        update(charts.tP, pA, pB);
        update(charts.tV, toS(rA,'v_avg'), toS(rB,'v_avg'));
        update(charts.tI, toS(rA,'i_avg'), toS(rB,'i_avg'));
        update(charts.tPF, toS(rA,'pf'), toS(rB,'pf'));
      } catch(e) { console.warn('History error:', e); }
    }

    function changeRange(h) { selRange.value = h; loadHistory(); }

    // ─── Alerts ───
    async function loadAlerts() {
      try {
        const res = await fetch('/api/alerts?limit=100');
        alerts.value = await res.json();
      } catch(e) {}
    }
    async function ackAlert(id) {
      await fetch(`/api/alerts/${id}/ack`, {method:'POST'});
      loadAlerts();
    }
    async function ackAll() {
      await fetch('/api/alerts/ack-all', {method:'POST'});
      loadAlerts();
    }

    // ─── Billing ───
    async function calcBill() {
      try {
        const res = await fetch(`/api/billing?meter_id=${bill.meter}&hours=${bill.hours}&rate=${bill.rate}`);
        bill.result = await res.json();
      } catch(e) {}
    }

    // ─── Thresholds ───
    async function loadThresholds() {
      try { const r = await fetch('/api/alerts/thresholds'); Object.assign(thresholds, await r.json()); } catch(e){}
    }
    async function saveThresholds() {
      try {
        await fetch('/api/alerts/thresholds', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(thresholds)});
        alert('設定已儲存');
      } catch(e){}
    }

    // ─── AI Analysis ───
    async function checkAiStatus() {
      try {
        const r = await fetch('/api/analysis/status');
        const d = await r.json();
        Object.assign(aiStatus, d);
      } catch(e) { aiStatus.online = false; aiStatus.error = 'API 連線失敗'; }
    }

    async function runAnalysis() {
      ai.loading = true; ai.error = null;
      if(!ai.auto) ai.result = null; // 手動模式清除舊結果，自動模式保留到新結果
      try {
        const r = await fetch('/api/analysis', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({meter_id: ai.meter, note: ai.note, hours: ai.hours})
        });
        const d = await r.json();
        if(d.ok) {
          ai.result = d.content;
          if(ai.auto) ai.autoCount++;
          loadAnalysisHistory();
        } else {
          ai.error = d.error || '分析失敗';
        }
      } catch(e) { ai.error = '連線錯誤: ' + e.message; }
      ai.loading = false;
    }

    function toggleAutoAnalysis() {
      ai.auto = !ai.auto;
      if(ai.auto) { startAutoAnalysis(); }
      else { stopAutoAnalysis(); }
    }

    function startAutoAnalysis() {
      stopAutoAnalysis();
      // 立即執行一次
      runAnalysis();
      aiAutoTimer = setInterval(() => {
        if(!ai.loading) runAnalysis();
      }, ai.autoInterval * 1000);
    }

    function stopAutoAnalysis() {
      if(aiAutoTimer) { clearInterval(aiAutoTimer); aiAutoTimer = null; }
    }

    function onIntervalChange() {
      if(ai.auto) startAutoAnalysis(); // 重新啟動 timer
    }

    async function loadAnalysisHistory() {
      try {
        const r = await fetch('/api/analysis/history?limit=20');
        ai.history = await r.json();
      } catch(e) {}
    }

    function renderMd(text) {
      if(!text) return '';
      // Simple markdown to HTML
      return text
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/^## (.+)$/gm, '<h2>$1</h2>')
        .replace(/^### (.+)$/gm, '<h3 style="font-size:13px;color:var(--cyan);margin:10px 0 4px">$1</h3>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/`(.+?)`/g, '<code>$1</code>')
        .replace(/^- (.+)$/gm, '<li>$1</li>')
        .replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>')
        .replace(/\n{2,}/g, '<br><br>')
        .replace(/\n/g, '<br>');
    }

    // ─── Equipment ───
    async function loadEquipment() {
      try {
        const r = await fetch('/api/equipment');
        eq.list = await r.json();
      } catch(e) {}
    }

    async function saveEquipment() {
      const f = eq.form;
      if(!f.name.trim()) return;
      try {
        if(eq.editId) {
          await fetch(`/api/equipment/${eq.editId}`, {
            method:'PUT', headers:{'Content-Type':'application/json'},
            body: JSON.stringify(f)
          });
        } else {
          await fetch('/api/equipment', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify(f)
          });
        }
        cancelEditEq();
        loadEquipment();
      } catch(e) {}
    }

    function editEquipment(e) {
      eq.editId = e.id;
      eq.form.meter_id = e.meter_id;
      eq.form.name = e.name;
      eq.form.rated_watts = e.rated_watts;
      eq.form.description = e.description || '';
      eq.form.typical_hours = e.typical_hours;
    }

    function cancelEditEq() {
      eq.editId = null;
      eq.form.meter_id = 'meter_a';
      eq.form.name = '';
      eq.form.rated_watts = null;
      eq.form.description = '';
      eq.form.typical_hours = null;
    }

    async function deleteEquipment(id) {
      if(!confirm('確定要刪除此設備？')) return;
      try {
        await fetch(`/api/equipment/${id}`, {method:'DELETE'});
        loadEquipment();
      } catch(e) {}
    }

    // ─── Theme Toggle ───
    function applyTheme(isDark) {
      document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
      darkMode.value = isDark;
      localStorage.setItem('cpm80_theme', isDark ? 'dark' : 'light');
    }

    function toggleTheme() {
      applyTheme(!darkMode.value);
      // Refresh all ECharts with new theme colors
      nextTick(() => {
        initPageCharts();
        loadHistory();
        if (page.value === 'analysis' && loadProfile.data) {
          renderLoadProfileChart();
        }
      });
    }

    // ─── Demo Toggle ───
    async function toggleDemo() {
      try {
        const r = await fetch('/api/demo/toggle', {method:'POST'});
        const d = await r.json();
        isDemo.value = d.demo;
        await fetchConfig();
        // 重繪圖表（更新配色與 Demo 標記）
        await nextTick();
        initPageCharts();
        loadHistory();
      } catch(e) {}
    }

    // ─── Config ───
    async function fetchConfig() {
      try {
        const data = await (await fetch('/api/config')).json();
        if(data._meta) { isDemo.value = data._meta.demo; delete data._meta; }
        cfg.value = data;
      } catch(e){}
    }

    // ─── Load Profile / Anomaly / Rate ───
    async function fetchLoadProfile() {
      loadProfile.loading = true;
      try {
        const r = await fetch(`/api/analysis/load-profile?meter_id=${ai.meter}&hours=24`);
        loadProfile.data = await r.json();
        nextTick(() => renderLoadProfileChart());
      } catch(e) { console.error('fetchLoadProfile', e); }
      loadProfile.loading = false;
    }

    function renderLoadProfileChart() {
      const el = cLoadProfile.value;
      if (!el || !loadProfile.data?.hourly?.length) return;
      const c = charts._loadProfile || echarts.init(el);
      charts._loadProfile = c;
      const hd = loadProfile.data.hourly;
      const xData = hd.map(h => h.hour_bucket.split('T')[1] + ':00');
      const yData = hd.map(h => (h.avg_power || 0) / 1000);
      const colors = hd.map(h => h.is_peak ? '#e67e22' : '#3498db');
      const avgKW = loadProfile.data.kpi.avg_power_w / 1000;
      const markPoints = [];
      if (anomaly.data?.anomalies) {
        for (const a of anomaly.data.anomalies) {
          const idx = hd.findIndex(h => h.hour_bucket === a.hour_bucket);
          if (idx >= 0) {
            markPoints.push({
              coord: [idx, yData[idx]],
              value: 'Z=' + a.z_score.toFixed(1),
              itemStyle: { color: a.severity === 'high' ? '#e74c3c' : '#f39c12' },
            });
          }
        }
      }
      const ct = getChartTheme();
      c.setOption({
        tooltip: { ...ct.tooltip, trigger: 'axis', formatter: p => `${p[0].axisValue}<br/>功率: ${p[0].value.toFixed(3)} kW` },
        grid: { left: 50, right: 20, top: 30, bottom: 30 },
        xAxis: { type: 'category', data: xData, axisLabel: { fontSize: 11, color: ct.xAxis.axisLabel.color }, axisLine: ct.xAxis.axisLine },
        yAxis: { type: 'value', name: 'kW', axisLabel: { fontSize: 11, color: ct.yAxis.axisLabel.color }, axisLine: ct.yAxis.axisLine, splitLine: ct.yAxis.splitLine },
        series: [{
          type: 'bar',
          data: yData.map((v, i) => ({ value: v, itemStyle: { color: colors[i] } })),
          markLine: { silent: true, data: [{ yAxis: avgKW, label: { formatter: `平均 ${avgKW.toFixed(3)} kW`, fontSize: 11 }, lineStyle: { color: '#e74c3c', type: 'dashed' } }] },
          markPoint: markPoints.length ? { data: markPoints, symbolSize: 30, label: { fontSize: 10 } } : undefined,
        }],
      }, true);
    }

    async function fetchAnomaly() {
      anomaly.loading = true;
      try {
        const r = await fetch(`/api/analysis/anomaly?meter_id=${ai.meter}&baseline_days=7`);
        anomaly.data = await r.json();
        nextTick(() => renderLoadProfileChart());
      } catch(e) { console.error('fetchAnomaly', e); }
      anomaly.loading = false;
    }

    async function fetchRateOpt() {
      rateOpt.loading = true;
      try {
        const r = await fetch(`/api/analysis/rate-optimization?meter_id=${ai.meter}&hours=720`);
        rateOpt.data = await r.json();
      } catch(e) { console.error('fetchRateOpt', e); }
      rateOpt.loading = false;
    }

    watch(() => ai.meter, () => {
      if (page.value === 'analysis') {
        loadProfile.data = null; anomaly.data = null; rateOpt.data = null;
        fetchLoadProfile();
      }
    });

    // ─── Resize ───
    function onResize() { Object.values(charts).forEach(c=>{ try{c.resize();}catch(e){} }); }

    onMounted(async () => {
      // Restore theme from localStorage or detect system preference
      const saved = localStorage.getItem('cpm80_theme');
      if (saved) {
        applyTheme(saved === 'dark');
      } else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
        applyTheme(false);
      }
      await fetchConfig();
      connectWS();
      loadThresholds();
      await nextTick();
      initPageCharts();
      loadHistory();
      histTimer = setInterval(loadHistory, 30000);
      clockTimer = setInterval(()=>{ clock.value = new Date().toLocaleTimeString(); }, 1000);
      clock.value = new Date().toLocaleTimeString();
      window.addEventListener('resize', onResize);
    });

    onBeforeUnmount(() => {
      if(ws) ws.close();
      if(reconTimer) clearTimeout(reconTimer);
      if(histTimer) clearInterval(histTimer);
      if(clockTimer) clearInterval(clockTimer);
      stopAutoAnalysis();
      Object.values(charts).forEach(c=>{ try{c.dispose();}catch(e){} });
      window.removeEventListener('resize', onResize);
    });

    return {
      page, sidebarOpen, wsOn, lastTime, clock, meters, cfg, isDemo,
      alertCount, alerts, selRange, ranges, bill, exp, thresholds,
      cPower, cVoltage, cCurrent, cPF,
      tPower, tVoltage, tCurrent, tPF,
      pageTitle, mA, mB, fmt, fmtP, totalP, totalI, totalS,
      go, changeRange, loadAlerts, ackAlert, ackAll,
      calcBill, saveThresholds, toggleDemo, darkMode, toggleTheme,
      aiStatus, ai, aiRanges, eq,
      checkAiStatus, runAnalysis, loadAnalysisHistory, renderMd,
      toggleAutoAnalysis, onIntervalChange,
      loadEquipment, saveEquipment, editEquipment, cancelEditEq, deleteEquipment,
      loadProfile, anomaly, rateOpt, cLoadProfile,
      fetchLoadProfile, fetchAnomaly, fetchRateOpt,
    };
  }
}).mount('#app');
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


# ═══════════════════════════════════════════════════════════════════
# Section 9: 啟動
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    log.info("CPM-80 Dashboard 啟動中...%s", " (初始 Demo 模式)" if args.demo else "")
    log.info("瀏覽器: http://%s:%d", "localhost" if args.bind == "0.0.0.0" else args.bind, args.port)
    uvicorn.run(app, host=args.bind, port=args.port, log_level="info")
