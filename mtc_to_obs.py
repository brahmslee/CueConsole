# -*- coding: utf-8 -*-
"""
MTC -> OBS 节目控台（触发器）
=============================
监听灯控台/P1 发出的 MIDI Timecode (MTC)，时间码到达 cue 点时，
通过 obs-websocket 直驱 OBS 执行动作。

cues.json 每条 cue 结构：
  { "tc": "00:00:05:00", "action": "scene",      "params": { "name": "开场" } }
  { "tc": "00:00:10:00", "action": "media",      "params": { "source": "BGM", "cmd": "play" } }
  { "tc": "00:00:25:00", "action": "transition", "params": { "transition": "移动", "scene": "节目一-特写", "restore": "淡入淡出" } }

动作类型：
  scene      -> 切场景              params.name
  media      -> 播/停媒体源         params.source + params.cmd(play/stop/pause/restart/next/previous)
  transition -> 用指定转场切场景    params.transition + params.scene + params.restore(可选，切完恢复)
  visibility -> 显隐场景元素        params.scene + params.source + params.show(true/false)
  text       -> 改文本源文字        params.source + params.text
  audio      -> 静音/音量           params.source + params.mute(true/false) 或 params.volume(0.0~1.0)
  exec       -> 跑外部程序          params.command + params.args(可选列表) + params.cwd(可选)
  random     -> 随机执行候选动作     params.actions(候选动作列表[{action,params}])；兼容旧 params.scenes(场景名列表→切场景)
  queue      -> 顺序轮转执行候选动作 params.actions(候选动作列表)，按进程内计数器每触发一次轮转一位(循环)；key=tc 或可选 params.id
  transform  -> 场景项目变换        params.scene + params.source + 变换字段(x/y/scale_x/scale_y/rotation/crop_*)
                                     + params.duration_ms(可选，带动画) + params.easing(可选缓动名)

链路：P1/灯控台 --MTC--> 本脚本 --websocket:4455--> OBS

使用：
  1. OBS 里开启 WebSocket 服务器（工具 > WebSocket 服务器设置，端口 4455）
  2. pip install mido python-rtmidi obsws-python
  3. 改 cues.json：填 cue 点、MIDI 口名、OBS 密码
  4. python mtc_to_obs.py

时间码格式：时:分:秒:帧
MTC 解析：自动拼装 quarter-frame（2条/帧），自动识别 24/25/30fps
回卷（时间码倒退）时自动重置所有 cue，可直接重播
"""

import json
import os
import random
import subprocess
import sys
import time

import mido
from obsws_python import ReqClient

# 绕过系统代理：websocket-client 会读 HTTP_PROXY 环境变量，导致连 localhost 的 OBS
# 也走代理报 502（WorkBuddy 会话会注入 127.0.0.1:10312 的本地代理）。这里强制本机地址不走代理。
os.environ["NO_PROXY"] = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "localhost", "127.0.0.1"]))
os.environ["no_proxy"] = os.environ["NO_PROXY"]

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cues.json")

# ---------------------------------------------------------------- timecode

FPS_BITS = {0: 24, 1: 25, 2: 30, 3: 30}  # MTC 小时字节高两位 -> fps (2=30drop, 按30处理)


def tc_to_frames(h, m, s, f, fps):
    return ((h * 60 + m) * 60 + s) * fps + f


def parse_cue_tc(tc_str, fps):
    parts = [int(x) for x in tc_str.strip().split(":")]
    while len(parts) < 4:
        parts.insert(0, 0)
    h, m, s, f = parts
    return tc_to_frames(h, m, s, f, fps)


# ---------------------------------------------------------------- MTC 解析器

class MTCParser:
    """拼装 MTC quarter-frame 成完整时间码。

    MTC 一帧拆 8 条四分帧消息（0-7 号），seq 7 之后回到 0 时，说明
    上一帧的 8 片凑齐了，此时提交上一个完整帧。
    """

    def __init__(self):
        self.slots = [None] * 8
        self.fps = None
        self.frames = -1       # 当前完整帧（总帧数）
        self.prev_frames = -1  # 上一完整帧，用于回卷/换歌检测
        self.ready = False
        self.rewound = False   # 去抖：倒带过程连续递减，只在首次倒退时报一次回卷

    def feed(self, msg):
        if msg.type != "quarter_frame":
            return None
        seq = msg.frame_type    # 0-7（mido 1.3.x 序号在 frame_type）
        data = msg.frame_value  # 该片的半字节数据（mido 1.3.x 数据在 frame_value）
        self.slots[seq] = data

        if seq == 7:
            # 提交上一帧
            if all(x is not None for x in self.slots):
                fr_lo = self.slots[0] & 0x0F
                fr_hi = self.slots[1] & 0x0F
                sc_lo = self.slots[2] & 0x0F
                sc_hi = self.slots[3] & 0x0F
                mi_lo = self.slots[4] & 0x0F
                mi_hi = self.slots[5] & 0x0F
                hr_lo = self.slots[6] & 0x0F
                hr_hi = self.slots[7] & 0x0F
                fps_code = (self.slots[7] >> 1) & 0x03
                h = (hr_hi & 0x1F) | ((self.slots[7] >> 4) & 0x01) << 5
                # 简化：小时 = hr_lo + hr_hi位组合，够用到 23 时
                h = hr_lo | ((hr_hi & 0x01) << 4)
                m = (mi_hi << 4) | mi_lo
                s = (sc_hi << 4) | sc_lo
                f = (fr_hi << 4) | fr_lo
                if self.fps is None and fps_code in FPS_BITS:
                    self.fps = FPS_BITS[fps_code]
                    print(f"[MTC] 检测到帧率: {self.fps} fps")
                if self.fps:
                    self.prev_frames = self.frames
                    self.frames = tc_to_frames(h, m, s, f, self.fps)
                    self.ready = True
            self.slots = [None] * 8
        return None

    def rewind(self, new_frames):
        """时间码倒退（倒带/重放）-> 重置。

        倒带时时间码连续递减，若每次都判回卷会刷屏。用 rewound 去抖：
        只在第一次检测到倒退时报回卷，之后递减过程中不再重复，
        直到时间码重新开始递增（或停住）才复位。
        """
        if self.prev_frames >= 0 and new_frames < self.prev_frames:
            if not self.rewound:
                self.rewound = True
                return True
            return False
        self.rewound = False
        return False


# ---------------------------------------------------------------- 动作分发

MEDIA_ACTIONS = {
    "play": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY",
    "pause": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE",
    "stop": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP",
    "restart": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
    "next": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_NEXT",
    "previous": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PREVIOUS",
}

# ---------------------------------------------------------------- 新动作辅助

TRANSFORM_ALIASES = {
    "x": "positionX", "position_x": "positionX", "positionX": "positionX",
    "y": "positionY", "position_y": "positionY", "positionY": "positionY",
    "scale_x": "scaleX", "scalex": "scaleX", "scaleX": "scaleX",
    "scale_y": "scaleY", "scaley": "scaleY", "scaleY": "scaleY",
    "rotation": "rotation",
    "crop_left": "cropLeft", "crop_right": "cropRight",
    "crop_top": "cropTop", "crop_bottom": "cropBottom",
    "cropLeft": "cropLeft", "cropRight": "cropRight",
    "cropTop": "cropTop", "cropBottom": "cropBottom",
    "alignment": "alignment",
}

# 可写且无校验陷阱的 transform 字段。排除 bounds 相关（boundsWidth 在 NONE 时=0 会被 OBS 拒，要求>=1）
# 和只读字段（width/height/sourceWidth/sourceHeight 由 OBS 计算，不能 set）。
SETTABLE_TRANSFORM_KEYS = (
    "positionX", "positionY", "rotation", "scaleX", "scaleY",
    "cropLeft", "cropRight", "cropTop", "cropBottom", "alignment",
)

_EASINGS = {
    "linear": lambda p: p,
    "easeInQuad": lambda p: p * p,
    "easeOutQuad": lambda p: p * (2 - p),
    "easeInOutQuad": lambda p: 2 * p * p if p < 0.5 else 1 - (-2 * p + 2) ** 2 / 2,
    "easeInCubic": lambda p: p ** 3,
    "easeOutCubic": lambda p: 1 - (1 - p) ** 3,
    "easeInOutCubic": lambda p: 4 * p ** 3 if p < 0.5 else 1 - (-2 * p + 2) ** 3 / 2,
}


def _get_item_id(obs, scene, source):
    return obs.get_scene_item_id(scene, source).scene_item_id


def _extract_transform(params):
    """从 params 里挑出 transform 字段（支持 x/y/scale_x 等别名），返回 obs-websocket 字段名。"""
    t = {}
    for k, v in params.items():
        field = TRANSFORM_ALIASES.get(k)
        if field:
            t[field] = v
    # 等比缩放：scale 同时作用于 X/Y
    if "scale" in params:
        t["scaleX"] = params["scale"]
        t["scaleY"] = params["scale"]
    return t


def _lerp_transform(a, b, p):
    """对两个 transform 字典做数值插值，只处理可写字段白名单。

    排除 bounds 相关（boundsWidth=0 会被 OBS 拒）和只读字段（width/height 等），
    避免 set 时触发 402 校验错误。
    """
    if not a:
        return {k: v for k, v in b.items() if k in SETTABLE_TRANSFORM_KEYS}
    out = {}
    for k in SETTABLE_TRANSFORM_KEYS:
        av = a.get(k)
        bv = b.get(k)
        if av is None and bv is None:
            continue
        if bv is None:
            out[k] = av
            continue
        if av is None:
            out[k] = bv
            continue
        if isinstance(av, (int, float)) and not isinstance(av, bool) and \
           isinstance(bv, (int, float)) and not isinstance(bv, bool):
            out[k] = av + (bv - av) * p
        else:
            out[k] = bv
    return out


def _animate_transform(obs, scene, item_id, start, target, duration_ms, easing):
    """同步插值动画：在 duration_ms 内按 ~60fps 循环发中间 transform 值。

    同步阻塞主循环，动画期间 MTC 消息积压在 mido 缓冲区，恢复后快速追帧、
    按时间码补触发期间错过的 cue（绝对时间码语义下正确）。时长通常几百 ms，直播够用。
    """
    steps = max(1, int(duration_ms / 16))
    interval = duration_ms / 1000.0 / steps
    ease = _EASINGS.get(easing, _EASINGS["linear"])
    for i in range(1, steps + 1):
        mid = _lerp_transform(start, target, ease(i / steps))
        try:
            obs.set_scene_item_transform(scene, item_id, mid)
        except Exception:
            pass
        time.sleep(interval)
    try:
        obs.set_scene_item_transform(scene, item_id, target)  # 精确落点
    except Exception:
        pass


# queue 动作的进程内轮转计数器：key -> 当前应执行的候选动作索引。
# key 用时间码（同一 queue 动作在时间轴位置固定，每次触发 tc 一致），
# 或可选 params.id（同 tc 多个 queue 时手动区分）。
# 进程内有效：脚本重启归零。节目回卷重播时同一 tc 再次触发，继续往下轮转，
# 正好实现「每一遍运行顺序取下一个」的语义。
_QUEUE_CURSOR = {}


def fire_action(obs, cue):
    """按 cue 的 action 字段分发动作，直驱 OBS。

    action="scene"        params.name                      -> 切场景
    action="media"        params.source + params.cmd        -> 播/停媒体源
    action="transition"   params.transition/scene/restore   -> 用指定转场切场景
    action="random"       params.actions                   -> 随机选一个候选动作递归执行
    action="queue"        params.actions                   -> 顺序轮转选一个候选动作递归执行
    """
    action = cue.get("action")
    params = cue.get("params") or {}

    if action == "scene":
        obs.set_current_program_scene(params["name"])
        return f"切场景「{params['name']}」"

    if action == "media":
        source = params["source"]
        cmd = params.get("cmd", "play")
        # OBS 媒体源在 restart_on_activate=False 时，play 对 STOPPED/ENDED 的源无效
        # （状态会显示 PLAYING 但 media cursor 冻结在初始值不前进），必须用 restart
        # 才能真正从头播。这里自动兜底：源没在真正播时，play -> restart。
        if cmd == "play":
            try:
                st = obs.get_media_input_status(source)
                if st.media_state in ("OBS_MEDIA_STATE_STOPPED", "OBS_MEDIA_STATE_ENDED"):
                    cmd = "restart"
            except Exception:
                pass  # 查不到状态就按原 play 发，不阻塞
        enum = MEDIA_ACTIONS.get(cmd)
        if enum is None:
            raise ValueError(f"media 动作未知 cmd: {cmd}")
        obs.trigger_media_input_action(source, enum)
        return f"媒体「{source}」{cmd}"

    if action == "transition":
        trans = params["transition"]
        scene = params["scene"]
        obs.set_current_scene_transition(trans)
        obs.set_current_program_scene(scene)
        restore = params.get("restore")
        if restore:
            obs.set_current_scene_transition(restore)
        label = f"转场「{trans}」切场景「{scene}」"
        if restore:
            label += f"（切完恢复「{restore}」）"
        return label

    if action == "visibility":
        item_id = _get_item_id(obs, params["scene"], params["source"])
        show = params.get("show", True)
        obs.set_scene_item_enabled(params["scene"], item_id, show)
        return f"显隐「{params['source']}」-> {'显示' if show else '隐藏'}"

    if action == "text":
        obs.set_input_settings(params["source"], {"text": params["text"]}, overlay=True)
        return f"改文字「{params['source']}」-> {params['text']!r}"

    if action == "audio":
        source = params["source"]
        if "mute" in params:
            obs.set_input_mute(source, params["mute"])
            return f"{'静音' if params['mute'] else '取消静音'}「{source}」"
        if "volume" in params:
            obs.set_input_volume(source, vol_mul=params["volume"])
            return f"音量「{source}」-> {params['volume']}"
        raise ValueError("audio 动作需要 mute 或 volume 参数")

    if action == "exec":
        cmd = params["command"]
        args = params.get("args", [])
        cwd = params.get("cwd")
        subprocess.Popen([cmd] + list(args), cwd=cwd)
        return f"跑外部程序「{cmd}」"

    if action == "random":
        # 候选：优先 actions（动作列表），兼容旧的 scenes（场景名列表 -> 切场景动作）
        candidates = params.get("actions")
        if candidates is None:
            scenes = params.get("scenes")
            candidates = [{"action": "scene", "params": {"name": s}} for s in scenes] if scenes else None
        if not candidates:
            raise ValueError("random 动作需要 actions 候选动作列表（或旧版 scenes 场景列表）")
        pick = random.choice(candidates)
        return "随机 -> " + fire_action(obs, pick)

    if action == "queue":
        candidates = params.get("actions")
        if not candidates:
            raise ValueError("queue 动作需要 actions 候选动作列表")
        key = params.get("id") or cue.get("tc") or "default"
        idx = _QUEUE_CURSOR.get(key, 0) % len(candidates)
        _QUEUE_CURSOR[key] = idx + 1
        pick = candidates[idx]
        return f"队列[{idx + 1}/{len(candidates)}] -> " + fire_action(obs, pick)

    if action == "transform":
        scene = params["scene"]
        source = params["source"]
        item_id = _get_item_id(obs, scene, source)
        target = _extract_transform(params)
        if not target:
            raise ValueError("transform 动作缺少变换字段(x/y/scale_x/scale_y/rotation 等)")
        duration_ms = params.get("duration_ms") or params.get("duration")
        if not duration_ms:
            obs.set_scene_item_transform(scene, item_id, target)
            return f"变换「{source}」（瞬时）"
        easing = params.get("easing", "linear")
        try:
            start = obs.get_scene_item_transform(scene, item_id).scene_item_transform
        except Exception:
            start = None
        _animate_transform(obs, scene, item_id, start, target, duration_ms, easing)
        return f"变换「{source}」动画 {duration_ms}ms {easing}"

    # 兼容旧 cue 结构（无 action，直接给 scene）
    if cue.get("scene"):
        obs.set_current_program_scene(cue["scene"])
        return f"切场景「{cue['scene']}」"

    raise ValueError(f"cue 缺少可执行动作: {cue.get('tc')}")


# ---------------------------------------------------------------- 主程序

def main():
    # 日志 tee 到 bridge.log，后台跑/用户自己跑都能留痕，方便事后排查
    _log_fp = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.log"), "a", encoding="utf-8")

    class _Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, s):
            for st in self.streams:
                st.write(s)

        def flush(self):
            for st in self.streams:
                try:
                    st.flush()
                except Exception:
                    pass

    sys.stdout = _Tee(sys.stdout, _log_fp)
    print(f"===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====")

    with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
        cfg = json.load(fp)

    # 列出 MIDI 输入口
    ports = mido.get_input_names()
    print("[MIDI] 可用输入口:")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p}")

    port_name = cfg.get("midi_port", "")
    if not port_name or port_name not in ports:
        # 自动猜测：名字里带常见灯控台/ MIDI 接口关键字，否则取第一个非 loopMIDI 口
        guess = next((p for p in ports if "loopmidi" not in p.lower()), ports[0] if ports else None)
        if guess is None:
            print("[错误] 找不到任何 MIDI 输入口，检查灯控台连接")
            sys.exit(1)
        print(f"[MIDI] 未指定/未匹配到配置里的口，使用: {guess}")
        port_name = guess

    fps = cfg.get("fps")  # 可手动指定 24/25/30，留空自动
    cues = []
    for c in cfg["cues"]:
        if "action" not in c and "macro" not in c and "scene" not in c:
            print(f"[配置警告] cue {c.get('tc')} 缺少 action/macro/scene 字段，将跳过")
        cues.append({
            "tc": c["tc"],
            "action": c.get("action"),
            "params": c.get("params"),
            "macro": c.get("macro"),
            "scene": c.get("scene"),
            "variables": c.get("variables"),
            "frames": None,
            "fired": False,
        })
    print(f"[配置] 共 {len(cues)} 个 cue 点")

    # 连接 OBS
    obs = ReqClient(
        host=cfg.get("obs_host", "localhost"),
        port=cfg.get("obs_port", 4455),
        password=cfg.get("obs_password", ""),
    )
    print(f"[OBS] 已连接 ws://{cfg.get('obs_host', 'localhost')}:{cfg.get('obs_port', 4455)}")

    parser = MTCParser()
    if fps:
        parser.fps = fps

    last_print = 0.0
    with mido.open_input(port_name) as inlet:
        # mido 1.3.x 的 rtmidi 后端默认已 ignore_types(False, False, True)，
        # 不过滤 timing 消息，MTC 四分帧(0xF1)能正常进来，无需手动干预
        print(f"[MIDI] 正在监听: {port_name}，等待 MTC 信号...")
        debug_count = 0
        for msg in inlet:
            if debug_count < 5:
                debug_count += 1
                print(f"[DEBUG] 收到消息: {msg}")
            parser.feed(msg)

            # 帧率确定后，把 cue 的时间字符串换算成帧
            if parser.fps and cues and cues[0]["frames"] is None:
                for c in cues:
                    c["frames"] = parse_cue_tc(c["tc"], parser.fps)
                print(f"[配置] cue 点已按 {parser.fps} fps 换算")

            if not parser.ready:
                continue

            now_frames = parser.frames

            # 回卷检测：重置所有 cue
            if cues and cues[0]["frames"] is not None and parser.rewind(now_frames):
                for c in cues:
                    c["fired"] = False
                print("[MTC] 检测到回卷，cue 已重置")

            # 触发：时间码越过 cue 点即触发（只触发一次）
            for c in cues:
                if c["frames"] is not None and not c["fired"] and now_frames >= c["frames"]:
                    c["fired"] = True
                    try:
                        label = fire_action(obs, c)
                        h, rem = divmod(now_frames, parser.fps * 3600)
                        m_, rem = divmod(rem, parser.fps * 60)
                        s_, f_ = divmod(rem, parser.fps)
                        print(f"[触发] {h:02d}:{m_:02d}:{s_:02d}:{f_:02d} -> {label}")
                    except Exception as e:
                        print(f"[OBS错误] {e}，尝试重连...")
                        try:
                            obs = ReqClient(
                                host=cfg.get("obs_host", "localhost"),
                                port=cfg.get("obs_port", 4455),
                                password=cfg.get("obs_password", ""),
                            )
                        except Exception:
                            pass

            # 每 2 秒打一次当前时间码（心跳，确认链路活着）
            if time.time() - last_print > 2.0:
                last_print = time.time()
                if parser.fps:
                    h, rem = divmod(now_frames, parser.fps * 3600)
                    m_, rem = divmod(rem, parser.fps * 60)
                    s_, f_ = divmod(rem, parser.fps)
                    print(f"[MTC] 当前 {h:02d}:{m_:02d}:{s_:02d}:{f_:02d}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n退出")
