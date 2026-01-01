"""Small desktop entry point; all data operations use the same tested CLI."""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    return subprocess.run([sys.executable, "-m", "collector_music", *map(str,args)], cwd=ROOT).returncode


def select_log():
    try:
        import tkinter as tk
        from tkinter.filedialog import askopenfilename
        window = tk.Tk()
        window.withdraw()
        try:
            return askopenfilename(title="选择设备导出的完整 Pano JSONL 日志", filetypes=[("JSONL", "*.jsonl")])
        finally:
            window.destroy()
    except ImportError:
        return input("日志完整路径：").strip().strip('"')


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    run("init")
    while True:
        print("\n音乐数据 · 本地控制台\n"
              "1 查看状态    2 导入手机日志    3 导入平板日志\n"
              "4 网易云扫码登录    5 采集网易云快照    6 发布标准数据\n"
              "7 查看日级数据    8 一致性备份    9 校验资料\n"
              "10 从 USB 接收手机日志    11 打开网页看板    0 退出")
        choice = input("选择：").strip()
        if choice == "0":
            return
        if choice in ("2", "3"):
            path = select_log()
            if not path:
                continue
            stream = input("日志实例名 [pano-main-001]（同一文件的历次副本保持相同）：").strip() or "pano-main-001"
            device = "phone" if choice == "2" else "tablet"
            if run("import-pano", path, "--device", device, "--stream", stream) == 0:
                run("publish")
        elif choice == "1":
            run("status")
        elif choice == "4":
            run("login")
        elif choice == "5":
            if run("collect") in (0, 2):
                run("publish")
        elif choice == "6":
            run("publish")
        elif choice == "7":
            run("query", "SELECT * FROM daily_music_features ORDER BY date DESC,source,device_key")
        elif choice == "8":
            target = ROOT / "data/backups" / ("music-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".sqlite")
            run("backup", target)
        elif choice == "9":
            run("verify")
        elif choice == "10":
            run("receive-phone")
        elif choice == "11":
            run("dashboard", "--background")
        else:
            print("请选择 0–11。")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("已退出。")
