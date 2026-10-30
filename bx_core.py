#!/usr/bin/env python3
"""
bx_core.py — Núcleo unificado do BX Audio Manager (Atualização 02).

Camadas:
  1. Constantes e imports
  2. Config (dataclass, textos, diretórios, logging)
  3. Banco SQLite (WAL, catálogo 16 campos, links, caches)
  4. Arquivos atômicos (.bxpart + SHA256 + fsync), diário, manifestos (18 campos)
  5. Runtime (flock, wake-lock, sinais, media scan, temporários)
  6. Probe/ffprobe, cache, tags, AAC (libfdk_aac→aac), ReplayGain
"""
from __future__ import annotations

import atexit
import fcntl
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

# --- Re-exports dos modulos resultantes do split (Bloco 2) ---
import bx_io as _bx_io
import bx_media as _bx_media

from bx_kernel import (  # noqa: F401
    D_ART, D_SING, D_DESC, D_INT, D_QUAR, D_LOGS, D_SUBST, D_IDX, D_REV,
    D_LETRAS, D_LINKS, D_Z, D_Y, PASTAS_BLINDADAS,
    MIN_ART, MAX_MIN, TIMEOUT, RETRY_DIAS, MAX_PAR, LOTE_MB, LOTE_N, LOTE_SQL,
    SUF_DEFAULT, SEP_DEFAULT, SUFIXO_PARCIAL, RAIZ_PADRAO,
    SUFIXOS_PADRAO, GRUPOS_PADRAO, SELOS_PADRAO, NOSPLIT_PADRAO,
    CAMPOS, N_CAMPOS, I_TAM, I_KHZ, I_BITRATE, I_CODEC, I_PRODUTOR,
    I_REPLAYGAIN, I_ORIGEM_MORTA, I_NULOS, MAX_TRAD, COLUNAS_PERMITIDAS, SQL_INS,
    MANIFESTO_CAMPOS, MANIFESTO_VERSAO, ISO639_2, ESTADOS_MANIFESTO,
    PADROES_TEMP, PRODUTOR_RE,
    iso639_2, _fix_mojibake, _limpo, _normalizar, _linha_para_pipe,
    normalizar_unicode, extrair_produtor,
)

from bx_io import (  # noqa: F401
    tam, sha256, log_erro, _destino_proibido, _fsync, _fsync_dir,
    escrever_atomico, escrever_bytes_atomico, enviar_sd,
    diario_add, diario_rotar, historico_append, auditar_link_http,
    _rename_case_insensitive, mover,
    journal_registrar, journal_listar, journal_verify,
    erro_registrar, erro_listar, erro_remover, erro_limpar_antigos,
    montar_manifesto, achar_manifesto, rm_manifestos,
    mget, manif_campo, manif_caminho, validar_manifesto,
    espaco_ok, _st_mtime, _formato_score, _vencedor_dup,
    mover_para_substituidos,
)

from bx_media import (  # noqa: F401
    ProbeInfo, ProbeCacheDB,
    abrir_cache, fechar_cache, limpar_cache,
    _chave_cache, _info_from_dict, _cache_get, _cache_put,
    probe_raw, _parse_probe, probe,
    _fork_ctx, _probe_worker, precache_batch,
    dur, dur_float, compat_check, pode_tagear,
    detectar_encoder_aac, transcodificar_aac,
    _duracao_ffprobe, calcular_replaygain, gravar_tags,
    probe_cache_limpar_antigos,
    _MEM_CACHE, _CACHE_LOCK, _CACHE_MAX,
)


# ============================================================================
# 2. CONFIG
# ============================================================================

@dataclass(frozen=True)
class Config:
    """Configuracao imutavel (v0.23 item 11).

    Paths derivados de bx_home/raiz_sd sao @property (recomputados a
    cada acesso, custo desprezivel). Mutacao via helpers com_*() que
    retornam NOVO Config (dataclasses.replace). Nunca `cfg.x = y`.
    """
    bx_home: Path
    raiz_sd: Path
    modo: str = "sim"
    forcar: bool = False
    serial: bool = False
    net: bool = False
    api_off: bool = False
    replaygain_on: bool = True

    sufixos: tuple[str, ...] = ()
    grupos: tuple[str, ...] = ()
    selos: tuple[str, ...] = ()
    nosplit: tuple[str, ...] = ()
    internacionais: tuple[str, ...] = ()
    nacionais: tuple[str, ...] = ()
    equiv: dict[str, str] = field(default_factory=dict, compare=False, repr=False)
    sep: str = SEP_DEFAULT
    suf_re: str = SUF_DEFAULT
    hoje: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d"))

    _db_override: Optional[Path] = field(default=None, repr=False, compare=False)

    # --- Paths derivados (imutaveis) ---
    @property
    def cfg_dir(self) -> Path: return self.bx_home / "config"
    @property
    def tmp_dir(self) -> Path: return self.bx_home / "temp_bx"
    @property
    def log_dir(self) -> Path: return self.bx_home / "logs"
    @property
    def bkp_dir(self) -> Path: return self.bx_home / "backups"
    @property
    def bin_dir(self) -> Path: return self.bx_home / "bin"
    @property
    def ytdlp(self) -> Path: return self.bin_dir / "yt-dlp"
    @property
    def db_path(self) -> Path:
        if self._db_override is not None:
            return self._db_override
        return self.bx_home / "biblioteca.db"
    @property
    def diario(self) -> Path: return self.bx_home / "diario_renomeacoes.log"
    @property
    def pend(self) -> Path: return self.bx_home / "pendentes.txt"
    @property
    def fila(self) -> Path: return self.bx_home / "fila_downloads.txt"
    @property
    def lock(self) -> Path: return self.bx_home / "bx.lock"
    @property
    def quar(self) -> Path: return self.raiz_sd / D_QUAR
    @property
    def subst(self) -> Path: return self.raiz_sd / D_SUBST
    @property
    def desc(self) -> Path: return self.raiz_sd / D_DESC
    @property
    def idx(self) -> Path: return self.raiz_sd / D_IDX
    @property
    def rev(self) -> Path: return self.raiz_sd / D_REV
    @property
    def letras(self) -> Path: return self.raiz_sd / D_LETRAS
    @property
    def links(self) -> Path: return self.raiz_sd / D_LINKS
    @property
    def cat_sd(self) -> Path: return self.raiz_sd / D_LOGS / "backup_catalogo.csv"
    @property
    def log_erros(self) -> Path: return self.log_dir / f"erros_{self.hoje}.log"
    @property
    def log_auto(self) -> Path: return self.log_dir / f"auto_decisoes_{self.hoje}.log"
    @property
    def rel_arq(self) -> Path: return self.log_dir / f"relatorio_{self.hoje}.txt"

    # --- Helpers imutaveis ---
    def com(self, **kwargs) -> "Config":
        return replace(self, **kwargs)
    def com_modo(self, modo: str) -> "Config":
        return replace(self, modo=modo)
    def com_forcar(self, forcar: bool = True) -> "Config":
        return replace(self, forcar=forcar)
    def com_serial(self, serial: bool = True) -> "Config":
        return replace(self, serial=serial)
    def com_net(self, net: bool = True) -> "Config":
        return replace(self, net=net)
    def com_db(self, path: Optional[Path]) -> "Config":
        return replace(self,
                       _db_override=Path(path) if path is not None else None)


# --- Leitura de configs ---
def _ler_lista(arquivo: Path) -> list[str]:
    if not arquivo.is_file():
        return []
    r: list[str] = []
    with arquivo.open("r", encoding="utf-8", errors="replace") as f:
        for linha in f:
            linha = linha.rstrip("\r\n")
            if not linha.strip() or linha.lstrip().startswith("#"):
                continue
            r.append(linha)
    return r


def _ler_equiv(arquivo: Path) -> dict[str, str]:
    r: dict[str, str] = {}
    for linha in _ler_lista(arquivo):
        p = linha.split("|", 1)
        if len(p) != 2:
            continue
        e, c = p[0].strip().lower(), p[1].strip()
        if e and c:
            r[e] = c
    return r


_EQUIV_SUGESTOES_TEXTO = """\
# Sugestoes geradas a partir do acervo real (v0.23).
# NAO sao aplicadas automaticamente. Copie as linhas uteis para equiv.txt.
# Formato: ERRADO|CERTO. Use ; para multiplos artistas.

# Leetspeak (nome proprio em outra grafia)
post malon3|Post Malone
billie eili5h|Billie Eilish
al3x|Al3x
sierr4|Sierr4
hailee steinf3ld|Hailee Steinfeld
issam alnajj4r|Issam Alnajjar
lil pe3p|Lil Peep
xxxtentati0n|XXXTENTACION
p0wfu|Powfu

# Aliases
baco exu|Baco Exu do Blues
d$ luqi|D$ Luqi
ds luqi|D$ Luqi
touno|Tuono
llowzzy|Ilowzzy
k a m a i t a c h i|Kamaitachi
p3dr1n|P3DR1N
kamaitach|Kamaitachi

# Feat composto (quebra via ' e ')
baco exu do blues e péricles|Baco Exu do Blues; Péricles
kavinn e jeovanna beatriz|Kavinn; Jeovanna Beatriz
"""


def _construir_suf_re(sufixos: list[str]) -> str:
    if not sufixos:
        return SUF_DEFAULT
    padrao = "|".join(re.escape(s) for s in sufixos if s.strip())
    if not padrao:
        return SUF_DEFAULT
    return f"M4A_[0-9]+K?|MP3_[0-9]+K?|MP4_[0-9]+K?|{padrao}"


def _textos_from_disk(cfg_dir: Path) -> dict:
    sufixos = _ler_lista(cfg_dir / "sufixos.txt")
    grupos = _ler_lista(cfg_dir / "grupos.txt")
    selos = _ler_lista(cfg_dir / "selos.txt")
    nosplit = _ler_lista(cfg_dir / "nosplit.txt")
    internacionais = _ler_lista(cfg_dir / "internacionais.txt")
    nacionais = _ler_lista(cfg_dir / "nacionais.txt")
    equiv = _ler_equiv(cfg_dir / "equiv.txt")
    suf_re = _construir_suf_re(sufixos)
    sep_linhas = _ler_lista(cfg_dir / "sep.txt")
    sep = (sep_linhas[0].strip() if sep_linhas else SEP_DEFAULT) or SEP_DEFAULT
    return dict(
        sufixos=tuple(sufixos), grupos=tuple(grupos), selos=tuple(selos),
        nosplit=tuple(nosplit), internacionais=tuple(internacionais),
        nacionais=tuple(nacionais), equiv=equiv, suf_re=suf_re, sep=sep,
    )


def carregar_textos(cfg: Config) -> Config:
    """Le textos de disco. Retorna NOVO Config (v0.23: puro)."""
    return replace(cfg, **_textos_from_disk(cfg.cfg_dir))


def _escrever_se_faltar(arquivo: Path, conteudo: str) -> None:
    if arquivo.is_file():
        return
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text(conteudo, encoding="utf-8")


def _gerar_equiv_sugestoes(cfg: Config) -> None:
    """Gera equiv.sugestoes.txt (nao toca equiv.txt) - v0.23."""
    sugestoes_arq = cfg.cfg_dir / "equiv.sugestoes.txt"
    if sugestoes_arq.is_file():
        return
    try:
        sugestoes_arq.write_text(_EQUIV_SUGESTOES_TEXTO, encoding="utf-8")
    except OSError:
        pass


def migrar_z_antigo(cfg: Config) -> None:
    if not cfg.raiz_sd.is_dir():
        return
    mapa = [
        ("z_desconhecidos", D_DESC),
        ("z_área de transferência", D_QUAR),
        ("z_logs", D_LOGS),
        ("z_substituídos", D_SUBST),
        ("z_indice_artistas", D_IDX),
        ("z_revisao", D_REV),
        ("z_links", D_LINKS),
    ]
    for antigo_nome, novo_nome in mapa:
        antigo = cfg.raiz_sd / antigo_nome
        novo = cfg.raiz_sd / novo_nome
        if not antigo.is_dir() or antigo == novo:
            continue
        if novo.is_dir():
            for item in list(antigo.iterdir()):
                alvo = novo / item.name
                if alvo.exists():
                    continue
                try:
                    shutil.move(str(item), str(alvo))
                except OSError:
                    pass
            # Item L-FIX: rmdir so se ficou vazia. Arquivos duplicados
            # residuais em `novo` impedem rmdir -- nao e erro, e normal.
            try:
                if not any(antigo.iterdir()):
                    antigo.rmdir()
            except OSError as _e:
                logging.getLogger(__name__).info(
                    "migrar_z_antigo: rmdir(%s) falhou: %s", antigo, _e)
        else:
            try:
                antigo.rename(novo)
            except OSError as _e:
                logging.getLogger(__name__).info(
                    "migrar_z_antigo: rename(%s -> %s) falhou: %s",
                    antigo, novo, _e)


def criar_estrutura_sd(cfg: Config) -> None:
    raiz = cfg.raiz_sd
    for sub in (D_ART, D_SING, D_DESC, D_QUAR, D_LOGS, D_SUBST,
                D_INT, D_IDX, D_REV, D_LETRAS, D_LINKS, D_Z, D_Y):
        (raiz / sub).mkdir(parents=True, exist_ok=True)
    for letra in list("abcdefghijklmnopqrstuvwxyz") + ["#"]:
        (raiz / D_SING / letra).mkdir(parents=True, exist_ok=True)
    for sub in (D_LOGS, D_IDX, D_REV, D_LETRAS, D_LINKS, D_QUAR, D_SUBST, D_DESC):
        n = raiz / sub / ".nomedia"
        if not n.exists():
            n.write_text("", encoding="utf-8")


def init_dirs(cfg: Config) -> None:
    # Fix 10 (v2): arquivos criados por nos nascem 0600/0700.
    _prev = os.umask(0o077)
    try:
        _init_dirs_impl(cfg)
    finally:
        os.umask(_prev)


def _init_dirs_impl(cfg: Config) -> None:
    for d in (cfg.cfg_dir, cfg.tmp_dir, cfg.log_dir, cfg.bkp_dir, cfg.bin_dir):
        d.mkdir(parents=True, exist_ok=True)
    n = cfg.tmp_dir / ".nomedia"
    if not n.exists():
        n.write_text("", encoding="utf-8")
    for p in (cfg.diario, cfg.pend, cfg.fila):
        if not p.exists():
            p.write_text("", encoding="utf-8")
    _escrever_se_faltar(
        cfg.cfg_dir / "sufixos.txt",
        "# Sufixos lixo removidos entre ( ) ou [ ]. Um por linha.\n"
        + "\n".join(SUFIXOS_PADRAO) + "\n")
    _escrever_se_faltar(cfg.cfg_dir / "grupos.txt",
        "# Um grupo por linha.\n" + "\n".join(GRUPOS_PADRAO) + "\n")
    _escrever_se_faltar(cfg.cfg_dir / "selos.txt",
        "# Selos/canais (um por linha).\n" + "\n".join(SELOS_PADRAO) + "\n")
    _escrever_se_faltar(cfg.cfg_dir / "nosplit.txt",
        "# Nomes nunca divididos.\n" + "\n".join(NOSPLIT_PADRAO) + "\n")
    _escrever_se_faltar(cfg.cfg_dir / "equiv.txt",
        "# ERRADO|CERTO (artista).\n")
    _escrever_se_faltar(cfg.cfg_dir / "sep.txt", "# Separador de artistas.\n;\n")
    _escrever_se_faltar(cfg.cfg_dir / "internacionais.txt",
        "# Artistas forçados para Internacionais/.\n")
    _escrever_se_faltar(cfg.cfg_dir / "nacionais.txt",
        "# Artistas forçados como nacionais.\n")
    _escrever_se_faltar(cfg.cfg_dir / "raiz.txt",
        "# Pasta raiz do acervo.\n")
    _escrever_se_faltar(cfg.cfg_dir / "urls.txt",
        "# Links do YouTube (um por linha).\n")
    _gerar_equiv_sugestoes(cfg)
    # Fix 5 (v2): limpar logs diarios > 90 dias.
    _limite_log = time.time() - 90 * 86400
    for _pat in ("erros_*.log", "auto_decisoes_*.log"):
        try:
            for _p in cfg.log_dir.glob(_pat):
                try:
                    if _p.stat().st_mtime < _limite_log:
                        _p.unlink()
                except OSError:
                    pass
        except OSError:
            pass
    if cfg.modo == "apply" and cfg.raiz_sd.is_dir():
        try:
            _legados = [d.name for d in cfg.raiz_sd.iterdir()
                        if d.is_dir() and d.name.startswith("z_")]
        except OSError:
            _legados = []
        if _legados:
            logging.getLogger(__name__).warning(
                "Pastas legadas detectadas: %s. "
                "Rode 'bx fix migrar' para migrar.", _legados)
        criar_estrutura_sd(cfg)


def setup_logging(cfg: Config, nivel: int = logging.INFO) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(nivel)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(
        cfg.log_dir / "bx.log", maxBytes=5 * 1024 * 1024, backupCount=3,
        encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(ch)


def resolver_bx_home() -> Path:
    env = os.environ.get("BX_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".bx_manager"


def resolver_raiz(arg: Optional[str]) -> Path:
    if arg:
        return Path(arg).expanduser()
    env = os.environ.get("BX_RAIZ", "").strip()
    if env:
        return Path(env).expanduser()
    raiz_file = resolver_bx_home() / "config" / "raiz.txt"
    if raiz_file.is_file():
        for linha in raiz_file.read_text(encoding="utf-8", errors="replace").splitlines():
            linha = linha.strip()
            if linha and not linha.startswith("#"):
                return Path(linha).expanduser()
    return Path(RAIZ_PADRAO)


def extrair_raiz_arg(argv: list[str]) -> tuple[Optional[str], list[str]]:
    """Parser canonico de --raiz (unico ponto de verdade).

    Regra: ULTIMO --raiz vence (consistente com argparse).
    Remove tanto `--raiz X` quanto `--raiz=X` do argv retornado.
    """
    raiz: Optional[str] = None
    novo: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--raiz" and i + 1 < len(argv):
            raiz = argv[i + 1]; i += 2; continue
        if a.startswith("--raiz="):
            v = a.split("=", 1)[1]
            if v: raiz = v
            i += 1; continue
        novo.append(a); i += 1
    return (raiz, novo)


def carregar_config(raiz_arg=None, modo="sim", forcar=False, serial=False,
                    upd=True, net=False, replaygain_on=True) -> Config:
    cfg = Config(
        bx_home=resolver_bx_home(),
        raiz_sd=resolver_raiz(raiz_arg),
        modo=modo, forcar=forcar, serial=serial, net=net,
        replaygain_on=replaygain_on,
    )
    init_dirs(cfg)
    return carregar_textos(cfg)


# ============================================================================
# 3. BANCO SQLITE — 16 campos + migração
# ============================================================================

def conectar(db_path: str | Path, thread_safe: bool = False) -> sqlite3.Connection:
    con = sqlite3.connect(
        str(db_path), timeout=30.0, isolation_level=None,
        check_same_thread=not thread_safe)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("PRAGMA busy_timeout=30000")
    _criar_schema(con)
    _migrar_schema(con)
    return con


def _criar_schema(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS catalogo (
            id         TEXT PRIMARY KEY,
            artista    TEXT,
            titulo     TEXT,
            estado     TEXT,
            rel        TEXT,
            tam        INTEGER,
            data       TEXT,
            sha        TEXT,
            idioma     TEXT,
            khz        REAL,
            tipo_letra TEXT,
            bitrate    INTEGER,
            tem_capa   TEXT,
            codec      TEXT,
            produtor   TEXT,
            replaygain REAL,
            origem_morta TEXT DEFAULT 'N'
        );
        CREATE INDEX IF NOT EXISTS idx_catalogo_rel     ON catalogo(rel);
        CREATE INDEX IF NOT EXISTS idx_catalogo_artista ON catalogo(artista);
        CREATE INDEX IF NOT EXISTS idx_catalogo_titulo  ON catalogo(titulo);
        CREATE INDEX IF NOT EXISTS idx_catalogo_estado  ON catalogo(estado);

        CREATE TABLE IF NOT EXISTS links_youtube (
            video_id    TEXT PRIMARY KEY,
            catalogo_id TEXT,
            FOREIGN KEY (catalogo_id) REFERENCES catalogo(id)
        );
        CREATE INDEX IF NOT EXISTS idx_links_catalogo ON links_youtube(catalogo_id);

        CREATE TABLE IF NOT EXISTS traducao_cache (
            hash_verso     TEXT PRIMARY KEY,
            original       TEXT NOT NULL,
            traduzido      TEXT NOT NULL,
            idioma_origem  TEXT NOT NULL,
            idioma_destino TEXT NOT NULL,
            data           TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS probe_cache (
            chave   TEXT PRIMARY KEY,
            tamanho INTEGER NOT NULL,
            mtime   REAL    NOT NULL,
            json    TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_trad_data ON traducao_cache(data);

        CREATE TABLE IF NOT EXISTS schema_version (
            versao      INTEGER PRIMARY KEY,
            aplicada_em TEXT
        );

        CREATE TABLE IF NOT EXISTS fila_downloads (
            video_id    TEXT PRIMARY KEY,
            estado      TEXT NOT NULL DEFAULT 'pendente',
            url         TEXT,
            tent        INTEGER NOT NULL DEFAULT 0,
            ultima_ts   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_fila_estado ON fila_downloads(estado);
    """)
    # Fix 8 (v2): 2=Atualizacao 02, 3=fila_downloads SQLite.
    for _v in (2, 3):
        try:
            con.execute(
                "INSERT OR IGNORE INTO schema_version(versao, aplicada_em) "
                "VALUES (?, datetime('now'))", (_v,))
        except sqlite3.Error:
            pass


def _migrar_schema(con: sqlite3.Connection) -> None:
    """ALTER TABLE aditivo (nunca destrói dados)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(catalogo)")}
    for col, tipo in (("codec", "TEXT"), ("produtor", "TEXT"),
                       ("replaygain", "REAL"),
                       ("origem_morta", "TEXT DEFAULT 'N'")):
        if col not in cols:
            try:
                con.execute(f"ALTER TABLE catalogo ADD COLUMN {col} {tipo}")
            except sqlite3.Error:
                pass


@contextmanager
def transacao(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    con.execute("BEGIN")
    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


@contextmanager
def sessao_db(cfg: Config) -> Iterator[sqlite3.Connection]:
    con = conectar(cfg.db_path)
    try:
        yield con
    finally:
        con.close()


def instalar_panic_hook(log_dir: Optional[Path] = None) -> None:
    """E1 - grava traceback completo antes de qualquer crash nao tratado."""
    import traceback as _tb

    def _hook(exc_type, exc_value, exc_tb):
        try:
            tbs = "".join(_tb.format_exception(exc_type, exc_value, exc_tb))
            ld = log_dir or (resolver_bx_home() / "logs")
            ld.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            p = ld / f"CRASH_CRITICO_{ts}.log"
            p.write_text(tbs, encoding="utf-8")
            sys.stderr.write(f"\n[PANICO] traceback em {p}\n")
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


def verificar_db(cfg: Config) -> dict:
    """X10 - PRAGMA quick_check + integrity_check para diagnostico."""
    if not cfg.db_path.is_file():
        return {"ok": False, "erro": "arquivo nao existe"}
    try:
        con = sqlite3.connect(str(cfg.db_path), timeout=15.0)
        try:
            quick = con.execute("PRAGMA quick_check").fetchone()
            integ = con.execute("PRAGMA integrity_check").fetchone()
            return {
                "ok": bool(quick and quick[0] == "ok"
                           and integ and integ[0] == "ok"),
                "quick_check": quick[0] if quick else "",
                "integrity_check": integ[0] if integ else "",
            }
        finally:
            con.close()
    except sqlite3.Error as e:
        return {"ok": False, "erro": str(e)}


# --- Normalização/format ---
# B12-bis: mapa de mojibake para casos onde o char não é encodable em
# cp1252/latin-1 (ex: Ł U+0141 que nunca aparece via corrupção real,
# mas pode vir de digitção manual do usuário).
_MOJIBAKE_MAP: dict[str, str] = {
    "Ã£": "ã", "Ã¡": "á", "Ã ": "à", "Ã¢": "â", "Ã¤": "ä",
    "Ã©": "é", "Ãª": "ê", "Ã¨": "è",
    "Ã­": "í", "Ã¬": "ì", "Ã®": "î",
    "Ã³": "ó", "Ã´": "ô", "Ã²": "ò", "Ãµ": "õ",
    "Ãº": "ú", "Ã¹": "ù", "Ã¼": "ü",
    "Ã§": "ç", "Ã±": "ñ",
    "Ã\x81": "Á", "Ã‰": "É", "Ã\x8d": "Í", "Ã\x8e": "Î",
    "Ã“": "Ó", "Ã”": "Ô", "Ã•": "Õ", "Ãš": "Ú", "Ãœ": "Ü", "Ã‡": "Ç",
}

def _fix_mojibake(s: str) -> str:
    """Tenta decodificar mojibake em ordem de robustez."""
    if "Ã" not in s and "Â" not in s:
        return s
    # Fix 6 (v2): charset_normalizer opcional (melhora deteccao
    # em textos nao-romanos mal-encodados como cp1252).
    try:
        from charset_normalizer import from_bytes as _fb
        try:
            _r = _fb(s.encode("latin-1", "ignore")).best()
            if _r and _r.encoding and _r.encoding.lower() not in (
                    "latin-1", "latin1", "cp1252", "windows-1252"):
                _out = str(_r)
                if _out and len(_out) <= len(s) + 2:
                    return _out
        except Exception:
            pass
    except ImportError:
        pass
    # Tentativa 1: cp1252 (mais chars que latin-1)
    try:
        fixed = s.encode("cp1252").decode("utf-8")
        if fixed and len(fixed) <= len(s) + 2:
            return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    # Tentativa 2: latin-1
    try:
        fixed = s.encode("latin-1").decode("utf-8")
        if fixed and len(fixed) <= len(s) + 2:
            return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    # Tentativa 3: substituição manual (para Ł, etc.)
    for k, v in _MOJIBAKE_MAP.items():
        s = s.replace(k, v)
    return s

def _limpo(v: object) -> str:
    """S6/S8 - remove Cc/Cf/Cs/Co, normaliza NFC, troca | por espaco."""
    if v is None:
        return ""
    s = str(v)
    if not s:
        return ""
    # B12-bis: corrige mojibake com fallback em cascata.
    s = _fix_mojibake(s)
    out = []
    for c in s:
        cat = unicodedata.category(c)
        if cat in ("Cc", "Cf", "Cs", "Co"):
            if c == "\t":
                out.append(" ")
            continue
        out.append(c)
    s = "".join(out)
    s = unicodedata.normalize("NFC", s)
    return s.replace("|", " ")


def _normalizar(campos: Sequence[object]) -> tuple[object, ...]:
    if len(campos) > N_CAMPOS:
        logging.getLogger(__name__).warning(
            "_normalizar: %d campos truncados para %d",
            len(campos) - N_CAMPOS, N_CAMPOS)
    r: list[object] = [(_limpo(x) if x is not None else "") for x in list(campos)[:N_CAMPOS]]
    r += [""] * (N_CAMPOS - len(r))
    for i in (I_TAM, I_BITRATE):
        if r[i] == "":
            r[i] = None
        else:
            try:
                r[i] = int(float(str(r[i])))
            except (ValueError, TypeError):
                r[i] = None
    for i in (I_KHZ, I_REPLAYGAIN):
        if r[i] == "":
            r[i] = None
        else:
            try:
                r[i] = float(str(r[i]))
            except (ValueError, TypeError):
                r[i] = None
    for i in I_NULOS:
        if r[i] == "":
            r[i] = None
    if not r[I_ORIGEM_MORTA]:
        r[I_ORIGEM_MORTA] = "N"
    return tuple(r)


def _linha_para_pipe(row: Sequence[object]) -> str:
    partes: list[str] = []
    for i, v in enumerate(row):
        if v is None:
            partes.append("")
        elif i in (I_KHZ, I_REPLAYGAIN):
            partes.append(f"{float(v):.1f}")
        else:
            partes.append(str(v))
    return "|".join(partes)


# --- Catálogo ---
def db_contar(con: sqlite3.Connection) -> int:
    return int(con.execute("SELECT COUNT(*) FROM catalogo").fetchone()[0])


def db_inserir(con: sqlite3.Connection, campos: Sequence[object]) -> None:
    # D8 - id vazio vira PK "" e faz INSERT OR REPLACE sobrescrever
    # silenciosamente outras linhas. Rejeita explicitamente.
    normal = _normalizar(campos)
    if not normal or not str(normal[0]).strip():
        raise ValueError("db_inserir: id vazio rejeitado (PK colidiria)")
    con.execute(SQL_INS, normal)


def db_dump(con: sqlite3.Connection, destino: Optional[Path] = None) -> str:
    linhas = [_linha_para_pipe(r) for r in con.execute("SELECT * FROM catalogo ORDER BY rowid")]
    txt = "".join(x + "\n" for x in linhas)
    if destino is not None:
        if not escrever_atomico(destino, txt):
            raise OSError(f"db_dump: falha ao gravar {destino}")
    return txt


def _importar_linhas(con: sqlite3.Connection, linhas: Iterable[str]) -> int:
    """Importa linhas pipe-separated. Aceita 13/16 campos (legado) e 17
    (atual). Item 7c: warning para legado sem rejeitar."""
    total = 0
    rejeitadas = 0
    legado = 0
    for linha in linhas:
        linha = linha.rstrip("\r\n")
        if not linha:
            continue
        n_pipes = linha.count("|")
        if n_pipes not in (12, 15, 16):
            rejeitadas += 1
            continue
        if n_pipes in (12, 15):
            legado += 1
        partes = linha.split("|")
        # D8 - linha sem id vira PK "" e colide; rejeita
        if not partes or not partes[0].strip():
            rejeitadas += 1
            continue
        con.execute(SQL_INS, _normalizar(partes))
        total += 1
    if legado:
        logging.getLogger(__name__).info(
            "_importar_linhas: %d linha(s) legado (13/16 campos) "
            "importada(s) com defaults", legado)
    # D11 - nao descarta em silencio: loga total de rejeitadas
    if rejeitadas:
        logging.getLogger(__name__).warning(
            "_importar_linhas: %d linha(s) rejeitada(s) por formato/id vazio",
            rejeitadas)
    return total


def db_boot_csv(con: sqlite3.Connection, csv_path: Path,
                cfg: "Config | None" = None) -> int:
    if db_contar(con) > 0 or not csv_path.is_file():
        return 0
    with transacao(con):
        with csv_path.open("r", encoding="utf-8", errors="replace") as f:
            total = _importar_linhas(con, f)
    logging.getLogger(__name__).info("boot: %d faixas de %s", total, csv_path.name)
    return total


def db_substituir_catalogo(con: sqlite3.Connection, linhas: Iterable[str],
                            cfg: "Config | None" = None) -> int:
    # A17: snapshot ANTES do DELETE. cfg.bkp_dir se fornecido (item 11).
    try:
        bkp = cfg.bkp_dir if cfg is not None else (resolver_bx_home() / "backups")
        bkp.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap = bkp / f"cat_pre_substituicao_{ts}.csv"
        atual = list(con.execute("SELECT * FROM catalogo ORDER BY rowid"))
        if atual:
            escrever_atomico(snap, "".join(
                _linha_para_pipe(r) + "\n" for r in atual))
    except Exception as _e:
        logging.getLogger(__name__).warning(
            "db_substituir_catalogo: backup falhou (%s); abortando", _e)
        return 0
    with transacao(con):
        con.execute("DELETE FROM catalogo")
        total = _importar_linhas(con, linhas)
    logging.getLogger(__name__).info("catálogo substituído: %d faixas", total)
    return total


def db_set_rel(con: sqlite3.Connection, id_: str, rel: str) -> None:
    con.execute("UPDATE catalogo SET rel=? WHERE id=?", (rel, id_))


def db_set_col(con: sqlite3.Connection, coluna: str, valor: str,
               id_: str, raiz_sd: Optional[Path] = None) -> bool:
    if coluna not in COLUNAS_PERMITIDAS:
        return False
    v: object = valor if valor != "" else None
    try:
        if coluna == "khz" and v is not None:
            v = float(str(v))
        elif coluna == "bitrate" and v is not None:
            v = int(float(str(v)))
        elif coluna == "replaygain" and v is not None:
            v = float(str(v))
    except ValueError:
        return False

    # Captura rel ANTES do UPDATE (para invalidar cache cirurgicamente)
    rel = ""
    try:
        row = con.execute("SELECT rel FROM catalogo WHERE id=?", (id_,)).fetchone()
        rel = row[0] if row and row[0] else ""
    except sqlite3.Error:
        rel = ""

    con.execute(f"UPDATE catalogo SET {coluna}=? WHERE id=?", (v, id_))

    chave_alvo = ""
    if rel:
        raiz = raiz_sd
        if raiz is None:
            try:
                raiz = resolver_raiz(None)
            except Exception:
                raiz = None
        if raiz is not None:
            try:
                arq = Path(raiz) / rel
                if arq.is_file():
                    chave_alvo, _, _ = _chave_cache(arq)
            except (OSError, ValueError):
                chave_alvo = ""

    try:
        with _CACHE_LOCK:
            if chave_alvo:
                # Invalidação cirúrgica: só a chave do arquivo do id afetado
                for _k in [k for k in _MEM_CACHE if k[0] == chave_alvo]:
                    del _MEM_CACHE[_k]
                try:
                    con.execute("DELETE FROM probe_cache WHERE chave=?", (chave_alvo,))
                except sqlite3.Error:
                    pass
            else:
                # Sem rel/file físico: conservador, limpa tudo (comportamento antigo)
                logging.getLogger(__name__).warning(
                    "db_set_col: rel vazio para id=%s, limpando cache global", id_)
                _MEM_CACHE.clear()
                try:
                    con.execute("DELETE FROM probe_cache")
                except sqlite3.Error:
                    pass
    except NameError:
        pass
    return True

def db_id_por_rel(con: sqlite3.Connection, rel: str) -> Optional[str]:
    row = con.execute("SELECT id FROM catalogo WHERE rel=? LIMIT 1", (rel,)).fetchone()
    return row[0] if row else None


def _db_obter_por(con: sqlite3.Connection, coluna: str, valor: str) -> Optional[str]:
    row = con.execute(f"SELECT * FROM catalogo WHERE {coluna}=? LIMIT 1", (valor,)).fetchone()
    return _linha_para_pipe(row) if row else None


def db_obter(con: sqlite3.Connection, id_: str) -> Optional[str]:
    return _db_obter_por(con, "id", id_)


def db_obter_por_rel(con: sqlite3.Connection, rel: str) -> Optional[str]:
    return _db_obter_por(con, "rel", rel)


def db_obter_row(con: sqlite3.Connection, id_: str) -> Optional[tuple]:
    """X2 - retorna linha como tuple (fmt fica no CLI)."""
    row = con.execute("SELECT * FROM catalogo WHERE id=? LIMIT 1", (id_,)).fetchone()
    return tuple(row) if row else None


def db_obter_por_rel_row(con: sqlite3.Connection, rel: str) -> Optional[tuple]:
    """X2 - retorna linha como tuple (fmt fica no CLI)."""
    row = con.execute("SELECT * FROM catalogo WHERE rel=? LIMIT 1", (rel,)).fetchone()
    return tuple(row) if row else None


def db_apagar(con: sqlite3.Connection, id_: str) -> None:
    with transacao(con):
        con.execute("DELETE FROM links_youtube WHERE catalogo_id=?", (id_,))
        con.execute("DELETE FROM catalogo WHERE id=?", (id_,))


# --- Links YouTube ---
def link_add(con: sqlite3.Connection, video_id: str, catalogo_id: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO links_youtube(video_id, catalogo_id) VALUES (?,?)",
        (video_id, catalogo_id))


def links_repassar(con: sqlite3.Connection, id_perdedor: str, id_vencedor: str) -> int:
    """D10 - repassa vinculos sem violar UNIQUE (video_id e PK).

    Ordem correta (PK em video_id):
      1. Coleta video_ids do perdedor.
      2. DELETE de TODOS no perdedor (libera as PKs).
      3. INSERT OR IGNORE no vencedor (video_id ja existente no vencedor
         nao duplica e nao sobrescreve).
    """
    if not id_perdedor or not id_vencedor or id_perdedor == id_vencedor:
        return 0
    vids = [r[0] for r in con.execute(
        "SELECT video_id FROM links_youtube WHERE catalogo_id=?",
        (id_perdedor,))]
    if not vids:
        return 0
    # Passo 1: libera as PKs do perdedor
    con.execute("DELETE FROM links_youtube WHERE catalogo_id=?", (id_perdedor,))
    # Passo 2: insere no vencedor (IGNORE se ja existir)
    repassados = 0
    for vid in vids:
        cur = con.execute(
            "INSERT OR IGNORE INTO links_youtube(video_id, catalogo_id) VALUES (?,?)",
            (vid, id_vencedor))
        repassados += cur.rowcount or 0
    return repassados


def links_bulk(con: sqlite3.Connection, linhas: Iterable[str]) -> int:
    total = 0
    with transacao(con):
        for linha in linhas:
            p = linha.rstrip("\r\n").split("|")
            if len(p) >= 2 and p[0] and p[1]:
                link_add(con, p[0], p[1])
                total += 1
    return total


def link_rel(con: sqlite3.Connection, video_id: str) -> Optional[str]:
    row = con.execute(
        "SELECT l.catalogo_id, k.rel FROM links_youtube l "
        "LEFT JOIN catalogo k ON k.id = l.catalogo_id WHERE l.video_id = ?",
        (video_id,)).fetchone()
    if not row:
        return None
    return row[1] or ""


def links_all(con: sqlite3.Connection) -> list[str]:
    return [f"{r[0]}|{r[1]}" for r in con.execute(
        "SELECT k.rel, group_concat(l.video_id) "
        "FROM links_youtube l JOIN catalogo k ON k.id = l.catalogo_id GROUP BY k.id")]


def links_of(con: sqlite3.Connection, catalogo_id: str) -> list[str]:
    return [x[0] for x in con.execute(
        "SELECT video_id FROM links_youtube WHERE catalogo_id=? ORDER BY video_id",
        (catalogo_id,))]


# --- Tradução ---
def trad_get(con: sqlite3.Connection, hash_verso: str) -> Optional[str]:
    row = con.execute(
        "SELECT traduzido FROM traducao_cache WHERE hash_verso=?", (hash_verso,)).fetchone()
    return row[0] if row else None


_TRAD_INSERTS_DESDE_PRUNE = 0
_TRAD_PRUNE_INTERVALO = 500


def trad_limpar_excesso(con: sqlite3.Connection,
                        max_linhas: int = MAX_TRAD) -> int:
    """LRU da traducao_cache: mantem as max_linhas mais recentes.

    Chave: 'data' (formato 'YYYY-MM-DD_HH:MM', lexicografico = cronologico),
    desempate por rowid. Retorna no de linhas removidas.
    """
    row = con.execute("SELECT COUNT(*) FROM traducao_cache").fetchone()
    total = int(row[0]) if row else 0
    if total <= max_linhas:
        return 0
    excesso = total - max_linhas
    con.execute(
        "DELETE FROM traducao_cache WHERE hash_verso IN ("
        "  SELECT hash_verso FROM traducao_cache "
        "  ORDER BY data ASC, rowid ASC LIMIT ?"
        ")",
        (excesso,))
    logging.getLogger(__name__).info(
        "trad_limpar_excesso: %d linhas removidas (total=%d, teto=%d)",
        excesso, total, max_linhas)
    return excesso


_TRAD_PRUNE_LOCK = threading.Lock()


def trad_put(con, hash_verso, original, traduzido, idioma_origem, idioma_destino) -> None:
    global _TRAD_INSERTS_DESDE_PRUNE
    agora = datetime.now().strftime("%Y-%m-%d_%H:%M")
    con.execute(
        "INSERT OR REPLACE INTO traducao_cache"
        "(hash_verso, original, traduzido, idioma_origem, idioma_destino, data) "
        "VALUES (?,?,?,?,?,?)",
        (hash_verso, original, traduzido, idioma_origem, idioma_destino, agora))
    with _TRAD_PRUNE_LOCK:
        _TRAD_INSERTS_DESDE_PRUNE += 1
        deve_podar = _TRAD_INSERTS_DESDE_PRUNE >= _TRAD_PRUNE_INTERVALO
        if deve_podar:
            _TRAD_INSERTS_DESDE_PRUNE = 0
    if deve_podar:
        try:
            trad_limpar_excesso(con)
        except sqlite3.Error:
            pass


def probe_cache_limpar_antigos(con: sqlite3.Connection,
                                antes_dias: int = 90,
                                max_remover: int = 10000) -> int:
    """Remove entradas de probe_cache com mtime antigo (batched)."""
    if antes_dias <= 0:
        return 0
    limite_ts = time.time() - antes_dias * 86400
    cur = con.execute(
        "DELETE FROM probe_cache WHERE chave IN ("
        "  SELECT chave FROM probe_cache WHERE mtime < ? LIMIT ?"
        ")",
        (limite_ts, int(max_remover)))
    removidos = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    if removidos:
        logging.getLogger(__name__).info(
            "probe_cache_limpar_antigos: %d linhas removidas (antes de %dd)",
            removidos, antes_dias)
    return removidos


# ============================================================================
# 5. RUNTIME
# ============================================================================

_LOCK_FD: Optional[int] = None
_SESSAO_ATIVA: bool = False
# Item 1a: journal espelhamento global (setado em iniciar_sessao).
# diario_add cai para este valor quando cfg=None -> callers legados
# continuam funcionando sem passar cfg explicitamente.
_CFG_ATUAL: "Config | None" = None

# ST7 - registro de filhos vivos para _sig_handler matar deterministicamente
_FILHOS_ATIVOS: "set[subprocess.Popen]" = set()
_FILHOS_LOCK = threading.Lock()


def registrar_filho(p: "subprocess.Popen") -> None:
    with _FILHOS_LOCK:
        _FILHOS_ATIVOS.add(p)


def desregistrar_filho(p: "subprocess.Popen") -> None:
    with _FILHOS_LOCK:
        _FILHOS_ATIVOS.discard(p)


def _matar_filhos(timeout: float = 1.5) -> None:
    """ST7 - SIGTERM no PGID de cada filho, espera, depois SIGKILL."""
    with _FILHOS_LOCK:
        filhos = list(_FILHOS_ATIVOS)
    if not filhos:
        return
    try:
        pgid_pai = os.getpgid(0)
    except OSError:
        pgid_pai = -1
    for p in filhos:
        if p.poll() is None:
            try:
                pgid = os.getpgid(p.pid)
                if pgid != pgid_pai:
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    p.terminate()
            except (OSError, ProcessLookupError):
                try:
                    p.terminate()
                except OSError:
                    pass
    t0 = time.time()
    while time.time() - t0 < timeout:
        if all(p.poll() is not None for p in filhos):
            return
        time.sleep(0.1)
    for p in filhos:
        if p.poll() is None:
            try:
                pgid = os.getpgid(p.pid)
                if pgid != pgid_pai:
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    p.kill()
            except (OSError, ProcessLookupError):
                try:
                    p.kill()
                except OSError:
                    pass


def trava(cfg: Config) -> bool:
    global _LOCK_FD
    # ST5b: previne unlink externo do lockfile. Se ja temos _LOCK_FD
    # de chamada anterior, ele pode apontar para inode orfao (path
    # deletado por outro processo). Comparamos ANTES de adquirir novo
    # lock; se divergir, abortamos para evitar dupla exclusividade mutua.
    if _LOCK_FD is not None:
        try:
            st_fd = os.fstat(_LOCK_FD)
            st_path = os.stat(str(cfg.lock))
            if (st_fd.st_dev, st_fd.st_ino) != (st_path.st_dev, st_path.st_ino):
                try:
                    fcntl.flock(_LOCK_FD, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(_LOCK_FD)
                except OSError:
                    pass
                _LOCK_FD = None
                logging.getLogger(__name__).warning(
                    "trava: lockfile unlinked externamente detectado; "
                    "abortando para evitar dupla exclusividade")
                return False
        except OSError:
            try:
                fcntl.flock(_LOCK_FD, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(_LOCK_FD)
            except OSError:
                pass
            _LOCK_FD = None
            logging.getLogger(__name__).warning(
                "trava: lockfile desapareceu enquanto seguravamos o lock")
            return False
    try:
        cfg.lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(cfg.lock), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _LOCK_FD = fd
    # ST5: valida (st_dev, st_ino) - detecta race microscopica entre
    # open() e stat() (unlink em milissegundos por terceiro processo).
    try:
        st_fd = os.fstat(fd)
        st_path = os.stat(str(cfg.lock))
        if (st_fd.st_dev, st_fd.st_ino) != (st_path.st_dev, st_path.st_ino):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
            _LOCK_FD = None
            return False
    except OSError:
        pass
    return True


def libera(cfg: Optional[Config] = None) -> None:
    global _LOCK_FD
    if _LOCK_FD is None:
        return
    try:
        fcntl.flock(_LOCK_FD, fcntl.LOCK_UN)
        os.close(_LOCK_FD)
    except OSError:
        pass
    _LOCK_FD = None


def _termux_call(cmd: str, timeout: int = 10) -> None:
    try:
        subprocess.run([cmd], timeout=timeout, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _sig_handler(signum: int, frame) -> None:
    # ST7/Z5 - ignora sinais adicionais enquanto mata filhos (evita
    # re-entrada se o usuário apertar Ctrl+C duas vezes).
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except (ValueError, OSError):
        pass
    try:
        _matar_filhos()
    except Exception:
        pass
    raise SystemExit(130)


def iniciar_sessao(cfg: Config) -> None:
    global _SESSAO_ATIVA, _CFG_ATUAL
    if _SESSAO_ATIVA:
        return
    if not trava(cfg):
        print("Outra execução ativa.", file=sys.stderr)
        sys.exit(1)
    _SESSAO_ATIVA = True
    _CFG_ATUAL = cfg  # item 1a: journal ativo durante a sessao
    _bx_io._set_cfg_atual(cfg)
    atexit.register(fim, cfg)
    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    # ST7 - SIGHUP (fechar terminal no Termux)
    try:
        signal.signal(signal.SIGHUP, _sig_handler)
    except (AttributeError, OSError):
        pass
    _termux_call("termux-wake-lock")


def fim(cfg: Optional[Config] = None) -> None:
    global _SESSAO_ATIVA, _CFG_ATUAL
    if not _SESSAO_ATIVA:
        return
    _SESSAO_ATIVA = False
    _CFG_ATUAL = None
    _bx_io._set_cfg_atual(None)
    _termux_call("termux-wake-unlock")
    if cfg is not None:
        limpar_temporarios(cfg)
        # Self-containment: prune final da traducao_cache no fechamento
        try:
            with sessao_db(cfg) as _con:
                with transacao(_con):
                    trad_limpar_excesso(_con)
                    probe_cache_limpar_antigos(_con, 90)
        except Exception:
            pass
        sync_sd()
    libera(cfg)


def sync_sd() -> None:
    try:
        os.sync()
    except AttributeError:
        try:
            subprocess.run(["sync"], timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass


_MEDIA_SCAN_STATE: dict = {"probed": False}
_MEDIA_SCAN_LOCK = threading.Lock()


def _find_am() -> Optional[str]:
    p = shutil.which("am")
    if p:
        return p
    for cand in ("/system/bin/am", "/system/xbin/am"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _find_su() -> Optional[str]:
    p = shutil.which("su")
    if p:
        return p
    for cand in ("/system/xbin/su", "/system/bin/su", "/sbin/su", "/su/bin/su"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _has_termux_api_app() -> bool:
    """Verifica app Termux:API via pm list packages (timeout 3s)."""
    pm = shutil.which("pm") or "/system/bin/pm"
    if not os.path.isfile(pm):
        return False
    try:
        r = subprocess.run(
            [pm, "list", "packages", "com.termux.api"],
            timeout=3, capture_output=True, text=True, check=False,
        )
        for line in (r.stdout or "").splitlines():
            if line.strip() == "package:com.termux.api":
                return True
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _probe_media_scan_tools() -> dict:
    """Detecta ferramentas UMA vez por processo. Cache inclui app Termux:API."""
    with _MEDIA_SCAN_LOCK:
        if _MEDIA_SCAN_STATE.get("probed"):
            return _MEDIA_SCAN_STATE.copy()
        has_bin = shutil.which("termux-media-scan") is not None
        has_app = _has_termux_api_app() if has_bin else False
        _MEDIA_SCAN_STATE["termux_scan_bin"] = has_bin
        _MEDIA_SCAN_STATE["termux_api_app"] = has_app
        _MEDIA_SCAN_STATE["termux_scan"] = has_bin and has_app
        _MEDIA_SCAN_STATE["am"] = _find_am()
        _MEDIA_SCAN_STATE["su"] = _find_su()
        _MEDIA_SCAN_STATE["probed"] = True
        return _MEDIA_SCAN_STATE.copy()


def _media_scan_disable_termux() -> None:
    with _MEDIA_SCAN_LOCK:
        _MEDIA_SCAN_STATE["termux_scan"] = False


def media_scan(arquivos, log_erros=None, api_off=False) -> dict:
    """Notifica Android MediaStore. Ordem: termux-media-scan -> am -> su.

    Regras rigidas:
      - Filtra arquivos inexistentes ANTES de qualquer syscall (am broadcast
        retorna rc=0 para URI morta -> falso positivo).
      - termux-media-scan so e tentado se (binario presente E app
        com.termux.api instalado via pm). Primeira batch usa timeout 5s
        (probe); se estourar, desabilita para a sessao inteira.
      - am broadcast: 3s/arquivo, cap 50, desiste apos 3 falhas consecutivas.
      - su -c <cmd> (nao-interativo): 3s/arquivo, cap 20, desiste apos 3 falhas.
    Nunca levanta excecao. Retorna dict com contagens + metodo + tools.
    """
    result: dict = {"ok": 0, "fail": 0, "method": "none", "erros": [], "tools": {}}

    tools = _probe_media_scan_tools()
    result["tools"] = {
        "termux_scan": bool(tools.get("termux_scan")),
        "termux_scan_bin": bool(tools.get("termux_scan_bin")),
        "termux_api_app": bool(tools.get("termux_api_app")),
        "am": bool(tools.get("am")),
        "su": bool(tools.get("su")),
    }

    lista = [str(a) for a in arquivos if a and os.path.isfile(str(a))]
    if not lista:
        return result

    if tools.get("termux_scan") and not api_off:
        first_done = False
        for i in range(0, len(lista), 25):
            batch = lista[i:i + 25]
            timeout = 30 if first_done else 5
            try:
                r = subprocess.run(
                    ["termux-media-scan"] + batch,
                    timeout=timeout, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False,
                )
                if r.returncode == 0:
                    first_done = True
                    result["ok"] += len(batch)
                else:
                    result["fail"] += len(batch)
                    if not first_done:
                        result["erros"].append(
                            f"termux-media-scan rc={r.returncode}, desistindo")
                        _media_scan_disable_termux()
                        break
            except subprocess.TimeoutExpired:
                if not first_done:
                    result["erros"].append(
                        f"termux-media-scan timeout {timeout}s, desistindo")
                    _media_scan_disable_termux()
                    break
                result["fail"] += len(batch)
            except FileNotFoundError:
                result["erros"].append("termux-media-scan sumiu")
                _media_scan_disable_termux()
                break
        if result["ok"] > 0:
            result["method"] = "termux-media-scan"
            return result

    if tools.get("am"):
        am = tools["am"]
        ok = fail = consec = 0
        for arq in lista[:50]:
            uri = f"file://{arq}"
            try:
                r = subprocess.run(
                    [am, "broadcast",
                     "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                     "-d", uri],
                    timeout=3, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False,
                )
                if r.returncode == 0:
                    ok += 1; consec = 0
                else:
                    fail += 1; consec += 1
            except (FileNotFoundError, subprocess.TimeoutExpired):
                fail += 1; consec += 1
            if consec >= 3:
                result["erros"].append(
                    "am broadcast: 3 falhas consecutivas, desistindo")
                break
        if ok > 0:
            result["ok"] += ok
            result["fail"] += fail
            result["method"] = "am broadcast"
            if fail:
                result["erros"].append(f"am broadcast: {fail} falhas")
            return result
        result["fail"] += fail

    if tools.get("su") and tools.get("am"):
        am = tools["am"]; su = tools["su"]
        ok = fail = consec = 0
        for arq in lista[:20]:
            uri = f"file://{arq}"
            cmd_str = " ".join(shlex.quote(c) for c in [
                am, "broadcast",
                "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                "-d", uri,
            ])
            try:
                r = subprocess.run(
                    [su, "-c", cmd_str],
                    timeout=3, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False,
                )
                if r.returncode == 0:
                    ok += 1; consec = 0
                else:
                    fail += 1; consec += 1
            except (FileNotFoundError, subprocess.TimeoutExpired):
                fail += 1; consec += 1
            if consec >= 3:
                result["erros"].append("su: 3 falhas consecutivas, desistindo")
                break
        if ok > 0:
            result["ok"] += ok
            result["fail"] += fail
            result["method"] = "su am broadcast"
        else:
            result["fail"] += fail

    if result["ok"] == 0:
        msg = (f"media_scan: nenhum metodo funcionou "
               f"(bin={result['tools']['termux_scan_bin']} "
               f"app={result['tools']['termux_api_app']} "
               f"am={result['tools']['am']} su={result['tools']['su']})")
        log_erro(log_erros, msg)
        result["erros"].append(msg)

    return result

def limpar_temporarios(cfg: Config, idade_min_seg: int = 1800) -> None:
    """ST6 - so limpa arquivos da sessao atual ou com mais de 30 min.

    Preserva in-flight de execucoes paralelas (bx get rodar + bx fix aplicar).
    Arquivos da sessao sao reconhecidos pelo PID no nome (t_PID_, m_PID_).
    """
    agora = time.time()
    pid_atual = os.getpid()
    for padrao in PADROES_TEMP:
        try:
            for f in cfg.tmp_dir.glob(padrao):
                if f.is_dir():
                    shutil.rmtree(f, ignore_errors=True)
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                if f"_{pid_atual}_" in f.name or f"_{pid_atual}." in f.name:
                    f.unlink(missing_ok=True)
                    continue
                if (agora - st.st_mtime) > idade_min_seg:
                    f.unlink(missing_ok=True)
        except OSError:
            pass


# --- Painel odometro lateral (3 linhas, ANSI, throttle) --- item 3 ---
_PAINEL_STATE: dict = {"last_atual": -1, "last_arq": "", "last_pct": -1}


def painel_progresso(atual: int, total: int, dt_acumulado: float,
                     stats_letras: dict, arquivo: str = "",
                     quiet: bool = False, largura_arq: int = 60) -> None:
    """Odometro lateral de 3 linhas (ANSI + \x1b[3A).

    Throttle: escreve apenas quando arquivo muda, % muda >=0.1%,
    avanco >=10 itens, ou atual==total. ~20 writes por 500 itens.
    Formato odometro (digitos zerados): 000234/000500.
    """
    if quiet:
        return
    pct_int = (atual * 1000 // total) if total > 0 else 0
    arq_mudou = _PAINEL_STATE["last_arq"] != arquivo
    pct_mudou = abs(pct_int - _PAINEL_STATE["last_pct"]) >= 1
    avanco = atual - _PAINEL_STATE["last_atual"]
    primeiro = _PAINEL_STATE["last_atual"] < 0
    final = total > 0 and atual >= total
    if not (primeiro or arq_mudou or pct_mudou or avanco >= 10 or final):
        return
    _PAINEL_STATE["last_atual"] = atual
    _PAINEL_STATE["last_arq"] = arquivo
    _PAINEL_STATE["last_pct"] = pct_int

    pct = (atual / total * 100.0) if total > 0 else 0.0
    it_s = (atual / dt_acumulado) if dt_acumulado > 0.01 else 0.0
    eta = ((total - atual) / it_s) if it_s > 0.01 else 0.0
    arq = arquivo[-largura_arq:] if len(arquivo) > largura_arq else arquivo
    sync_n = stats_letras.get("SYNC", 0)
    static_n = stats_letras.get("STATIC", 0)
    nenhuma_n = stats_letras.get("NENHUMA", 0)
    l1 = f"\U0001F50E Alvo: {arq}"
    l2 = (f"\U0001F4CA {atual:06d}/{total:06d}  {pct:5.1f}%  |  "
          f"{it_s:06.1f} it/s  |  ETA {int(eta):05d}s")
    l3 = (f"\U0001F3B5 SYNC:{sync_n:05d}  STATIC:{static_n:05d}  "
          f"NENHUMA:{nenhuma_n:05d}")
    sys.stdout.write(f"\r\x1b[K{l1}\n\r\x1b[K{l2}\n\r\x1b[K{l3}\x1b[3A")
    sys.stdout.flush()


def painel_limpar() -> None:
    """Limpa painel + reseta throttle."""
    _PAINEL_STATE["last_atual"] = -1
    _PAINEL_STATE["last_arq"] = ""
    _PAINEL_STATE["last_pct"] = -1
    sys.stdout.write("\r\x1b[0J")
    sys.stdout.flush()


# --- Historico de execucoes para --estimate (hibrido linear 70/30) ---
class HistoricoExecucoes:
    """Persiste throughput por execucao em historico_execucoes.jsonl.

    Modelo: 70% por byte (I/O de audio + RG dominam) + 30% por arquivo
    (overhead fixo de probe). Separado por com_rg True/False.
    """
    def __init__(self, cfg: "Config") -> None:
        self.path = cfg.bx_home / "historico_execucoes.jsonl"

    def registrar(self, n_arqs: int, bytes_total: int,
                  tempo_s: float, com_rg: bool = False) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "n": int(n_arqs),
                "bytes": int(bytes_total),
                "tempo_s": round(float(tempo_s), 2),
                "com_rg": bool(com_rg),
            }
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def estimar(self, n_arqs: int, bytes_total: int,
                com_rg: bool = False) -> "float | None":
        if not self.path.is_file():
            return None
        registros: list = []
        try:
            for ln in self.path.read_text(
                    encoding="utf-8", errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    r = json.loads(ln)
                    n_r = int(r.get("n", 0))
                    tempo_r = float(r.get("tempo_s", 0))
                    # H1: filtra runs rapir demais (cached/simular/dry-run).
                    # Apply real gasta >=200ms/arquivo (probe+mover+DB).
                    if (r.get("com_rg") == com_rg
                            and int(r.get("bytes", 0)) > 0
                            and n_r > 0
                            and tempo_r / n_r >= 0.2):
                        registros.append(r)
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
        except OSError:
            return None
        if not registros:
            return None
        n_hist = sum(int(r["n"]) for r in registros)
        bytes_hist = sum(int(r["bytes"]) for r in registros)
        tempo_hist = sum(float(r["tempo_s"]) for r in registros)
        if n_hist <= 0 or bytes_hist <= 0 or tempo_hist <= 0:
            return None
        s_por_arq = tempo_hist / n_hist
        s_por_byte = tempo_hist / bytes_hist
        return (0.7 * (s_por_byte * bytes_total)
                + 0.3 * (s_por_arq * n_arqs))


def fmt_duracao(segundos: float) -> str:
    """Formata segundos como '45s', '12min', '1h30min'."""
    s = max(0, int(segundos))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}min"
    return f"{s // 3600}h{(s % 3600) // 60}min"


def estimate_acervo(cfg: "Config",
                    arquivos: "list | None" = None,
                    com_rg: bool = False) -> dict:
    """Dry-run rapido para --estimate (sem ffprobe/ffmpeg)."""
    if arquivos is None:
        arquivos = []
    n = len(arquivos)
    bytes_total = 0
    for a in arquivos:
        try:
            bytes_total += a.stat().st_size
        except OSError:
            pass
    hist = HistoricoExecucoes(cfg)
    h_sem = hist.estimar(n, bytes_total, com_rg=False)
    h_com = hist.estimar(n, bytes_total, com_rg=True)
    # H2-FIX: semantica consistente. RG nunca menor que sem-RG.
    # Deriva o lado faltante do historico real (RG ~4x mais lento).
    RG_FACTOR = 4.0
    if h_com is not None and h_sem is None:
        est_sem = h_com / RG_FACTOR
        est_com = h_com
    elif h_sem is not None and h_com is None:
        est_sem = h_sem
        est_com = h_sem * RG_FACTOR
    elif h_sem is not None and h_com is not None:
        est_sem = h_sem
        est_com = max(h_com, h_sem)
    else:
        est_sem = n * 0.6
        est_com = est_sem * RG_FACTOR
    if n > 0:
        est_sem = max(est_sem, n * 0.1)
        est_com = max(est_com, est_sem)
    return {
        "n_arqs": n,
        "bytes_total": bytes_total,
        "hist_sem_rg": h_sem,
        "hist_com_rg": h_com,
        "est_sem_rg": est_sem,
        "est_com_rg": est_com,
    }


def imprimir_estimate(cfg: "Config", arquivos: "list",
                      usando_rg: bool = False) -> None:
    """Imprime o bloco de --estimate (usado por bx_fix e bx_get)."""
    r = estimate_acervo(cfg, arquivos)
    mb = r["bytes_total"] / (1024 * 1024)
    print(f"Varredura: {r['n_arqs']} arquivos ({mb:.1f} MB)")
    if r["hist_sem_rg"] is not None:
        print(f"Historico: {r['n_arqs']} arqs, "
              f"est. por byte = {r['hist_sem_rg']:.2f}s (sem RG)")
    else:
        print("Historico: nenhum (usando referencia 0.6s/arquivo)")
    # Dual: sempre mostra os dois cenarios (RG on/off)
    print(f"Estimado: ~{fmt_duracao(r['est_sem_rg'])} (sem RG) / "
          f"~{fmt_duracao(r['est_com_rg'])} (com RG)")


# ============================================================================
# Testes (rodam so com --teste)
# ============================================================================
# ============================================================================
# Fila de downloads (SQLite) -- re-adicionada apos split
# ============================================================================
def fila_read_db(con: sqlite3.Connection) -> list[list[str]]:
    """Retorna [[estado, video_id, url, tent_str, ultima_ts], ...]."""
    return [[r[0], r[1], r[2] or "", str(r[3]), r[4] or ""]
            for r in con.execute(
                "SELECT estado, video_id, url, tent, ultima_ts "
                "FROM fila_downloads ORDER BY rowid")]


def fila_add_db(con: sqlite3.Connection, video_id: str, url: str) -> bool:
    """INSERT OR IGNORE. Retorna True se inseriu (nao existia)."""
    cur = con.execute(
        "INSERT OR IGNORE INTO fila_downloads"
        "(video_id, estado, url, tent, ultima_ts) "
        "VALUES (?, 'pendente', ?, 0, '')",
        (video_id, url))
    return (cur.rowcount or 0) > 0


def fila_set_db(con: sqlite3.Connection, video_id: str,
                estado: str, tent: int) -> None:
    con.execute(
        "UPDATE fila_downloads SET estado=?, tent=?, ultima_ts=? "
        "WHERE video_id=?",
        (estado, int(tent), datetime.now().strftime("%Y%m%d"), video_id))


def fila_migrar_txt(cfg: "Config", con: sqlite3.Connection) -> int:
    """Migra fila_downloads.txt para a tabela (idempotente, renomeia .bak)."""
    if not cfg.fila.is_file():
        return 0
    linhas: list[tuple[str, str, str, int, str]] = []
    try:
        conteudo = cfg.fila.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for l in conteudo.splitlines():
        if not l.strip():
            continue
        partes = l.split("|")
        while len(partes) < 5:
            partes.append("")
        estado, vid, url, tent_s, ts = partes[:5]
        if not vid:
            continue
        try:
            tent = int(tent_s)
        except (ValueError, TypeError):
            tent = 0
        linhas.append((vid, estado or "pendente", url, tent, ts))
    if not linhas:
        return 0
    with transacao(con):
        for vid, estado, url, tent, ts in linhas:
            con.execute(
                "INSERT OR IGNORE INTO fila_downloads"
                "(video_id, estado, url, tent, ultima_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (vid, estado, url, tent, ts))
    try:
        bak = cfg.fila.with_name(cfg.fila.name + ".bak")
        if not bak.exists():
            cfg.fila.rename(bak)
    except OSError:
        pass
    logging.getLogger(__name__).info(
        "fila_migrar_txt: %d entradas migradas", len(linhas))
    return len(linhas)


# ============================================================================
# Arbitragem de duplicatas -- re-adicionada apos split
# ============================================================================
def _id_por_rel_seguro(con: Optional[sqlite3.Connection], cfg: "Config",
                       rel: str) -> str:
    if not rel:
        return ""
    try:
        if con is not None:
            row = con.execute(
                "SELECT id FROM catalogo WHERE rel=? LIMIT 1", (rel,)
            ).fetchone()
            return row[0] if row else ""
        with sessao_db(cfg) as _con:
            row = _con.execute(
                "SELECT id FROM catalogo WHERE rel=? LIMIT 1", (rel,)
            ).fetchone()
            return row[0] if row else ""
    except sqlite3.Error:
        return ""


def resolver_duplicata(novo: Path, existente: Path, cfg: "Config",
                       con: Optional[sqlite3.Connection] = None) -> dict:
    """Decide vencedor entre dois arquivos do mesmo artista-titulo.

    Retorna {'vencedor': Path, 'perdedor': Path|None,
             'id_perdedor': str, 'motivo': str}.
    NAO altera o DB. NAO move arquivos -- o chamador decide.
    """
    if not existente.is_file():
        return {"vencedor": novo, "perdedor": None,
                "id_perdedor": "", "motivo": "existente ausente"}
    if not novo.is_file():
        return {"vencedor": existente, "perdedor": None,
                "id_perdedor": "", "motivo": "novo ausente"}
    info_novo = probe(novo, cfg)
    info_exist = probe(existente, cfg)
    br_novo = info_novo.bitrate if info_novo else 0
    br_exist = info_exist.bitrate if info_exist else 0
    if _vencedor_dup(br_novo, br_exist, novo, existente):
        rel_exist = ""
        try:
            rel_exist = str(existente.relative_to(cfg.raiz_sd))
        except ValueError:
            pass
        id_perd = _id_por_rel_seguro(con, cfg, rel_exist)
        return {"vencedor": novo, "perdedor": existente,
                "id_perdedor": id_perd,
                "motivo": f"novo vence (br {br_novo}>{br_exist})"}
    rel_novo = ""
    try:
        rel_novo = str(novo.relative_to(cfg.raiz_sd))
    except ValueError:
        pass
    id_perd = _id_por_rel_seguro(con, cfg, rel_novo)
    return {"vencedor": existente, "perdedor": novo,
            "id_perdedor": id_perd,
            "motivo": f"existente vence (br {br_exist}>={br_novo})"}


def _teste() -> int:
    ok = fail = 0
    def chk(nome: str, cond: bool) -> None:
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"[PASS] {nome}")
        else:
            fail += 1
            print(f"[FAIL] {nome}")

    # Regressão: constantes críticas SEM mojibake/cirílico
    chk("constante D_QUAR sem mojibake",
        not any(0x400 <= ord(c) <= 0x4ff for c in D_QUAR))
    chk("constante D_SUBST sem mojibake",
        not any(0x400 <= ord(c) <= 0x4ff for c in D_SUBST))
    chk("D_QUAR exato",
        D_QUAR == "Z_\u00e1rea de transfer\u00eancia")
    chk("D_SUBST exato",
        D_SUBST == "Z_substitu\u00eddos")

    chk("mojibake cp1252", _fix_mojibake("Ã\u00a3o") == "\u00e3o")
    chk("mojibake passa limpo", _fix_mojibake("abc") == "abc")
    chk("limpo remove Cc", _limpo("a\x00b") == "ab")
    chk("limpo troca |", _limpo("a|b") == "a b")
    chk("limpo vazio", _limpo("") == "")
    chk("limpo None", _limpo(None) == "")

    n = _normalizar(["id1", "art", "tit", "COMPLETO", "rel", "1000",
                     "data", "sha", "pt", "44.1", "", "128", "sim",
                     "aac", "prod", "-14.5"])
    chk(f"normalizar len {N_CAMPOS}", len(n) == N_CAMPOS)
    chk("normalizar tam int", n[5] == 1000 and isinstance(n[5], int))
    chk("normalizar khz float", n[9] == 44.1 and isinstance(n[9], float))
    chk("normalizar bitrate int", n[11] == 128 and isinstance(n[11], int))
    chk("normalizar rg float", n[15] == -14.5 and isinstance(n[15], float))
    chk("normalizar tipo_letra None", n[10] is None)

    linha = _linha_para_pipe(n)
    chk(f"pipe {N_CAMPOS-1} sep", linha.count("|") == N_CAMPOS - 1)
    chk("pipe khz", "44.1" in linha)
    chk("pipe rg", "-14.5" in linha)

    chk("produtor basico",
        extrair_produtor("Song (prod. Fulano)") == "Fulano")
    _pp = extrair_produtor("Song (prod. Fulano & Ciclano)")
    chk("produtor multiplo", "Fulano" in _pp and "Ciclano" in _pp)
    chk("produtor vazio", extrair_produtor("") == "")
    chk("produtor sem match", extrair_produtor("Song normal") == "")

    txt = montar_manifesto({"VERSÃO": "3", "ID": "BX-1"})
    chk("manifesto 18 linhas", len(txt.strip().split("\n")) == 18)
    for _c in MANIFESTO_CAMPOS:
        chk(f"manifesto {_c}", f"{_c}:" in txt)

    chk("suf_re default", _construir_suf_re([]) == SUF_DEFAULT)
    _rs = _construir_suf_re(["Official"])
    chk("suf_re custom", _rs != SUF_DEFAULT and "Official" in _rs)
    chk("suf_re prefixo", _rs.startswith("M4A_"))

    _ia = ProbeInfo(codec="aac", channels=2)
    chk("compat m4a ok", compat_check(_ia, ".m4a"))
    chk("compat mp3 recusa", not compat_check(_ia, ".mp3"))
    chk("compat codec errado",
        not compat_check(ProbeInfo(codec="mp3", channels=2), ".m4a"))
    chk("compat mono", not compat_check(ProbeInfo(codec="aac", channels=1), ".m4a"))
    chk("compat None", not compat_check(None, ".m4a"))
    chk("tagear m4a", pode_tagear(_ia, ".m4a"))
    chk("tagear mp3", pode_tagear(ProbeInfo(codec="mp3", channels=2), ".mp3"))
    chk("tagear flac", pode_tagear(ProbeInfo(codec="flac", channels=2), ".flac"))
    chk("tagear wav recusa", not pode_tagear(ProbeInfo(codec="pcm", channels=2), ".wav"))

    # LRU da traducao_cache (self-containment)
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _tdb:
        _con = conectar(Path(_tdb) / "t.db")
        try:
            with transacao(_con):
                for i in range(100):
                    dia = 1 + i // (60 * 24)
                    hora = (i // 60) % 24
                    minuto = i % 60
                    _dt = f"2020-01-{dia:02d}_{hora:02d}:{minuto:02d}"
                    _con.execute(
                        "INSERT INTO traducao_cache VALUES (?,?,?,?,?,?)",
                        (f"h{i:04d}", f"o{i}", f"t{i}", "en", "pt", _dt))
            _n = trad_limpar_excesso(_con, max_linhas=50)
            chk("LRU trad remove excesso (100->50)", _n == 50)
            _cnt = _con.execute(
                "SELECT COUNT(*) FROM traducao_cache").fetchone()[0]
            chk("LRU trad mantem teto 50", _cnt == 50)
            _row = _con.execute(
                "SELECT hash_verso FROM traducao_cache "
                "WHERE hash_verso='h0000'").fetchone()
            chk("LRU trad removeu mais antigo (h0000)", _row is None)
            _row = _con.execute(
                "SELECT hash_verso FROM traducao_cache "
                "WHERE hash_verso='h0099'").fetchone()
            chk("LRU trad preservou mais recente (h0099)", _row is not None)
        finally:
            _con.close()

    print(f"== RESUMO bx_core: PASS={ok} FAIL={fail} ==")
    return 0 if fail == 0 else 1


# ============================================================================
# Fork hook (reseta estado do core no child)
# ============================================================================
def _reset_child_state_core() -> None:
    global _LOCK_FD, _SESSAO_ATIVA, _CFG_ATUAL, _TRAD_INSERTS_DESDE_PRUNE
    _LOCK_FD = None
    _SESSAO_ATIVA = False
    _CFG_ATUAL = None
    _TRAD_INSERTS_DESDE_PRUNE = 0
    _bx_io._set_cfg_atual(None)


def _teste_child_state() -> tuple:
    """Uso interno de smoke test (retorna estado no child pos-fork)."""
    return (_LOCK_FD, _SESSAO_ATIVA)


if hasattr(os, "register_at_fork"):
    try:
        os.register_at_fork(after_in_child=_reset_child_state_core)
    except Exception:
        pass


if __name__ == "__main__":
    if "--teste" in sys.argv:
        sys.exit(_teste())
    sys.exit(0)