# -*- coding: utf-8 -*-
"""
timeline.json（编辑层） -> cues.json（执行层）编译

两层模型：
  编辑层 timeline.json = 相对时间事件序列（从 0 起，不绑目标）
  编译   本脚本        = 相对时间 + offset -> 绝对时间码，合并环境配置
  执行层 cues.json     = mtc_to_obs.py 直接消费

用法：
  python compile.py              编译同目录下的 timeline.json
  python compile.py path.json    编译指定时间线文件

产物：cues.json（覆盖写入）
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TIMELINE_PATH = os.path.join(HERE, "timeline.json")
CUES_PATH = os.path.join(HERE, "cues.json")

# 环境/目标配置（不属于节目内容，编译时合并进来；换电脑/换 MIDI 口/改 OBS 密码只动这里）
DEFAULT_ENV = {
    "midi_port": "loopMIDI Port 1 0",
    "obs_host": "localhost",
    "obs_port": 4455,
    "obs_password": "",
}


def tc_to_frames(tc, fps):
    """'h:mm:ss:ff' -> 总帧数"""
    parts = [int(x) for x in tc.strip().split(":")]
    while len(parts) < 4:
        parts.insert(0, 0)
    h, m, s, f = parts
    return ((h * 60 + m) * 60 + s) * fps + f


def frames_to_tc(frames, fps):
    """总帧数 -> 'hh:mm:ss:ff'"""
    f = frames % fps
    total_s = frames // fps
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def compile_timeline(tl):
    """timeline dict -> cue 列表（绝对时间码）"""
    fps = tl.get("fps") or 30
    offset_frames = tc_to_frames(tl.get("offset", "00:00:00:00"), fps)
    cues = []
    for ev in tl["events"]:
        abs_frames = tc_to_frames(ev["t"], fps) + offset_frames
        cues.append({
            "tc": frames_to_tc(abs_frames, fps),
            "action": ev["action"],
            "params": ev.get("params"),
        })
    return cues


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else TIMELINE_PATH
    with open(path, "r", encoding="utf-8") as fp:
        tl = json.load(fp)

    fps = tl.get("fps") or 30
    cues = compile_timeline(tl)

    out = dict(DEFAULT_ENV)
    # timeline 显式指定了 fps 就写进产物；None 则执行层运行时自动检测
    out["fps"] = tl.get("fps")
    out["cues"] = cues

    with open(CUES_PATH, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=4)

    print(f"[编译] {tl.get('name', '未命名')}：{len(cues)} 条 event")
    print(f"[编译] offset={tl.get('offset', '00:00:00:00')}  fps={fps}")
    for c in cues:
        p = c.get("params")
        print(f"  {c['tc']}  {c['action']:<10} {p}")
    print(f"[编译] 已写入 {CUES_PATH}")


if __name__ == "__main__":
    main()
