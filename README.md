# Q台 CueConsole

> A timeline-based cue console for live shows. Edit cues on a visual timeline, auto-fire OBS scenes / media / text, and sync lighting desks via MTC timecode — all from one timeline.

节目控台：可视化时间轴编排 cue，到点自动直驱 OBS（切镜 / 切片 / 文字 / 显隐）+ 发 MTC 时间码带灯控台同步。

## What it does

Replace the split toolchain (ASS automation + P1 player + lighting-console chasing) with **one timeline**:

- **Visual timeline editor** — drag cues, set timecodes, per-program start offset.
- **Built-in master clock** — sends MTC timecode and fires cues frame-accurately (30 fps non-drop).
- **Drives OBS directly** — 9 action types: `scene` / `media` / `transition` / `visibility` / `text` / `audio` / `exec` / `random` / `transform`.
- **Lights follow via MTC** — a lighting desk or DAW chases the same timecode.

<!-- TODO: screenshot of editor + bridge console -->

## Architecture (3 layers)

```
timeline.json   (editing layer: relative time + offset, device-agnostic)
   → compile.py  (frame-accurate t+offset + param normalization)
cues.json        (execution layer: {tc, action, params})
   → mtc_to_obs.py / bridge_server.py  (fire actions directly at OBS)
```

The timeline is a pure content asset decoupled from any device — sell / reuse / re-host the same timeline anywhere.

## Requirements

- Windows
- Python 3.12
- OBS Studio with obs-websocket enabled (default port 4455)
- (optional) loopMIDI + lighting desk / DAW for MTC chase

## Quick start

1. Install Python 3.12 (tick **Add python.exe to PATH**).
2. Double-click `setup_env.bat` — creates a project-local `venv` and installs dependencies.
3. Double-click `start_bridge.bat` — starts the local bridge (editor ↔ OBS).
4. Open `editor/index.html` in a browser.
5. Double-click a program card → it plays MTC and fires cues at OBS at the right timecodes.

## Project layout

```
bridge_server.py   local bridge + built-in master clock
mtc_to_obs.py      engine (MTC parse + 9 actions)
compile.py         compiler (timeline.json → cues.json)
mtc_sender.py      standalone MTC sender
editor/            web editor UI
examples/          sample timeline.json / cues.json
tests/             tests
```

## Status

**v0.1** — core loop verified on real hardware (OBS direct cue firing + Reaper MTC chase). On the roadmap: `light` action (lighting desk cue), media playback timeline (waveform + params), manual cue control (pause / skip / emergency stop with configurable hotkeys).

## License

MIT
