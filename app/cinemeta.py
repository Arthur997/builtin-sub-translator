"""Resolve o título de filmes/séries a partir do IMDb ID via Cinemeta.

Cinemeta é o addon de metadados público e oficial do próprio Stremio — não
precisa de chave de API. Usamos isso porque o `id` da requisição de legenda
(IMDb ID, e para séries + temporada/episódio) SEMPRE chega, mesmo quando o
player não manda `filename`/`videoSize`/`videoHash`. Com o título em mãos, dá
pra casar contra os nomes dos torrents/arquivos no TorBox.
"""

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger("cinemeta")

_CINEMETA_BASE = "https://v3-cinemeta.strem.io/meta"
_cache: dict[str, str | None] = {}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Normaliza texto pra comparação fuzzy: minúsculo, só alfanumérico."""
    return _NON_ALNUM.sub("", text.lower())


def parse_sub_id(sub_id: str) -> tuple[str, int | None, int | None]:
    """Extrai (imdb_id, season, episode) de um id tipo 'tt123' ou 'tt123:1:11'."""
    parts = sub_id.split(":")
    imdb_id = parts[0]
    season = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    episode = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    return imdb_id, season, episode


async def get_title(imdb_id: str, media_type: str) -> str | None:
    """Busca o título do filme/série pelo IMDb ID via Cinemeta. Cacheado em memória."""
    cache_key = f"{media_type}:{imdb_id}"
    if cache_key in _cache:
        return _cache[cache_key]

    url = f"{_CINEMETA_BASE}/{media_type}/{imdb_id}.json"
    title: str | None = None
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            title = (resp.json().get("meta") or {}).get("name")
    except httpx.HTTPError as exc:
        logger.warning("Falha ao buscar título no Cinemeta (%s): %s", imdb_id, exc)
    except (ValueError, TypeError) as exc:
        logger.warning("Resposta inesperada do Cinemeta (%s): %s", imdb_id, exc)

    _cache[cache_key] = title
    return title
