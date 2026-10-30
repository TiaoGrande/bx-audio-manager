#!/usr/bin/env python3
"""bx_io.py -- I/O atomico, SHA256, fsync, journal, manifestos, erros.

Depende apenas de bx_kernel. NAO importa bx_core (evita ciclo). Config e
recebido por duck-typing: callers passam o objeto real, o modulo so usa
`cfg.attr` sem isinstance. Anotacoes usam TYPE_CHECKING para mypy.

Split de bx_core (Bloco 2, Parte 2). Funcoes migradas na Parte 4.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import urllib.error as _ue
import urllib.request as _u
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional, Sequence

from bx_kernel import (
    D_LOGS,
    ESTADOS_MANIFESTO,
    MANIFESTO_CAMPOS,
    MANIFESTO_VERSAO,
    PASTAS_BLINDADAS,
    SUFIXO_PARCIAL,
    _limpo,
)

if TYPE_CHECKING:
    from bx_core import Config


# ============================================================================
# Estado (para diario_add fallback legado)
# ============================================================================
_CFG_ATUAL_REF: list = [None]


def _set_cfg_atual(cfg) -> None:
    _CFG_ATUAL_REF[0] = cfg


def _get_cfg_atual():
    return _CFG_ATUAL_REF[0]


# ============================================================================
# Constantes
# ============================================================================
_JOURNAL_LOCK = threading.Lock()
_ERRO_TIPOS_VALIDOS = frozenset({"TRANSITORIO", "PERMANENTE", "LOGICA"})
_DIARIO_REVERSIVEIS = frozenset({"MV", "QUAR"})
_DIARIO_IRREVERSIVEIS = frozenset({
    "RMF", "RMD", "LNK", "BAK", "NOVO", "SUBST", "TAGS", "RECONCILIAR",
})


# ============================================================================
# Primitivas FS
# ============================================================================
def tam(caminho: Path) -> int:
    try:
        return caminho.stat().st_size
    except OSError:
        return 0


def sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    try:
        with caminho.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def log_erro(log_erros: Optional[Path], msg: str) -> None:
    if log_erros is None:
        return
    try:
        log_erros.parent.mkdir(parents=True, exist_ok=True)
        with log_erros.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}\n")
    except OSError:
        pass


def _destino_proibido(destino: Path, raiz: Path) -> bool:
    try:
        d_abs = destino.resolve(strict=False)
        r_abs = raiz.resolve(strict=False)
    except OSError:
        return True
    try:
        rel = d_abs.relative_to(r_abs)
    except ValueError:
        return False
    if not rel.parts:
        return False
    return rel.parts[0] in PASTAS_BLINDADAS


def _fsync(caminho: Path) -> None:
    try:
        fd = os.open(str(caminho), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _fsync_dir(diretorio: Path) -> None:
    try:
        fd = os.open(str(diretorio),
                     os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


# ============================================================================
# Escrita atomica
# ============================================================================
def escrever_atomico(destino: Path, texto: str,
                     encoding: str = "utf-8") -> bool:
    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_name(destino.name + SUFIXO_PARCIAL)
    try:
        parcial.write_text(texto, encoding=encoding)
        _fsync(parcial)
        os.replace(parcial, destino)
        _fsync_dir(destino.parent)
        return True
    except OSError:
        parcial.unlink(missing_ok=True)
        return False


def escrever_bytes_atomico(destino: Path, dados: bytes) -> bool:
    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_name(destino.name + SUFIXO_PARCIAL)
    try:
        parcial.write_bytes(dados)
        _fsync(parcial)
        os.replace(parcial, destino)
        _fsync_dir(destino.parent)
        return True
    except OSError:
        parcial.unlink(missing_ok=True)
        return False


def enviar_sd(origem: Path, destino: Path, log_erros: Optional[Path] = None,
              cfg: Optional["Config"] = None) -> bool:
    """Copia para .bxpart, valida SHA256 single-pass, substitui destino."""
    if cfg is not None and _destino_proibido(destino, cfg.raiz_sd):
        log_erro(log_erros, f"enviar_sd: destino blindado {destino}")
        return False
    parcial = destino.with_name(destino.name + SUFIXO_PARCIAL)
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log_erro(log_erros, f"enviar_sd: mkdir {e}")
        return False

    try:
        fd = os.open(str(parcial),
                     os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    except OSError as e:
        log_erro(log_erros, f"enviar_sd: open .bxpart falhou {e}")
        return False

    h = hashlib.sha256()
    total = 0
    try:
        with origem.open("rb") as src_f, os.fdopen(fd, "wb") as dst_f:
            for chunk in iter(lambda: src_f.read(65536), b""):
                h.update(chunk)
                dst_f.write(chunk)
                total += len(chunk)
            dst_f.flush()
            _fsync(parcial)
    except OSError as e:
        try: parcial.unlink(missing_ok=True)
        except OSError: pass
        log_erro(log_erros, f"enviar_sd: copy {e}")
        return False

    if tam(origem) != total:
        log_erro(log_erros, f"enviar_sd: tamanho diverge {origem} -> {destino}")
        try: parcial.unlink(missing_ok=True)
        except OSError: pass
        return False
    if tam(parcial) != total:
        log_erro(log_erros, f"enviar_sd: copia truncada {parcial}")
        try: parcial.unlink(missing_ok=True)
        except OSError: pass
        return False

    try:
        os.replace(parcial, destino)
        _fsync_dir(destino.parent)
        return True
    except OSError as e:
        log_erro(log_erros, f"enviar_sd: replace {e}")
        try: parcial.unlink(missing_ok=True)
        except OSError: pass
        return False


# ============================================================================
# Journal (antes de diario_add para clareza de leitura)
# ============================================================================
def journal_registrar(cfg, operacao: str, **campos) -> Optional[str]:
    """Append em journal.jsonl com hash-chain (read+append serializados)."""
    try:
        cfg.bx_home.mkdir(parents=True, exist_ok=True)
        p = cfg.bx_home / "journal.jsonl"
        with _JOURNAL_LOCK:
            prev = ""
            if p.is_file():
                for ln in reversed(p.read_text(encoding="utf-8").splitlines()):
                    if ln.strip():
                        try:
                            prev = json.loads(ln).get("hash", "")
                        except json.JSONDecodeError:
                            prev = ""
                        break
            ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            rec = {"ts": ts, "operacao": operacao, "prev": prev}
            rec.update(campos)
            rec["hash"] = hashlib.sha256(
                (prev + json.dumps(
                    {k: v for k, v in rec.items() if k != "hash"},
                    sort_keys=True, ensure_ascii=False)).encode()
            ).hexdigest()[:16]
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return rec["hash"]
    except OSError:
        return None


def journal_listar(cfg, desde: Optional[str] = None,
                   ate: Optional[str] = None) -> list[dict]:
    p = cfg.bx_home / "journal.jsonl"
    if not p.is_file():
        return []
    out: list[dict] = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
            ts = rec.get("ts", "")
            if desde and ts < desde:
                continue
            if ate and ts > ate:
                continue
            out.append(rec)
        except json.JSONDecodeError:
            continue
    return out


def journal_verify(cfg) -> dict:
    p = cfg.bx_home / "journal.jsonl"
    if not p.is_file():
        return {"ok": True, "total": 0, "quebrado_em": None}
    prev = ""
    total = 0
    for i, ln in enumerate(
            p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not ln.strip():
            continue
        total += 1
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            return {"ok": False, "total": total,
                    "quebrado_em": i, "motivo": "json invalido"}
        esperado = hashlib.sha256(
            (prev + json.dumps(
                {k: v for k, v in rec.items() if k != "hash"},
                sort_keys=True, ensure_ascii=False)).encode()
        ).hexdigest()[:16]
        if rec.get("hash") != esperado or rec.get("prev", "") != prev:
            return {"ok": False, "total": total,
                    "quebrado_em": i, "motivo": "hash quebrado"}
        prev = rec.get("hash", "")
    return {"ok": True, "total": total, "quebrado_em": None}


# ============================================================================
# Diario
# ============================================================================
def diario_add(diario: Path, tipo: str, origem: str, destino: str,
               cfg: Optional["Config"] = None) -> None:
    """Registra no diario texto; se cfg e tipo MV/QUAR, espelha no journal."""
    try:
        diario.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
        with diario.open("a", encoding="utf-8") as f:
            f.write(f"{ts}|{tipo}|{origem}|{destino}\n")
    except OSError:
        pass
    _cfg_ef = cfg if cfg is not None else _get_cfg_atual()
    if _cfg_ef is not None and tipo in ("MV", "QUAR"):
        try:
            journal_registrar(_cfg_ef, tipo, origem=str(origem),
                              destino=str(destino))
        except Exception:
            pass


def diario_rotar(diario: Path, raiz_sd: Optional[Path] = None) -> None:
    if not diario.is_file() or diario.stat().st_size == 0:
        return
    if raiz_sd is not None:
        zlogs = raiz_sd / D_LOGS
        if zlogs.is_dir():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            destino = zlogs / f"diario_{ts}.log"
            tmp = destino.with_name(destino.name + SUFIXO_PARCIAL)
            try:
                shutil.copy2(diario, tmp)
                _fsync(tmp)
                os.replace(tmp, destino)
                _fsync_dir(destino.parent)
            except OSError:
                tmp.unlink(missing_ok=True)
            limite = time.time() - 30 * 86400
            for f in zlogs.glob("diario_*.log"):
                try:
                    if f.stat().st_mtime < limite:
                        f.unlink()
                except OSError:
                    pass
    try:
        diario.write_text("", encoding="utf-8")
    except OSError:
        pass


def historico_append(cfg: "Config", resumo: str) -> bool:
    """Append linha unica em cfg.raiz_sd/historico.txt."""
    arq = cfg.raiz_sd / "historico.txt"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        arq.parent.mkdir(parents=True, exist_ok=True)
        with arq.open("a", encoding="utf-8") as f:
            f.write(f"{ts} | {resumo}\n")
        return True
    except OSError:
        return False


def auditar_link_http(video_id: str, timeout: int = 10) -> bool:
    """True=link vivo. False=morto. urllib direto."""
    url = ("https://www.youtube.com/oembed?format=json&url="
           f"https://www.youtube.com/watch?v={video_id}")
    try:
        req = _u.Request(url, headers={"User-Agent": "BX-Audio-Manager/0.23"})
        with _u.urlopen(req, timeout=timeout) as r:
            return getattr(r, "status", 0) == 200
    except _ue.HTTPError as e:
        return e.code not in (404, 410)
    except (_ue.URLError, OSError):
        return True


# ============================================================================
# Rename case-insensitive + mover
# ============================================================================
def _rename_case_insensitive(origem: Path, destino: Path) -> bool:
    """Rename quando origem e destino diferem so em case (FAT/FUSE)."""
    tmp = origem.with_name(origem.name + f".bxtmp{os.getpid()}")
    try:
        origem.rename(tmp)
    except OSError:
        return False
    try:
        tmp.rename(destino)
        return True
    except OSError:
        try:
            tmp.rename(origem)
        except OSError:
            try:
                logging.getLogger(__name__).error(
                    "_rename_case_insensitive: arquivo preso em %s "
                    "(origem=%s destino=%s)", tmp, origem, destino)
            except Exception:
                pass
        return False


def mover(origem: Path, destino: Path, modo: str, diario: Path,
          cfg: Optional["Config"] = None) -> bool:
    """Move atomico, NUNCA sobrescreve destino."""
    if modo != "apply":
        return True
    if origem == destino:
        return True
    if cfg is not None and _destino_proibido(destino, cfg.raiz_sd):
        return False
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    try:
        origem_e_dir = origem.is_dir()
    except OSError:
        return False

    # DIRETORIOS
    if origem_e_dir:
        if destino.exists():
            try:
                if origem.samefile(destino):
                    return True
            except OSError:
                pass
            if str(origem).lower() == str(destino).lower():
                if _rename_case_insensitive(origem, destino):
                    diario_add(diario, "MV", str(origem), str(destino), cfg=cfg)
                    _fsync_dir(destino.parent)
                    return True
                return False
            return False
        try:
            os.rename(str(origem), str(destino))
            diario_add(diario, "MV", str(origem), str(destino), cfg=cfg)
            _fsync_dir(destino.parent)
            return True
        except FileExistsError:
            return False
        except OSError:
            if destino.exists():
                return False
            try:
                shutil.move(str(origem), str(destino))
                diario_add(diario, "MV", str(origem), str(destino), cfg=cfg)
                return True
            except (OSError, shutil.Error):
                return False

    # ARQUIVOS
    if hasattr(os, "link"):
        try:
            os.link(str(origem), str(destino))
            try:
                os.unlink(str(origem))
            except OSError as _e:
                logging.getLogger(__name__).warning(
                    "mover: hardlink OK mas unlink(origem) falhou (%s); "
                    "origem e destino coexistem: %s", _e, str(origem))
            diario_add(diario, "MV", str(origem), str(destino), cfg=cfg)
            _fsync_dir(destino.parent)
            return True
        except FileExistsError:
            try:
                if origem.samefile(destino):
                    return True
            except OSError:
                pass
            if str(origem).lower() == str(destino).lower():
                if _rename_case_insensitive(origem, destino):
                    diario_add(diario, "MV", str(origem), str(destino), cfg=cfg)
                    _fsync_dir(destino.parent)
                    return True
                return False
            return False
        except (OSError, AttributeError, NotImplementedError):
            pass

    try:
        fd = os.open(str(destino),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            if origem.samefile(destino):
                return True
        except OSError:
            pass
        if str(origem).lower() == str(destino).lower():
            if _rename_case_insensitive(origem, destino):
                diario_add(diario, "MV", str(origem), str(destino), cfg=cfg)
                _fsync_dir(destino.parent)
                return True
            return False
        return False
    except (OSError, AttributeError):
        return False

    try:
        src_f = origem.open("rb")
    except OSError:
        os.close(fd)
        try: os.unlink(str(destino))
        except OSError: pass
        return False
    try:
        with src_f, os.fdopen(fd, "wb") as dst_f:
            shutil.copyfileobj(src_f, dst_f)
            dst_f.flush()
            try:
                os.fsync(dst_f.fileno())
            except OSError:
                pass
        os.unlink(str(origem))
        diario_add(diario, "MV", str(origem), str(destino), cfg=cfg)
        _fsync_dir(destino.parent)
        return True
    except OSError:
        try: os.unlink(str(destino))
        except OSError: pass
        return False


# ============================================================================
# Manifesto
# ============================================================================
def montar_manifesto(campos: dict, arquivo=None) -> str:
    """Gera o manifesto na ordem canonica (18 campos)."""
    dados = dict(campos)
    if arquivo is not None:
        try:
            ts = Path(arquivo).stat().st_mtime
            dados["DATA"] = datetime.fromtimestamp(ts).strftime(
                "%Y-%m-%d_%H:%M")
        except OSError:
            pass
    if not dados.get("ORIGEM_MORTA"):
        dados["ORIGEM_MORTA"] = "N"
    if "REPLAYGAIN" not in dados:
        dados["REPLAYGAIN"] = ""
    linhas = []
    for k in MANIFESTO_CAMPOS:
        v = _limpo(dados.get(k, ""))
        linhas.append(f"{k}:{v}")
    return "\n".join(linhas) + "\n"


def achar_manifesto(diretorio: Path, base: str) -> Optional[Path]:
    for estado in ESTADOS_MANIFESTO:
        for sep in (" ", ""):
            c = diretorio / f"{base}{sep}[{estado}].txt"
            if c.is_file():
                return c
    return None


def rm_manifestos(diretorio: Path, base: str, manter: Optional[Path],
                  diario: Path) -> None:
    for estado in ESTADOS_MANIFESTO:
        for sep in (" ", ""):
            f = diretorio / f"{base}{sep}[{estado}].txt"
            if not f.is_file():
                continue
            if manter is not None and (f == manter):
                continue
            try:
                if manter is not None and f.samefile(manter):
                    continue
            except OSError:
                pass
            try:
                f.unlink()
                diario_add(diario, "RM", str(f), "")
            except OSError:
                pass


def mget(manifesto: Path, chave: str) -> str:
    prefixo = f"{chave}:"
    try:
        with manifesto.open("r", encoding="utf-8", errors="replace") as f:
            for linha in f:
                if linha.startswith(prefixo):
                    return linha[len(prefixo):].strip()
    except OSError:
        pass
    return ""


def manif_campo(manifesto: Path, chave: str, valor: str) -> bool:
    if not manifesto.is_file():
        return False
    try:
        linhas = manifesto.read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    prefixo = f"{chave}:"
    novo: list[str] = []
    achou = False
    for linha in linhas:
        if linha.startswith(prefixo):
            novo.append(f"{chave}:{valor}")
            achou = True
        else:
            novo.append(linha)
    if not achou:
        novo.append(f"{chave}:{valor}")
    return escrever_atomico(manifesto, "\n".join(novo) + "\n")


def manif_caminho(manifesto: Path, novo_rel: str) -> bool:
    return manif_campo(manifesto, "CAMINHO", novo_rel)


def validar_manifesto(path: Path) -> list[str]:
    """Retorna lista de erros estruturais. Vazio = manifesto valido."""
    erros: list[str] = []
    if not path.is_file():
        return ["arquivo nao existe"]
    try:
        texto = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [f"leitura falhou: {e}"]
    chaves: dict[str, str] = {}
    for i, linha in enumerate(texto.splitlines(), 1):
        if not linha.strip():
            continue
        if ":" not in linha:
            erros.append(f"linha {i}: sem ':'")
            continue
        k, _, v = linha.partition(":")
        chaves[k.strip()] = v
    esperadas = set(MANIFESTO_CAMPOS)
    recebidas = set(chaves.keys())
    faltando = esperadas - recebidas
    extras = recebidas - esperadas
    if faltando:
        erros.append(f"faltando: {sorted(faltando)}")
    if extras:
        erros.append(f"extras: {sorted(extras)}")
    estado = chaves.get("ESTADO", "")
    if estado and estado not in ESTADOS_MANIFESTO:
        erros.append(f"ESTADO invalido: {estado!r}")
    versao = chaves.get("VERS\u00c3O", "")
    if versao and versao != MANIFESTO_VERSAO:
        erros.append(f"VERS\u00c3O divergente: {versao!r}")
    return erros


# ============================================================================
# Erros estruturados (JSON-lines)
# ============================================================================
def erro_registrar(cfg, tipo: str, operacao: str, alvo: str,
                   motivo: str, **extra) -> Optional[str]:
    """Append JSON-line em erros_pendentes.jsonl. Retorna id ou None."""
    if tipo not in _ERRO_TIPOS_VALIDOS:
        tipo = "LOGICA"
    try:
        cfg.bx_home.mkdir(parents=True, exist_ok=True)
        p = cfg.bx_home / "erros_pendentes.jsonl"
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        _alvo_hash = hashlib.sha256(str(alvo).encode("utf-8")).hexdigest()[:4]
        errid = (f"{ts.split('.')[0].replace('-', '').replace(':', '')}"
                 f"-{os.getpid()}-{_alvo_hash}")
        rec = {"id": errid, "ts": ts, "tipo": tipo,
               "operacao": operacao, "alvo": alvo, "motivo": motivo}
        rec.update(extra)
        rec["hash"] = hashlib.sha256(
            json.dumps(rec, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return errid
    except OSError:
        return None


def erro_listar(cfg, tipo: Optional[str] = None) -> list[dict]:
    p = cfg.bx_home / "erros_pendentes.jsonl"
    if not p.is_file():
        return []
    out: list[dict] = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
            if tipo and rec.get("tipo") != tipo:
                continue
            out.append(rec)
        except json.JSONDecodeError:
            continue
    return out


def erro_remover(cfg, errid: str) -> bool:
    p = cfg.bx_home / "erros_pendentes.jsonl"
    if not p.is_file():
        return False
    linhas: list[str] = []
    removido = False
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
            if rec.get("id") == errid:
                removido = True
                continue
        except json.JSONDecodeError:
            pass
        linhas.append(ln)
    if removido:
        escrever_atomico(p, "\n".join(linhas) + ("\n" if linhas else ""))
    return removido


def erro_limpar_antigos(cfg, antes_dias: int = 7) -> int:
    p = cfg.bx_home / "erros_pendentes.jsonl"
    if not p.is_file():
        return 0
    limite = datetime.now() - timedelta(days=antes_dias)
    limite_str = limite.strftime("%Y-%m-%dT%H:%M:%S")
    linhas: list[str] = []
    removidos = 0
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
            if rec.get("ts", "") < limite_str:
                removidos += 1
                continue
        except json.JSONDecodeError:
            pass
        linhas.append(ln)
    if removidos:
        escrever_atomico(p, "\n".join(linhas) + ("\n" if linhas else ""))
    return removidos


# ============================================================================
# Espaco livre / mtime / duplicatas (helpers puros do FS)
# ============================================================================
def espaco_ok(diretorio: Path, min_kb: int) -> bool:
    try:
        st = os.statvfs(str(diretorio))
    except OSError:
        return False
    bloco = st.f_frsize or st.f_bsize
    if bloco <= 0:
        return False
    return (st.f_bavail * bloco) // 1024 >= min_kb


def _st_mtime(path: Optional[Path]) -> float:
    if path is None:
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _formato_score(path: Optional[Path]) -> int:
    """AAC/M4A(3) > FLAC(2) > MP3(1) > outro(0)."""
    if path is None:
        return 0
    try:
        ext = path.suffix.lower().lstrip(".")
        if ext in ("m4a", "mp4"):
            return 3
        if ext == "flac":
            return 2
        if ext == "mp3":
            return 1
    except Exception:
        pass
    return 0


def _vencedor_dup(bitrate_novo: int, bitrate_existente: int,
                  path_novo: Optional[Path] = None,
                  path_existente: Optional[Path] = None) -> bool:
    """True = novo vence. Ordem: formato > bitrate > mtime mais antigo."""
    sc_novo = _formato_score(path_novo)
    sc_exist = _formato_score(path_existente)
    if sc_novo != sc_exist:
        return sc_novo > sc_exist
    if bitrate_novo > 0 and bitrate_existente > 0:
        if bitrate_novo != bitrate_existente:
            return bitrate_novo > bitrate_existente
    elif bitrate_novo > 0 and bitrate_existente == 0:
        return True
    elif bitrate_novo == 0 and bitrate_existente > 0:
        return False
    if path_novo is None or path_existente is None:
        return False
    mt_novo = _st_mtime(path_novo)
    mt_exist = _st_mtime(path_existente)
    if mt_novo == 0.0 or mt_exist == 0.0:
        return False
    if mt_novo == mt_exist:
        try:
            lrc_novo = path_novo.with_suffix(".lrc").is_file()
            lrc_exist = path_existente.with_suffix(".lrc").is_file()
        except OSError:
            return False
        if lrc_novo != lrc_exist:
            return lrc_novo
        return False
    return mt_novo < mt_exist


def mover_para_substituidos(arq: Path, cfg: "Config",
                            motivo: str) -> Optional[Path]:
    """Move arq + familia para Z_substituidos/."""
    if not arq.is_file():
        return None
    if cfg.modo != "apply":
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefixo = f"{ts}_{motivo}_"
    destino = cfg.subst / f"{prefixo}{arq.name}"
    i = 1
    while destino.exists():
        destino = cfg.subst / f"{prefixo}{i}_{arq.name}"
        i += 1
    base_antigo = arq.stem
    parent_antigo = arq.parent
    if not mover(arq, destino, cfg.modo, cfg.diario, cfg):
        return None
    if cfg.modo == "apply":
        base_novo = destino.stem

        def _alvo_livre(sufixo: str) -> Path:
            alvo = cfg.subst / f"{base_novo}{sufixo}"
            j = 1
            while alvo.exists():
                alvo = cfg.subst / f"{base_novo}_{j}{sufixo}"
                j += 1
            return alvo

        for suf in (".lrc", ".lrc.org", ".pdf", ".jpg", ".jpeg", ".png"):
            lat = parent_antigo / f"{base_antigo}{suf}"
            if lat.is_file():
                mover(lat, _alvo_livre(suf), cfg.modo, cfg.diario, cfg)
        m = achar_manifesto(parent_antigo, base_antigo)
        if m is not None:
            sufixo_m = m.name[len(base_antigo):]
            mover(m, _alvo_livre(sufixo_m), cfg.modo, cfg.diario, cfg)
    return destino
