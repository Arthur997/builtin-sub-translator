"""Configuração central do addon, carregada de variáveis de ambiente / .env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TORBOX_API_KEY: str = os.getenv("TORBOX_API_KEY", "").strip()
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()

# URL pela qual o dispositivo do player alcança este servidor (sem barra final).
BASE_URL: str = os.getenv("BASE_URL", "http://127.0.0.1:7000").rstrip("/")

# Diretório onde os .srt traduzidos são persistidos.
CACHE_DIR: Path = Path(os.getenv("CACHE_DIR", "cache")).resolve()

PORT: int = int(os.getenv("PORT", "7000"))

GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Token secreto no caminho da URL (ex.: /{ADDON_TOKEN}/manifest.json). Protege o
# addon quando exposto à internet pública — sem ele, TODAS as rotas retornam 404.
ADDON_TOKEN: str = os.getenv("ADDON_TOKEN", "").strip()

# Base da API do TorBox.
TORBOX_BASE_URL: str = "https://api.torbox.app/v1"

# Garante que o diretório de cache exista já na importação.
CACHE_DIR.mkdir(parents=True, exist_ok=True)
