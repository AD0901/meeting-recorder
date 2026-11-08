#!/usr/bin/env python3
"""持续打印设备串口日志（带时间戳）；设备重启 / 重新插拔后自动重连。"""
import os, sys, time
import serial

port = sys.argv[1] if len(sys.argv) > 1 else "/dev/cu.usbmodem101"
while True:
    try:
        s = serial.Serial(port, 115200, timeout=1)
        ino, last = os.stat(port).st_ino, time.time()
        print(f"-- 已连接 {port}", flush=True)
        while True:
            line = s.readline()
            if line:
                print(time.strftime("%H:%M:%S"), line.decode(errors="replace").rstrip(), flush=True)
            if time.time() - last > 2:
                last = time.time()
                if os.stat(port).st_ino != ino:
                    raise OSError("re-enumerated")
    except KeyboardInterrupt:
        break
    except Exception as e:  # noqa: BLE001
        print("-- 串口断开:", e, flush=True)
        time.sleep(1)
