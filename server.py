import os
import threading
import queue
import ctypes
import subprocess
import time
import socket
import re

os.environ['PYOPENGL_PLATFORM'] = 'egl'
os.environ['__EGL_VENDOR_LIBRARY_FILENAMES'] = '/usr/share/glvnd/egl_vendor.d/10_nvidia.json'

from OpenGL.GL import *
import live2d.v3 as live2d
import numpy as np
import cv2

WIDTH, HEIGHT = 1920, 1080
FPS = 60
MODEL_PATH = os.environ.get("MODEL_PATH", "model/6xb.model3.json")
TWITCH_KEY = os.environ.get("TWITCH_STREAM_KEY", "live_1481935876_Mnc9yEIad19L1B72RBUadNmQAZ3ktJ")
TWITCH_URL = f"rtmp://live.twitch.tv/app/{TWITCH_KEY}"
TWITCH_CHANNEL = "neuro_aluna"

frame_queue = queue.Queue(maxsize=2)
twitch_frame_queue = queue.Queue(maxsize=3)
audio_queue = queue.Queue(maxsize=32)
mouth_open = 0.0
mouth_lock = threading.Lock()
live2d_model = None
model_lock = threading.Lock()

# --- Twitch чат через IRC ---
chat_messages = []
chat_lock = threading.Lock()
MAX_CHAT_LINES = 10

def twitch_chat_reader():
    while True:
        try:
            s = socket.socket()
            s.connect(("irc.chat.twitch.tv", 6667))
            s.send(b"NICK justinfan12345\r\n")
            s.send(b"USER justinfan12345 0 * :justinfan\r\n")
            s.send(f"JOIN #{TWITCH_CHANNEL}\r\n".encode())
            buf = ""
            while True:
                data = s.recv(2048).decode("utf-8", errors="ignore")
                buf += data
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
                    if line.startswith("PING"):
                        s.send(b"PONG :tmi.twitch.tv\r\n")
                    m = re.match(r":(\w+)!\w+@\S+ PRIVMSG #\S+ :(.+)", line)
                    if m:
                        user, text = m.group(1), m.group(2)
                        with chat_lock:
                            chat_messages.append((user, text))
                            if len(chat_messages) > MAX_CHAT_LINES:
                                chat_messages.pop(0)
                            try:
                                import textwrap
                                wrapped = []
                                for u, t in chat_messages[-MAX_CHAT_LINES:]:
                                    line = f"{u}: {t}"
                                    wrapped.extend(textwrap.wrap(line, width=35) or [line])
                                with open('/tmp/chat_overlay.txt', 'w') as f:
                                    f.write('\n'.join(wrapped[-MAX_CHAT_LINES*2:]))
                            except Exception:
                                pass
        except Exception as e:
            print(f"Chat IRC error: {e}, reconnecting in 5s...")
            time.sleep(5)

# --- Субтитры и индикатор "думает" ---
subtitle_text = ""
subtitle_lock = threading.Lock()
thinking_text = ""
thinking_lock = threading.Lock()

def set_subtitle(text):
    global subtitle_text
    with subtitle_lock:
        subtitle_text = text

# --- HTTP API на порту 19002 для stream_main.py ---
from http.server import HTTPServer, BaseHTTPRequestHandler
import json as _json

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        global subtitle_text, thinking_text
        length = int(self.headers.get('Content-Length', 0))
        if self.path == "/audio":
            pcm = self.rfile.read(length)
            try: audio_queue.put_nowait(pcm)
            except Exception: pass
            if pcm and len(pcm) > 0:
                import io as _io, wave as _wave
                buf = _io.BytesIO()
                with _wave.open(buf, 'wb') as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
                    w.writeframes(pcm)
                try: web_audio_queue.put_nowait(buf.getvalue())
                except queue.Full: pass
                _ws_broadcast_pcm(pcm)
        elif self.path == "/motion":
            body = _json.loads(self.rfile.read(length))
            name = body.get("name", "")
            with model_lock:
                if live2d_model and name:
                    try: live2d_model.StartMotion(name, 0, 3)
                    except Exception as e: print(f"motion error: {e}")
        else:
            body = _json.loads(self.rfile.read(length))
            text = body.get("text", "")
            if self.path == "/subtitle":
                with subtitle_lock:
                    subtitle_text = text
            elif self.path == "/thinking":
                with thinking_lock:
                    thinking_text = text
        self.send_response(200)
        self.end_headers()

def _start_api():
    HTTPServer(("0.0.0.0", 19002), _Handler).serve_forever()
threading.Thread(target=_start_api, daemon=True).start()

# --- UDP lipsync listener (порт 19003) ---
def _mouth_udp_listener():
    import socket as _socket
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 19003))
    sock.settimeout(0.5)
    while True:
        try:
            data, _ = sock.recvfrom(32)
            val = float(data.decode().strip())
            with mouth_lock:
                global mouth_open
                mouth_open = val
        except _socket.timeout:
            continue
        except Exception:
            continue
threading.Thread(target=_mouth_udp_listener, daemon=True).start()

# --- Motion расписание ---
MOTION_CYCLE = [
    ("mtn_06", 8),
    ("mtn_07", 10),
    ("mtn_00", 12),
    ("Hiyori_m01", 7),
    ("mtn_06", 8),
    ("Hiyori_m09", 6),
    ("mtn_07", 10),
    ("mtn_02", 8),
    ("Hiyori_m04", 7),
    ("mtn_05", 10),
]

def init_egl():
    from OpenGL.EGL import (eglGetDisplay, EGL_DEFAULT_DISPLAY, eglInitialize,
        eglChooseConfig, eglBindAPI, EGL_OPENGL_API, eglCreateContext, EGL_NO_CONTEXT,
        eglCreatePbufferSurface, eglMakeCurrent, EGLint, EGLConfig,
        EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_BLUE_SIZE, EGL_GREEN_SIZE,
        EGL_RED_SIZE, EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT, EGL_NONE, EGL_WIDTH, EGL_HEIGHT)
    import ctypes

    dpy = None
    try:
        EGL_PLATFORM_DEVICE_EXT = 0x313F
        _libegl = ctypes.CDLL("libEGL.so.1")
        _libegl.eglGetProcAddress.restype = ctypes.c_void_p
        _libegl.eglGetProcAddress.argtypes = [ctypes.c_char_p]

        _queryDevicesAddr = _libegl.eglGetProcAddress(b"eglQueryDevicesEXT")
        _getPlatformAddr  = _libegl.eglGetProcAddress(b"eglGetPlatformDisplayEXT")

        if _queryDevicesAddr and _getPlatformAddr:
            PFNEGLQUERYDEVICESEXT = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
            PFNEGLGETPLATFORMDISPLAY = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)
            _queryDevices = PFNEGLQUERYDEVICESEXT(_queryDevicesAddr)
            _getPlatform  = PFNEGLGETPLATFORMDISPLAY(_getPlatformAddr)

            num_devices = ctypes.c_int(0)
            _queryDevices(0, None, ctypes.byref(num_devices))
            if num_devices.value > 0:
                devices = (ctypes.c_void_p * num_devices.value)()
                _queryDevices(num_devices.value, devices, ctypes.byref(num_devices))
                raw = _getPlatform(EGL_PLATFORM_DEVICE_EXT, devices[0], None)
                if raw:
                    dpy = ctypes.cast(raw, ctypes.c_void_p)
                    print(f"EGL: eglGetPlatformDisplayEXT OK, {num_devices.value} device(s)", flush=True)
    except Exception as e:
        print(f"eglGetPlatformDisplayEXT failed: {e}", flush=True)

    if not dpy:
        try:
            EGL_PLATFORM_GBM_KHR = 0x31D7
            _libegl2 = ctypes.CDLL("libEGL.so.1")
            _libegl2.eglGetProcAddress.restype = ctypes.c_void_p
            _libegl2.eglGetProcAddress.argtypes = [ctypes.c_char_p]
            _getPlatformAddr = _libegl2.eglGetProcAddress(b"eglGetPlatformDisplayEXT")
            if _getPlatformAddr:
                PFNEGLGETPLATFORMDISPLAY = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)
                _getPlatform = PFNEGLGETPLATFORMDISPLAY(_getPlatformAddr)
                drm_fd = os.open("/dev/dri/renderD128", os.O_RDWR)
                raw = _getPlatform(EGL_PLATFORM_GBM_KHR, ctypes.c_void_p(drm_fd), None)
                if raw:
                    dpy = ctypes.cast(raw, ctypes.c_void_p)
                    print("EGL: GBM/DRM OK", flush=True)
        except Exception as e:
            print(f"EGL GBM fallback failed: {e}", flush=True)

    if not dpy:
        print("EGL: falling back to eglGetDisplay", flush=True)
        try:
            dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY)
        except Exception as e:
            print(f"eglGetDisplay failed: {e}", flush=True)
            dpy = None

    if not dpy:
        print("FATAL: No EGL display available!", flush=True)
        raise RuntimeError("Cannot initialize EGL display")

    eglInitialize(dpy, None, None)
    attrs = (EGLint*12)(EGL_SURFACE_TYPE,EGL_PBUFFER_BIT,EGL_BLUE_SIZE,8,EGL_GREEN_SIZE,8,EGL_RED_SIZE,8,EGL_RENDERABLE_TYPE,EGL_OPENGL_BIT,EGL_NONE)
    configs = (EGLConfig*1)(); num = EGLint(0)
    eglChooseConfig(dpy, attrs, configs, 1, num)
    eglBindAPI(EGL_OPENGL_API)
    ctx = eglCreateContext(dpy, configs[0], EGL_NO_CONTEXT, None)
    surf = eglCreatePbufferSurface(dpy, configs[0], (EGLint*5)(EGL_WIDTH,WIDTH,EGL_HEIGHT,HEIGHT,EGL_NONE))
    eglMakeCurrent(dpy, surf, surf, ctx)
    print("EGL:", glGetString(GL_VERSION), "|", glGetString(GL_RENDERER), flush=True)

def render_loop():
    init_egl()
    live2d.init()
    live2d.glInit()
    model = live2d.LAppModel()
    model.LoadModelJson(MODEL_PATH)
    global live2d_model
    with model_lock:
        live2d_model = model

    def motion_scheduler():
        import itertools
        time.sleep(3)
        for name, delay in itertools.cycle(MOTION_CYCLE):
            with model_lock:
                if live2d_model:
                    try: live2d_model.StartMotion(name, 0, 2)
                    except Exception: pass
            time.sleep(delay)

    threading.Thread(target=motion_scheduler, daemon=True).start()

    MODEL_X = int(WIDTH * 0.15)
    MODEL_SIZE = int(HEIGHT * 2.2)
    MODEL_VIEWPORT_Y = -int(MODEL_SIZE * 0.38)

    # Фон
    bg_tex = None
    _bg_mtime = 0.0

    def _reload_bg():
        nonlocal bg_tex, _bg_mtime
        try:
            mtime = os.path.getmtime('bg.jpg')
            if mtime == _bg_mtime:
                return
            bg_raw = cv2.imread('bg.jpg')
            if bg_raw is None:
                return
            bg_resized = cv2.resize(bg_raw, (WIDTH, HEIGHT))
            bg_rgb = cv2.cvtColor(bg_resized, cv2.COLOR_BGR2RGB)
            if bg_tex is None:
                bg_tex = glGenTextures(1)
            glBindTexture(GL_TEXTURE_2D, bg_tex)
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, WIDTH, HEIGHT, 0, GL_RGB, GL_UNSIGNED_BYTE, bg_rgb)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            glBindTexture(GL_TEXTURE_2D, 0)
            _bg_mtime = mtime
            print("Background reloaded", flush=True)
        except Exception as e:
            print(f"bg reload error: {e}", flush=True)

    _reload_bg()  # первая загрузка

    vert_src = b"""
    #version 130
    in vec2 pos; out vec2 uv;
    void main() { uv = pos * 0.5 + 0.5; uv.y = 1.0 - uv.y; gl_Position = vec4(pos, 0.0, 1.0); }
    """
    frag_src = b"""
    #version 130
    in vec2 uv; uniform sampler2D tex; out vec4 color;
    void main() { color = texture(tex, uv); }
    """
    grad_frag_src = b"""
    #version 130
    in vec2 uv;
    uniform float time;
    out vec4 color;
    void main() {
        vec3 c0 = vec3(0.847, 0.749, 0.918);
        vec3 c1 = vec3(1.000, 0.847, 0.773);
        vec3 c2 = vec3(0.749, 0.918, 0.855);
        vec3 c3 = vec3(0.988, 0.773, 0.847);

        float t = uv.x * 0.6 + uv.y * 0.4 + time * 0.08;
        t = mod(t, 1.0);

        vec3 col;
        if (t < 0.25) col = mix(c0, c1, t / 0.25);
        else if (t < 0.5) col = mix(c1, c2, (t - 0.25) / 0.25);
        else if (t < 0.75) col = mix(c2, c3, (t - 0.5) / 0.25);
        else col = mix(c3, c0, (t - 0.75) / 0.25);

        color = vec4(col, 1.0);
    }
    """

    def compile_shader(src, stype):
        s = glCreateShader(stype); glShaderSource(s, src); glCompileShader(s); return s

    bg_prog = glCreateProgram()
    glAttachShader(bg_prog, compile_shader(vert_src, GL_VERTEX_SHADER))
    glAttachShader(bg_prog, compile_shader(frag_src, GL_FRAGMENT_SHADER))
    glLinkProgram(bg_prog)

    grad_prog = glCreateProgram()
    glAttachShader(grad_prog, compile_shader(vert_src, GL_VERTEX_SHADER))
    glAttachShader(grad_prog, compile_shader(grad_frag_src, GL_FRAGMENT_SHADER))
    glLinkProgram(grad_prog)
    grad_time_loc = glGetUniformLocation(grad_prog, b"time")

    quad = np.array([-1,-1, 1,-1, -1,1, 1,1], dtype=np.float32)
    vao = glGenVertexArrays(1); vbo = glGenBuffers(1)
    glBindVertexArray(vao); glBindBuffer(GL_ARRAY_BUFFER, vbo)
    glBufferData(GL_ARRAY_BUFFER, quad.nbytes, quad, GL_STATIC_DRAW)
    loc = glGetAttribLocation(bg_prog, b"pos")
    glEnableVertexAttribArray(loc); glVertexAttribPointer(loc, 2, GL_FLOAT, False, 0, None)
    glBindVertexArray(0)

    # FBO
    fbo = glGenFramebuffers(1)
    color_tex = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, color_tex)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, WIDTH, HEIGHT, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
    glBindFramebuffer(GL_FRAMEBUFFER, fbo)
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, color_tex, 0)
    glBindFramebuffer(GL_FRAMEBUFFER, 0)

    model.Resize(MODEL_SIZE, MODEL_SIZE)

    # Readback: преаллоцированный буфер через ctypes — без PBO (PBO не используется)
    _readback_buf = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    _libgl = ctypes.CDLL("libGL.so.1")

    t = 0.0
    last = time.time()
    frame_target = 1.0 / FPS
    motion_started = False
    next_frame_time = time.time()
    frame_count = 0

    while True:
        now = time.time()
        dt = now - last
        last = now
        t += dt

        if t > 2.0 and not motion_started:
            try: model.StartMotion("mtn_00", 0, 2)
            except Exception: pass
            motion_started = True

        glBindFramebuffer(GL_FRAMEBUFFER, fbo)

        # 1. Фон (перечитываем bg.jpg если изменился)
        if frame_count % 120 == 0:  # каждые 2с при 60fps
            _reload_bg()
        glViewport(0, 0, WIDTH, HEIGHT)
        glDisable(GL_BLEND)
        glClearColor(0.0, 0.0, 0.0, 1.0)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        if bg_tex is not None:
            glUseProgram(bg_prog)
            glBindTexture(GL_TEXTURE_2D, bg_tex)
            glBindVertexArray(vao)
            glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
            glBindVertexArray(0)
            glUseProgram(0)
        else:
            glUseProgram(grad_prog)
            glUniform1f(grad_time_loc, t)
            glBindVertexArray(vao)
            glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
            glBindVertexArray(0)
            glUseProgram(0)

        # 2. Модель
        glViewport(MODEL_X, MODEL_VIEWPORT_Y, MODEL_SIZE, MODEL_SIZE)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        model.Update()
        with mouth_lock:
            mo = mouth_open
        model.SetParameterValue('ParamMouthOpenY', mo, 1.0)
        model.Draw()

        # 3. Readback
        glViewport(0, 0, WIDTH, HEIGHT)
        _libgl.glReadPixels(0, 0, WIDTH, HEIGHT, 0x1908, 0x1401, _readback_buf.ctypes.data_as(ctypes.c_void_p))
        _readback_time = time.time() - now
        frame = np.ascontiguousarray(_readback_buf[::-1, :, :3])

        _dropped_twitch = False
        try: frame_queue.put_nowait(frame)
        except queue.Full: pass
        try: twitch_frame_queue.put_nowait(frame)
        except queue.Full: _dropped_twitch = True

        frame_count += 1
        if _dropped_twitch:
            print(f"[render] DROP frame {frame_count} | readback={_readback_time*1000:.1f}ms | q={twitch_frame_queue.qsize()}", flush=True)
        elif frame_count % 60 == 1:
            print(f"[render] frame {frame_count}, q={twitch_frame_queue.qsize()}, readback={_readback_time*1000:.1f}ms", flush=True)

        glBindFramebuffer(GL_FRAMEBUFFER, 0)

        # FPS limiter: абсолютное время — drift не накапливается
        next_frame_time += frame_target
        sleep_time = next_frame_time - time.time()
        if sleep_time > 0:
            time.sleep(sleep_time)
        elif sleep_time < -frame_target:
            next_frame_time = time.time()

def twitch_loop():
    import os
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    font_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    for f in ['/tmp/chat_overlay.txt', '/tmp/subtitle.txt', '/tmp/game_overlay.txt']:
        if not os.path.exists(f):
            open(f, 'w').close()
    model_x = int(WIDTH * 0.15)
    vf = (
        f"drawtext=fontfile={font_bold}:textfile=/tmp/chat_overlay.txt"
        f":reload=1:x=10:y=80:fontsize=16:fontcolor=white"
        f":shadowcolor=black:shadowx=2:shadowy=2:line_spacing=4,"
        f"drawtext=fontfile={font_bold}:textfile=/tmp/subtitle.txt"
        f":reload=1:x={model_x}:y=h-th-20:fontsize=18:fontcolor=white"
        f":shadowcolor=black:shadowx=2:shadowy=2:line_spacing=3:fix_bounds=1,"
        f"drawtext=fontfile={font_bold}:textfile=/tmp/game_overlay.txt"
        f":reload=1:x=(w-tw)/2:y=20:fontsize=22:fontcolor=yellow"
        f":shadowcolor=black:shadowx=2:shadowy=2:line_spacing=5"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-re",
        "-thread_queue_size", "16",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{WIDTH}x{HEIGHT}", "-pix_fmt", "rgb24", "-r", str(FPS),
        "-i", "/tmp/videopipe",
        "-thread_queue_size", "16",
        "-f", "s16le", "-ar", "44100", "-ac", "1", "-i", "/tmp/tts_audio_pipe",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-threads", "4",
        "-g", "120", "-keyint_min", "60",
        "-b:v", "6000k", "-maxrate", "8000k", "-bufsize", "6000k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-f", "flv", TWITCH_URL,
    ]
    if not os.path.exists('/tmp/tts_audio_pipe'):
        os.mkfifo('/tmp/tts_audio_pipe')

    print("Streaming to Twitch...")
    if not os.path.exists('/tmp/videopipe'): os.mkfifo('/tmp/videopipe')
    import fcntl as _fcntl
    _vfd = os.open('/tmp/videopipe', os.O_RDWR)
    vfd = os.fdopen(_vfd, 'wb', buffering=0)
    try: _fcntl.fcntl(_vfd, 1031, 64*1024*1024)
    except: pass
    proc = subprocess.Popen(cmd)

    # Отдельная очередь для записи в pipe — не блокирует чтение из twitch_frame_queue
    pipe_write_queue = queue.Queue(maxsize=3)

    def pipe_writer():
        WRITE_CHUNK = 1024 * 1024
        while True:
            try:
                data = pipe_write_queue.get(timeout=1.0)
                for i in range(0, len(data), WRITE_CHUNK):
                    vfd.write(data[i:i+WRITE_CHUNK])
            except queue.Empty:
                continue
            except BrokenPipeError:
                break

    threading.Thread(target=pipe_writer, daemon=True).start()

    while True:
        try:
            frame = twitch_frame_queue.get(timeout=1.0)
            try: pipe_write_queue.put_nowait(frame.tobytes())
            except queue.Full: pass
        except queue.Empty:
            continue
        except BrokenPipeError:
            break

# --- Web viewer ---
web_clients = []
web_clients_lock = threading.Lock()
web_audio_queue = queue.Queue(maxsize=256)

ws_audio_clients = set()
ws_audio_lock = threading.Lock()

def _ws_broadcast_pcm(pcm_bytes):
    with ws_audio_lock:
        dead = set()
        for ws_send in list(ws_audio_clients):
            try:
                ws_send(pcm_bytes)
            except Exception:
                dead.add(ws_send)
        for d in dead:
            ws_audio_clients.discard(d)

def web_server():
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import io as _io

    HTML = """<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Мизури</title>
<style>
body{margin:0;background:#111;display:flex;flex-direction:column;align-items:center;font-family:sans-serif}
img{width:100%;max-width:1280px}
#controls{display:flex;gap:8px;margin:10px;width:90%;max-width:800px}
#msg{flex:1;padding:8px;font-size:16px;border-radius:6px;border:1px solid #444;background:#222;color:#fff}
button{padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-size:16px}
#sendbtn{background:#5a3fa0;color:#fff}
#soundbtn{background:#2a6a2a;color:#fff;min-width:120px}
#log{color:#aaa;font-size:13px;margin:4px;width:90%;max-width:800px;height:80px;overflow-y:auto}
</style></head><body>
<img src="/video">
<div id=controls>
  <input id=msg placeholder="Напиши Мизури..." onkeydown="if(event.key==='Enter')send()">
  <button id=sendbtn onclick="send()">Отправить</button>
  <button id=soundbtn onclick="toggleSound()">🔇 Звук выкл</button>
</div>
<div id=log></div>
<script>
const ctx = new AudioContext({sampleRate: 44100});
let soundOn = false;
let nextPlayTime = 0;
let ws = null;

function addLog(t){const d=document.getElementById('log');d.innerHTML+=t+'<br>';d.scrollTop=d.scrollHeight;}

function toggleSound(){
  soundOn = !soundOn;
  const btn = document.getElementById('soundbtn');
  if(soundOn){
    ctx.resume();
    btn.textContent = '🔊 Звук вкл';
    btn.style.background = '#1a8a1a';
    connectWS();
  } else {
    btn.textContent = '🔇 Звук выкл';
    btn.style.background = '#2a6a2a';
  }
}

function connectWS(){
  if(ws && ws.readyState <= 1) return;
  ws = new WebSocket('ws://' + location.hostname + '/ws');
  ws.binaryType = 'arraybuffer';
  ws.onmessage = (e) => {
    if(!soundOn) return;
    const pcm = new Int16Array(e.data);
    const buf = ctx.createBuffer(1, pcm.length, 44100);
    const ch = buf.getChannelData(0);
    for(let i=0;i<pcm.length;i++) ch[i] = pcm[i] / 32768.0;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    const now = ctx.currentTime;
    if(nextPlayTime < now) nextPlayTime = now + 0.05;
    src.start(nextPlayTime);
    nextPlayTime += buf.duration;
  };
  ws.onclose = () => { if(soundOn) setTimeout(connectWS, 1000); };
}

async function send(){
  const inp=document.getElementById('msg');
  const msg=inp.value.trim(); if(!msg)return;
  inp.value=''; addLog('Ты: '+msg);
  await fetch('/chat', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message:msg,user:'viewer'})});
}
</script></body></html>""".encode('utf-8')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        @staticmethod
        def _ws_send(conn, data):
            import struct
            l = len(data)
            if l < 126:
                header = bytes([0x82, l])
            elif l < 65536:
                header = bytes([0x82, 126]) + struct.pack('>H', l)
            else:
                header = bytes([0x82, 127]) + struct.pack('>Q', l)
            conn.sendall(header + data)

        def do_GET(self):
            if self.path == '/ws':
                import hashlib, base64, struct
                key = self.headers.get('Sec-WebSocket-Key', '').strip()
                accept = base64.b64encode(hashlib.sha1(
                    (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()
                ).digest()).decode()
                self.send_response(101, 'Switching Protocols')
                self.send_header('Upgrade', 'websocket')
                self.send_header('Connection', 'Upgrade')
                self.send_header('Sec-WebSocket-Accept', accept)
                self.end_headers()
                self.wfile.flush()
                conn = self.connection
                send_fn = lambda pcm: self._ws_send(conn, pcm)
                with ws_audio_lock:
                    ws_audio_clients.add(send_fn)
                try:
                    while True:
                        d = conn.recv(64)
                        if not d: break
                except Exception:
                    pass
                finally:
                    with ws_audio_lock:
                        ws_audio_clients.discard(send_fn)
                return

            elif self.path == '/':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(HTML)

            elif self.path == '/overlay' or self.path.startswith('/overlay?'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                try:
                    with open('/teamspace/studios/this_studio/overlay_tools.html', 'rb') as f:
                        self.wfile.write(f.read())
                except Exception as e:
                    self.wfile.write(f'Error: {e}'.encode())

            elif self.path == '/video':
                self.send_response(200)
                self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                self.end_headers()
                q = queue.Queue(maxsize=2)
                with web_clients_lock:
                    web_clients.append(q)
                try:
                    while True:
                        frame_jpg = q.get(timeout=5)
                        self.wfile.write(
                            b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame_jpg + b'\r\n'
                        )
                        self.wfile.flush()
                except Exception:
                    pass
                finally:
                    with web_clients_lock:
                        web_clients.remove(q)

    HTTPServer(('0.0.0.0', 5000), Handler).serve_forever()

def web_frame_pusher():
    """Кодирует кадры в JPEG и рассылает web клиентам. 15fps для экономии CPU."""
    last_push = 0
    web_fps = 15
    web_interval = 1.0 / web_fps
    while True:
        try:
            frame = frame_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        now = time.time()
        if now - last_push < web_interval:
            continue
        last_push = now
        # MJPEG: снижаем качество для скорости (web viewer не нужен 1080p quality)
        ret, jpg = cv2.imencode('.jpg', cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
                                [cv2.IMWRITE_JPEG_QUALITY, 50])
        if not ret:
            continue
        jpg_bytes = jpg.tobytes()
        with web_clients_lock:
            for q in list(web_clients):
                try:
                    q.put_nowait(jpg_bytes)
                except queue.Full:
                    pass

def ws_audio_server():
    import socket as _socket, hashlib, base64, struct

    def handshake(conn):
        data = conn.recv(4096).decode()
        key = re.search(r'Sec-WebSocket-Key: (.+)', data).group(1).strip()
        accept = base64.b64encode(hashlib.sha1(
            (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()
        ).digest()).decode()
        conn.send((
            'HTTP/1.1 101 Switching Protocols\r\n'
            'Upgrade: websocket\r\nConnection: Upgrade\r\n'
            f'Sec-WebSocket-Accept: {accept}\r\n\r\n'
        ).encode())

    def send_frame(conn, data):
        header = b'\x82'
        l = len(data)
        if l < 126:
            header += bytes([l])
        elif l < 65536:
            header += b'\x7e' + struct.pack('>H', l)
        else:
            header += b'\x7f' + struct.pack('>Q', l)
        conn.sendall(header + data)

    def client_handler(conn):
        send_fn = lambda pcm: send_frame(conn, pcm)
        with ws_audio_lock:
            ws_audio_clients.add(send_fn)
        try:
            while True:
                d = conn.recv(1024)
                if not d: break
        except Exception:
            pass
        finally:
            with ws_audio_lock:
                ws_audio_clients.discard(send_fn)
            conn.close()

    srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', 5002))
    srv.listen(10)
    while True:
        conn, _ = srv.accept()
        try:
            handshake(conn)
            threading.Thread(target=client_handler, args=(conn,), daemon=True).start()
        except Exception:
            conn.close()

if __name__ == '__main__':
    import traceback as _tb
    def _safe(fn):
        def wrapper():
            try: fn()
            except Exception: print(f"[THREAD ERROR] {fn.__name__}:\n{_tb.format_exc()}", flush=True)
        return wrapper

    threading.Thread(target=web_server, daemon=True).start()
    threading.Thread(target=twitch_chat_reader, daemon=True).start()
    threading.Thread(target=_safe(render_loop), daemon=True).start()
    print("Waiting for first frame...", flush=True)
    for _ in range(300):
        if not frame_queue.empty():
            break
        time.sleep(0.05)
    if frame_queue.empty():
        print("WARNING: no frames yet, starting anyway", flush=True)
    print("First frame ready, starting streams...", flush=True)
    threading.Thread(target=web_frame_pusher, daemon=True).start()
    print("Web viewer: http://0.0.0.0:5000", flush=True)
    twitch_loop()