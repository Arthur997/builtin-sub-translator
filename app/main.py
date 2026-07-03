"""Servidor FastAPI: manifesto, rota de legendas do Stremio, static e jobs em background."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import time
from urllib.parse import parse_qs, unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, ffmpeg_utils, torbox, translator
from .ffmpeg_utils import NoEmbeddedSubtitle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("main")

app = FastAPI(title="Stremio Translator Addon")

# Stremio busca o manifesto/legendas de origem web -> CORS liberado.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

# Todas as rotas (manifest, subtitles e cache) exigem o mesmo token no prefixo
# da URL. Sem ADDON_TOKEN configurado, o addon fica inacessível por padrão —
# nunca fica "aberto" por esquecimento.

# Jobs de tradução em andamento, para não disparar trabalho duplicado.
IN_FLIGHT: set[str] = set()


def _check_token(token: str) -> None:
    if not config.ADDON_TOKEN or not secrets.compare_digest(token, config.ADDON_TOKEN):
        raise HTTPException(status_code=404)


MANIFEST = {
    "id": "com.nfasoft.stremio.translator",
    "version": "1.0.0",
    "name": "Legendas Traduzidas (TorBox → PT-BR)",
    "description": (
        "Extrai a legenda embutida do arquivo no TorBox e traduz para português "
        "do Brasil via IA."
    ),
    "resources": ["subtitles"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
}


def _parse_extra(extra: str) -> dict[str, str]:
    """Converte o segmento `extra` do Stremio (k=v&k=v) num dict."""
    extra = unquote(extra or "").removesuffix(".json")
    if not extra:
        return {}
    parsed = parse_qs(extra, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _cache_key(sub_id: str, filename: str | None, video_size: str | None) -> str:
    basis = filename or f"{sub_id}:{video_size or ''}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def _human_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "tamanho desconhecido"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


async def _pipeline(
    key: str,
    filename: str | None,
    video_size: int | None,
    video_hash: str | None,
    sub_id: str,
    sub_type: str,
) -> None:
    """Busca no TorBox, extrai a legenda, traduz e grava atomicamente no cache."""
    final_path = config.CACHE_DIR / f"{key}.srt"
    raw_path = config.CACHE_DIR / f"{key}.raw.srt"
    tmp_path = config.CACHE_DIR / f"{key}.tmp.srt"
    t0 = time.monotonic()
    try:
        logger.info("[%s] [1/4] Buscando arquivo no TorBox...", key)
        result = await torbox.resolve_download_url(
            filename, video_size, video_hash, sub_id=sub_id, media_type=sub_type
        )
        if not result.url:
            logger.info(
                "[%s] Sem download_url no TorBox — abortando (%.1fs).",
                key,
                time.monotonic() - t0,
            )
            return
        logger.info(
            '[%s] [1/4] Arquivo resolvido em %.1fs: "%s" (%s) — %s.',
            key,
            time.monotonic() - t0,
            result.name or "?",
            _human_size(result.size),
            "confirmado por hash de conteúdo" if result.verified else "heurística nome/tamanho, sem garantia",
        )

        t_stage = time.monotonic()
        logger.info("[%s] [2/4] Analisando faixas de legenda (ffprobe)...", key)
        stream_idx = await ffmpeg_utils.probe_text_subtitle(result.url)
        logger.info(
            "[%s] [2/4] Faixa de legenda escolhida (índice %s) em %.1fs.",
            key,
            stream_idx,
            time.monotonic() - t_stage,
        )

        t_stage = time.monotonic()
        logger.info(
            "[%s] [3/4] Extraindo legenda com ffmpeg (pode demorar minutos em "
            "arquivos grandes)...",
            key,
        )
        await ffmpeg_utils.extract_subtitle(
            result.url, stream_idx, str(raw_path), log_prefix=key
        )
        logger.info(
            "[%s] [3/4] Legenda extraída em %.1fs.", key, time.monotonic() - t_stage
        )

        t_stage = time.monotonic()
        logger.info("[%s] [4/4] Traduzindo para PT-BR via Gemini...", key)
        await translator.translate_srt(str(raw_path), str(tmp_path))
        logger.info(
            "[%s] [4/4] Tradução concluída em %.1fs.", key, time.monotonic() - t_stage
        )

        os.replace(tmp_path, final_path)  # publicação atômica
        logger.info(
            "[%s] Legenda pronta em cache. Tempo total: %.1fs.",
            key,
            time.monotonic() - t0,
        )
    except NoEmbeddedSubtitle:
        logger.info(
            "[%s] Arquivo não possui legenda textual embutida (%.1fs).",
            key,
            time.monotonic() - t0,
        )
    except Exception as exc:  # noqa: BLE001 - job em background não pode derrubar o server
        logger.error(
            "[%s] Falha no pipeline após %.1fs: %s", key, time.monotonic() - t0, exc
        )
    finally:
        for p in (raw_path, tmp_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        IN_FLIGHT.discard(key)


def _subtitle_response(key: str) -> dict:
    return {
        "subtitles": [
            {
                "id": key,
                "url": f"{config.BASE_URL}/{config.ADDON_TOKEN}/cache/{key}.srt",
                "lang": "pob",  # código do Stremio para português do Brasil
            }
        ]
    }


async def _handle_subtitles(
    sub_type: str, sub_id: str, extra: str, request: Request
) -> dict:
    query = dict(request.query_params)
    logger.info(
        "[RAW] %s %s | extra_path=%r query_string=%r headers=%s",
        request.method,
        request.url.path,
        extra,
        query,
        dict(request.headers),
    )

    meta = _parse_extra(extra)
    # Alguns players mandam filename/videoSize/videoHash como query string em vez
    # de no segmento de path `extra` do protocolo padrão do Stremio — aceita os dois.
    for k, v in query.items():
        meta.setdefault(k, v)

    filename = meta.get("filename")
    video_size_raw = meta.get("videoSize")
    video_size = int(video_size_raw) if (video_size_raw or "").isdigit() else None
    video_hash = meta.get("videoHash") or None

    key = _cache_key(sub_id, filename, video_size_raw)
    final_path = config.CACHE_DIR / f"{key}.srt"

    if final_path.exists():
        logger.info("[%s] cache hit — servindo legenda já traduzida.", key)
        return _subtitle_response(key)

    if key in IN_FLIGHT:
        logger.info(
            "[%s] job já em andamento — esta requisição NÃO reinicia o pipeline "
            "(o player costuma repetir o pedido; aguarde o log de progresso acima).",
            key,
        )
        return {"subtitles": []}

    IN_FLIGHT.add(key)
    if not filename and not video_size and not video_hash:
        logger.warning(
            "[%s] Player não enviou filename/videoSize/videoHash — tentando casar "
            "pelo título (via Cinemeta) + episódio; se isso falhar, cai no último "
            "recurso (maior vídeo de toda a conta TorBox, baixíssima confiança).",
            key,
        )
    logger.info(
        "[%s] Novo pedido de legenda — sub_id=%s filename=%s videoSize=%s videoHash=%s.",
        key,
        sub_id,
        filename,
        video_size,
        "sim" if video_hash else "não",
    )
    asyncio.create_task(
        _pipeline(key, filename, video_size, video_hash, sub_id, sub_type)
    )

    # Prime: responde vazio agora; a legenda aparece ao reabrir o menu de legendas.
    return {"subtitles": []}


@app.get("/{token}/manifest.json")
async def manifest(token: str) -> dict:
    _check_token(token)
    return MANIFEST


@app.get("/{token}/subtitles/{sub_type}/{sub_id}/{extra:path}")
async def subtitles_with_extra(
    token: str, sub_type: str, sub_id: str, extra: str, request: Request
) -> dict:
    _check_token(token)
    return await _handle_subtitles(sub_type, sub_id, extra, request)


@app.get("/{token}/subtitles/{sub_type}/{sub_id}.json")
async def subtitles_plain(
    token: str, sub_type: str, sub_id: str, request: Request
) -> dict:
    _check_token(token)
    return await _handle_subtitles(sub_type, sub_id, "", request)


@app.get("/{token}/cache/{filename}")
async def serve_cache_file(token: str, filename: str) -> FileResponse:
    _check_token(token)
    safe_name = os.path.basename(filename)  # evita path traversal
    path = config.CACHE_DIR / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="text/plain; charset=utf-8")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "in_flight": len(IN_FLIGHT)}


@app.exception_handler(StarletteHTTPException)
async def _log_404(request: Request, exc: StarletteHTTPException):
    # Loga requisições que não bateram com NENHUMA rota (token errado, ou o
    # player chamando um formato de URL diferente do esperado) — útil pra ver
    # exatamente o que está chegando quando nada parece funcionar.
    if exc.status_code == 404:
        logger.warning(
            "[404] %s %s%s headers=%s",
            request.method,
            request.url.path,
            f"?{request.url.query}" if request.url.query else "",
            dict(request.headers),
        )
    return await http_exception_handler(request, exc)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=config.PORT)
