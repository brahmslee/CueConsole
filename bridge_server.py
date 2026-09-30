# -*- coding: utf-8 -*-
"""
节目控台 - 本地桥接服务（bridge）
================================
编辑器(浏览器, 纯 HTML) --HTTP--> 本服务 --obs-websocket--> OBS
                                 本服务 --mido--> loopMIDI（发 MTC，第 3 步预留）

为什么要有这一层：浏览器 JS 无法直接走 obs-websocket 的鉴权+协议封装，
所以编辑器连本服务，本服务用 obsws-python 连 OBS。第 3 步「内置主时钟」
（发 MTC + 触发 cue）也走这同一进程，接口面一次设计好。

接口（全部 GET，返回 JSON，已开 CORS）：
  /api/status        -> {"ok":true,"connected":bool,"current_scene":str}
  /api/resources     -> 一次性返回编辑器下拉所需全部资源（见 build_resources）
  /api/scenes        -> {"scenes":[...]}
  /api/transitions   -> {"transitions":[...]}
  /api/playback      -> {"ok":true,"playing":bool,"current_tc":str}（内置主时钟状态）
  POST /api/play     -> {"fps":30,"start_tc":"00:00:00:00","cues":[...],"midi_port":"loopMIDI Port 1"} 起内置主时钟播放
  POST /api/stop     -> 停止播放

源类型过滤规则（用 unversionedInputKind 判，比 inputKind 少了 _v3 版本后缀，稳）：
  text   文本源   text_gdiplus / text_ft2
  media  媒体源   ffmpeg_source / browser_source / vlc_source（有 play/stop 控制）
  audio  音频源   wasapi_* 三兄弟 + 上面三类媒体源（都有音频轨，能静音/调音量）
  items  场景项目 get_scene_item_list 的 sourceName 并集（visibility/transform 用）

运行：
  python bridge_server.py            # 起 HTTP 服务（默认 127.0.0.1:8765）
  python bridge_server.py --selftest # 不启服务，直接打印 build_resources() 结果
"""

import os
import sys

# 绕过系统代理（同 mtc_to_obs.py，否则连 localhost OBS 走代理报 502）
os.environ["NO_PROXY"] = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "localhost", "127.0.0.1"]))
os.environ["no_proxy"] = os.environ["NO_PROXY"]

import json
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import mido
from obsws_python import ReqClient

# 复用引擎动作分发（第 3 步：bridge 内置主时钟，发 MTC + 到点触发 cue 直驱 OBS）
from mtc_to_obs import fire_action

HOST = "127.0.0.1"
PORT = 8765

OBS_HOST = "localhost"
OBS_PORT = 4455
OBS_PASSWORD = ""

# 源类型过滤（unversionedInputKind 白名单）
TEXT_KINDS = ("text_gdiplus", "text_ft2")
MEDIA_KINDS = ("ffmpeg_source", "browser_source", "vlc_source")
AUDIO_KINDS = ("wasapi_output_capture", "wasapi_input_capture",
               "wasapi_process_output_capture") + MEDIA_KINDS

obs = None


def get_obs():
    """获取（懒连接）OBS websocket 客户端，失败抛异常。"""
    global obs
    if obs is None:
        obs = ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD)
    return obs


def build_resources():
    """拉取 OBS 全部资源，返回编辑器下拉所需的结构化数据。"""
    o = get_obs()
    scenes = [s["sceneName"] for s in o.get_scene_list().scenes]
    transitions = [t["transitionName"] for t in o.get_scene_transition_list().transitions]
    inputs = o.get_input_list().inputs  # list[dict]，key 驼峰

    text_sources = [i["inputName"] for i in inputs
                    if i.get("unversionedInputKind", "") in TEXT_KINDS]
    media_sources = [i["inputName"] for i in inputs
                     if i.get("unversionedInputKind", "") in MEDIA_KINDS]
    audio_sources = [i["inputName"] for i in inputs
                     if i.get("unversionedInputKind", "") in AUDIO_KINDS]

    # 场景项目：按场景分组（visibility/transform 的"目标源"要跟着所选场景联动）
    scene_items = {}  # {场景名: [项目源名, ...]}
    items = set()     # 并集兜底
    for s in scenes:
        try:
            names = [it["sourceName"] for it in o.get_scene_item_list(s).scene_items]
            scene_items[s] = names
            items.update(names)
        except Exception:
            pass

    return {
        "scenes": scenes,
        "transitions": transitions,
        "text": text_sources,
        "media": media_sources,
        "audio": audio_sources,
        "scene_items": scene_items,
        "items": sorted(items),
        "current_scene": o.get_current_program_scene().scene_name,
    }


# ---------------------------------------------------------------- 内置主时钟（第 3 步）

def tc_to_frames(tc, fps):
    """'h:mm:ss:ff' -> 总帧数"""
    parts = [int(x) for x in str(tc).strip().split(":")]
    while len(parts) < 4:
        parts.insert(0, 0)
    h, m, s, f = parts
    return ((h * 60 + m) * 60 + s) * fps + f


def frames_to_tc(frames, fps):
    """总帧数 -> 'hh:mm:ss:ff'"""
    f = frames % fps
    total_s = frames // fps
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def quarter_data(frames, fps):
    """完整帧号 -> 8 条 quarter-frame 数据半字节（seq 0-7）。节奏同 mtc_sender.py。"""
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
        ((h >> 4) & 0x01) | (fps_code << 1),   # seq7 时高位 + fps 码
    ]


class Player:
    """内置主时钟播放器：起后台线程，一边发 MTC（带灯控台），一边按帧触发 cue 直驱 OBS。

    替代 P1：编辑器双击节目 -> POST /api/play -> 本线程接管整场播放。
    节奏复用 mtc_sender.py 的标准 MTC（每帧 4 条 quarter-frame，8 条跨 2 帧）。
    """

    def __init__(self):
        self.thread = None
        self.stop_flag = threading.Event()
        self.playing = False
        self.current_tc = "00:00:00:00"
        self.mtc_active = False   # 是否真的打开了 MIDI 发送口在发 MTC

    def play(self, fps, start_tc, cues, midi_port="", duration_frames=0):
        """启动播放线程。cues 的 tc 已含 start_tc 偏移（编辑器编译时加好）。
        duration_frames=节目总帧数（相对 start_tc），>0 时播到点自动停；0=不限长。"""
        self.stop()
        self.stop_flag.clear()
        self.thread = threading.Thread(
            target=self._run, args=(fps, start_tc, cues, midi_port, int(duration_frames or 0)), daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)
        self.playing = False

    def _run(self, fps, start_tc, cues, midi_port, duration_frames=0):
        self.playing = True
        fps = int(fps) or 30

        # 打开 MIDI 输出口发 MTC（找不到口则只触发 cue，不发 MTC，不崩）
        out = None
        try:
            ports = mido.get_output_names()
            name = None
            if midi_port:
                name = next((p for p in ports if midi_port in p), None)
            if name is None and ports:
                name = next((p for p in ports if "loopmidi" in p.lower()), ports[0])
            if name:
                out = mido.open_output(name)
                self.mtc_active = True
                print(f"[主时钟] MTC 输出口: {name}")
            else:
                self.mtc_active = False
                print("[主时钟] 未找到 MIDI 输出口，仅触发 cue 不发 MTC")
        except Exception as e:
            self.mtc_active = False
            print(f"[主时钟] 打开发送口失败: {e}")

        start_frames = tc_to_frames(start_tc, fps)
        end_frames = start_frames + duration_frames if duration_frames > 0 else None
        cue_list = []
        for c in cues:
            cue_list.append(dict(c, frames=tc_to_frames(c.get("tc", start_tc), fps), fired=False))

        interval = 1.0 / (4 * fps)
        tick = 0
        next_t = time.time()
        last_report = 0.0
        try:
            while not self.stop_flag.is_set():
                # 帧推进节奏同 mtc_sender.py
                frames = start_frames + (tick // 8) * 2 + (1 if tick % 8 >= 4 else 0)
                seq = tick % 8
                if out:
                    data = quarter_data(frames, fps)[seq]
                    out.send(mido.Message("quarter_frame", frame_type=seq, frame_value=data))

                # 触发到点且未触发的 cue
                for c in cue_list:
                    if not c["fired"] and frames >= c["frames"]:
                        c["fired"] = True
                        try:
                            label = fire_action(get_obs(), c)
                            print(f"[主时钟触发] {c.get('tc')} -> {label}")
                        except Exception as e:
                            print(f"[主时钟触发错误] {c.get('tc')}: {e}")

                self.current_tc = frames_to_tc(frames, fps)
                tick += 1

                # 节目时长到点 → 自动停（不再无限跑）
                if end_frames and frames >= end_frames:
                    print(f"[主时钟] 播放结束 @ {self.current_tc}（时长 {duration_frames} 帧）")
                    break

                now = time.time()
                if now - last_report >= 2.0:
                    last_report = now
                    print(f"[主时钟] {self.current_tc}")

                # 绝对时间调度，避免 sleep 粒度误差累积
                next_t += interval
                sleep_for = next_t - time.time()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_t = time.time()
        finally:
            if out:
                try:
                    out.close()
                except Exception:
                    pass
            self.playing = False
            self.mtc_active = False
            self.current_tc = frames_to_tc(frames, fps) if 'frames' in dir() else "00:00:00:00"


player = Player()


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        try:
            if path == "/api/play":
                fps = body.get("fps", 30)
                start_tc = body.get("start_tc", "00:00:00:00")
                cues = body.get("cues", [])
                midi_port = body.get("midi_port", "")
                duration_frames = body.get("duration_frames", 0)
                if not cues:
                    self._json({"ok": False, "error": "没有 cue，无法播放"}, 400)
                    return
                player.play(fps, start_tc, cues, midi_port, duration_frames)
                self._json({"ok": True, "playing": True, "cues": len(cues)})
            elif path == "/api/stop":
                player.stop()
                self._json({"ok": True, "playing": False})
            else:
                self._json({"error": "not found", "path": path}, 404)
        except Exception as e:
            self._json({"ok": False, "connected": False, "error": str(e)}, 500)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/status":
                self._json({"ok": True, "connected": True,
                            "current_scene": get_obs().get_current_program_scene().scene_name})
            elif path == "/api/resources":
                self._json(build_resources())
            elif path == "/api/scenes":
                self._json({"scenes": [s["sceneName"] for s in get_obs().get_scene_list().scenes]})
            elif path == "/api/transitions":
                self._json({"transitions": [t["transitionName"] for t in get_obs().get_scene_transition_list().transitions]})
            elif path == "/api/playback":
                self._json({"ok": True, "playing": player.playing,
                            "current_tc": player.current_tc,
                            "mtc_active": player.mtc_active})
            else:
                self._json({"error": "not found", "path": path}, 404)
        except Exception as e:
            self._json({"ok": False, "connected": False, "error": str(e)}, 500)

    def log_message(self, *a):
        pass  # 静默日志


def selftest():
    try:
        r = build_resources()
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print("[OBS 连接失败]", e)
        sys.exit(1)


def main():
    global obs
    print(f"[桥接服务] http://{HOST}:{PORT} 启动（编辑器连这个地址）")
    try:
        obs = ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD)
        print(f"[OBS] 已连接 ws://{OBS_HOST}:{OBS_PORT}")
    except Exception as e:
        print(f"[OBS] 连接失败：{e}")
        print("[OBS] 服务照常启动，OBS 开启后调用接口会自动重连")
    HTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
