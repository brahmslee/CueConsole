# -*- coding: utf-8 -*-
"""
MTC 主时钟发送器
================
从指定起始时间码开始，按标准节奏（每帧 4 条 quarter-frame）持续发送 MTC。

用途：
  1. 验证接收端（P1 时间码源切「MIDI Timecode」/ 灯控台 MTC 从机）能否跟随我们的时间码
  2. 将来引擎「当主时钟」能力的雏形——替代 P1 给灯控台发时间码

标准节奏说明：MTC 一帧 4 条 quarter-frame，8 条（seq 0-7）描述同一个帧值、
跨 2 帧时长。30fps = 每秒 120 条。

用法：
  python mtc_sender.py                          默认: loopMIDI Port 2, 起点 00:49:55:00, 30fps
  python mtc_sender.py <口名子串> <起始tc> <fps>
Ctrl+C 停止
"""

import sys
import time

import mido


def tc_to_frames(tc, fps):
    parts = [int(x) for x in tc.strip().split(":")]
    while len(parts) < 4:
        parts.insert(0, 0)
    h, m, s, f = parts
    return ((h * 60 + m) * 60 + s) * fps + f


def frames_to_tc(frames, fps):
    f = frames % fps
    total_s = frames // fps
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def quarter_data(frames, fps):
    """完整帧号 -> 8 条 quarter-frame 的数据半字节（seq 0-7）"""
    f = frames % fps
    total_s = frames // fps
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    fps_code = 3  # 30 non-drop（接收端 FPS_BITS 把 2/3 都当 30）
    return [
        f & 0x0F,                              # seq0 帧低
        (f >> 4) & 0x0F,                       # seq1 帧高
        s & 0x0F,                              # seq2 秒低
        (s >> 4) & 0x0F,                       # seq3 秒高
        m & 0x0F,                              # seq4 分低
        (m >> 4) & 0x0F,                       # seq5 分高
        h & 0x0F,                              # seq6 时低
        ((h >> 4) & 0x01) | (fps_code << 1),   # seq7 时高位(bit0) + fps码(bit1-2)
    ]


def main():
    args = sys.argv[1:]

    ports = mido.get_output_names()
    if not ports:
        print("[错误] 未找到任何 MIDI 输出口，请确认灯控台/loopMIDI 已连接")
        input("按回车退出...")
        sys.exit(1)

    print("[MIDI] 可用输出口:")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p}")

    if args:
        # 命令行模式：口名子串 + 起始tc + fps
        port_hint = args[0]
        start_tc = args[1] if len(args) > 1 else "00:49:55:00"
        fps = int(args[2]) if len(args) > 2 else 30
        port_name = next((p for p in ports if port_hint in p), None)
        if port_name is None:
            print(f"[错误] 找不到包含「{port_hint}」的输出口")
            sys.exit(1)
    else:
        # 交互模式（双击 exe）：选口序号 + 起始tc + fps
        try:
            idx = int(input("请输入目标口序号: ").strip())
            port_name = ports[idx]
        except (ValueError, IndexError):
            print("[错误] 序号无效")
            input("按回车退出...")
            sys.exit(1)
        s = input("起始时间码 [默认 00:49:55:00]: ").strip()
        start_tc = s if s else "00:49:55:00"
        s = input("帧率 [默认 30]: ").strip()
        fps = int(s) if s else 30

    start_frames = tc_to_frames(start_tc, fps)
    interval = 1.0 / (4 * fps)

    print(f"[MTC] 输出口: {port_name}")
    print(f"[MTC] 起点: {start_tc} @ {fps}fps，发送中... (Ctrl+C 停止)")

    with mido.open_output(port_name) as out:
        tick = 0
        next_t = time.time()
        last_report = 0.0
        while True:
            # 标准节奏：seq 0-3 与 seq 4-7 描述同一个帧；每 8 条跨 2 帧、帧号推进 2
            frames = start_frames + (tick // 8) * 2 + (1 if tick % 8 >= 4 else 0)
            seq = tick % 8
            data = quarter_data(frames, fps)[seq]
            out.send(mido.Message("quarter_frame", frame_type=seq, frame_value=data))
            tick += 1

            now = time.time()
            if now - last_report >= 2.0:
                last_report = now
                print(f"[MTC] 发送中 {frames_to_tc(frames, fps)}")

            # 绝对时间调度，避免 sleep 粒度误差累积；落后太多则重置防追赶风暴
            next_t += interval
            sleep_for = next_t - time.time()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_t = time.time()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[退出]")
