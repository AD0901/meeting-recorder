#!/usr/bin/env python3
"""
通义听悟 POC：一段会议录音 → 会议纪要网页。

    cd server
    .venv/bin/python poc_tingwu.py ../recordings/session_xxx.wav     # 真实调用听悟
    .venv/bin/python poc_tingwu.py --demo                             # 不调用任何接口，用示例数据看页面效果
    .venv/bin/python poc_tingwu.py --from-results data/poc/<任务目录>  # 用已下载的听悟结果重新生成页面

输出在 server/data/poc/<时间>_<文件名>/：
    tingwu/*.json   听悟原始结果
    minutes.json    统一纪要结构
    minutes.html    纪要页面（双击打开）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
load_dotenv(HERE / ".env")

from app.minutes.render import render_html  # noqa: E402
from app.providers.tingwu import Tingwu, to_minutes  # noqa: E402


def write_outputs(outdir: Path, minutes: dict) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "minutes.json").write_text(json.dumps(minutes, ensure_ascii=False, indent=2))
    page = outdir / "minutes.html"
    page.write_text(render_html(minutes))
    return page


def run_real(audio: Path, speakers: int) -> Path:
    marks = []
    meta = audio.with_suffix(".json")
    if meta.exists():  # 设备录音附带的打点信息
        marks = json.loads(meta.read_text()).get("marks_ms", [])
    duration = None  # mp3 / m4a 等格式由听悟结果里的 AudioInfo 给出时长
    if audio.suffix.lower() == ".wav":
        with wave.open(str(audio)) as w:
            duration = w.getnframes() / w.getframerate()

    outdir = HERE / "data" / "poc" / f"{time.strftime('%Y%m%d_%H%M%S')}_{audio.stem}"
    tw = Tingwu()
    print(f"① 上传到 OSS：{audio.name}" + (f"（{duration:.1f} 秒）" if duration else ""))
    key, url = tw.upload(audio)
    print(f"   oss://{tw.bucket.bucket_name}/{key}")
    print("② 创建听悟离线任务（说话人分离 + 摘要 + 章节 + 发言总结 + 问答 + 待办 + 下一步规划）")
    task_id = tw.create_task(url, speaker_count=speakers)
    print(f"   TaskId = {task_id}")
    print("③ 等待处理（通常是音频时长的 1/5 ~ 1/2）")
    info = tw.wait(task_id, on_poll=lambda s, t: print(f"\r   {s}  已等待 {t:4.0f} 秒", end="", flush=True))
    print("\n④ 下载结果")
    results = Tingwu.download_results(info, outdir / "tingwu")
    print(f"   {', '.join(results)}")
    minutes = to_minutes(results, duration_s=duration, marks_ms=marks)
    return write_outputs(outdir, minutes)


def run_from_results(d: Path) -> Path:
    results = {p.stem: json.loads(p.read_text()) for p in (d / "tingwu").glob("*.json")}
    return write_outputs(d, to_minutes(results))


def demo_minutes() -> dict:
    """示例数据：只用于预览页面排版，内容是虚构的。"""
    T = lambda m, s=0: (m * 60 + s) * 1000  # noqa: E731
    return {
        "title": "【示例】会议录音器原型评审会",
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "duration_s": 1520,
        "engine": "示例数据（未调用任何接口）",
        "speakers": [{"name": "高一丹", "talk_s": 640}, {"name": "张三", "talk_s": 420}, {"name": "李四", "talk_s": 300}],
        "keywords": ["会议录音器", "说话人分离", "私有化部署", "通义听悟", "麦克风阵列", "知识图谱"],
        "summary": "本次会议评审了会议录音器原型的进展。设备端已完成按键录音、屏幕状态和本地缓冲，"
                   "下一步接入 Wi-Fi 上传与转写服务。与会者认为私有化部署是主要差异点，"
                   "但板载双麦拾音距离有限，正式方案需更换麦克风阵列。\n"
                   "- 原型已跑通录音与导出，音频完整无丢片\n- 转写与纪要先用通义听悟验证效果，后续支持本地模型切换\n"
                   "- 纪要需支持上传 PPT 作为补充材料",
        "key_points": ["客户最关心数据是否出公司", "6 人以上会议需要更好的拾音硬件"],
        "actions": [
            {"text": "完成设备 Wi-Fi 配网与分片上传", "owner": "高一丹", "due": "下周三"},
            {"text": "调研带 DSP 的麦克风阵列模块并给出报价", "owner": "张三", "due": "本周五"},
            {"text": "整理 3 家竞品的纪要样例做对比", "owner": "李四", "due": "未明确"},
        ],
        "next_steps": "- 用真实 3 人会议录音验证说话人分离准确率（负责人：高一丹；时间：下周）\n"
                      "- 确认私有化部署的服务器配置（负责人：张三；时间：未明确）",
        "chapters": [
            {"start_ms": T(0), "end_ms": T(6), "title": "原型进展汇报", "summary": "介绍录音、屏幕、缓冲三项已完成的功能和测试结果。"},
            {"start_ms": T(6), "end_ms": T(15), "title": "拾音硬件讨论", "summary": "板载麦克风只适合近距离，讨论换阵列麦克风的成本。"},
            {"start_ms": T(15), "end_ms": T(25), "title": "交付形式与竞品", "summary": "对比通义听悟、飞书妙记，确定纪要页面结构。"},
        ],
        "speaker_summaries": [
            {"speaker": "高一丹", "summary": "汇报原型进展，提出先用听悟验证纪要效果，再做本地化与模型切换。"},
            {"speaker": "张三", "summary": "关注拾音距离与硬件成本，建议评估 XVF3800 等阵列方案。"},
            {"speaker": "李四", "summary": "从客户视角强调私有化与知识库沉淀，建议纪要支持附件材料。"},
        ],
        "qa": [{"q": "设备断网时录音会丢吗？", "a": "设备内存可缓冲约 2 分钟；TF 卡到货后改为先存卡、有网再补传。"}],
        "marks_ms": [T(7, 30)],
        "transcript": [
            {"speaker": "高一丹", "start_ms": T(0, 5), "text": "今天主要同步一下录音器原型的进展，按键录音和屏幕都已经可以用了。"},
            {"speaker": "张三", "start_ms": T(7, 20), "text": "我比较担心拾音距离，会议室大一点的话板载麦克风可能不够。"},
            {"speaker": "李四", "start_ms": T(15, 2), "text": "客户那边最在意的是数据能不能留在自己公司。"},
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio", nargs="?", type=Path, help="会议录音 WAV")
    ap.add_argument("--speakers", type=int, default=0, help="已知人数时填 2；0 = 自动判断（默认）")
    ap.add_argument("--demo", action="store_true", help="用示例数据生成页面")
    ap.add_argument("--from-results", type=Path, help="用已下载的听悟结果目录重新生成页面")
    a = ap.parse_args()

    if a.demo:
        page = write_outputs(HERE / "data" / "poc" / "demo", demo_minutes())
    elif a.from_results:
        page = run_from_results(a.from_results)
    elif a.audio:
        page = run_real(a.audio, a.speakers)
    else:
        ap.print_help()
        return 1
    print(f"✅ 纪要页面：{page}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
