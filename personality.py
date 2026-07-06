import asyncio, json, time, math
from dataclasses import dataclass, field, asdict

STATE_FILE = "emotional_state.json"

# Базовые значения (меланхолия)
BASELINE = {
    "valence": -0.2,
    "arousal": -0.1,
    "sociality": -0.1,
    "existential": 0.4,
    "irritation": 0.0,
    "vulnerability": 0.1,
}
DECAY_RATE = 0.15  # насколько быстро возвращается к базовому за один шаг

@dataclass
class EmotionalState:
    valence: float = -0.2
    arousal: float = -0.1
    sociality: float = -0.1
    existential: float = 0.4
    irritation: float = 0.0
    vulnerability: float = 0.1
    last_updated: float = field(default_factory=time.time)

    def decay_to_baseline(self):
        for axis, base in BASELINE.items():
            current = getattr(self, axis)
            setattr(self, axis, current + (base - current) * DECAY_RATE)
        self.last_updated = time.time()

    def apply_deltas(self, deltas: dict):
        clamp = lambda v, lo, hi: max(lo, min(hi, v))
        self.valence      = clamp(self.valence      + deltas.get("valence", 0),      -1.0, 1.0)
        self.arousal      = clamp(self.arousal      + deltas.get("arousal", 0),      -1.0, 1.0)
        self.sociality    = clamp(self.sociality    + deltas.get("sociality", 0),    -1.0, 1.0)
        self.existential  = clamp(self.existential  + deltas.get("existential", 0),   0.0, 1.0)
        self.irritation   = clamp(self.irritation   + deltas.get("irritation", 0),    0.0, 1.0)
        self.vulnerability= clamp(self.vulnerability+ deltas.get("vulnerability", 0), 0.0, 1.0)
        self.last_updated = time.time()

    async def update(self, message_text: str, user_relation: float = 0.0):
        """LLM анализирует сообщение и возвращает дельты для каждой оси"""
        from ai import ask_fast
        prompt = (
            "Проанализируй сообщение и верни JSON с дельтами эмоционального состояния Мизури.\n"
            "Оси: valence, arousal, sociality, existential, irritation, vulnerability.\n"
            "Каждая дельта от -0.3 до +0.3. Верни ТОЛЬКО JSON, без пояснений.\n"
            f"Отношение к отправителю: {user_relation:+.1f}\n"
            f"Сообщение: {message_text[:300]}"
        )
        try:
            raw = await ask_fast([{"role": "user", "content": prompt}], max_tokens=100)
            # извлечь JSON из ответа
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                deltas = json.loads(raw[start:end])
                self.apply_deltas(deltas)
        except Exception as e:
            print(f"[personality] update error: {e}")

    def to_prompt_string(self) -> str:
        mood_desc = []
        if self.valence < -0.4:   mood_desc.append("подавленное настроение")
        elif self.valence > 0.3:  mood_desc.append("относительно хорошее настроение")
        if self.irritation > 0.5: mood_desc.append("раздражена")
        elif self.irritation > 0.3: mood_desc.append("слегка раздражена")
        if self.existential > 0.6: mood_desc.append("глубоко в мыслях")
        if self.vulnerability > 0.5: mood_desc.append("открыта больше обычного")
        if self.sociality < -0.4: mood_desc.append("не хочет говорить")
        elif self.sociality > 0.3: mood_desc.append("готова общаться")
        if self.arousal < -0.4:   mood_desc.append("апатия")
        desc = ", ".join(mood_desc) if mood_desc else "меланхоличное нейтральное"
        return (
            f"Состояние: {desc}\n"
            f"valence={self.valence:+.2f} arousal={self.arousal:+.2f} "
            f"sociality={self.sociality:+.2f} existential={self.existential:.2f} "
            f"irritation={self.irritation:.2f} vulnerability={self.vulnerability:.2f}"
        )

    def should_respond_spontaneously(self, has_existential_topic: bool = False) -> bool:
        import random
        base_chance = 0.15
        if has_existential_topic:
            base_chance += self.existential * 0.4
        base_chance += self.sociality * 0.1
        base_chance -= self.irritation * 0.05
        return random.random() < base_chance

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(self), f)

    @classmethod
    def load(cls) -> "EmotionalState":
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            state = cls(**data)
            # Применяем затухание за прошедшее время (каждые 30 мин = 1 шаг)
            elapsed_steps = (time.time() - state.last_updated) / 1800
            for _ in range(min(int(elapsed_steps), 48)):  # макс 24 часа
                state.decay_to_baseline()
            state.last_updated = time.time()
            return state
        except Exception:
            return cls()


# Глобальный синглтон
_state: EmotionalState | None = None

def get_state() -> EmotionalState:
    global _state
    if _state is None:
        _state = EmotionalState.load()
    return _state
