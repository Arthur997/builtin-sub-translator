"""Extração da primeira legenda TEXTUAL embutida via FFmpeg/FFprobe.

Fazemos `ffprobe` primeiro para escolher uma faixa baseada em texto (subrip/ass/…)
e ignorar faixas image-based (PGS/VOBSUB), que não convertem para SRT sem OCR.
Preferimos inglês como idioma de origem.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger("ffmpeg")

# Codecs de legenda baseados em texto (conversíveis para SRT).
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
# Codecs baseados em imagem (exigiriam OCR — ignorados).
IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}

ENGLISH_LANGS = {"eng", "en", "english"}

_PROBE_TIMEOUT = 90
_EXTRACT_TIMEOUT = 600  # legendas em MKV exigem varrer quase o arquivo inteiro


class NoEmbeddedSubtitle(Exception):
    """Nenhuma faixa de legenda textual embutida foi encontrada."""


class FFmpegError(Exception):
    """Falha genérica ao invocar ffmpeg/ffprobe."""


# Flags de robustez para leitura HTTP (reconexão em quedas de conexão).
_HTTP_FLAGS = [
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_delay_max", "5",
]


async def _run(cmd: list[str], timeout: int) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise FFmpegError(f"Timeout ({timeout}s) executando {cmd[0]}.") from exc
    return proc.returncode or 0, stdout, stderr


async def probe_text_subtitle(url: str) -> int:
    """Retorna o índice (0-based dentro das faixas de legenda) da melhor faixa textual.

    O índice retornado é o `index` relativo às legendas, usado como `-map 0:s:<idx>`.
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-select_streams", "s",
        *_HTTP_FLAGS,
        "-i", url,
    ]
    code, stdout, stderr = await _run(cmd, _PROBE_TIMEOUT)
    if code != 0:
        raise FFmpegError(f"ffprobe falhou: {stderr.decode(errors='ignore')[:400]}")

    try:
        streams = json.loads(stdout or b"{}").get("streams", [])
    except json.JSONDecodeError as exc:
        raise FFmpegError("ffprobe retornou JSON inválido.") from exc

    text_tracks: list[tuple[int, str]] = []  # (índice_relativo_de_legenda, lang)
    for sub_idx, s in enumerate(streams):
        codec = (s.get("codec_name") or "").lower()
        if codec in IMAGE_SUB_CODECS:
            continue
        if codec in TEXT_SUB_CODECS:
            lang = (s.get("tags", {}) or {}).get("language", "").lower()
            text_tracks.append((sub_idx, lang))

    if not text_tracks:
        raise NoEmbeddedSubtitle("Sem faixa de legenda textual embutida.")

    # Preferir inglês; senão a primeira faixa textual.
    for sub_idx, lang in text_tracks:
        if lang in ENGLISH_LANGS:
            return sub_idx
    return text_tracks[0][0]


async def extract_subtitle(url: str, sub_stream_index: int, out_path: str) -> None:
    """Extrai a faixa `-map 0:s:<idx>` diretamente para SRT em `out_path`."""
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        *_HTTP_FLAGS,
        "-i", url,
        "-map", f"0:s:{sub_stream_index}",
        "-c:s", "srt",
        "-f", "srt",
        out_path,
    ]
    code, _stdout, stderr = await _run(cmd, _EXTRACT_TIMEOUT)
    if code != 0:
        err = stderr.decode(errors="ignore")[:400]
        raise FFmpegError(f"ffmpeg falhou ao extrair legenda: {err}")
