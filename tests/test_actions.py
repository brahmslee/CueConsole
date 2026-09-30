# -*- coding: utf-8 -*-
"""临时单元测试：逐个验证 fire_action 的 6 个新动作。跑完可删。"""
import os
import sys
import time

os.environ["NO_PROXY"] = "localhost,127.0.0.1"
sys.path.insert(0, r"C:/mtc-obs-bridge")
from mtc_to_obs import fire_action
from obsws_python import ReqClient

obs = ReqClient(host="localhost", port=4455, password="")


def run(action, params):
    cue = {"action": action, "params": params}
    try:
        label = fire_action(obs, cue)
        print(f"[OK]   {action:10s} {label}")
    except Exception as e:
        print(f"[FAIL] {action:10s} {type(e).__name__}: {e}")


print("=== 前置：切到「节目一-全景」看录屏 ===")
obs.set_current_program_scene("节目一-全景")
time.sleep(0.5)

print("\n=== 1. visibility 显隐（录屏画面应消失再出现）===")
run("visibility", {"scene": "节目一-全景", "source": "录屏", "show": False})
time.sleep(0.5)
run("visibility", {"scene": "节目一-全景", "source": "录屏", "show": True})
time.sleep(0.3)

print("\n=== 2. transform 瞬时缩放（录屏缩到 0.6）===")
run("transform", {"scene": "节目一-全景", "source": "录屏", "scale": 0.6})
time.sleep(0.5)

print("\n=== 3. transform 动画缩放回 1.0（500ms easeOutCubic，应看到平滑放大）===")
run("transform", {"scene": "节目一-全景", "source": "录屏", "scale": 1.0, "duration_ms": 500, "easing": "easeOutCubic"})
time.sleep(0.8)

print("\n=== 4. transform 动画位移（x 移到 200 再回来）===")
run("transform", {"scene": "节目一-全景", "source": "录屏", "x": 200, "duration_ms": 500, "easing": "easeInOutCubic"})
time.sleep(0.8)
run("transform", {"scene": "节目一-全景", "source": "录屏", "x": 0, "duration_ms": 500, "easing": "easeInOutCubic"})
time.sleep(0.8)

print("\n=== 5. text 改文字（切到开场看文字）===")
obs.set_current_program_scene("开场")
time.sleep(0.3)
run("text", {"source": "文本 (GDI+)", "text": "测试文字-已修改"})

print("\n=== 6. audio 静音/音量（BGM）===")
run("audio", {"source": "BGM", "mute": True})
run("audio", {"source": "BGM", "mute": False})
run("audio", {"source": "BGM", "volume": 0.5})

print("\n=== 7. exec 跑外部程序（生成 _exec_test.txt）===")
py = r"C:/Users/1/.workbuddy/binaries/python/envs/py312/Scripts/python.exe"
run("exec", {"command": py, "args": ["-c", "open(r'C:/mtc-obs-bridge/_exec_test.txt','w',encoding='utf-8').write('exec ok')"]})
time.sleep(0.5)
print("  exec 产物存在:", os.path.exists(r"C:/mtc-obs-bridge/_exec_test.txt"))

print("\n=== 8. random 随机切场景（连跑 3 次看是否随机）===")
for _ in range(3):
    run("random", {"scenes": ["开场", "场景1", "节目一-全景", "节目一-特写"]})
    time.sleep(0.3)

print("\n=== 全部完成 ===")
