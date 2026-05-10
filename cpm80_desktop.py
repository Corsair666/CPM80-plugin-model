#!/usr/bin/env python3
"""
CPM-80 桌面 App — pywebview 原生視窗啟動器

將 cpm80_dashboard.py (FastAPI + Vue 3 SPA) 包裝為 macOS 桌面應用程式，
使用 WebKit 原生視窗顯示，不需要外部瀏覽器。

用法：
    python cpm80_desktop.py              # Demo 模式（預設）
    python cpm80_desktop.py --no-demo    # 連接實體電表
    python cpm80_desktop.py --no-demo --host-a 10.0.60.21 --host-b 10.0.60.22
"""

import argparse
import sys
import threading
import time
import urllib.request
import urllib.error


def parse_desktop_args():
    """解析桌面啟動器專用的 CLI 參數。"""
    p = argparse.ArgumentParser(
        description="CPM-80 桌面 App — pywebview 原生視窗",
    )
    p.add_argument("--no-demo", action="store_true",
                   help="連接實體電表（預設為 Demo 模式）")
    p.add_argument("--port", type=int, default=8088,
                   help="內部 server port (預設: 8088)")
    p.add_argument("--host-a", default="10.0.60.21",
                   help="電表 A IP (預設: 10.0.60.21)")
    p.add_argument("--host-b", default="10.0.60.22",
                   help="電表 B IP (預設: 10.0.60.22)")
    p.add_argument("--ollama-url", default="http://10.0.60.180:11434",
                   help="Ollama API URL")
    p.add_argument("--ollama-model", default="qwen2.5:14b",
                   help="Ollama 模型名稱")
    return p.parse_args()


def build_dashboard_argv(desktop_args):
    """將桌面參數轉換為 cpm80_dashboard.py 可接受的 sys.argv 格式。"""
    argv = ["cpm80_dashboard.py"]
    # 桌面 App 強制綁定 127.0.0.1，不對外開放
    argv += ["--bind", "127.0.0.1"]
    argv += ["--port", str(desktop_args.port)]
    argv += ["--host-a", desktop_args.host_a]
    argv += ["--host-b", desktop_args.host_b]
    argv += ["--ollama-url", desktop_args.ollama_url]
    argv += ["--ollama-model", desktop_args.ollama_model]
    # 預設 Demo 模式；--no-demo 時不加 --demo
    if not desktop_args.no_demo:
        argv.append("--demo")
    return argv


def wait_for_server(port, timeout=15):
    """輪詢等待 server 就緒，最多等 timeout 秒。"""
    url = f"http://127.0.0.1:{port}/api/config"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def run_server(app, port):
    """在背景 thread 中啟動 uvicorn。"""
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main():
    desktop_args = parse_desktop_args()
    port = desktop_args.port

    # ── 1. 注入 sys.argv，讓 dashboard 的 module-level parse_args() 正確解析 ──
    sys.argv = build_dashboard_argv(desktop_args)

    # ── 2. 匯入 dashboard module（此時會觸發 parse_args()） ──
    import cpm80_dashboard  # noqa: E402

    mode = "Demo 模式" if not desktop_args.no_demo else "Live 模式"
    print(f"[Desktop] 啟動 CPM-80 桌面 App（{mode}）")
    print(f"[Desktop] 內部 server: http://127.0.0.1:{port}")

    # ── 3. 背景 thread 啟動 uvicorn ──
    server_thread = threading.Thread(
        target=run_server,
        args=(cpm80_dashboard.app, port),
        daemon=True,
    )
    server_thread.start()

    # ── 4. 等待 server 就緒 ──
    print("[Desktop] 等待 server 啟動...")
    if not wait_for_server(port):
        print("[Desktop] 錯誤：server 啟動逾時（15 秒），請檢查 port 是否被佔用。")
        sys.exit(1)
    print("[Desktop] Server 就緒！")

    # ── 5. 開啟 pywebview 原生視窗 ──
    import webview

    window = webview.create_window(
        title="CPM-80 智慧電表",
        url=f"http://127.0.0.1:{port}",
        width=1280,
        height=820,
        min_size=(800, 600),
    )
    # macOS 預設使用 WebKit，保留系統標題列（紅綠燈按鈕）
    webview.start()

    # ── 6. 視窗關閉後，daemon thread 自動結束 ──
    print("[Desktop] 視窗已關閉，程式結束。")


if __name__ == "__main__":
    main()
