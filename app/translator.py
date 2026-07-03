"""Tradução do SRT para PT-BR via Google Gemini.

Estratégia robusta: parseamos o SRT com a lib `srt`, enviamos APENAS o texto de
cada bloco (numa lista JSON), e remontamos preservando índices e timestamps —
estes nunca passam pelo modelo, então não há como quebrá-los.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Sequence

import srt
from google import genai
from google.genai import types

from . import config

logger = logging.getLogger("translator")

CHUNK_SIZE = 60           # blocos de diálogo por requisição
MAX_CONCURRENCY = 4       # chunks em paralelo
MAX_RETRIES = 3

SYSTEM_PROMPT = (
    "Você é um tradutor especializado em legendas. Recebe um array JSON de strings, "
    "cada uma sendo a fala de uma legenda. Traduza TODAS as strings para português do "
    "Brasil (PT-BR). É OBRIGATÓRIO retornar um array JSON com EXATAMENTE a mesma "
    "quantidade de itens, na MESMA ordem, sem fundir nem dividir itens. Preserve tags "
    "de formatação inline como <i>, </i>, {\\an8} e quebras de linha internas. Não "
    "adicione numeração, timestamps, comentários ou qualquer texto fora do array JSON. "
    "Responda apenas com o array JSON traduzido."
)

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not config.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY não configurada.")
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def _chunks(items: Sequence[srt.Subtitle], size: int):
    for i in range(0, len(items), size):
        yield i, items[i : i + size]


async def _translate_texts(texts: list[str]) -> list[str]:
    """Traduz uma lista de textos; devolve lista de mesmo tamanho."""
    client = _get_client()
    payload = json.dumps(texts, ensure_ascii=False)

    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.aio.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=payload,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
            out = json.loads(resp.text)
            if isinstance(out, list) and len(out) == len(texts):
                return [str(x) for x in out]
            logger.warning(
                "Chunk voltou com tamanho divergente (%s != %s), tentativa %s.",
                len(out) if isinstance(out, list) else "?",
                len(texts),
                attempt,
            )
        except Exception as exc:  # rede, rate limit, JSON inválido
            last_err = exc
            logger.warning("Erro traduzindo chunk (tentativa %s): %s", attempt, exc)

        await asyncio.sleep(2 ** attempt)  # backoff exponencial

    logger.error("Chunk falhou após %s tentativas: %s", MAX_RETRIES, last_err)
    return texts  # fallback: mantém original, não perde o arquivo


async def translate_srt(raw_path: str, out_path: str) -> None:
    """Lê o SRT bruto, traduz e escreve o SRT traduzido em `out_path`."""
    with open(raw_path, encoding="utf-8", errors="ignore") as fh:
        subs = list(srt.parse(fh.read()))

    if not subs:
        raise ValueError("SRT vazio ou não parseável.")

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def worker(start: int, block: list[srt.Subtitle]) -> tuple[int, list[str]]:
        async with sem:
            translated = await _translate_texts([s.content for s in block])
            return start, translated

    tasks = [worker(start, block) for start, block in _chunks(subs, CHUNK_SIZE)]
    results = await asyncio.gather(*tasks)

    for start, translated in results:
        for offset, text in enumerate(translated):
            subs[start + offset].content = text

    composed = srt.compose(subs, reindex=False)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(composed)

    logger.info("Legenda traduzida (%s blocos) salva em %s.", len(subs), out_path)
