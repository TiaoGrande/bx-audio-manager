#!/usr/bin/env python3
"""bx_media.py -- Probe, cache, tags, AAC, ReplayGain.

Depende de bx_kernel (constantes) e bx_io (FS/SHA/fsync/atomic). NAO importa
bx_core (evita ciclo). Config e duck-typed: so usa cfg.attr sem isinstance.

Split de bx_core (Bloco 2, Parte 3). Funcoes migradas na Parte 4.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
from concurrent.futures import (
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from bx_kernel import TIMEOUT, _limpo
from bx_io import (
    diario_add,
    enviar_sd,
    espaco_ok,
    log_erro,
    tam,
)

if TYPE_CHECKING:
    from bx_core import Config


# ============================================================================
# ProbeInfo
# ============================================================================
@dataclass
class ProbeInfo:
    codec: str = ""
    channels: int = 0
    sample_rate: float = 0.0
    bitrate: int = 0
    duration: float = 0.0
    title: str = ""
    artist: str = ""
    comment: str = ""
    album: str = ""
    tem_capa: bool = False
    raw_title: str = ""
    raw_artist: str = ""

    @property
    def vazio(self) -> bool:
        return not self.codec


# ============================================================================
# Cache global (mem + SQLite)
# ============================================================================
_MEM_CACHE: dict[tuple[str, int, float], ProbeInfo] = {}
_DB: Optional["ProbeCacheDB"] = None
_CACHE_LOCK = threading.RLock()
_CACHE_MAX = 10_000


class ProbeCacheDB:
    """Cache probe_cache auto-suficiente: abre a propria conexao sqlite.

    Nao usa bx_core.conectar (evita ciclo). Cria a tabela se ausente,
    para ser robusto em testes que apontam para DB vazio.
    """

    def __init__(self, db_path: Path) -> None:
        self._con = sqlite3.connect(
            str(db_path), timeout=30.0, isolation_level=None,
            check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.execute("PRAGMA busy_timeout=30000")
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS probe_cache ("
            "  chave   TEXT PRIMARY KEY,"
            "  tamanho INTEGER NOT NULL,"
            "  mtime   REAL    NOT NULL,"
            "  json    TEXT    NOT NULL"
            ")"
        )
        self._lock = threading.Lock()

    def get(self, chave: str, tam_: int,
            mtime_: float) -> Optional[str]:
        with self._lock:
            row = self._con.execute(
                "SELECT json FROM probe_cache "
                "WHERE chave=? AND tamanho=? AND mtime=?",
                (chave, int(tam_), float(mtime_))).fetchone()
            return row[0] if row else None

    def put(self, chave: str, tam_: int, mtime_: float, js: str) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO probe_cache"
                "(chave, tamanho, mtime, json) VALUES (?,?,?,?)",
                (chave, int(tam_), float(mtime_), js))

    def limpar(self) -> None:
        with self._lock:
            self._con.execute("DELETE FROM probe_cache")

    def remover(self, chave: str) -> None:
        with self._lock:
            self._con.execute(
                "DELETE FROM probe_cache WHERE chave=?", (chave,))

    def close(self) -> None:
        with self._lock:
            try:
                self._con.close()
            except sqlite3.Error:
                pass


def abrir_cache(cfg: "Config") -> None:
    global _DB
    with _CACHE_LOCK:
        if _DB is not None:
            return
        _DB = ProbeCacheDB(cfg.db_path)
        atexit.register(fechar_cache)


def fechar_cache() -> None:
    global _DB
    with _CACHE_LOCK:
        if _DB is not None:
            _DB.close()
            _DB = None


def limpar_cache(cfg: "Config") -> None:
    with _CACHE_LOCK:
        _MEM_CACHE.clear()
        if _DB is not None:
            _DB.limpar()


def _chave_cache(path: Path) -> tuple[str, int, float]:
    try:
        st = path.stat()
    except OSError:
        return ("", 0, 0.0)
    tam_ = int(st.st_size)
    mtime_ns = getattr(st, "st_mtime_ns",
                       int(st.st_mtime * 1_000_000_000))
    mtime_ = float(int(st.st_mtime))
    chave = hashlib.sha1(
        f"{tam_}:{mtime_ns}".encode()).hexdigest()[:16]
    return (chave, tam_, mtime_)


def _info_from_dict(d: dict) -> ProbeInfo:
    return ProbeInfo(
        codec=str(d.get("codec", "") or ""),
        channels=int(d.get("channels", 0) or 0),
        sample_rate=float(d.get("sample_rate", 0.0) or 0.0),
        bitrate=int(d.get("bitrate", 0) or 0),
        duration=float(d.get("duration", 0.0) or 0.0),
        title=str(d.get("title", "") or ""),
        artist=str(d.get("artist", "") or ""),
        comment=str(d.get("comment", "") or ""),
        album=str(d.get("album", "") or ""),
        tem_capa=bool(d.get("tem_capa", False)),
        raw_title=str(d.get("raw_title", "") or ""),
        raw_artist=str(d.get("raw_artist", "") or ""),
    )


def _cache_get(cfg: "Config", chave: str, tam_: int,
               mtime_: float) -> Optional[ProbeInfo]:
    k = (chave, tam_, mtime_)
    with _CACHE_LOCK:
        if k in _MEM_CACHE:
            return _MEM_CACHE[k]
        if _DB is None:
            abrir_cache(cfg)
        if _DB is None:
            return None
        js = _DB.get(chave, tam_, mtime_)
        if js is None:
            return None
        try:
            info = _info_from_dict(json.loads(js))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            try:
                _DB.remover(chave)
            except sqlite3.Error:
                pass
            return None
        _MEM_CACHE[k] = info
        while len(_MEM_CACHE) > _CACHE_MAX:
            try:
                _MEM_CACHE.pop(next(iter(_MEM_CACHE)))
            except (StopIteration, KeyError):
                break
        return info


def _cache_put(cfg: "Config", chave: str, tam_: int,
               mtime_: float, info: ProbeInfo) -> None:
    k = (chave, tam_, mtime_)
    with _CACHE_LOCK:
        _MEM_CACHE[k] = info
        while len(_MEM_CACHE) > _CACHE_MAX:
            try:
                _MEM_CACHE.pop(next(iter(_MEM_CACHE)))
            except (StopIteration, KeyError):
                break
        if _DB is None:
            abrir_cache(cfg)
        if _DB is not None:
            _DB.put(chave, tam_, mtime_,
                    json.dumps(asdict(info), ensure_ascii=False))


def probe_cache_limpar_antigos(con: sqlite3.Connection,
                               antes_dias: int = 90,
                               max_remover: int = 10000) -> int:
    if antes_dias <= 0:
        return 0
    import time as _t
    limite_ts = _t.time() - antes_dias * 86400
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
# Probe raw / parse
# ============================================================================
def probe_raw(path: Path,
              timeout: int = TIMEOUT) -> Optional[ProbeInfo]:
    MAX_BYTES = 16 * 1024 * 1024
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_streams", "-show_format", str(path)]
    try:
        r = subprocess.run(cmd, timeout=timeout,
                           capture_output=True, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    if len(r.stdout) > MAX_BYTES:
        return None
    try:
        data = json.loads(r.stdout.decode("utf-8", errors="replace"))
        return _parse_probe(data)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None


def _parse_probe(data: dict) -> ProbeInfo:
    streams = data.get("streams", []) or []
    fmt = data.get("format", {}) or {}
    info = ProbeInfo()
    audio = next(
        (s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        return info
    info.codec = str(audio.get("codec_name", "") or "")
    try:
        info.channels = int(audio.get("channels", 0) or 0)
    except (ValueError, TypeError):
        info.channels = 0
    try:
        info.sample_rate = round(
            float(audio.get("sample_rate", "0")) / 1000.0, 1)
    except (ValueError, TypeError):
        info.sample_rate = 0.0
    try:
        br_stream = int(audio.get("bit_rate", 0) or 0)
    except (ValueError, TypeError):
        br_stream = 0
    try:
        br_fmt = int(fmt.get("bit_rate", 0) or 0)
    except (ValueError, TypeError):
        br_fmt = 0
    br_bytes = br_fmt or br_stream
    if br_bytes <= 0:
        try:
            dur_f = float(fmt.get("duration", "0") or 0)
            size_f = int(fmt.get("size", 0) or 0)
            if dur_f > 0 and size_f > 0:
                br_bytes = int(size_f * 8 / dur_f)
        except (ValueError, TypeError):
            pass
    if br_bytes > 0:
        info.bitrate = int(br_bytes / 1000 + 0.5)
    for s in streams:
        if s.get("codec_type") != "video":
            continue
        disp = s.get("disposition", {}) or {}
        try:
            if int(disp.get("attached_pic", 0)) == 1:
                info.tem_capa = True
                break
        except (ValueError, TypeError):
            continue
    for src in (fmt, audio):
        try:
            v = float(src.get("duration", "0"))
            if v > 0:
                info.duration = v
                break
        except (ValueError, TypeError):
            continue
    tags_fmt = fmt.get("tags", {}) or {}
    tags_audio = audio.get("tags", {}) or {}

    def _tag(key: str) -> str:
        for src in (tags_fmt, tags_audio):
            for k, v in src.items():
                if k.lower() == key:
                    return str(v or "")
        return ""

    raw_title = _tag("title")
    raw_artist = _tag("artist")
    info.raw_title = raw_title
    info.raw_artist = raw_artist
    info.title = _limpo(raw_title)
    info.artist = _limpo(raw_artist)
    info.comment = _limpo(_tag("comment"))
    info.album = _limpo(_tag("album"))
    return info


def probe(path: Path, cfg: "Config",
          force: bool = False) -> Optional[ProbeInfo]:
    chave, tam_, mtime_ = _chave_cache(path)
    if not chave:
        return None
    if not force:
        with _CACHE_LOCK:
            cached = _cache_get(cfg, chave, tam_, mtime_)
        if cached is not None:
            return cached
    info = probe_raw(path)
    if info is not None and not info.vazio:
        with _CACHE_LOCK:
            _cache_put(cfg, chave, tam_, mtime_, info)
        return info
    return None


# ============================================================================
# Fork handling / precache
# ============================================================================
def _fork_ctx():
    try:
        import multiprocessing as _mp
        return _mp.get_context("fork")
    except (ImportError, ValueError):
        return None


def _reset_child_state() -> None:
    """Zera _DB herdado via fork (parent's connection nao deve ser usada
    nem fechada pelo child -- deixa o kernel fechar o fd no exit do child).
    """
    global _DB
    _DB = None
    _MEM_CACHE.clear()


if hasattr(os, "register_at_fork"):
    try:
        os.register_at_fork(after_in_child=_reset_child_state)
    except Exception:
        pass


def _probe_worker(path_str: str) -> Optional[dict]:
    try:
        info = probe_raw(Path(path_str))
        if info is None or info.vazio:
            return None
        return asdict(info)
    except Exception:
        return None


def precache_batch(paths: Iterable[Path], cfg: "Config",
                   max_workers: Optional[int] = None) -> int:
    if max_workers is None:
        try:
            max_workers = min(os.cpu_count() or 2, 4)
        except Exception:
            max_workers = 2

    faltantes: list[Path] = []
    for p in paths:
        chave, tam_, mtime_ = _chave_cache(p)
        if not chave:
            continue
        with _CACHE_LOCK:
            hit = _cache_get(cfg, chave, tam_, mtime_)
        if hit is None:
            faltantes.append(p)
    if not faltantes:
        return 0

    executados = 0
    LOTE_PRECACHE = 32
    log = logging.getLogger(__name__)

    if max_workers <= 1 or len(faltantes) < 8:
        for p in faltantes:
            info = probe_raw(p)
            if info is not None and not info.vazio:
                chave, tam_, mtime_ = _chave_cache(p)
                if chave:
                    with _CACHE_LOCK:
                        _cache_put(cfg, chave, tam_, mtime_, info)
                    executados += 1
        return executados

    _ctx = _fork_ctx()
    if _ctx is None:
        for ini in range(0, len(faltantes), LOTE_PRECACHE):
            bloco = faltantes[ini:ini + LOTE_PRECACHE]
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = [ex.submit(probe, p, cfg) for p in bloco]
                for f in as_completed(futures):
                    try:
                        if f.result() is not None:
                            executados += 1
                    except Exception as e:
                        log.warning("precache(thread): %s", e)
        return executados
    try:
        for ini in range(0, len(faltantes), LOTE_PRECACHE):
            bloco = faltantes[ini:ini + LOTE_PRECACHE]
            with ProcessPoolExecutor(max_workers=max_workers,
                                     mp_context=_ctx) as ex:
                futures = {
                    ex.submit(_probe_worker, str(p)): p for p in bloco
                }
                for fut in as_completed(futures):
                    p = futures[fut]
                    try:
                        d = fut.result()
                    except Exception as e:
                        log.warning("precache: %s (%s)", e, p)
                        continue
                    if d is None:
                        continue
                    try:
                        info = _info_from_dict(d)
                    except Exception:
                        continue
                    chave, tam_, mtime_ = _chave_cache(p)
                    if not chave:
                        continue
                    with _CACHE_LOCK:
                        _cache_put(cfg, chave, tam_, mtime_, info)
                    executados += 1
    except (ImportError, NotImplementedError, OSError) as e:
        log.warning(
            "ProcessPool indisponivel (%s), usando ThreadPool", e)
        for ini in range(0, len(faltantes), LOTE_PRECACHE):
            bloco = faltantes[ini:ini + LOTE_PRECACHE]
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = [ex.submit(probe, p, cfg) for p in bloco]
                for f in as_completed(futures):
                    try:
                        if f.result() is not None:
                            executados += 1
                    except Exception as e:
                        log.warning("precache(thread): %s", e)
    return executados


# ============================================================================
# Duracao / compat / tageavel
# ============================================================================
def dur(path: Path) -> int:
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration", "-of", "csv=p=0", str(path)]
    try:
        r = subprocess.run(cmd, timeout=TIMEOUT, capture_output=True,
                           text=True, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    try:
        return int(r.stdout.strip().split(".")[0])
    except (ValueError, TypeError):
        return 0


def dur_float(path: Path) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration", "-of", "csv=p=0", str(path)]
    try:
        r = subprocess.run(cmd, timeout=TIMEOUT, capture_output=True,
                           text=True, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0.0
    try:
        return float(r.stdout.strip())
    except (ValueError, TypeError):
        return 0.0


_CODECS_AAC_OK = frozenset({"aac", "mp4a", "aac_latm"})
_FORMATOS_TAGEAVEIS = frozenset(
    {"m4a", "mp4", "mp3", "flac", "ogg", "opus"})


def compat_check(info: Optional[ProbeInfo], ext: str) -> bool:
    if info is None:
        return False
    if ext.lower().lstrip(".") not in ("m4a", "mp4"):
        return False
    if info.codec.lower() not in _CODECS_AAC_OK:
        return False
    return info.channels == 2


def pode_tagear(info: Optional[ProbeInfo], ext: str) -> bool:
    if info is None:
        return False
    ext_l = ext.lower().lstrip(".")
    return ext_l in _FORMATOS_TAGEAVEIS


# ============================================================================
# AAC / transcodificacao
# ============================================================================
_ENCODER_AAC_CACHE: Optional[tuple[str, list[str]]] = None


def detectar_encoder_aac(force: bool = False) -> tuple[str, list[str]]:
    global _ENCODER_AAC_CACHE
    if _ENCODER_AAC_CACHE is not None and not force:
        return _ENCODER_AAC_CACHE
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            timeout=15, capture_output=True, text=True, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _ENCODER_AAC_CACHE = (
            "aac", ["-aac_coder", "twoloop", "-cutoff", "18000"])
        return _ENCODER_AAC_CACHE
    if "libfdk_aac" in r.stdout:
        _ENCODER_AAC_CACHE = ("libfdk_aac", [])
    else:
        _ENCODER_AAC_CACHE = (
            "aac", ["-aac_coder", "twoloop", "-cutoff", "18000"])
    return _ENCODER_AAC_CACHE


def transcodificar_aac(origem: Path, destino: Path,
                       bitrate: str = "128k",
                       timeout: int = 600) -> bool:
    enc, flags = detectar_encoder_aac()
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(origem),
        "-map", "0:a:0", "-map", "0:v?",
        "-disposition:v", "attached_pic",
        "-c:a", enc, "-b:a", bitrate,
    ] + flags + ["-c:v", "copy", str(destino)]
    try:
        r = subprocess.run(cmd, timeout=timeout, capture_output=True,
                           text=True, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return (r.returncode == 0 and destino.is_file()
            and destino.stat().st_size > 0)


# ============================================================================
# ReplayGain
# ============================================================================
def _duracao_ffprobe(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of", "csv=p=0", str(path)],
            timeout=10, capture_output=True, text=True, check=False)
        return float((r.stdout or "0").strip() or 0)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return 0.0


def calcular_replaygain(path: Path, timeout: int = 60,
                        sample_s: float = 0) -> Optional[float]:
    if sample_s > 0:
        dur_ = _duracao_ffprobe(path)
        if dur_ <= 0:
            return None
        inicio = max(0.0, dur_ / 2 - sample_s / 2)
        cmd = ["ffmpeg", "-nostdin", "-hide_banner",
               "-ss", f"{inicio:.2f}", "-t", f"{sample_s:.2f}",
               "-i", str(path), "-af", "loudnorm=print_format=json",
               "-f", "null", "-"]
        try:
            r = subprocess.run(cmd, timeout=timeout, capture_output=True,
                               text=True, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', r.stderr or "")
        if not m:
            return None
        try:
            input_i = float(
                json.loads(m.group(0)).get("input_i", 0.0))
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
        if input_i == 0.0 or input_i < -70.0:
            return None
        return round(-18.0 - input_i, 2)

    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path),
           "-af", "ebur128=peak=true", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, timeout=timeout, capture_output=True,
                           text=True, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    matches = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS",
                         r.stderr or "")
    if not matches:
        return None
    try:
        input_i = float(matches[-1])
    except (ValueError, IndexError):
        return None
    if input_i == 0.0 or input_i < -70.0:
        return None
    return round(-18.0 - input_i, 2)


# ============================================================================
# Tags
# ============================================================================
def gravar_tags(path: Path, artista: str, titulo: str, comentario: str,
                cfg: "Config", info: Optional[ProbeInfo] = None,
                produtor: str = "", replaygain: Optional[float] = None,
                featuring: str = "", track: Optional[int] = None) -> bool:
    if not titulo and not artista:
        return False
    if info is None:
        info = probe(path, cfg)
    if info is None:
        return False
    sz = tam(path)
    if sz == 0:
        return False
    if not espaco_ok(cfg.tmp_dir, sz // 1024 * 2 + 20480):
        log_erro(cfg.log_erros, f"gravar_tags: pouco espaco em TMP para {path}")
        return False
    if not espaco_ok(cfg.raiz_sd, sz // 1024 + 10240):
        log_erro(cfg.log_erros, f"gravar_tags: pouco espaco no destino para {path}")
        return False
    ext_out = path.suffix.lower() or ".m4a"
    out = cfg.tmp_dir / (
        f"t_{os.getpid()}_"
        f"{hashlib.sha1(str(path).encode()).hexdigest()[:8]}{ext_out}"
    )
    out.unlink(missing_ok=True)
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
        "-map", "0:a:0", "-map", "0:v?",
        "-c", "copy", "-disposition:v", "attached_pic",
        "-metadata", f"title={titulo}",
        "-metadata",
        f"artist={(artista + '; ' + featuring) if featuring else artista}",
        "-metadata", f"album_artist={artista}",
        "-metadata", f"comment={comentario}",
    ]
    if info.album:
        cmd += ["-metadata", f"album={info.album}"]
    if produtor:
        cmd += ["-metadata", f"composer={produtor}"]
    if replaygain is not None:
        cmd += ["-metadata", f"REPLAYGAIN_GAIN={replaygain:.2f} dB"]
    if track is not None:
        cmd += ["-metadata", f"track={track}"]
    cmd.append(str(out))
    try:
        r = subprocess.run(cmd, timeout=300, capture_output=True,
                           text=True, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        out.unlink(missing_ok=True)
        return False
    if (r.returncode != 0 or not out.is_file()
            or out.stat().st_size == 0):
        log_erro(cfg.log_erros, f"gravar_tags: ffmpeg falhou em {path}")
        out.unlink(missing_ok=True)
        return False
    if info.duration > 0 and abs(int(info.duration) - dur(out)) > 2:
        log_erro(cfg.log_erros,
                 f"gravar_tags: duracao divergiu em {path}")
        out.unlink(missing_ok=True)
        return False
    old_chave, old_tam, old_mtime = _chave_cache(path)
    if enviar_sd(out, path, cfg.log_erros, cfg):
        out.unlink(missing_ok=True)
        diario_add(cfg.diario, "TAGS", str(path), "", cfg=cfg)
        if old_chave:
            with _CACHE_LOCK:
                _MEM_CACHE.pop((old_chave, old_tam, old_mtime), None)
            try:
                if _DB is not None:
                    _DB.remover(old_chave)
            except sqlite3.Error:
                pass
        return True
    out.unlink(missing_ok=True)
    return False
