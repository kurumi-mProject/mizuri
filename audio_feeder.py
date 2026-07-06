"""
audio_feeder.py — единственный писатель в tts_audio_pipe.
stream_main кладёт PCM в /tmp/tts_pcm_queue (файл), feeder его читает и воспроизводит.
"""
import time, wave, os, logging, struct, socket

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [audio_feeder] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/tmp/audio_feeder.log', encoding='utf-8')
    ]
)
log = logging.getLogger('audio_feeder')

PIPE      = "/tmp/tts_audio_pipe"
SILENCE   = "/tmp/silence.wav"
ACTIVE    = "/tmp/active.wav"
PCM_QUEUE = "/tmp/tts_pcm_queue"  # stream_main пишет сюда PCM чанки
CHUNK     = int(44100 * 2 * 0.033)  # 33ms для 60fps lipsync
CHUNK_DUR = 0.033                     # длительность чанка в секундах
MOUTH_AMP = "/tmp/mouth_amp"        # lipsync: amplitude value

def load_pcm(path):
    with wave.open(path) as w:
        return w.readframes(w.getnframes())

log.info(f"Запуск, открываю pipe {PIPE}")
fd = open(PIPE, "wb", buffering=0)
log.info("Pipe открыт")

# UDP socket для lipsync (быстрее чем файл и HTTP)
_mouth_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

silence_pcm = load_pcm(SILENCE)
bg_pcm = silence_pcm
bg_pos = 0
last_mtime = 0
chunks_written = 0

# Очередь PCM от TTS: файл с префиксом длины [4 байта LE uint32][data]
pcm_buf = bytearray()  # буфер TTS аудио

def drain_pcm_queue():
    """Читает все накопленные PCM данные из файла-очереди."""
    global pcm_buf
    if not os.path.exists(PCM_QUEUE):
        return
    try:
        with open(PCM_QUEUE, 'rb') as f:
            data = f.read()
        os.remove(PCM_QUEUE)
        # Формат: [uint32 len][pcm bytes] повторяется
        pos = 0
        count = 0
        while pos + 4 <= len(data):
            length = struct.unpack_from('<I', data, pos)[0]
            pos += 4
            if pos + length > len(data):
                break
            pcm_buf.extend(data[pos:pos+length])
            pos += length
            count += 1
        if count:
            log.info(f"Получено {count} TTS чанков, буфер: {len(pcm_buf)//2/44100:.2f}s")
    except Exception as e:
        log.warning(f"drain_pcm_queue: {e}")

while True:
    t0 = time.time()

    # Читаем новые TTS данные
    drain_pcm_queue()

    if len(pcm_buf) >= CHUNK:
        # Есть TTS аудио — воспроизводим
        chunk = bytes(pcm_buf[:CHUNK])
        del pcm_buf[:CHUNK]
        if len(pcm_buf) == 0:
            log.info("TTS буфер опустел — возвращаюсь к фону")
    else:
        # Нет TTS — фоновое аудио
        try:
            mtime = os.path.getmtime(ACTIVE)
            if mtime != last_mtime:
                bg_pcm = load_pcm(ACTIVE)
                bg_pos = 0
                last_mtime = mtime
                log.info(f"Фон переключён: {ACTIVE} ({len(bg_pcm)//2/44100:.1f}s)")
        except Exception:
            pass

        end = bg_pos + CHUNK
        if end <= len(bg_pcm):
            chunk = bg_pcm[bg_pos:end]
            bg_pos = end
        else:
            chunk = bg_pcm[bg_pos:] + bg_pcm[:end - len(bg_pcm)]
            bg_pos = end - len(bg_pcm)

    try:
        fd.write(chunk)
        chunks_written += 1
        if chunks_written % 200 == 0:
            log.info(f"Живой, чанков: {chunks_written}, tts_buf: {len(pcm_buf)//2/44100:.2f}s")
        # lipsync: амплитуда → UDP на server.py (порт 19003)
        samples = struct.unpack_from(f'<{len(chunk)//2}h', chunk)
        amp = sum(abs(s) for s in samples) / len(samples) / 32768.0
        mouth = min(1.0, amp * 8.0) if len(pcm_buf) > 0 or amp > 0.01 else 0.0
        try:
            _mouth_sock.sendto(f"{mouth:.3f}".encode(), ("127.0.0.1", 19003))
        except Exception:
            pass
    except BrokenPipeError:
        log.warning("BrokenPipe — переоткрываю")
        fd.close()
        fd = open(PIPE, "wb", buffering=0)

    sleep = CHUNK_DUR - (time.time() - t0)
    if sleep > 0:
        time.sleep(sleep)
