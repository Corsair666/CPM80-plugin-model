#!/usr/bin/env python3
"""
CPM80 Modbus TCP Reader — Duo (雙電表同時讀取)
===============================================
同時連接兩顆 CPM-80 電表，並排顯示即時量測資料。
自動偵測各電表的接線方式（1P2W / 1P3W / 3P3W / 3P4W）與 PT/CT 設定。

預設：
  電表 A (樓上)  : 10.0.60.21
  電表 B (B2)    : 10.0.60.22
"""
import argparse
import time
from pymodbus.client import ModbusTcpClient

# ─── 系統設定暫存器 (0x0000 區) ─────────────────────────────────
REG_WIRE_FUNC   = 0x0000
REG_PT_PRIMARY  = 0x0001
REG_PT_SECOND   = 0x0003
REG_CT_PRIMARY  = 0x0004
REG_CT_SEC_SEL  = 0x000A

# ─── 量測暫存器 (0x1000 區，已驗證) ─────────────────────────────
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

# ─── Date/Time ──────────────────────────────────────────────────
REG_YEAR   = 0x01A6

# ─── 對照表 ─────────────────────────────────────────────────────
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

COL_W = 35  # 每欄寬度


def read_signed_int16(value):
    if value >= 0x8000:
        return value - 0x10000
    return value


def parse_args():
    p = argparse.ArgumentParser(
        description="CPM-80 Duo Reader — 同時讀取兩顆電表"
    )
    p.add_argument("--host-a", default="10.0.60.21", help="電表 A IP (樓上)")
    p.add_argument("--host-b", default="10.0.60.22", help="電表 B IP (B2)")
    p.add_argument("--port", type=int, default=502, help="Modbus TCP Port")
    p.add_argument("--device-id", type=int, default=1, help="Unit ID")
    p.add_argument("--interval", type=float, default=1.0, help="讀取間隔（秒）")
    p.add_argument("--once", action="store_true", help="只讀取一次")
    p.add_argument("--name-a", default="樓上", help="電表 A 名稱")
    p.add_argument("--name-b", default="B2", help="電表 B 名稱")
    return p.parse_args()


# ─── 讀取函式（共用） ───────────────────────────────────────────

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
    pf_calc = (p_kw / s_kva) if s_kva > 0 else 0.0

    return {
        "freq": freq,
        "v_a": v_a, "v_b": v_b, "v_c": v_c, "v_avg": v_avg,
        "i_a": i_a, "i_b": i_b, "i_c": i_c, "i_avg": i_avg,
        "p_sum": p_sum, "pf": pf,
        "s_kva": s_kva, "q_kvar": q_kvar, "pf_calc": pf_calc,
    }


# ─── 並排顯示 ───────────────────────────────────────────────────

def dual_line(left, right=""):
    print(f"  {left:<{COL_W}}│  {right}")


def print_dual_config(name_a, cfg_a, name_b, cfg_b, host_a, host_b):
    w = COL_W + 3  # 含前導空白
    total_w = w * 2 + 1
    print("=" * total_w)
    dual_line(f"【{name_a}】 {host_a}", f"【{name_b}】 {host_b}")
    print("-" * total_w)
    dual_line(
        f"接線: {cfg_a['wire_code']} ({cfg_a['wire_name']})",
        f"接線: {cfg_b['wire_code']} ({cfg_b['wire_name']})",
    )
    pt_a = cfg_a['pt_primary'] / cfg_a['pt_secondary'] if cfg_a['pt_secondary'] else 0
    pt_b = cfg_b['pt_primary'] / cfg_b['pt_secondary'] if cfg_b['pt_secondary'] else 0
    dual_line(
        f"PT: {cfg_a['pt_primary']}V/{cfg_a['pt_secondary']}V ({pt_a:.0f}:1)",
        f"PT: {cfg_b['pt_primary']}V/{cfg_b['pt_secondary']}V ({pt_b:.0f}:1)",
    )
    dual_line(
        f"CT: {cfg_a['ct_primary']}A / {cfg_a['ct_secondary']}",
        f"CT: {cfg_b['ct_primary']}A / {cfg_b['ct_secondary']}",
    )
    print("=" * total_w)


def format_voltage(d, wire_code):
    if wire_code.startswith("1P"):
        return f"V:  {d['v_avg']:7.1f} V  (A={d['v_a']:.1f})"
    return f"V:  {d['v_avg']:7.1f} V  (AB={d['v_a']:.1f})"


def print_dual_data(name_a, ts_a, d_a, wc_a, name_b, ts_b, d_b, wc_b):
    w = COL_W + 3
    total_w = w * 2 + 1

    dual_line(f"【{name_a}】 {ts_a}", f"【{name_b}】 {ts_b}")
    print("-" * total_w)
    dual_line(
        f"Freq:   {d_a['freq']:6.2f} Hz   ({wc_a})",
        f"Freq:   {d_b['freq']:6.2f} Hz   ({wc_b})",
    )
    dual_line(format_voltage(d_a, wc_a), format_voltage(d_b, wc_b))
    dual_line(
        f"Iavg: {d_a['i_avg']:7.3f} A",
        f"Iavg: {d_b['i_avg']:7.3f} A",
    )
    dual_line(
        f"Psum: {d_a['p_sum']/1000:7.3f} kW  ({d_a['p_sum']} W)",
        f"Psum: {d_b['p_sum']/1000:7.3f} kW  ({d_b['p_sum']} W)",
    )
    dual_line(
        f"PF:   {d_a['pf']:7.3f}  (calc={d_a['pf_calc']:.3f})",
        f"PF:   {d_b['pf']:7.3f}  (calc={d_b['pf_calc']:.3f})",
    )
    dual_line(
        f"S:    {d_a['s_kva']:7.3f} kVA",
        f"S:    {d_b['s_kva']:7.3f} kVA",
    )
    dual_line(
        f"Q:    {d_a['q_kvar']:7.3f} kVAr",
        f"Q:    {d_b['q_kvar']:7.3f} kVAr",
    )

    # 合計
    p_total = d_a['p_sum'] + d_b['p_sum']
    s_total = d_a['s_kva'] + d_b['s_kva']
    print("-" * total_w)
    print(f"  合計  Psum: {p_total/1000:.3f} kW ({p_total} W)"
          f"    S: {s_total:.3f} kVA")
    print("=" * total_w)


# ─── 單一電表資料讀取（含錯誤處理） ─────────────────────────────

def read_meter(client, device_id, wire_code):
    """讀取一顆電表，回傳 (timestamp, decoded_data) 或 None"""
    ts = read_time(client, device_id)
    block = read_block(client, 0x1000, 0xA0, device_id)
    decoded = decode_main(block, 0x1000, wire_code)
    return ts, decoded


# ─── Main ───────────────────────────────────────────────────────

def main():
    args = parse_args()

    # 連接兩顆電表
    print(f"正在連接電表 A【{args.name_a}】({args.host_a}:{args.port})...")
    client_a = ModbusTcpClient(args.host_a, port=args.port)
    if not client_a.connect():
        print(f"電表 A 連線失敗: {args.host_a}")
        return

    print(f"正在連接電表 B【{args.name_b}】({args.host_b}:{args.port})...")
    client_b = ModbusTcpClient(args.host_b, port=args.port)
    if not client_b.connect():
        print(f"電表 B 連線失敗: {args.host_b}")
        client_a.close()
        return

    print("兩顆電表皆連線成功！\n")

    # 讀取系統設定
    try:
        cfg_a = read_system_config(client_a, args.device_id)
        wc_a = cfg_a["wire_code"]
    except Exception as e:
        print(f"電表 A 系統設定讀取失敗: {e}，以 3P3W 繼續")
        cfg_a = {"wire_code": "3P3W", "wire_name": "預設", "pt_primary": 0,
                 "pt_secondary": 0, "ct_primary": 0, "ct_secondary": "N/A"}
        wc_a = "3P3W"

    try:
        cfg_b = read_system_config(client_b, args.device_id)
        wc_b = cfg_b["wire_code"]
    except Exception as e:
        print(f"電表 B 系統設定讀取失敗: {e}，以 3P3W 繼續")
        cfg_b = {"wire_code": "3P3W", "wire_name": "預設", "pt_primary": 0,
                 "pt_secondary": 0, "ct_primary": 0, "ct_secondary": "N/A"}
        wc_b = "3P3W"

    print_dual_config(args.name_a, cfg_a, args.name_b, cfg_b,
                      args.host_a, args.host_b)
    print(f"\n開始讀取量測資料 (Ctrl+C 停止)...\n")

    try:
        while True:
            err_a = err_b = None
            ts_a = ts_b = ""
            d_a = d_b = None

            try:
                ts_a, d_a = read_meter(client_a, args.device_id, wc_a)
            except Exception as e:
                err_a = str(e)

            try:
                ts_b, d_b = read_meter(client_b, args.device_id, wc_b)
            except Exception as e:
                err_b = str(e)

            if err_a or err_b:
                if err_a:
                    print(f"  電表 A【{args.name_a}】讀取錯誤: {err_a}")
                if err_b:
                    print(f"  電表 B【{args.name_b}】讀取錯誤: {err_b}")
                print("-" * 60)
                if args.once:
                    break
                time.sleep(max(args.interval, 0.1))
                continue

            print_dual_data(args.name_a, ts_a, d_a, wc_a,
                            args.name_b, ts_b, d_b, wc_b)

            if args.once:
                break
            time.sleep(max(args.interval, 0.1))

    except KeyboardInterrupt:
        print("\n停止讀取...")
    finally:
        client_a.close()
        client_b.close()
        print("兩顆電表連線已關閉。")


if __name__ == "__main__":
    main()
