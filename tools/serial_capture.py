#!/usr/bin/env python3
"""
从 USB 串口接收设备发来的录音，存成 WAV。支持两种固件：

1. firmware/（正式固件，里程碑 2）——会话协议，和第 5 节上传协议一一对应：
     @@BEGIN session=1 device_id=... sample_rate=16000 channels=1 format=pcm_s16le
     @@CHUNK session=1 seq=0 bytes=32000 crc32=xxxxxxxx b64=...
     @@MARK  session=1 offset_ms=12345
     @@END   session=1 last_seq=29
   收到 END 后检查分片是否齐全，按 seq 拼接成：
     recordings/session_<时间>_<device>_s<N>.wav  以及同名 .json（打点、缺片、校验结果）

2. hw-test/mic_probe（诊断固件）——四路原始 PCM，按通道各存一个 WAV。

用法（用 ESP-IDF 自带的 python，里面有 pyserial）：
    . ~/esp/esp-idf/export.sh
    python tools/serial_capture.py                    # 默认 /dev/cu.usbmodem101
    python tools/serial_capture.py -q                 # 不打印设备日志
按 Ctrl+C 退出。
"""
import argparse
import base64
import binascii
import json
import math
import os
import re
import struct
import sys
import time
import wave
from pathlib import Path

import serial

ROOT = Path(__file__).resolve().parent.parent
KV_RE = re.compile(r"(\w+)=(\S+)")
PROBE_BEGIN_RE = re.compile(r"===PCM BEGIN rate=(\d+) ch=(\d+) bits=16 bytes=(\d+) crc32=([0-9a-f]+)===")


def kv(line: str) -> dict:
    return dict(KV_RE.findall(line))


def dbfs(samples) -> float:
    if not samples:
        return -90.0
    rms = math.sqrt(sum(v * v for v in samples) / len(samples))
    return -90.0 if rms < 1 else 20 * math.log10(rms / 32768)


class Session:
    def __init__(self, meta: dict):
        self.id = int(meta["session"])
        self.device_id = meta.get("device_id", "unknown")
        self.rate = int(meta.get("sample_rate", 16000))
        self.chunks: dict[int, bytes] = {}
        self.marks: list[int] = []
        self.bad_crc: list[int] = []
        self.dup = 0
        self.t0 = time.time()

    def add_chunk(self, meta: dict, b64: str) -> None:
        seq = int(meta["seq"])
        try:
            data = base64.b64decode(b64)
        except binascii.Error:
            self.bad_crc.append(seq)
            return
        ok = len(data) == int(meta["bytes"]) and (binascii.crc32(data) & 0xFFFFFFFF) == int(meta["crc32"], 16)
        if not ok:
            self.bad_crc.append(seq)
            return
        if seq in self.chunks:
            self.dup += 1  # 幂等：重复 seq 直接忽略
            return
        self.chunks[seq] = data

    def finish(self, last_seq: int, outdir: Path) -> Path:
        expected = list(range(last_seq + 1)) if last_seq >= 0 else []
        missing = [s for s in expected if s not in self.chunks]
        pcm = b"".join(self.chunks.get(s, b"\x00" * 32000) for s in expected)  # 缺片补静音，保证时间轴不错位
        outdir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.t0))
        base = outdir / f"session_{stamp}_{self.device_id[-6:]}_s{self.id}"
        with wave.open(str(base.with_suffix(".wav")), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(pcm)
        n = len(pcm) // 2
        samples = struct.unpack(f"<{n}h", pcm) if n else ()
        info = {
            "device_id": self.device_id,
            "session": self.id,
            "duration_s": round(n / self.rate, 3),
            "chunks_received": len(self.chunks),
            "last_seq": last_seq,
            "missing_seq": missing,
            "bad_crc_seq": self.bad_crc,
            "duplicate_chunks": self.dup,
            "marks_ms": self.marks,
            "rms_dbfs": round(dbfs(samples), 1),
            "peak": max((abs(v) for v in samples), default=0),
        }
        base.with_suffix(".json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
        ok = not missing and not self.bad_crc
        print(f"\n{'✅' if ok else '⚠️ '} 会话 {self.id} 已保存：{base.with_suffix('.wav')}")
        print(f"   时长 {info['duration_s']:.2f} 秒，分片 {len(self.chunks)}/{len(expected)}，"
              f"缺片 {missing or '无'}，校验失败 {self.bad_crc or '无'}，"
              f"打点 {[round(m / 1000, 1) for m in self.marks] or '无'}（秒），"
              f"电平 {info['rms_dbfs']} dBFS，峰值 {info['peak']}\n")
        return base.with_suffix(".wav")


def save_probe(outdir: Path, pcm: bytes, rate: int, ch: int) -> None:
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
        print(f"  ch{c}: RMS {dbfs(mono):6.1f} dBFS  peak {max((abs(v) for v in mono), default=0):5d}  → {path}")
    print()


def make_minutes(wav: Path) -> None:
    """调用 server/poc_tingwu.py（听悟）生成纪要，并在浏览器打开。"""
    import subprocess
    server = ROOT / "server"
    print("→ 自动生成会议纪要（通义听悟）…")
    proc = subprocess.run([str(server / ".venv/bin/python"), "-u", str(server / "poc_tingwu.py"), str(wav)],
                          cwd=server, capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    for line in out.replace("\r", "\n").splitlines():
        if line.strip() and "已等待" not in line:
            print("   " + line)
    page = next((l.split("：", 1)[1].strip() for l in out.splitlines() if l.startswith("✅ 纪要页面：")), None)
    if page:
        subprocess.run(["open", page])


def port_ino(port):
    try:
        return os.stat(port).st_ino
    except OSError:
        return None


def open_port(port):
    """打开串口；设备不在就一直等。返回 (串口, 设备节点 inode)。"""
    while True:
        ino = port_ino(port)
        if ino is not None:
            try:
                return serial.Serial(port, 115200, timeout=1), ino
            except (serial.SerialException, OSError):
                pass
        time.sleep(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--port", default="/dev/cu.usbmodem101")
    ap.add_argument("-o", "--outdir", default=str(ROOT / "recordings"))
    ap.add_argument("-q", "--quiet", action="store_true", help="不打印设备日志")
    ap.add_argument("--minutes", action="store_true", help="收到录音后自动调用通义听悟生成纪要并打开页面")
    args = ap.parse_args()
    outdir = Path(args.outdir)

    ser, ino = open_port(args.port)
    print(f"监听 {args.port}，录音保存到 {outdir}（Ctrl+C 退出）")
    last_check = time.time()
    sessions: dict[int, Session] = {}
    probe = None  # mic_probe: (rate, ch, nbytes, crc, [lines])

    while True:
        try:
            raw = ser.readline()
        except (serial.SerialException, OSError):
            raw, ino = b"", None  # 设备拔掉 / 重启：强制重连
        # USB-JTAG 重新枚举后旧句柄不再收数据也不报错，定期比对设备节点
        if ino is None or (time.time() - last_check > 2 and port_ino(args.port) != ino):
            try:
                ser.close()
            except Exception:
                pass
            print("\n⚠️  串口断开，等待设备重新连接 …", flush=True)
            ser, ino = open_port(args.port)
            print(f"✅ 已重新连接 {args.port}", flush=True)
        if time.time() - last_check > 2:
            last_check = time.time()
        if not raw:
            continue
        line = raw.decode(errors="replace").rstrip("\r\n")

        if probe is not None:
            if line.startswith("===PCM END==="):
                rate, ch, nbytes, crc, parts = probe
                probe = None
                pcm = base64.b64decode("".join(parts))
                ok = len(pcm) == nbytes and (binascii.crc32(pcm) & 0xFFFFFFFF) == crc
                print(f"收到 {len(pcm)} 字节，校验{'通过' if ok else '失败'}")
                save_probe(outdir, pcm, rate, ch)
            else:
                probe[4].append(line.strip())
            continue

        if line.startswith("@@CHUNK"):
            head, _, b64 = line.partition(" b64=")
            meta = kv(head)
            s = sessions.get(int(meta["session"]))
            if s:
                s.add_chunk(meta, b64.strip())
                print(f"\r   接收中：{len(s.chunks)} 秒" + (f"（校验失败 {len(s.bad_crc)}）" if s.bad_crc else ""),
                      end="", flush=True)
            continue
        if line.startswith("@@BEGIN"):
            meta = kv(line)
            sessions[int(meta["session"])] = Session(meta)
            print(f"\n● 会话 {meta['session']} 开始（设备 {meta.get('device_id')}）")
            continue
        if line.startswith("@@MARK"):
            meta = kv(line)
            s = sessions.get(int(meta["session"]))
            if s:
                s.marks.append(int(meta["offset_ms"]))
                print(f"\n   ★ 打点 {int(meta['offset_ms']) / 1000:.1f} 秒")
            continue
        if line.startswith("@@END"):
            meta = kv(line)
            s = sessions.pop(int(meta["session"]), None)
            if s:
                wav = s.finish(int(meta["last_seq"]), outdir)
                if args.minutes:
                    make_minutes(wav)
            else:
                print(f"\n⚠️  收到会话 {meta['session']} 的 END，但没收到它的 BEGIN（脚本开晚了？）")
            continue
        m = PROBE_BEGIN_RE.search(line)
        if m:
            probe = (int(m[1]), int(m[2]), int(m[3]), int(m[4], 16), [])
            print(f"接收诊断录音 {m[3]} 字节 …")
            continue
        if line and not args.quiet and not line.startswith("LVL") and " xport: " not in line:
            print(f"\n   [设备] {line}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n退出")
