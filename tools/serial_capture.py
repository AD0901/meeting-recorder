#!/usr/bin/env python3
"""
接收 mic_probe 固件从串口发来的录音，按通道存成 WAV。

用法（用 ESP-IDF 自带的 python，里面有 pyserial）：
    . ~/esp/esp-idf/export.sh
    python tools/serial_capture.py                 # 默认 /dev/cu.usbmodem101
    python tools/serial_capture.py -p /dev/cu.usbmodemXXX -o recordings

待机时会滚动显示四路电平；在板子上按 BOOT 录 5 秒，录完自动保存：
    recordings/probe_<时间>_ch0.wav ... ch3.wav，以及 4 声道的 _all.wav
按 Ctrl+C 退出。
"""
import argparse
import base64
import binascii
import re
import struct
import sys
import time
import wave
from pathlib import Path

import serial

BEGIN_RE = re.compile(r"===PCM BEGIN rate=(\d+) ch=(\d+) bits=16 bytes=(\d+) crc32=([0-9a-f]+)===")


def save(outdir: Path, pcm: bytes, rate: int, ch: int) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    n = len(pcm) // (2 * ch)
    samples = struct.unpack(f"<{n * ch}h", pcm[: n * ch * 2])

    with wave.open(str(outdir / f"probe_{stamp}_all.wav"), "wb") as w:
        w.setnchannels(ch); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm[: n * ch * 2])

    print(f"\n已保存 {n / rate:.2f} 秒，{ch} 路：")
    for c in range(ch):
        mono = samples[c::ch]
        path = outdir / f"probe_{stamp}_ch{c}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            w.writeframes(struct.pack(f"<{len(mono)}h", *mono))
        rms = (sum(v * v for v in mono) / max(len(mono), 1)) ** 0.5
        peak = max((abs(v) for v in mono), default=0)
        db = -90.0 if rms < 1 else 20 * __import__("math").log10(rms / 32768)
        print(f"  ch{c}: RMS {db:6.1f} dBFS  peak {peak:5d}  → {path}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--port", default="/dev/cu.usbmodem101")
    ap.add_argument("-o", "--outdir", default=str(Path(__file__).resolve().parent.parent / "recordings"))
    args = ap.parse_args()

    ser = serial.Serial(args.port, 115200, timeout=1)
    print(f"监听 {args.port}，在板子上按 BOOT 开始录音（Ctrl+C 退出）")
    collecting = None  # (rate, ch, nbytes, crc, [chunks])
    while True:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode(errors="replace").rstrip("\r\n")
        if collecting is None:
            m = BEGIN_RE.search(line)
            if m:
                rate, ch, nbytes = int(m[1]), int(m[2]), int(m[3])
                collecting = (rate, ch, nbytes, int(m[4], 16), [])
                print(f"接收中：{nbytes} 字节 …", end="", flush=True)
            elif line:
                print(line)
            continue
        if line.startswith("===PCM END==="):
            rate, ch, nbytes, crc, chunks = collecting
            collecting = None
            try:
                pcm = base64.b64decode("".join(chunks))
            except binascii.Error as e:
                print(f"\nbase64 解码失败：{e}")
                continue
            ok = len(pcm) == nbytes and (binascii.crc32(pcm) & 0xFFFFFFFF) == crc
            print(f" 收到 {len(pcm)} 字节，校验{'通过' if ok else '失败（数据可能不完整）'}")
            save(Path(args.outdir), pcm, rate, ch)
            continue
        collecting[4].append(line.strip())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n退出")
