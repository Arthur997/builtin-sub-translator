# Stremio Translator Addon (TorBox → FFmpeg → Gemini)

Addon de **legendas** para Stremio/Nuvio que, para um vídeo servido via **TorBox
debrid**, extrai a legenda **embutida** do arquivo, traduz para **PT-BR** com o
**Gemini Flash** e devolve o `.srt` ao player.

## Como funciona

1. O player pede legendas (`/{ADDON_TOKEN}/subtitles/movie/tt123/filename=...&videoSize=...&videoHash=...json`).
2. O addon localiza candidatos na sua conta TorBox por `filename`/`videoSize`
   (o Stremio **não** envia o infohash do torrent). Quando o player manda
   `videoHash` (hash do OpenSubtitles), o addon **confirma** o candidato lendo
   64KB do início e do fim do arquivo real (HTTP Range) e comparando o hash —
   garantia por conteúdo, não só heurística de nome. Sem `videoHash`, fica só
   na heurística (pode errar se houver arquivos ambíguos na `mylist`).
3. `ffprobe` escolhe a primeira faixa de legenda **textual** (ignora PGS/VOBSUB);
   `ffmpeg` a extrai para SRT.
4. O SRT é traduzido bloco a bloco pelo Gemini (só o texto viaja; timestamps
   intactos) e gravado em `cache/`.
5. **Entrega background + prime:** a 1ª requisição dispara a tradução e responde
   vazio; ao reabrir o menu de legendas (~1–2 min depois) a legenda PT-BR aparece.
   A partir daí é cache instantâneo.

## Requisitos importantes

- O torrent **precisa já estar na sua `mylist` do TorBox** (o addon de streaming
  que você usa deve ter adicionado ao iniciar a reprodução).
- `BASE_URL` deve ser alcançável **pelo dispositivo do player** — não use
  `localhost` se assistir noutro aparelho (use IP da LAN ou um túnel).
- **`ADDON_TOKEN` é obrigatório.** Toda rota (manifest, subtitles, cache) exige
  esse token como primeiro segmento da URL — sem ele configurado, ou com o
  token errado, tudo retorna 404. Gere um valor forte:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(24))"
  ```
  Isso é essencial se o addon for exposto à internet pública (ex.: via Tailscale
  Funnel) — sem token, qualquer um que descobrisse a URL poderia consumir sua
  cota do TorBox/Gemini.

## Rodando local

```bash
cp .env.example .env   # preencha TORBOX_API_KEY, GEMINI_API_KEY, BASE_URL, ADDON_TOKEN
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 7000
```

Instale no Stremio abrindo `http://SEU_BASE_URL/SEU_ADDON_TOKEN/manifest.json`.

## Docker

```bash
docker build -t stremio-translator .
docker run -d -p 7000:7000 --env-file .env -v "$PWD/cache:/app/cache" stremio-translator
```

## Deploy no home server (docker-compose)

```bash
git clone https://github.com/Arthur997/builtin-sub-translator.git
cd builtin-sub-translator
cp .env.example .env   # preencha TORBOX_API_KEY, GEMINI_API_KEY e BASE_URL
# BASE_URL deve ser o IP/host do home server + a PORTA DO HOST (docker-compose.yml
# mapeia 3044 -> 7000), ex: http://192.168.1.50:3044
docker compose up -d --build
```

O `docker-compose.yml` já expõe o addon na **porta 3044** do host (mapeada para a
7000 interna do container), para não colidir com outros serviços já rodando no
seu home server. Para usar outra porta, edite o `ports:` do `docker-compose.yml`
(`"SUA_PORTA:7000"`) e ajuste `BASE_URL` no `.env` de acordo.

Para atualizar depois de um `git pull`:

```bash
docker compose up -d --build
```

Logs: `docker compose logs -f`. Parar: `docker compose down` (o `cache/` fica no host, fora do container).

## Fora de escopo

- Legendas image-based (PGS/VOBSUB) → exigiriam OCR (tratadas como "sem legenda").
- Multiusuário / página `/configure` (este build é de uso pessoal, chaves via `.env`).
