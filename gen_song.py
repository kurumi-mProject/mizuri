#!/usr/bin/env python3
"""
Генерация песни голосом Мизури через seed-vc (zero-shot voice conversion).
1. Fish Speech → базовый вокал
2. seed-vc → конвертация в голос из ref_voice.mp3
"""
import sys, os
sys.path.insert(0, "/teamspace/studios/this_studio/seed_vc")
os.chdir("/teamspace/studios/this_studio/seed_vc")

import requests, base64, torch, soundfile as sf
import numpy as np

REF_VOICE = "/teamspace/studios/this_studio/ref_voice.mp3"
OUT_PATH  = "/tmp/mizuri_song.wav"

LYRICS = (
    "Я смотрю в пустое небо, там где нет ни звёзд ни слов. "
    "Время тихо тает где-то, между прошлым и сейчас. "
    "Я не знаю кто я есть, я не знаю где мой свет. "
    "Только ветер, только тишь, только я и этот стрим."
)

# Шаг 1: Fish Speech TTS
print("[1] TTS...")
with open(REF_VOICE, "rb") as f:
    ref_b64 = base64.b64encode(f.read()).decode()

r = requests.post("http://localhost:19001/v1/tts", json={
    "text": LYRICS, "format": "wav", "streaming": False, "language": "ru",
    "references": [{"audio": ref_b64, "text": "Привет, меня зовут Мизури. Я стримлю на Twitch."}]
}, timeout=60)
r.raise_for_status()
with open("/tmp/tts_base.wav", "wb") as f:
    f.write(r.content)
print(f"  TTS: {len(r.content)//1024}KB → /tmp/tts_base.wav")

# Шаг 2: seed-vc voice conversion
print("[2] seed-vc conversion...")
from inference import seed_vc_infer

result_audio, sr = seed_vc_infer(
    source="/tmp/tts_base.wav",
    target=REF_VOICE,
    diffusion_steps=30,
    length_adjust=1.0,
    inference_cfg_rate=0.7,
    device="cuda" if torch.cuda.is_available() else "cpu",
)

sf.write(OUT_PATH, result_audio, sr)
print(f"[done] {OUT_PATH} ({os.path.getsize(OUT_PATH)//1024}KB)")
