# BX Audio Manager — Requisitos e Arquitetura

Organizador de acervo musical para Android/Termux.
Renomeia, move, tagueia e mantém catálogo SQLite com durabilidade
(POSIX atômico, fsync, journal hash-chain, lock flock).

## Versão mínima
- Python >= 3.11
- ffmpeg >= 4.0 (encoder aac, filtro loudnorm)
- ffprobe >= 4.0 (incluso no pacote ffmpeg)
- Opcional: termux-api (MediaStore scan), yt-dlp (downloads)

## Instalação Termux (do zero, em ~/.bx_manager)

    pkg update && pkg upgrade
    pkg install python ffmpeg termux-api curl
    termux-setup-storage
    # ^ roda so uma vez

A partir do clone do repositorio:

    git clone https://github.com/TiaoGrande/bx-audio-manager.git ~/.bx_manager
    cd ~/.bx_manager
    pip install -r requirements.txt   # opcional (so charset-normalizer)
    python3 bx.py teste               # 519/519 esperados

## Suite de testes

    python3 bx.py teste

Politica PASS/FAIL/SKIP:
- PASS — comportamento correto verificado.
- FAIL — regressao. Exit code = 1.
- SKIP — ferramenta externa ausente (nao quebra).

Regras rigidas:
- Ferramenta AUSENTE -> SKIP.
- Ferramenta PRESENTE com versao < minimo -> FAIL.
- Ferramenta PRESENTE sem capability (aac, loudnorm) -> FAIL.

bx teste retorna exit 0 apenas se nenhum modulo tiver FAIL.

## Contratos entre modulos
- bx_kernel.py — constantes e helpers puros (sem I/O, sem DB, sem subprocess)
- bx_io.py — I/O atomico, SHA256, fsync, journal, manifestos, erros
- bx_media.py — probe, cache, tags, AAC, ReplayGain
- bx_core.py — fundacao (Config, DB, runtime); re-exporta kernel/io/media
- bx_nmx.py -> bx_core
- bx_crx.py -> bx_core, bx_nmx
- bx_fix.py -> bx_core, bx_nmx, bx_crx
- bx_get.py -> bx_core, bx_nmx, bx_fix
- bx_dbx.py -> bx_core
- bx.py — orquestrador via subprocess

## Fila de downloads
Vive em fila_downloads (SQLite WAL). O arquivo legado
fila_downloads.txt e migrado na primeira execucao e renomeado .bak.

## Manifesto
18 campos canonicos CHAVE:valor (versao 3), incluindo REPLAYGAIN e
TRAVADO. Estados validos em bx_kernel.ESTADOS_MANIFESTO:
COMPLETO, LETRADO, LETRA ESTATICA, SEM LETRA,
RESGATADO LETRA ESTATICA, RESGATADO LETRADO, RESGATADO, PRESERVADO.

## Versao
0.23.0 (ver pyproject.toml e bx.py).
