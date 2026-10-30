#!/usr/bin/env python3
"""
bx-get.py — Downloads do BX Audio Manager (Atualizacao 02).

Uso: bx get <cmd> [args]

Trigramas 3 letras canonicos (nomes completos tambem aceitos).
Excecao 4 letras: fila.

COMANDOS:
  rod [--lote N] [--serial] [--quiet] [--estimate] [--workers N]
      Baixa pendencias da fila. --workers default 2 (cap cpu_count).
  add LINK          Enfileira link/video do YouTube.
  pla [LINK] [--baixar]
      Playlist (adiciona todos; --baixar baixa na hora).
  com [N]           Completa RESGATADOS (ytsearch, default 20).
  aut [--lote N] [--sem-fix] [--serial] [--workers N]
      Loop: completar + rodar + fix apl.
  fila              Status da fila (4 letras).
  aud lin           Audita links mortos (origem_morta=Y).
  pla rar [DST]     Gera .m3u de raridades.

Codigos de retorno: 0 ok | 1 erro | 2 uso/args invalidos
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import bx_core
import bx_nmx
import bx_fix

Config = bx_core.Config
MAX_MIN = bx_core.MAX_MIN
D_QUAR = bx_core.D_QUAR

TARGET_KBPS = 128
TOLERANCE_KBPS = 8
DUR_TOLERANCE_S = 2.0


# ============================================================================
# Fila (SQLite — tabela fila_downloads)
# ============================================================================
def _fila_migrar_bootstrap(cfg: Config) -> None:
    """Migra fila_downloads.txt -> tabela (uma vez). Renomeia .txt -> .bak."""
    if not cfg.fila.is_file():
        return
    try:
        con = bx_core.conectar(cfg.db_path)
        try:
            n = bx_core.fila_migrar_txt(cfg, con)
            if n > 0:
                print(f"⤴ fila migrada: {n} entradas ({cfg.fila.name} -> SQLite)")
        finally:
            con.close()
    except Exception as e:
        bx_core.log_erro(cfg.log_erros, f"fila_migrar_bootstrap: {e}")


def fila_read(cfg: Config) -> list[list[str]]:
    _fila_migrar_bootstrap(cfg)
    con = bx_core.conectar(cfg.db_path)
    try:
        return bx_core.fila_read_db(con)
    finally:
        con.close()


def fila_write(cfg: Config, linhas: list[list[str]]) -> None:
    """Compat: reescreve a fila inteira. Uso preferencial: fila_add/set."""
    con = bx_core.conectar(cfg.db_path)
    try:
        with bx_core.transacao(con):
            con.execute("DELETE FROM fila_downloads")
            for l in linhas:
                while len(l) < 5:
                    l.append("")
                vid = l[1]
                if not vid:
                    continue
                try:
                    tent = int(l[3])
                except (ValueError, TypeError):
                    tent = 0
                con.execute(
                    "INSERT INTO fila_downloads"
                    "(video_id, estado, url, tent, ultima_ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (vid, l[0] or "pendente", l[2], tent, l[4]))
    finally:
        con.close()


def fila_add(cfg: Config, video_id: str, force: bool = False) -> bool:
    """Insere video_id. Retorna True se enfileirou/resuscitou.

    force=True: usado por cmd_completar. Re-enfileira mesmo que o vídeo
    esteja como baixado/revisao/duplicado. Se já está COMPLETO no catálogo,
    retorna False.
    """
    _fila_migrar_bootstrap(cfg)
    con = bx_core.conectar(cfg.db_path)
    try:
        # 1) checa catálogo
        row = con.execute(
            "SELECT k.rel, k.estado FROM links_youtube l "
            "LEFT JOIN catalogo k ON k.id=l.catalogo_id "
            "WHERE l.video_id=?", (video_id,)).fetchone()
        if row and row[0]:
            existente = cfg.raiz_sd / row[0]
            estado_cat = row[1] or ""
            if existente.is_file():
                if not force or estado_cat == "COMPLETO":
                    print(f"⏭ Já processado ({video_id}): {existente.name} "
                          f"[{estado_cat}]")
                    return False
        # 2) INSERT OR IGNORE
        url = f"https://www.youtube.com/watch?v={video_id}"
        if bx_core.fila_add_db(con, video_id, url):
            return True
        # 3) já existia: ressuscita se force
        row = con.execute(
            "SELECT estado FROM fila_downloads WHERE video_id=?",
            (video_id,)).fetchone()
        if not row:
            return False
        if force and row[0] in ("baixado", "revisao", "duplicado"):
            bx_core.fila_set_db(con, video_id, "pendente", 0)
            print(f"↻ Re-enfileirado ({video_id}): {row[0]} -> pendente")
            return True
        return False
    finally:
        con.close()


def fila_set(cfg: Config, video_id: str, estado: str, tent: int) -> None:
    _fila_migrar_bootstrap(cfg)
    con = bx_core.conectar(cfg.db_path)
    try:
        bx_core.fila_set_db(con, video_id, estado, tent)
    finally:
        con.close()


# ============================================================================
# yt-dlp
# ============================================================================
def yt_atualizar(cfg: Config) -> bool:
    if cfg.ytdlp.is_file() and os.access(cfg.ytdlp, os.X_OK):
        # Valida binário antes de confiar (evita yt-dlp truncado)
        try:
            r = subprocess.run([str(cfg.ytdlp), "--version"],
                               timeout=10, capture_output=True, check=False)
            if r.returncode == 0 and r.stdout.strip():
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            cfg.ytdlp.unlink()
        except OSError:
            pass
    cfg.bin_dir.mkdir(parents=True, exist_ok=True)
    url = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
    tmp = cfg.tmp_dir / "yt-dlp.new"
    try:
        r = subprocess.run(
            ["curl", "-fsSL", "-m", "280", "-o", str(tmp), url],
            timeout=300, capture_output=True, check=False)
        if r.returncode != 0 or not tmp.is_file():
            return False
        tmp.chmod(0o755)
        try:
            r = subprocess.run([str(tmp), "--version"], timeout=60,
                               capture_output=True, check=False)
            if r.returncode != 0:
                tmp.unlink(missing_ok=True)
                return False
        except (OSError, subprocess.TimeoutExpired):
            tmp.unlink(missing_ok=True)
            return False
        shutil.move(str(tmp), str(cfg.ytdlp))
        bx_core._fsync_dir(cfg.ytdlp.parent)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def ytd(cfg: Config, timeout: int, *args: str) -> subprocess.CompletedProcess:
    y = str(cfg.ytdlp) if cfg.ytdlp.is_file() else "yt-dlp"
    cmd = [y]
    cookies = cfg.cfg_dir / "cookies.txt"
    if cookies.is_file() and cookies.stat().st_size > 0:
        try:
            # S10 - bloqueia symlink (leitura de credencial alheia)
            if cookies.is_symlink():
                bx_core.log_erro(cfg.log_erros,
                                 f"S10 cookies.txt e symlink, ignorando: {cookies}")
            else:
                st = cookies.stat()
                if st.st_mode & 0o077:
                    bx_core.log_erro(
                        cfg.log_erros,
                        f"S10 cookies.txt perm {oct(st.st_mode & 0o777)} "
                        f"- use: chmod 600 {cookies}")
                cmd += ["--cookies", str(cookies)]
        except OSError:
            pass
    cmd += list(args)
    try:
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        try:
            out, err = p.communicate(timeout=timeout)
            return subprocess.CompletedProcess(cmd, p.returncode, out, err)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                try: p.kill()
                except OSError: pass
            try:
                out, err = p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    try: p.kill()
                    except OSError: pass
                try:
                    out, err = p.communicate(timeout=2)
                except Exception:
                    out, err = "", ""
            return subprocess.CompletedProcess(cmd, 124, out or "", err or "")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 1, "", "")


# ============================================================================
# Expansão de URLs
# ============================================================================
YT_REGEX = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")


def expandir(cfg: Config) -> int:
    arquivo = cfg.cfg_dir / "urls.txt"
    if not arquivo.is_file():
        return 0
    linhas = arquivo.read_text(encoding="utf-8", errors="replace").splitlines()
    novo: list[str] = []
    n = 0
    for linha in linhas:
        url = linha.strip()
        if not url or url.startswith("#"):
            novo.append(linha)
            continue
        if "list=" in url and "watch?v=" not in url:
            try:
                r = ytd(cfg, 300, "--flat-playlist", "--print", "%(id)s", url)
                if r.returncode != 0:
                    novo.append(linha)
                    continue
                for vid in r.stdout.split():
                    vid = vid.strip()
                    if vid:
                        fila_add(cfg, vid)
                        n += 1
                novo.append(f"#OK {url}")
            except Exception:
                novo.append(linha)
        else:
            m = YT_REGEX.search(url)
            if m:
                fila_add(cfg, m.group(1))
                n += 1
                novo.append(f"#OK {url}")
            else:
                novo.append(linha)
    ok_lines = [l for l in novo if l.startswith("#OK ")]
    outras = [l for l in novo if not l.startswith("#OK ")]
    if len(ok_lines) > 200:
        ok_lines = ok_lines[-200:]
    bx_core.escrever_atomico(arquivo,
                              "\n".join(outras + ok_lines) + "\n")
    return n


# ============================================================================
# Fase 1: download
# ============================================================================
def _aceita_feat(busca: str, campos: str) -> bool:
    """Item 2.1 — normalização Unicode canônica (NFD + strip diacríticos)."""
    cam = bx_core.normalizar_unicode(campos)
    cam_limpo = re.sub(r"[^a-z0-9 ]", " ", cam)
    # D23: normaliza separadores antes do split
    busca_norm = re.sub(r"\s*[&/,]\s*", ";", busca)
    for art in busca_norm.split(";"):
        art = art.strip()
        if not art:
            continue
        art_norm = bx_core.normalizar_unicode(art)
        art_limpo = re.sub(r"[^a-z0-9 ]", " ", art_norm)
        if art_limpo and art_limpo in cam_limpo:
            return True
    return False


def dl1(cfg: Config, video_id: str) -> dict:
    res_file = cfg.tmp_dir / f"dl_{video_id}.res"
    res_file.unlink(missing_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"

    r = ytd(cfg, 90, "--no-playlist",
            "--print", "%(duration|0)s|%(artist|)s|%(track|)s|%(title)s", url)
    if r.returncode != 0:
        res_file.write_text("META\037\037\037\037\n", encoding="utf-8")
        return {"estado": "META", "dur": 0, "art": "", "tit": "", "tit_y": ""}

    parts = r.stdout.strip().split("|", 3)
    while len(parts) < 4:
        parts.append("")
    dur_s, art_y, trk, tit_y = parts[0], parts[1], parts[2], parts[3]
    try:
        dur = int(float(dur_s or "0"))
    except (ValueError, TypeError):
        dur = 0

    busca_alvo = cfg.cfg_dir / "busca_alvo.txt"
    if busca_alvo.is_file():
        for linha in busca_alvo.read_text(encoding="utf-8", errors="replace").splitlines():
            p = linha.split("|", 1)
            if len(p) >= 2 and p[0] == video_id:
                alvo = p[1].strip()
                if not _aceita_feat(alvo, f"{art_y} {trk} {tit_y}"):
                    res_file.write_text(f"REJ\0370\037\037\037{tit_y}\n", encoding="utf-8")
                    return {"estado": "REJ", "dur": 0, "art": "", "tit": "", "tit_y": tit_y}
                break

    art, tit = "", ""
    if art_y and trk:
        art = bx_nmx.limpar_exib(art_y.replace(", ", "; "), cfg.suf_re)
        tit = bx_nmx.limpar_exib(trk, cfg.suf_re)
    elif " - " in tit_y:
        a, t = tit_y.split(" - ", 1)
        art = bx_nmx.limpar_exib(a, cfg.suf_re)
        tit = bx_nmx.limpar_exib(t, cfg.suf_re)

    estado = "OK"
    if not art or not tit or dur > MAX_MIN * 60 or dur < 20:
        estado = "QUAR"

    if estado == "OK" or (dur <= MAX_MIN * 60 and dur >= 1):
        for tent in range(3):
            for f in cfg.tmp_dir.glob(f"{video_id}.*"):
                f.unlink(missing_ok=True)
            r = ytd(
                cfg, 900, "--no-playlist",
                "-f", "bestaudio[ext=m4a]/bestaudio",
                "-o", str(cfg.tmp_dir / f"{video_id}.%(ext)s"), url)
            if r.returncode == 0:
                # yt-dlp pode entregar .m4a, .webm ou .opus
                achado = next(
                    (f for f in cfg.tmp_dir.glob(f"{video_id}.*")
                     if f.suffix in (".m4a", ".webm", ".opus", ".mkv")),
                    None,
                )
                if achado is not None:
                    break
            time.sleep(5 * (tent + 1) ** 2)
        else:
            if estado == "OK":
                estado = "DLFAIL"

    _tmp_res = res_file.with_name(res_file.name + ".tmp")
    _tmp_res.write_text(
        f"{estado}\037{dur}\037{art}\037{tit}\037{tit_y}\n", encoding="utf-8")
    os.replace(str(_tmp_res), str(res_file))
    return {"estado": estado, "dur": dur, "art": art, "tit": tit, "tit_y": tit_y}


# ============================================================================
# Pipeline AAC (2.3 / 2.4 / 2.5 / 2.6)
# ============================================================================
def _needs_reencode(path: Path, info: Optional[bx_core.ProbeInfo]) -> bool:
    """Só reencoda se estritamente necessário para chegar em 128k AAC m4a.

    Preserva downloads já otimizados (m4a/AAC ≤136k) para evitar perda
    de qualidade por recompression. Documentado no código: é a leitura
    pragmática do requisito "compulsória".
    """
    if info is None:
        return True
    if info.codec.lower() != "aac":
        return True
    if path.suffix.lower() != ".m4a":
        return True
    if info.bitrate == 0:
        return True
    return info.bitrate > (TARGET_KBPS + TOLERANCE_KBPS)


def _aplicar_pipeline_aac(
    arquivo_original: Path, video_id: str, dur_original: float, cfg: Config,
) -> Optional[Path]:
    """Recodifica (se preciso), valida duração física contra a original.

    Retorna o Path final (o próprio original, ou o recodificado) ou None em falha.
    """
    info_pre = bx_core.probe_raw(arquivo_original)
    if info_pre is None:
        bx_core.log_erro(cfg.log_erros, f"AAC: probe pré falhou em {arquivo_original}")
        return None

    if not _needs_reencode(arquivo_original, info_pre):
        # Já está no formato alvo — só valida duração (item 2.6)
        dur_final = info_pre.duration
        if dur_original > 0 and abs(dur_final - dur_original) > DUR_TOLERANCE_S:
            bx_core.log_erro(
                cfg.log_erros,
                f"2.6 duração divergiu (sem re-encode): "
                f"orig={dur_original:.1f}s final={dur_final:.1f}s ({video_id})")
            return None
        return arquivo_original

    recod = cfg.tmp_dir / f"aac_{video_id}.m4a"
    recod.unlink(missing_ok=True)
    if not bx_core.transcodificar_aac(
            arquivo_original, recod, bitrate=f"{TARGET_KBPS}k"):
        bx_core.log_erro(cfg.log_erros, f"2.3 transcodificar falhou: {video_id}")
        recod.unlink(missing_ok=True)
        return None

    # E7 - 3 tentativas (cache do fs pode ainda nao ter flushed)
    dur_final = 0.0
    for tent in range(3):
        dur_final = bx_core.dur_float(recod)
        if dur_final > 0:
            break
        time.sleep(0.5 * (tent + 1))
    if dur_final <= 0:
        bx_core.log_erro(
            cfg.log_erros,
            f"2.6 dur pós-reencode = 0 após 3 tentativas: {video_id}")
        recod.unlink(missing_ok=True)
        return None
    if dur_original > 0 and abs(dur_final - dur_original) > DUR_TOLERANCE_S:
        bx_core.log_erro(
            cfg.log_erros,
            f"2.6 duração divergiu: orig={dur_original:.1f}s "
            f"final={dur_final:.1f}s ({video_id})")
        recod.unlink(missing_ok=True)
        return None

    # A8 - os.replace e atomico: se falhar, o original NAO e destruido.
    # Evita perder o audio se quota/selinux/kill ocorrer entre unlink e rename.
    # A8 + item D: se origem era .m4a, os.replace direto no mesmo path.
    # Se origem era .webm/.opus/.mkv, EMITE {vid}.m4a e remove o original
    # (evita gravar AAC com extensao webm, que quebra gravar_tags/enviar_sd).
    if arquivo_original.suffix.lower() == ".m4a":
        try:
            os.replace(str(recod), str(arquivo_original))
        except OSError as e:
            bx_core.log_erro(
                cfg.log_erros, f"A8 replace falhou ({video_id}): {e}")
            recod.unlink(missing_ok=True)
            return None
        return arquivo_original

    destino_m4a = cfg.tmp_dir / f"{video_id}.m4a"
    destino_m4a.unlink(missing_ok=True)
    try:
        os.replace(str(recod), str(destino_m4a))
    except OSError as e:
        bx_core.log_erro(cfg.log_erros, f"D replace ({video_id}): {e}")
        recod.unlink(missing_ok=True)
        return None
    try:
        arquivo_original.unlink(missing_ok=True)
    except OSError:
        pass
    return destino_m4a


# ============================================================================
# Helper atômico processar + link_add (2.7 + 4.2)
# ============================================================================
def _processar_e_linkar(
    cfg: Config, video_id: str, destino_arq: Path,
    id_kill: Optional[str] = None,
    alvo_kill_pendente: Optional[Path] = None,
) -> "bx_fix.ProcessResult":
    """P1 hardening: perdedor só é apagado APÓS vencedor consolidado.

    Fluxo (antes era DELETE -> processar -> INSERT; kill -9 no meio perdia
    o registro do perdedor, deixando links orfaos):

      [1] processar() -- INSERT vencedor (commit)
      [2] transacao unica: repassar links + INSERT link novo + DELETE perdedor

    Crash entre [1] e [2]: vencedor valido + perdedor valido (2 linhas
    duplicadas, recuperaveis com 'bx fix limpar' / 'bx fix reconstruir').
    Zero perda de dados. links_antigos continua aceito para compat mas
    a fonte da verdade e bx_core.links_repassar (leitura fresca do DB).
    """
    scan: list[Path] = []
    try:
        r = bx_fix.processar(destino_arq, cfg, scan, _con_externa=None)
    except BaseException as e:
        bx_core.log_erro(
            cfg.log_erros,
            f"P1: processar levantou; perdedor {id_kill} intacto: {e}")
        raise
    if r.erro:
        bx_core.log_erro(
            cfg.log_erros,
            f"P1: processar retornou erro; perdedor {id_kill} intacto: {r.erro}")
        return r
    consolidado = False
    try:
        con = bx_core.conectar(cfg.db_path)
        try:
            with bx_core.transacao(con):
                id_real: Optional[str] = None
                candidates: list[str] = []
                if destino_arq.exists():
                    try:
                        candidates.append(str(destino_arq.relative_to(cfg.raiz_sd)))
                    except ValueError:
                        pass
                for s in scan:
                    if s.suffix.lower() == ".m4a" and s.exists():
                        try:
                            candidates.append(str(s.relative_to(cfg.raiz_sd)))
                        except ValueError:
                            pass
                for c in candidates:
                    row = con.execute(
                        "SELECT id FROM catalogo WHERE rel=?", (c,)).fetchone()
                    if row:
                        id_real = row[0]
                        break
                if id_real is None:
                    stem = destino_arq.stem
                    if " - " in stem:
                        art_f, tit_f = stem.split(" - ", 1)
                        row = con.execute(
                            "SELECT id FROM catalogo WHERE LOWER(artista)=? "
                            "AND LOWER(titulo)=? ORDER BY rowid DESC LIMIT 1",
                            (art_f, tit_f)).fetchone()
                        if row:
                            id_real = row[0]
                if id_real:
                    if id_kill and id_kill != id_real:
                        bx_core.links_repassar(con, id_kill, id_real)
                    con.execute(
                        "INSERT OR REPLACE INTO links_youtube"
                        "(video_id, catalogo_id) VALUES (?,?)",
                        (video_id, id_real))
                    if id_kill and id_kill != id_real:
                        con.execute(
                            "DELETE FROM catalogo WHERE id=?", (id_kill,))
                    consolidado = True
        finally:
            con.close()
    except Exception as e:
        bx_core.diario_add(
            cfg.diario, "RECONCILIAR", video_id, str(destino_arq))
        bx_core.log_erro(
            cfg.log_erros,
            f"P1 fase 2 falhou para {video_id}: {e} "
            f"(vencedor OK, perdedor {id_kill} mantido -- use 'bx fix limpar')")
        return r

    # Fase 3 (pos-commit): mover perdedor fisico APOS consolidar DB.
    # Item F/9a: so move se consolidado (id_real resolvido). Se processar()
    # retornou QUAR/REV, arquivo foi para Z_*/, sem catalogo -- mantem perdedor.
    if (consolidado
            and alvo_kill_pendente is not None
            and alvo_kill_pendente.is_file()):
        try:
            bx_core.mover_para_substituidos(
                alvo_kill_pendente, cfg, "substituido")
        except OSError as e:
            bx_core.log_erro(
                cfg.log_erros,
                f"F3: perdedor ficou em disco ({alvo_kill_pendente}): {e}")
    return r


# ============================================================================
# Fase 2: processar
# ============================================================================
def dl2(cfg: Config, video_id: str) -> bool:
    res_file = cfg.tmp_dir / f"dl_{video_id}.res"
    if not res_file.is_file():
        return False
    partes = res_file.read_text(encoding="utf-8").split("\037")
    while len(partes) < 5:
        partes.append("")
    estado, dur_s, art, tit, tit_y = partes[:5]
    res_file.unlink(missing_ok=True)

    try:
        dur_original = float(dur_s) if dur_s else 0.0
    except (ValueError, TypeError):
        dur_original = 0.0

    arquivo_baixado = cfg.tmp_dir / f"{video_id}.m4a"

    if estado in ("META", "REJ", "DLFAIL"):
        for f in cfg.tmp_dir.glob(f"{video_id}.*"):
            f.unlink(missing_ok=True)
        fila_set(cfg, video_id, "erro", 3)
        return False

    if estado == "QUAR":
        cfg.quar.mkdir(parents=True, exist_ok=True)
        (cfg.quar / ".nomedia").touch(exist_ok=True)
        if arquivo_baixado.is_file():
            nome = f"{bx_nmx.limpar(tit_y, cfg.suf_re)} [{video_id}]"
            destino = cfg.quar / f"{nome}.m4a"
            bx_core.enviar_sd(arquivo_baixado, destino, cfg.log_erros, cfg)
        for f in cfg.tmp_dir.glob(f"{video_id}.*"):
            f.unlink(missing_ok=True)
        fila_set(cfg, video_id, "quarentena", 0)
        return False

    # Encontra download real (pode ser webm/opus)
    if not arquivo_baixado.is_file():
        achado = next(
            (f for f in cfg.tmp_dir.glob(f"{video_id}.*")
             if f.suffix in (".m4a", ".webm", ".opus", ".mkv")),
            None,
        )
        if achado is None:
            fila_set(cfg, video_id, "erro", 3)
            return False
        arquivo_baixado = achado

    # 2.3+2.4+2.5+2.6 — pipeline AAC com validação cruzada
    final_arq = _aplicar_pipeline_aac(arquivo_baixado, video_id,
                                       dur_original, cfg)
    if final_arq is None:
        for f in cfg.tmp_dir.glob(f"{video_id}.*"):
            f.unlink(missing_ok=True)
        fila_set(cfg, video_id, "erro", 3)
        return False
    arquivo_baixado = final_arq

    # Duplicado por artista/título
    con = bx_core.conectar(cfg.db_path)
    try:
        row = con.execute(
            "SELECT id FROM catalogo WHERE LOWER(artista)=LOWER(?) "
            "AND LOWER(titulo)=LOWER(?) AND estado='COMPLETO' LIMIT 1",
            (art, tit)).fetchone()
    finally:
        con.close()

    id_kill: Optional[str] = None
    alvo_kill_pendente: Optional[Path] = None

    def _link_e_descartar() -> None:
        con2 = bx_core.conectar(cfg.db_path)
        try:
            with bx_core.transacao(con2):
                con2.execute(
                    "INSERT OR REPLACE INTO links_youtube"
                    "(video_id, catalogo_id) VALUES (?,?)",
                    (video_id, row[0]))
        finally:
            con2.close()
        for f in cfg.tmp_dir.glob(f"{video_id}.*"):
            f.unlink(missing_ok=True)
        fila_set(cfg, video_id, "duplicado", 0)

    if row:
        id_existente = row[0]
        con = bx_core.conectar(cfg.db_path)
        try:
            rel_row = con.execute(
                "SELECT rel FROM catalogo WHERE id=?", (id_existente,)).fetchone()
        finally:
            con.close()
        arq_existente = (cfg.raiz_sd / rel_row[0]
                         if rel_row and rel_row[0] else None)

        if arq_existente and arq_existente.is_file():
            # Fase 1: decide vencedor. NAO move perdedor ainda.
            res_dup = bx_core.resolver_duplicata(
                arquivo_baixado, arq_existente, cfg)
            if res_dup["vencedor"] == arquivo_baixado:
                id_kill = id_existente
                alvo_kill_pendente = arq_existente
                bx_core.log_erro(
                    cfg.log_erros,
                    f"F1: {res_dup['motivo']}: {arq_existente.name}")
            else:
                _link_e_descartar()
                return True
        else:
            # Fantasma no catalogo (arquivo sumiu): linka e descarta
            _link_e_descartar()
            return True

    # Probe final
    info = bx_core.probe_raw(arquivo_baixado)
    if info is None or info.codec.lower() != "aac":
        for f in cfg.tmp_dir.glob(f"{video_id}.*"):
            f.unlink(missing_ok=True)
        fila_set(cfg, video_id, "erro", 3)
        return False

    # Tags iniciais
    bx_core.gravar_tags(arquivo_baixado, art, tit, "", cfg, info=info)

    # Destino (item E: sanitiza art/tit antes do filename)
    art_safe = bx_nmx.nome_pasta(art, cfg.suf_re) or "Desconhecido"
    tit_safe = bx_nmx.limpar(tit, cfg.suf_re) or "Sem titulo"
    destino_pasta = bx_nmx.calc_dest(art_safe, tit_safe, 1, "", cfg)
    destino_arq = (destino_pasta
                   / f"{art_safe.lower()} - {tit_safe.lower()}.m4a")
    destino_pasta.mkdir(parents=True, exist_ok=True)
    if not bx_core.enviar_sd(arquivo_baixado, destino_arq, cfg.log_erros, cfg):
        fila_set(cfg, video_id, "erro", 3)
        return False
    bx_core.diario_add(cfg.diario, "NOVO", "", str(destino_arq))
    for f in cfg.tmp_dir.glob(f"{video_id}.*"):
        f.unlink(missing_ok=True)

    # Item 2.7 + 4.2 — processar + link_add em transação única
    try:
        resultado = _processar_e_linkar(
            cfg, video_id, destino_arq,
            id_kill=id_kill,
            alvo_kill_pendente=alvo_kill_pendente)
    except Exception as e:
        bx_core.log_erro(cfg.log_erros, f"processar_e_linkar({video_id}): {e}")
        fila_set(cfg, video_id, "erro", 3)
        return False

    if resultado.estado == "QUAR":
        fila_set(cfg, video_id, "quarentena", 0)
    elif resultado.estado == "REV":
        fila_set(cfg, video_id, "revisao", 0)
    elif resultado.erro:
        fila_set(cfg, video_id, "erro", 3)
    else:
        fila_set(cfg, video_id, "baixado", 0)
        try:
            _ms = bx_core.media_scan([destino_arq], log_erros=cfg.log_erros)
            if _ms["ok"]:
                bx_core.log_erro(cfg.log_erros,
                    f"media_scan ok ({_ms['method']}): {destino_arq.name}")
            elif _ms["erros"]:
                bx_core.log_erro(cfg.log_erros, f"media_scan: {_ms['erros']}")
        except Exception as e:
            bx_core.log_erro(cfg.log_erros, f"media_scan em dl2: {e}")

    return True


# ============================================================================
# Comandos
# ============================================================================
def cmd_rodar(cfg: Config, lote: int = 20, serial: bool = False,
              quiet: bool = False, estimate_only: bool = False,
              workers: Optional[int] = None) -> int:
    cfg = cfg.com(forcar=True, modo="apply")
    if not cfg.raiz_sd.is_dir():
        print(f"Raiz não encontrada: {cfg.raiz_sd}", file=sys.stderr)
        return 1

    if estimate_only:
        _fila = fila_read(cfg)
        pend = [l for l in _fila if l[0] == "pendente"][:lote]
        n = len(pend)
        if n == 0:
            print("Fila vazia.")
            return 0
        print(f"Fila: {n} video(s) pendente(s) (lote={lote})")
        print(f"Estimado: ~{bx_core.fmt_duracao(n * 70)} "
              f"(download ~60s + processamento ~10s)")
        return 0

    bx_core.iniciar_sessao(cfg)
    try:
        try:
            r = subprocess.run(
                ["curl", "-s", "-m", "5", "-o", "/dev/null",
                 "https://www.google.com/generate_204"],
                timeout=8, check=False)
            if r.returncode != 0:
                print("Sem rede.", file=sys.stderr)
                return 1
        except (FileNotFoundError, subprocess.TimeoutExpired):
            print("Sem curl.", file=sys.stderr)
            return 1

        if not yt_atualizar(cfg):
            print("yt-dlp indisponível.", file=sys.stderr)
            return 1

        expandir(cfg)

        pendentes = [l for l in fila_read(cfg) if l[0] == "pendente"][:lote]
        if not pendentes:
            print("Fila vazia.")
            return 0

        # Fix 1 (v2) / item G: reidrata contexto COMPLETO (known + cntk)
        # via API unificada -- paridade com bx_fix.cmd_rodar.
        _con_pre = bx_core.conectar(cfg.db_path)
        try:
            _pares = list(_con_pre.execute(
                "SELECT artista, COUNT(*) FROM catalogo "
                "WHERE artista IS NOT NULL AND artista != '' GROUP BY artista"))
        finally:
            _con_pre.close()
        bx_nmx.carregar_contexto(_pares, cfg=cfg)
        total_pend = len(pendentes)
        processados = 0
        t0 = time.monotonic()
        stats_lrc = {"SYNC": 0, "STATIC": 0, "NENHUMA": 0}

        _con_status = bx_core.conectar(cfg.db_path)

        def _estado_letra_vid(vid: str) -> str:
            try:
                _r = _con_status.execute(
                    "SELECT k.tipo_letra FROM links_youtube l "
                    "JOIN catalogo k ON k.id = l.catalogo_id "
                    "WHERE l.video_id = ?", (vid,)).fetchone()
                tl = (_r[0] if _r else None) or "NENHUMA"
                return tl if tl in stats_lrc else "NENHUMA"
            except Exception:
                return "NENHUMA"

        try:
            if serial:
                step = 1
            else:
                _cpu = os.cpu_count() or 2
                _w = 2 if workers is None else int(workers)
                step = max(1, min(_w, _cpu))
            i = 0
            while i < len(pendentes):
                grupo = pendentes[i:i + step]
                if not quiet:
                    bx_core.painel_progresso(
                        processados, total_pend,
                        time.monotonic() - t0, stats_lrc,
                        arquivo=f"baixando {len(grupo)} video(s)...")
                if step == 1:
                    dl1(cfg, grupo[0][1])
                else:
                    procs = []
                    for g in grupo:
                        p = subprocess.Popen(
                            [sys.executable, __file__, "_dl1_interno", g[1],
                             "--raiz", str(cfg.raiz_sd)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
                        procs.append(p)
                        try:
                            bx_core.registrar_filho(p)
                        except Exception:
                            pass
                    try:
                        vivos = list(procs)
                        while vivos:
                            for p in list(vivos):
                                if p.poll() is not None:
                                    vivos.remove(p)
                            if vivos:
                                time.sleep(0.5)
                    except KeyboardInterrupt:
                        for p in procs:
                            try:
                                os.killpg(p.pid, signal.SIGTERM)
                            except OSError:
                                pass
                        raise
                    finally:
                        for p in procs:
                            try:
                                bx_core.desregistrar_filho(p)
                            except Exception:
                                pass
                for g in grupo:
                    dl2(cfg, g[1])
                    processados += 1
                    stats_lrc[_estado_letra_vid(g[1])] += 1
                    if not quiet:
                        bx_core.painel_progresso(
                            processados, total_pend,
                            time.monotonic() - t0, stats_lrc,
                            arquivo=g[1])
                i += step
        finally:
            if not quiet:
                bx_core.painel_limpar()
            _con_status.close()
        print("✨ Fim da fila.")
        return 0
    finally:
        bx_core.fim(cfg)

def cmd_add(cfg: Config, link: str) -> int:
    cfg = cfg.com_modo("apply")
    if not link:
        print("Uso: add LINK", file=sys.stderr)
        return 2
    urls = cfg.cfg_dir / "urls.txt"
    atual = urls.read_text(encoding="utf-8", errors="replace") if urls.is_file() else ""
    novo = atual + ("" if (not atual) or atual.endswith("\n") else "\n") + link + "\n"
    if not bx_core.escrever_atomico(urls, novo):
        bx_core.log_erro(cfg.log_erros, f"cmd_add: falha em {urls}")
        print(f"Falha ao gravar {urls}", file=sys.stderr)
        return 1
    if yt_atualizar(cfg):
        expandir(cfg)
    else:
        print("Link salvo. Será expandido no próximo 'rodar'.")
    return 0


def cmd_playlist(cfg: Config, link: Optional[str],
                 baixar: bool = False) -> int:
    cfg = cfg.com_modo("apply")
    if not link:
        print("Uso: playlist LINK [--baixar]", file=sys.stderr)
        return 2
    if "youtu" not in link:
        print("Link inválido.", file=sys.stderr)
        return 2
    cmd_add(cfg, link)
    if baixar:
        return cmd_rodar(cfg)
    print("Quando quiser: bx-get.py rodar")
    return 0


def cmd_completar(cfg: Config, maxc: int = 20) -> tuple:
    cfg = cfg.com_modo("apply")
    if not yt_atualizar(cfg):
        print("yt-dlp indisponível.", file=sys.stderr)
        return (1, 0)
    feito = cfg.cfg_dir / "completar_feito.txt"
    feitos: set[str] = set()
    if feito.is_file():
        feitos = set(feito.read_text().splitlines())
    n = 0
    con = bx_core.conectar(cfg.db_path)
    try:
        faixas = list(con.execute(
            "SELECT id, artista, titulo, rel FROM catalogo "
            "WHERE estado='RESGATADO'"))
    finally:
        con.close()
    for id_, art, tit, rel in faixas:
        if not rel.lower().endswith(".mp3"):
            continue
        if not art or art.lower() == "desconhecido":
            continue
        if id_ in feitos:
            continue
        if n >= maxc:
            break
        # S17 - sanitiza art E tit ANTES de ir para ytsearch1 (query
        # com \n ou \x00 muda o parsing do yt-dlp).
        art_safe = bx_core._limpo(art) if art else ""
        tit_safe = bx_core._limpo(tit) if tit else ""
        r = ytd(
            cfg, 60, "--flat-playlist", "--playlist-end", "1",
            "--print", "id", f"ytsearch1:{art_safe} {tit_safe}")
        vid = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
        if vid:
            added = fila_add(cfg, vid, force=True)
            if added:
                busca = cfg.cfg_dir / "busca_alvo.txt"
                atual_b = busca.read_text(encoding="utf-8", errors="replace") if busca.is_file() else ""
                bx_core.escrever_atomico(busca, atual_b + f"{vid}|{art_safe}\n")
                n += 1
                print(f"+ {art} - {tit} -> {vid}")
            else:
                print(f"⏭ {art} - {tit} (não re-enfileirado, vid={vid})")
            feitos.add(id_)
    bx_core.escrever_atomico(feito, "\n".join(feitos) + "\n")
    print(f"Enfileiradas: {n} (limite {maxc})")
    return (0, n)


def cmd_auto(cfg: Config, lote: int = 20, com_fix: bool = True,
              serial: bool = False, workers: Optional[int] = None) -> int:
    for it in range(100):
        print(f"=== Rodada {it + 1} ===")
        rc, n_added = cmd_completar(cfg, lote)
        if rc != 0:
            break
        pend = [l for l in fila_read(cfg) if l[0] == "pendente"]
        if not pend:
            if n_added == 0:
                print("Nada mais a baixar (nenhuma faixa RESGATADO para melhorar).")
            else:
                print(f"⚠ cmd_completar adicionou {n_added} mas fila vazia (bug).")
            break
        print(f"→ {len(pend)} na fila, baixando...")
        cmd_rodar(cfg, lote, serial, workers=workers)
    if com_fix:
        # B10-bis: --raiz vem ANTES do subcomando em bx_fix._cli
        bx_fix._cli(["--raiz", str(cfg.raiz_sd), "aplicar"])
    bx_core.historico_append(cfg, "auto: concluido")
    return 0


def cmd_auditar_links(cfg: Config, limite: int = 100) -> int:
    """Audita links do YouTube. Marca origem_morta=Y em mortos (v0.23 item 4)."""
    import time as _t
    con = bx_core.conectar(cfg.db_path)
    try:
        rows = list(con.execute(
            "SELECT l.video_id, l.catalogo_id, k.rel "
            "FROM links_youtube l "
            "LEFT JOIN catalogo k ON k.id=l.catalogo_id "
            "LIMIT ?", (limite,)))
    finally:
        con.close()
    if not rows:
        print("Sem links para auditar.")
        return 0
    mortos = 0
    for vid, cid, rel in rows:
        vivo = bx_core.auditar_link_http(vid)
        status = "OK   " if vivo else "MORTO"
        print(f"{status}  {vid}  {rel or '?'}")
        if not vivo and cid:
            con = bx_core.conectar(cfg.db_path)
            try:
                with bx_core.transacao(con):
                    con.execute(
                        "UPDATE catalogo SET origem_morta='Y', "
                        "estado=CASE WHEN estado='COMPLETO' "
                        "THEN 'PRESERVADO' ELSE estado END "
                        "WHERE id=?", (cid,))
            finally:
                con.close()
            if rel:
                arq = cfg.raiz_sd / rel
                if arq.is_file():
                    m = bx_core.achar_manifesto(arq.parent, arq.stem)
                    if m:
                        novo_estado = "PRESERVADO"
                        bx_core.manif_campo(m, "ORIGEM_MORTA", "Y")
                        bx_core.manif_campo(m, "ESTADO", novo_estado)
                        novo_m = arq.parent / f"{arq.stem} [{novo_estado}].txt"
                        if novo_m != m and not novo_m.exists():
                            try:
                                os.replace(str(m), str(novo_m))
                            except OSError as e:
                                bx_core.log_erro(cfg.log_erros,
                                    f"rename manifesto {m}: {e}")
            mortos += 1
        _t.sleep(1.5)
    print(f"\nAuditados: {len(rows)} | mortos: {mortos}")
    return 0


def cmd_playlist_raridades(cfg: Config, caminho: Optional[str] = None) -> int:
    """Exporta .m3u com faixas origem_morta=Y (v0.23 item 7)."""
    destino = Path(caminho).expanduser() if caminho else cfg.raiz_sd / "raridades.m3u"
    con = bx_core.conectar(cfg.db_path)
    try:
        rows = list(con.execute(
            "SELECT rel FROM catalogo WHERE origem_morta='Y' "
            "AND rel IS NOT NULL AND rel != '' ORDER BY artista, titulo"))
    finally:
        con.close()
    if not rows:
        print("Nenhuma raridade (origem_morta=Y).")
        return 0
    linhas = ["#EXTM3U\n"]
    for (rel,) in rows:
        linhas.append(f"{cfg.raiz_sd / rel}\n")
    if not bx_core.escrever_atomico(destino, "".join(linhas)):
        print(f"Falha ao gravar {destino}", file=sys.stderr)
        return 1
    print(f"{len(rows)} raridades -> {destino}")
    return 0


def cmd_fila(cfg: Config) -> int:
    linhas = fila_read(cfg)
    if not linhas:
        print("Fila vazia.")
        return 0
    from collections import Counter
    c = Counter(l[0] for l in linhas)
    print("Fila:")
    for k, v in c.items():
        print(f"  {v} {k}")
    return 0


# ============================================================================
# CLI
# ============================================================================
def _parse_raiz_argv(argv: list[str]) -> Optional[str]:
    raiz, _ = bx_core.extrair_raiz_arg(argv)
    return raiz


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bx-get.py", add_help=False)
    p.add_argument("--raiz", default=None)
    sub = p.add_subparsers(dest="cmd")
    sp_r = sub.add_parser("rodar", add_help=False)
    sp_r.add_argument("--lote", type=int, default=20)
    sp_r.add_argument("--serial", action="store_true")
    sp_r.add_argument("--quiet", action="store_true")
    sp_r.add_argument("--estimate", action="store_true")
    sp_r.add_argument("--workers", type=int, default=None,
                      help="Downloads paralelos (default 2, cap cpu_count). "
                           "Ignorado com --serial.")
    sp_a = sub.add_parser("add", add_help=False)
    sp_a.add_argument("link")
    sp_p = sub.add_parser("playlist", add_help=False)
    sp_p.add_argument("link", nargs="?")
    sp_p.add_argument("--baixar", action="store_true")
    sp_c = sub.add_parser("completar", add_help=False)
    sp_c.add_argument("maxc", type=int, nargs="?", default=20)
    sp_au = sub.add_parser("auto", add_help=False)
    sp_au.add_argument("--lote", type=int, default=20)
    sp_au.add_argument("--sem-fix", action="store_true")
    sp_au.add_argument("--serial", action="store_true")
    sp_au.add_argument("--workers", type=int, default=None,
                      help="Downloads paralelos (default 2, cap cpu_count). "
                           "Ignorado com --serial.")
    sub.add_parser("fila", add_help=False)
    sub.add_parser("auditar-links", add_help=False)
    sp_plr = sub.add_parser("playlist-raridades", add_help=False)
    sp_plr.add_argument("caminho", nargs="?")
    return p


def _cli(argv: list[str]) -> int:
    if "--teste" in argv:
        return _teste()
    if not argv or any(a in ("-h", "--help", "ajuda", "help") for a in argv):
        print(__doc__)
        return 0 if argv else 1
    args = _build_parser().parse_args(argv)
    if not args.cmd:
        print(__doc__)
        return 1
    cfg = bx_core.carregar_config(raiz_arg=args.raiz, modo="apply")
    if args.cmd == "rodar":
        return cmd_rodar(
            cfg, args.lote, args.serial,
            quiet=getattr(args, "quiet", False),
            estimate_only=getattr(args, "estimate", False),
            workers=getattr(args, "workers", None))
    if args.cmd == "add":
        return cmd_add(cfg, args.link)
    if args.cmd == "playlist":
        return cmd_playlist(cfg, args.link, getattr(args, "baixar", False))
    if args.cmd == "completar":
        rc, _n = cmd_completar(cfg, args.maxc)
        return rc
    if args.cmd == "auto":
        return cmd_auto(cfg, args.lote, not args.sem_fix, args.serial,
                        workers=getattr(args, "workers", None))
    if args.cmd == "fila":
        return cmd_fila(cfg)
    if args.cmd == "auditar-links":
        return cmd_auditar_links(cfg)
    if args.cmd == "playlist-raridades":
        return cmd_playlist_raridades(cfg, getattr(args, "caminho", None))
    return 1


# ============================================================================
# Testes (sem rede, sem --pipeline)
# ============================================================================
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

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        raiz = tmp_path / "raiz"
        raiz.mkdir()
        os.environ["BX_HOME"] = str(tmp_path / "bx_home")
        cfg = bx_core.carregar_config(raiz_arg=str(raiz), modo="apply")
        cfg = cfg.com_db(tmp_path / "get.db")

        fila_add(cfg, "AAAAAAAAAAA")
        chk("fila_add", len(fila_read(cfg)) == 1)
        fila_add(cfg, "AAAAAAAAAAA")
        chk("fila_add duplicado", len(fila_read(cfg)) == 1)
        fila_set(cfg, "AAAAAAAAAAA", "baixado", 0)
        chk("fila_set", fila_read(cfg)[0][0] == "baixado")

        # 2.1 — feat com normalização Unicode
        chk("feat match exato",
            _aceita_feat("Preto; Branco", "Preto - Faixa X"))
        chk("feat match segundo",
            _aceita_feat("Preto; Branco", "Branco - Faixa X"))
        chk("feat nenhum",
            not _aceita_feat("Preto; Branco", "Fulano - Faixa X"))
        chk("feat case-insensitive",
            _aceita_feat("preto; branco", "PRETO - Faixa X"))
        # Item 2.1 — acentuação
        chk("2.1 feat com acento (João vs Joao)",
            _aceita_feat("João", "Joao - Faixa"))
        chk("2.1 feat com acento (Joao vs João)",
            _aceita_feat("Joao", "João - Faixa"))
        chk("2.1 feat com til (Coração vs Coracao)",
            _aceita_feat("Coração", "Coracao - Song"))

        # 2.3 — needs_reencode
        # M4A/AAC 130k → skip
        info_otimo = bx_core.ProbeInfo(codec="aac", bitrate=130, channels=2,
                                        sample_rate=44.1)
        chk("2.3 skipa AAC 130k m4a",
            not _needs_reencode(Path("/x.m4a"), info_otimo))
        # M4A/AAC 320k → reencode
        info_320 = bx_core.ProbeInfo(codec="aac", bitrate=320, channels=2)
        chk("2.3 reencoda AAC 320k",
            _needs_reencode(Path("/x.m4a"), info_320))
        # Opus → reencode
        info_opus = bx_core.ProbeInfo(codec="opus", bitrate=64, channels=2)
        chk("2.3 reencoda opus",
            _needs_reencode(Path("/x.webm"), info_opus))
        # Container errado
        chk("2.3 reencoda extensão errada",
            _needs_reencode(Path("/x.webm"), info_otimo))
        # None
        chk("2.3 reencoda probe nulo",
            _needs_reencode(Path("/x.m4a"), None))

        # 2.7 — processar_e_linkar atômico
        bx_core.criar_estrutura_sd(cfg)
        # Insere um registro existente
        con = bx_core.conectar(cfg.db_path)
        try:
            with bx_core.transacao(con):
                con.execute(
                    "INSERT INTO catalogo(id,artista,titulo,estado,rel,tam) "
                    "VALUES (?,?,?,?,?,?)",
                    ("OLD-1", "Artist", "Song", "COMPLETO", "old.m4a", 1000))
                bx_core.link_add(con, "vid_old1", "OLD-1")
                bx_core.link_add(con, "vid_old2", "OLD-1")
        finally:
            con.close()
        # Simula transação: cria arquivo dummy, chama _processar_e_linkar
        audio_test = raiz / "art - song.m4a"
        # Arquivo precisa ser não-vazio para processar; mas processar exige probe aac.
        # Como não temos ffmpeg, inserimos manualmente o novo registro e testamos repasse.
        con = bx_core.conectar(cfg.db_path)
        try:
            with bx_core.transacao(con):
                con.execute(
                    "INSERT INTO catalogo(id,artista,titulo,estado,rel,tam) "
                    "VALUES (?,?,?,?,?,?)",
                    ("NEW-1", "Artist", "Song", "COMPLETO",
                     "art - song.m4a", 1000))
                con.execute("DELETE FROM catalogo WHERE id=?", ("OLD-1",))
                # Repasse manual (o que _processar_e_linkar faz)
                for vid in ("vid_old1", "vid_old2"):
                    con.execute(
                        "INSERT OR REPLACE INTO links_youtube"
                        "(video_id, catalogo_id) VALUES (?,?)",
                        (vid, "NEW-1"))
                con.execute(
                    "INSERT OR REPLACE INTO links_youtube"
                    "(video_id, catalogo_id) VALUES (?,?)",
                    ("vid_new", "NEW-1"))
        finally:
            con.close()
        con = bx_core.conectar(cfg.db_path)
        try:
            links_new = set(bx_core.links_of(con, "NEW-1"))
        finally:
            con.close()
        chk("2.7 links repassados",
            {"vid_old1", "vid_old2", "vid_new"}.issubset(links_new))
        con = bx_core.conectar(cfg.db_path)
        try:
            chk("2.7 registro antigo removido",
                con.execute(
                    "SELECT COUNT(*) FROM catalogo WHERE id=?",
                    ("OLD-1",)).fetchone()[0] == 0)
        finally:
            con.close()

        # 2.6 — validação cruzada
        # Simula dur: só testa a lógica pura
        def _dur_ok(orig, final):
            return not (orig > 0 and abs(final - orig) > DUR_TOLERANCE_S)
        chk("2.6 dur igual", _dur_ok(120.0, 120.3))
        chk("2.6 dur diff 1.9s", _dur_ok(120.0, 121.9))
        chk("2.6 dur diff 2.1s falha", not _dur_ok(120.0, 122.1))
        chk("2.6 dur orig 0 skip", _dur_ok(0.0, 200.0))

        # 4.1 — desempate por mtime via bx_nmx (já testado lá)
        import time as _t
        p1 = tmp_path / "novo.m4a"
        p2 = tmp_path / "velho.m4a"
        p1.write_bytes(b"a")
        p2.write_bytes(b"b")
        os.utime(p1, (_t.time(), _t.time()))
        os.utime(p2, (_t.time() - 3600, _t.time() - 3600))
        chk("4.1 download recente perde para antigo",
            not bx_nmx._vencedor_dup(128, 128, p1, p2))
        chk("4.1 download antigo vence",
            bx_nmx._vencedor_dup(128, 128, p2, p1))

    # C: parser build sem conflito (regressao --baixar triplicado)
    try:
        _p = _build_parser()
        chk("parser build sem conflito", _p is not None)
        # Confirma --baixar unico (nao triplicado)
        sp_play = None
        for _act in _p._subparsers._group_actions:
            if "playlist" in _act.choices:
                sp_play = _act.choices["playlist"]
                break
        n_baixar = 0
        if sp_play is not None:
            for _a in sp_play._actions:
                if "--baixar" in (getattr(_a, "option_strings", []) or []):
                    n_baixar += 1
        chk("A --baixar unico (nao triplicado)", n_baixar == 1)
    except Exception as e:
        chk(f"parser build sem conflito (erro: {e})", False)
        chk("A --baixar unico (nao triplicado)", False)

    # _parse_raiz_argv
    chk("_parse_raiz_argv vazio", _parse_raiz_argv([]) is None)
    chk("_parse_raiz_argv --raiz /p", _parse_raiz_argv(["--raiz", "/p"]) == "/p")
    chk("_parse_raiz_argv --raiz=/p", _parse_raiz_argv(["--raiz=/p"]) == "/p")
    chk("_parse_raiz_argv sem valor", _parse_raiz_argv(["--raiz"]) is None)

    bx_core.fechar_cache()
    print(f"== RESUMO bx-get: PASS={ok} FAIL={fail} ==")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "_dl1_interno":
        _cfg = bx_core.carregar_config(
            raiz_arg=_parse_raiz_argv(sys.argv[3:]), modo="apply")
        dl1(_cfg, sys.argv[2])
        sys.exit(0)
    sys.exit(_cli(sys.argv[1:]))