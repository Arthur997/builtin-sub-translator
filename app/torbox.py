"""Integração com a API do TorBox.

Como o Stremio NÃO envia o infohash na requisição de legenda, localizamos o
arquivo candidato na `mylist` por `filename`/`videoSize`. Quando o player manda
`videoHash` (hash do OpenSubtitles), CONFIRMAMOS a identidade do arquivo lendo
os bytes reais (primeiros/últimos 64KB via HTTP Range) e comparando o hash —
isso dá garantia por conteúdo, não só uma heurística de nome/tamanho.
"""

from __future__ import annotations

import logging
import struct
from typing import Any, NamedTuple

import httpx

from . import config

logger = logging.getLogger("torbox")

VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".ts")

_HEADERS = {"Authorization": f"Bearer {config.TORBOX_API_KEY}"}

_HASH_CHUNK = 65536  # 64 KiB — tamanho de bloco do algoritmo do OpenSubtitles
_MAX_HASH_CANDIDATES = 5  # limite de candidatos verificados por conteúdo por requisição


class Candidate(NamedTuple):
    torrent_id: int
    file_id: int
    name: str
    size: int


class ResolveResult(NamedTuple):
    url: str | None
    verified: bool
    name: str | None
    size: int | None


_NOT_FOUND = ResolveResult(None, False, None, None)


def _is_video(name: str) -> bool:
    return name.lower().endswith(VIDEO_EXTS)


def _basename(name: str) -> str:
    # TorBox costuma prefixar com o nome do torrent: "Pasta/arquivo.mkv".
    return name.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _opensubtitles_hash(first_chunk: bytes, last_chunk: bytes, filesize: int) -> str:
    """Algoritmo de hash do OpenSubtitles: fingerprint pelo conteúdo real do arquivo."""
    hash_value = filesize & 0xFFFFFFFFFFFFFFFF
    for chunk in (first_chunk, last_chunk):
        n = len(chunk) // 8
        for lv in struct.unpack(f"<{n}Q", chunk[: n * 8]):
            hash_value = (hash_value + lv) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % hash_value


async def _read_range(
    client: httpx.AsyncClient, url: str, start: int, length: int
) -> bytes | None:
    """Lê `length` bytes a partir de `start` via HTTP Range. None se o servidor não suportar."""
    end = start + length - 1
    try:
        async with client.stream(
            "GET", url, headers={"Range": f"bytes={start}-{end}"}
        ) as resp:
            if resp.status_code != 206:
                # Servidor ignorou o Range (mandaria o arquivo inteiro) — aborta,
                # não vale a pena baixar um vídeo de GBs só pra verificar.
                return None
            data = bytearray()
            async for part in resp.aiter_bytes():
                data.extend(part)
                if len(data) >= length:
                    break
            return bytes(data[:length])
    except httpx.HTTPError as exc:
        logger.warning("Falha lendo range %s-%s: %s", start, end, exc)
        return None


async def _verify_by_content_hash(
    client: httpx.AsyncClient, url: str, filesize: int, expected_hash: str
) -> bool:
    if filesize < _HASH_CHUNK * 2:
        return False  # algoritmo exige arquivo com pelo menos 128KB
    first = await _read_range(client, url, 0, _HASH_CHUNK)
    if first is None:
        return False
    last = await _read_range(client, url, filesize - _HASH_CHUNK, _HASH_CHUNK)
    if last is None:
        return False
    return _opensubtitles_hash(first, last, filesize) == expected_hash.lower()


def _rank_candidates(
    torrents: list[dict[str, Any]], filename: str | None, video_size: int | None
) -> list[Candidate]:
    """Ordena candidatos por probabilidade: match exato de nome > match de tamanho > resto."""
    target_name = _basename(filename) if filename else None

    by_name: list[Candidate] = []
    by_size: list[Candidate] = []
    others: list[Candidate] = []

    for t in torrents:
        tid = t.get("id")
        for f in t.get("files", []) or []:
            fid, fname, fsize = f.get("id"), f.get("name") or "", f.get("size") or 0
            if tid is None or fid is None or not _is_video(fname):
                continue
            c = Candidate(tid, fid, fname, fsize)
            if target_name and _basename(fname) == target_name:
                by_name.append(c)
            elif video_size and fsize == video_size:
                by_size.append(c)
            else:
                others.append(c)

    others.sort(key=lambda c: c.size, reverse=True)
    return by_name + by_size + others


async def _get_download_url(
    client: httpx.AsyncClient, candidate: Candidate
) -> str | None:
    try:
        dl = await client.get(
            "/api/torrents/requestdl",
            params={
                "token": config.TORBOX_API_KEY,
                "torrent_id": candidate.torrent_id,
                "file_id": candidate.file_id,
            },
        )
        dl.raise_for_status()
        return dl.json().get("data")
    except httpx.HTTPError as exc:
        logger.warning(
            "Falha pedindo download link (t=%s f=%s): %s",
            candidate.torrent_id,
            candidate.file_id,
            exc,
        )
        return None


async def resolve_download_url(
    filename: str | None,
    video_size: int | None,
    video_hash: str | None = None,
) -> ResolveResult:
    """Localiza o arquivo no TorBox e devolve (download_url, verificado, nome, tamanho).

    Com `video_hash` (OpenSubtitles hash enviado pelo Stremio no `extra`), o
    candidato é confirmado byte a byte antes de aceitar. Sem ele, cai na
    heurística de filename/videoSize — sem garantia de que é o arquivo certo.
    """
    if not config.TORBOX_API_KEY:
        logger.error("TORBOX_API_KEY não configurada.")
        return _NOT_FOUND

    async with httpx.AsyncClient(
        base_url=config.TORBOX_BASE_URL, headers=_HEADERS, timeout=30.0
    ) as client:
        try:
            resp = await client.get(
                "/api/torrents/mylist", params={"bypass_cache": "true"}
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Falha ao consultar mylist do TorBox: %s", exc)
            return _NOT_FOUND

        torrents = resp.json().get("data") or []
        if not isinstance(torrents, list):
            logger.error("Resposta inesperada da mylist do TorBox.")
            return _NOT_FOUND

        candidates = _rank_candidates(torrents, filename, video_size)
        if not candidates:
            logger.info(
                "Nenhum arquivo de vídeo casou (filename=%s, size=%s).",
                filename,
                video_size,
            )
            return _NOT_FOUND

        if video_hash:
            checked = candidates[:_MAX_HASH_CANDIDATES]
            for c in checked:
                url = await _get_download_url(client, c)
                if not url:
                    continue
                if await _verify_by_content_hash(client, url, c.size, video_hash):
                    logger.info(
                        "Match CONFIRMADO por hash de conteúdo: torrent=%s file=%s nome=%r tamanho=%s.",
                        c.torrent_id,
                        c.file_id,
                        c.name,
                        c.size,
                    )
                    return ResolveResult(url, True, c.name, c.size)
            logger.warning(
                "videoHash fornecido mas nenhum dos %s candidatos bateu — usando heurística.",
                len(checked),
            )

        # Fallback: heurística de nome/tamanho, sem garantia de conteúdo.
        best = candidates[0]
        url = await _get_download_url(client, best)
        if not url:
            return _NOT_FOUND
        logger.info(
            "Match HEURÍSTICO (sem verificação de conteúdo): torrent=%s file=%s nome=%r tamanho=%s.",
            best.torrent_id,
            best.file_id,
            best.name,
            best.size,
        )
        return ResolveResult(url, False, best.name, best.size)
