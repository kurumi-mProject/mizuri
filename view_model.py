#!/usr/bin/env python3
"""Запускает Live2D вьювер модели Мизури в браузере на localhost:8765"""
import http.server, threading, webbrowser, os, json

MOTIONS = [
    'Hiyori_m01','Hiyori_m02','Hiyori_m03','Hiyori_m04','Hiyori_m05',
    'Hiyori_m06','Hiyori_m07','Hiyori_m08','Hiyori_m09','Hiyori_m10',
    'haru_g_idle',
    'mtn_00','mtn_01','mtn_02','mtn_03','mtn_04','mtn_05','mtn_06','mtn_07'
]

HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Мизури Live2D</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #1a1a2e; overflow: hidden; }
canvas { display: block; }
#controls {
  position: fixed; bottom: 10px; left: 0; right: 0;
  display: flex; flex-wrap: wrap; gap: 5px; justify-content: center; padding: 0 10px;
}
button {
  background: rgba(124,58,237,0.3); color: #fff; border: 1px solid rgba(124,58,237,0.6);
  padding: 5px 10px; border-radius: 6px; cursor: pointer; font-size: 12px;
}
button:hover { background: rgba(124,58,237,0.7); }
button.idle { background: rgba(22,163,74,0.4); border-color: rgba(22,163,74,0.8); }
#status { position: fixed; top: 10px; left: 10px; color: #888; font-size: 12px; font-family: monospace; }
#scale-ctrl { position: fixed; top: 10px; right: 10px; display: flex; gap: 6px; align-items: center; }
#scale-ctrl button { font-size: 16px; padding: 4px 10px; }
#scale-val { color: #aaa; font-size: 12px; font-family: monospace; min-width: 40px; text-align: center; }
</style>
</head>
<body>
<div id="status">загрузка...</div>
<div id="scale-ctrl">
  <button id="btn-minus">−</button>
  <span id="scale-val">0.10</span>
  <button id="btn-plus">+</button>
</div>
<canvas id="canvas"></canvas>
<div id="controls"></div>

<script src="https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/pixi.js@7.3.2/dist/pixi.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/dist/cubism4.min.js"></script>
<script>
const MOTIONS = """ + json.dumps(MOTIONS) + r""";
let currentScale = 0.10;

(async () => {
  const status = document.getElementById('status');
  const scaleVal = document.getElementById('scale-val');

  const app = new PIXI.Application({
    view: document.getElementById('canvas'),
    width: window.innerWidth,
    height: window.innerHeight,
    backgroundAlpha: 0,
    antialias: true,
    resizeTo: window,
  });

  status.textContent = 'загрузка модели...';
  let model;
  try {
    model = await PIXI.live2d.Live2DModel.from('model/6xb.model3.json');
  } catch(e) {
    status.textContent = 'ошибка: ' + e.message;
    return;
  }
  window._model = model;
  app.stage.addChild(model);

  model.anchor.set(0.5, 0.0);

  function reposition() {
    model.x = app.screen.width / 2;
    model.y = 10;
    model.scale.set(currentScale);
    scaleVal.textContent = currentScale.toFixed(2);
  }
  reposition();
  window.addEventListener('resize', reposition);

  document.getElementById('btn-plus').onclick = () => {
    currentScale = Math.min(currentScale + 0.02, 1.0);
    reposition();
  };
  document.getElementById('btn-minus').onclick = () => {
    currentScale = Math.max(currentScale - 0.02, 0.02);
    reposition();
  };

  status.textContent = 'модель загружена ✓  (± кнопки справа вверху)';

  // Кнопки анимаций
  const ctrl = document.getElementById('controls');
  MOTIONS.forEach(name => {
    const btn = document.createElement('button');
    btn.textContent = name;
    if (name === 'haru_g_idle') btn.classList.add('idle');
    btn.onclick = () => { model.motion(name); status.textContent = '▶ ' + name; };
    ctrl.appendChild(btn);
  });

  model.motion('haru_g_idle');



  // Авто-цикл анимаций из server.py MOTION_CYCLE
  const MOTION_CYCLE = [
    ["mtn_06", 8], ["mtn_07", 10], ["mtn_00", 12], ["Hiyori_m01", 7],
    ["mtn_06", 8], ["Hiyori_m09", 6], ["mtn_07", 10], ["mtn_02", 8],
    ["Hiyori_m04", 7], ["mtn_05", 10],
  ];
  let cycleIdx = 0;
  function runCycle() {
    if (document.hidden) { setTimeout(runCycle, 1000); return; }
    const [name, delay] = MOTION_CYCLE[cycleIdx % MOTION_CYCLE.length];
    model.motion(name);
    status.textContent = '▶ ' + name;
    cycleIdx++;
    setTimeout(runCycle, delay * 1000);
  }
  setTimeout(runCycle, 2000);
})();
</script>
</body>
</html>
"""

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(HTML.encode())
        else:
            super().do_GET()
    def log_message(self, *a): pass

os.chdir(os.path.dirname(os.path.abspath(__file__)))
PORT = 8765
server = http.server.HTTPServer(('', PORT), Handler)
print(f"Открывай: http://localhost:{PORT}")
threading.Timer(0.8, lambda: webbrowser.open(f'http://localhost:{PORT}')).start()
server.serve_forever()
