"""
Embeddings через gen-api.ru (text-embedding-3-small).
Асинхронный polling: POST → request_id → GET до готовности.
"""
from __future__ import annotations
import asyncio, httpx, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

EMBED_URL = f"{config.GENAPI_URL}/networks/embeddings"
RESULT_URL = f"{config.GENAPI_URL}/request"
HEADERS = {"Authorization": f"Bearer {config.GENAPI_KEY}", "Content-Type": "application/json", "Accept": "application/json"}


async def embed_batch(texts: list[str]) -> list[list[float]] | None:
    if not texts:
        return []
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(EMBED_URL, json={"input": [str(t)[:2000] for t in texts]}, headers=HEADERS)
            r.raise_for_status()
            data = r.json()

            # Синхронный ответ
            if isinstance(data, list) and data and "embedding" in data[0]:
                data.sort(key=lambda x: x.get("index", 0))
                return [d["embedding"] for d in data]

            # Асинхронный — polling по request_id
            req_id = data.get("id") or data.get("request_id")
            if not req_id:
                print(f"[embed] no request_id: {data}")
                return None

            for _ in range(30):
                await asyncio.sleep(1)
                res = await client.get(f"{RESULT_URL}/{req_id}", headers=HEADERS)
                res.raise_for_status()
                result = res.json()
                status = result.get("status")
                if status == "success":
                    out = result.get("output") or result.get("data") or result.get("result")
                    if isinstance(out, list) and out and "embedding" in out[0]:
                        out.sort(key=lambda x: x.get("index", 0))
                        return [d["embedding"] for d in out]
                    if isinstance(out, list) and out and isinstance(out[0], list):
                        return out
                elif status in ("error", "failed"):
                    print(f"[embed] failed: {result}")
                    return None

            print("[embed] timeout")
            return None
    except Exception as e:
        print(f"[embed] {e}")
        return None
