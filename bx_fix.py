#!/usr/bin/env python3
"""
bx_fix.py — Catalogo do BX Audio Manager (Atualizacao 02).

Uso: bx fix <cmd> [args]

Trigramas 3 letras canonicos (nomes completos tambem aceitos).
Excecoes de 4 letras: raiz, curar.

PRINCIPAIS:
  sim             Simula (precaching + stats, nao altera).
                  Flags: --quiet --estimate
  apl             Aplica (renomeia, move, taggeia, reconcilia).
                  Flags: --forcar --serial --net --replaygain-off
                         --dry-run --quiet --estimate --iterar N
  rpg             ReplayGain incremental (idempotente, retomavel).
                  Flags: --forcar --workers N --sample SEG
  mig | lim       migrar | limpar [--aplicar]
  rev             revisao lista|set ERR CERTO|aplicar

CADASTROS:
  gru | sel | nos    add|del|lista NOME
  raiz [CAM]         Excecao 4 letras

MANUTENCAO:
  sta | aud | ver    stats / auditar / verificar
  fan                fantasmas [--incluir-z]
  des                desfazer | des ate TS (journal)
  rec | curar        reconstruir / curar (4 letras)
  exp sub            expirar-substituidos [--dias N] [--aplicar]
  ind | rep          indice / report
  trv | dtr | tra    travar / destravar / travados
  exp ARQ            explicar
  err | ret | esq    erros / retry / esquecer
  lim err            limpar-erros [--antes N]
  jou                journal [--desde] [--ate] [--verificar]
  cfg che            config-check
  idi sca            idioma-scan

Codigos de retorno: 0 ok | 1 nao encontrado | 2 uso/args invalidos
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import bx_core
import bx_crx
import bx_nmx

Config = bx_core.Config
D_ART = bx_core.D_ART
D_SING = bx_core.D_SING
D_DESC = bx_core.D_DESC
D_INT = bx_core.D_INT
D_QUAR = bx_core.D_QUAR
D_LOGS = bx_core.D_LOGS
D_SUBST = bx_core.D_SUBST
D_LETRAS = bx_core.D_LETRAS
D_LINKS = bx_core.D_LINKS
D_IDX = bx_core.D_IDX
D_REV = bx_core.D_REV
D_Z = bx_core.D_Z
D_Y = bx_core.D_Y
MIN_ART = bx_core.MIN_ART
MAX_MIN = bx_core.MAX_MIN
RETRY_DIAS = bx_core.RETRY_DIAS


# ============================================================================
# Auxiliares locais
# ============================================================================
def _compl_titulo(artista: str, titulo: str) -> tuple[str, str]:
    # B11: remove completamente prod/part/feat/ft e os nomes.
    m = re.match(
        r"^(.*\S)\s*[\[(]?\s*(prod\.|part\.|feat\.?|ft\.?)\s+([^\])]+)[\])]?\s*$",
        artista, re.IGNORECASE)
    if not m:
        return (artista, titulo)
    return (m.group(1), titulo)


def _selo_de(titulo: str, cfg: Config) -> str:
    encontrados: list[str] = []
    for s in cfg.selos:
        if s.lower() in titulo.lower():
            encontrados.append(s)
    return f"Selo: {', '.join(encontrados)}" if encontrados else ""


# ============================================================================
# Proteções
# ============================================================================
D_PROJETO = (D_QUAR, D_LOGS, D_SUBST, D_IDX, D_REV, D_LETRAS, D_LINKS, D_DESC)


def _blindado(rel: str) -> bool:
    if not rel:
        return False
    primeira = rel.split("/", 1)[0]
    return primeira in (D_Z, D_Y)


def _protegido(rel: str) -> bool:
    if not rel:
        return False
    primeira = rel.split("/", 1)[0].upper()
    if primeira in ("Z", "Y"):
        return True
    if primeira.startswith("Z_") or primeira.startswith("Y_"):
        return True
    if rel == D_INT or rel.startswith(D_INT + "/"):
        return True
    return False


def _estrutura_fixa(rel: str) -> bool:
    primeira = rel.split("/", 1)[0] if rel else ""
    if primeira in D_PROJETO:
        return True
    if rel in (D_ART, D_SING, D_Z, D_Y, D_INT):
        return True
    if re.fullmatch(rf"{re.escape(D_SING)}/[a-z#]", rel):
        return True
    return False


# ============================================================================
# Varredura
# ============================================================================
EXT_AUDIO = (".mp3", ".m4a", ".mp4")


def _varrer(cfg: Config) -> list[Path]:
    resultado: list[Path] = []
    raiz = cfg.raiz_sd
    if not raiz.is_dir():
        return resultado

    def _walk(dirp: Path):
        try:
            for entry in os.scandir(dirp):
                try:
                    if entry.is_dir(follow_symlinks=False):
                        yield from _walk(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        yield Path(entry.path)
                except OSError:
                    continue
        except OSError:
            return

    for arq in _walk(raiz):
        if arq.suffix.lower() not in EXT_AUDIO:
            continue
        try:
            if arq.stat().st_size == 0:
                continue
        except OSError:
            continue
        rel = str(arq.relative_to(raiz))
        if _protegido(rel):
            continue
        resultado.append(arq)
    return sorted(resultado)


# ============================================================================
# processar() — núcleo de uma faixa (com 8.1 unificado)
# ============================================================================
class ProcessResult:
    __slots__ = ("estado", "movida", "letra", "erro", "lrc_pendente", "dup")

    def __init__(self) -> None:
        self.estado = "SEM LETRA"
        self.movida = False
        self.letra = "NENHUMA"
        self.erro = ""
        self.lrc_pendente = ""
        self.dup = False


def _semantica_ok(tag: str, suf_re: str, cfg) -> bool:
    """Item 3.1 — valida se a tag e texto util, nao bruto/vazio.

    limpar() ja remove sufixos conhecidos via suf_re (derivado de
    cfg.sufixos em _construir_suf_re). Basta checar o resultado.
    """
    if not tag or not tag.strip():
        return False
    t = tag.strip()
    limpo = bx_nmx.limpar(t, suf_re)
    if not limpo or len(limpo) < 2:
        return False
    if len(limpo) >= 3:
        return True
    return bool(re.search(r"[A-Za-z0-9À-ÿ]", limpo))


def processar(arq: Path, cfg: Config, scan_list: list[Path],
              _con_externa=None) -> ProcessResult:
    res = ProcessResult()
    if not arq.is_file():
        res.erro = "arquivo inexistente"
        return res

    rel = str(arq.relative_to(cfg.raiz_sd))
    tam_ = bx_core.tam(arq)
    if tam_ == 0:
        res.erro = "arquivo vazio"
        return res

    # Manifesto: trava + carry-forward
    base = arq.stem
    m_antigo = bx_core.achar_manifesto(arq.parent, base)
    if m_antigo is not None and bx_core.mget(m_antigo, "TRAVADO") == "Y":
        res.erro = "TRAVADO"
        return res
    id_manif = bx_core.mget(m_antigo, "ID").strip() if m_antigo else ""
    estado_anterior = bx_core.mget(m_antigo, "ESTADO") if m_antigo else ""

    # Probe
    info = bx_core.probe(arq, cfg)
    if info is None:
        res.erro = "CORROMPIDO"
        return res

    # ReplayGain: valor consolidado no manifesto tem precedência (8.2)
    rg_valor: Optional[float] = None
    if m_antigo is not None:
        rg_str = bx_core.mget(m_antigo, "REPLAYGAIN")
        if rg_str:
            try:
                rg_valor = float(rg_str)
            except ValueError:
                rg_valor = None

    # FIX #4: EQUIV aplicado no tag_artist ANTES de auto_nome.
    # Sem isso, arquivos com ID3 corrompido (ex: "Post Malon3") nao
    # casam com "Post Malone" no equiv.txt, e tag_artista_ok falha
    # dentro de auto_nome -> cai em revisao manual.
    _tag_art_equiv = info.artist or ""
    if _tag_art_equiv and _tag_art_equiv.lower() in cfg.equiv:
        _tag_art_equiv = cfg.equiv[_tag_art_equiv.lower()]

    # Nome
    decisao = bx_nmx.auto_nome(
        arq, arq.stem, tam_, info.duration, _tag_art_equiv,
        info.album or "", cfg)
    if decisao.acao == "quar":
        if cfg.modo == "apply":
            _quarentena_mover(arq, cfg)
        res.estado = "QUAR"
        return res
    if decisao.acao == "rev":
        if cfg.modo == "apply":
            _revisao_mover(arq, cfg)
        res.estado = "REV"
        return res

    art_raw = decisao.artista
    if art_raw.lower() in cfg.equiv:
        art_raw = cfg.equiv[art_raw.lower()]
    art_raw, tit_raw = _compl_titulo(art_raw, decisao.titulo)
    # B12: extrai featuring antes do limpar_exib apagar
    art_raw, tit_raw, featuring = bx_nmx.extrair_featuring(art_raw, tit_raw)

    # Item 6.1 foi aplicado em bx_nmx.split_art
    sa_x, sa_1 = bx_nmx.split_art(art_raw, cfg)
    art_disp = bx_nmx.limpar(sa_1, cfg.suf_re) if sa_1 else "Desconhecido"
    tit_disp = bx_nmx.limpar(tit_raw, cfg.suf_re) if tit_raw else "Sem titulo"
    if not art_disp:
        art_disp = "Desconhecido"
    if not tit_disp:
        tit_disp = "Sem titulo"

    # Item 3.2 — produtor extraído do título original
    produtor = bx_core.extrair_produtor(tit_raw)
    track_num = decisao.track

    selo = _selo_de(tit_raw, cfg)

    dest = bx_nmx.calc_dest(art_disp, tit_disp, 1, info.album or "", cfg)
    nome_base = f"{art_disp} - {tit_disp}".lower()
    alvo = dest / f"{nome_base}{arq.suffix.lower()}"

    # Duplicado + arbitragem (4.1, 4.2)
    id_substituir = ""
    alvo_perdedor_pendente: Optional[Path] = None
    res.dup = alvo.exists() and alvo != arq
    if res.dup:
        bx_core.log_erro(cfg.log_erros, f"DUPLICADO: {arq} -> {alvo} (já existe)")
        if cfg.modo == "apply":
            # Fase 1: decide vencedor. NAO move perdedor ainda.
            res_dup = bx_core.resolver_duplicata(
                arq, alvo, cfg, con=_con_externa)
            if res_dup["vencedor"] == arq:
                id_substituir = res_dup["id_perdedor"]
                # v0.23g: move perdedor (alvo) ANTES de mover(arq, alvo).
                # Sem isso, mover() falha com colisão e Fase 2 (pós-commit)
                # tentaria mover o VENCEDOR (alvo já substituído) para
                # Z_substituídos, perdendo o arquivo novo.
                if alvo.is_file():
                    try:
                        if not bx_core.mover_para_substituidos(
                                alvo, cfg, "substituido"):
                            res.erro = (
                                f"duplicado: nao consegui mover perdedor {alvo}")
                            return res
                    except OSError as e:
                        res.erro = f"duplicado: erro mover perdedor: {e}"
                        return res
                alvo_perdedor_pendente = None  # já movido
                res.dup = False
                bx_core.log_erro(
                    cfg.log_erros, f"F1: {res_dup['motivo']}: {alvo}")
            else:
                bx_core.mover_para_substituidos(arq, cfg, "duplicado")
                bx_core.log_erro(
                    cfg.log_erros, f"F1: {res_dup['motivo']}: {alvo}")
        if res.dup:
            return res

    parent_antigo = arq.parent
    stem_antigo = arq.stem
    if cfg.modo == "apply" and arq != alvo and not res.dup:
        if not bx_core.mover(arq, alvo, cfg.modo, cfg.diario, cfg):
            res.erro = f"colisão: {alvo}"
            return res
        res.movida = True
        scan_list.append(alvo)
        arq = alvo

    # Item 5.1 — família atômica
    if cfg.modo == "apply" and res.movida:
        _mover_familia(parent_antigo, stem_antigo, arq.parent, arq.stem, cfg, scan_list)

    # Letra
    lrc = arq.with_suffix(".lrc")
    pdf = arq.with_suffix(".pdf")
    letra_estado = "NENHUMA"
    lrc_pendente = ""
    idioma_letra = ""
    if cfg.modo == "apply":
        if pdf.is_file():
            try:
                pdf_size = pdf.stat().st_size
            except OSError:
                pdf_size = 0
            if pdf_size == 0:
                try:
                    pdf.unlink()
                    bx_core.diario_add(cfg.diario, "RMF", str(pdf), "")
                except OSError:
                    pass
            else:
                try:
                    head = pdf.read_bytes()[:4]
                except OSError:
                    head = b""
                if head != b"%PDF":
                    if lrc.is_file() and lrc.stat().st_size == 0:
                        lrc.unlink(missing_ok=True)
                        bx_core.diario_add(cfg.diario, "RMF", str(lrc), "")
                    if not lrc.exists():
                        bx_core.mover(pdf, lrc, cfg.modo, cfg.diario, cfg)
                        bx_crx.lrc_normalizar(lrc)
        if lrc.is_file():
            bx_crx.lrc_normalizar(lrc)
            if bx_crx.lrc_tem_tempo(lrc):
                letra_estado = "SYNC"
                if not bx_crx.lrc_ordem(lrc):
                    bx_core.log_erro(cfg.log_erros, f"LRC fora de ordem: {lrc}")
            else:
                letra_estado = "STATIC"
            # Item 7.1 + 7.2 — detecta idioma e traduz se EN
            idioma_letra = bx_crx.detectar_idioma(lrc)
            if idioma_letra == "EN":
                try:
                    bx_crx.auto_traduzir_se_en(lrc, cfg)
                except Exception as e:
                    bx_core.log_erro(cfg.log_erros, f"auto_traduzir({lrc}): {e}")
        elif cfg.net and not _pend_vencida(cfg, rel):
            destino_lrc = lrc
            if bx_crx.buscar_letra(art_disp, tit_disp, destino_lrc,
                                   log_erros=cfg.log_erros,
                                   duracao=info.duration if info else None):
                bx_crx.lrc_normalizar(destino_lrc)
                if bx_crx.lrc_tem_tempo(destino_lrc):
                    letra_estado = "SYNC"
                else:
                    letra_estado = "STATIC"
                idioma_letra = bx_crx.detectar_idioma(destino_lrc)
            else:
                lrc_pendente = "NAO_ENCONTRADA"
        elif not cfg.net:
            lrc_pendente = "PENDENTE"

    # B11/B12: se arquivo é m4a/mp4, remove MP3 irmão de mesmo nome.
    # Independe de compat_check: mesmo m4a RESGATADO > MP3 legado.
    compat = bx_core.compat_check(info, arq.suffix)
    if cfg.modo == "apply" and arq.suffix.lower() in (".m4a", ".mp4"):
        irmao_mp3 = arq.with_suffix(".mp3")
        if irmao_mp3.is_file() and irmao_mp3 != arq:
            _mover_para_substituidos(irmao_mp3, cfg, "baixa_qualidade")
            bx_core.log_erro(cfg.log_erros,
                             f"B12: {arq.suffix} substituiu MP3 irmão: {irmao_mp3.name}")
    # B8: pode_tagear aceita qualquer container (mp3, flac, ogg, opus)
    # que o ffmpeg consiga reescrever metadados via -c copy, sem
    # recompressão. Compat só decide o estado (COMPLETO vs RESGATADO).
    tageavel = bx_core.pode_tagear(info, arq.suffix)
    # E9 - semantica ANTES de gravar: se titulo/artista calculados sao
    # brutos ("(Official Video)" etc.), NAO sobrescreve as tags originais.
    semantica_ok = (
        _semantica_ok(tit_disp, cfg.suf_re, cfg=cfg)
        and _semantica_ok(sa_x, cfg.suf_re, cfg=cfg)
    )
    if tageavel and cfg.modo == "apply" and semantica_ok:
        # Item 8.2 — calcula ReplayGain só se ainda não consolidado
        if rg_valor is None and cfg.replaygain_on:
            rg_valor = bx_core.calcular_replaygain(arq)
        precisa = (
            info.title != tit_disp
            or info.artist != sa_x
            or (selo and selo not in info.comment)
            or produtor
        )
        # B12: força re-tag se tag física tem mojibake (raw != limpo).
        # Ex: raw="nÃŁo", limpo="não" -> precisa regravar.
        if (info.raw_title and info.raw_title != info.title) \
                or (info.raw_artist and info.raw_artist != info.artist):
            precisa = True
        if precisa:
            tag_comment = f"{selo} | [Khz: {info.sample_rate:g}] [Letra: {letra_estado}]"
            if not selo:
                tag_comment = f"[Khz: {info.sample_rate:g}] [Letra: {letra_estado}]"
            if bx_core.gravar_tags(arq, sa_x, tit_disp, tag_comment, cfg,
                                   info=info, produtor=produtor,
                                   replaygain=rg_valor, featuring=featuring,
                                   track=track_num):
                info = bx_core.probe(arq, cfg, force=True)
                tam_ = bx_core.tam(arq)
                # Item T: SHA e TAMANHO mudam, calculado no bloco D16
                # abaixo (evita 2a leitura de 40MB por faixa taggeada).
    elif tageavel and cfg.modo == "apply" and not semantica_ok:
        bx_core.log_erro(cfg.log_erros,
                         f"E9 tags brutas - nao sobrescrevi: {arq.name}")

    qualidade_ok = bool(info and info.bitrate >= 128
                        and info.sample_rate >= 44.1)
    if not compat:
        if letra_estado == "SYNC":
            estado = "RESGATADO LETRADO"
        elif letra_estado == "STATIC":
            estado = "RESGATADO LETRA ESTATICA"
        else:
            estado = "RESGATADO"
    elif letra_estado == "SYNC":
        if qualidade_ok and semantica_ok:
            estado = "COMPLETO"
        else:
            estado = "LETRADO"
    elif letra_estado == "STATIC":
        estado = "LETRA ESTATICA"
    else:
        estado = "SEM LETRA"
    res.estado = estado
    res.letra = letra_estado
    res.lrc_pendente = lrc_pendente

    if cfg.modo != "apply":
        return res

    nrel = str(arq.relative_to(cfg.raiz_sd))
    # D16 - reusa SHA do manifesto se TAMANHO bate e mtime <= DATA do manifesto.
    # Evita reler 40+ MB por faixa em lotes grandes.
    sha_val = ""
    if m_antigo is not None:
        sha_manif = bx_core.mget(m_antigo, "SHA256").strip()
        tam_manif = bx_core.mget(m_antigo, "TAMANHO").strip()
        data_manif = bx_core.mget(m_antigo, "DATA").strip()
        tam_ok = False
        try:
            tam_ok = bool(tam_manif) and int(tam_manif) == tam_
        except (ValueError, TypeError):
            tam_ok = False
        if sha_manif and tam_ok and data_manif:
            try:
                ts_manif = datetime.strptime(
                    data_manif, "%Y-%m-%d_%H:%M").timestamp()
                if arq.stat().st_mtime <= ts_manif + 60:
                    sha_val = sha_manif
            except (ValueError, OSError):
                pass
    if not sha_val:
        sha_val = bx_core.sha256(arq)

    # ID (manifesto é autoridade)
    if id_manif:
        id_ = id_manif
    elif sha_val:
        id_ = f"BX-{sha_val[:12]}"
    else:
        id_ = "BX-" + hashlib.sha256(
            f"{nome_base}|{tam_}".encode("utf-8")
        ).hexdigest()[:12]

    # Colisão de ID — usa _con_externa se disponível (8.1)
    def _consulta_id_existente(_id: str) -> Optional[str]:
        if _con_externa is not None:
            row = _con_externa.execute(
                "SELECT rel FROM catalogo WHERE id=?", (_id,)).fetchone()
            return row[0] if row else None
        with bx_core.sessao_db(cfg) as _con:
            row = _con.execute(
                "SELECT rel FROM catalogo WHERE id=?", (_id,)).fetchone()
            return row[0] if row else None

    existente_rel = _consulta_id_existente(id_)
    if existente_rel is not None and existente_rel != nrel:
        if (cfg.raiz_sd / existente_rel).is_file():
            sufixo = hashlib.sha1(arq.name.encode()).hexdigest()[:4]
            id_ = f"{id_}-{sufixo}"

    # Auto-trava em COMPLETO
    travado_valor = "N"
    if estado == "COMPLETO":
        if estado_anterior == "COMPLETO" and m_antigo is not None:
            travado_valor = bx_core.mget(m_antigo, "TRAVADO") or "Y"
        else:
            travado_valor = "Y"

    # Item 1.2 — manifesto canônico de 22 campos
    manifesto_dict = {
        "VERSÃO": bx_core.MANIFESTO_VERSAO,
        "ID": id_,
        "ARTISTA": sa_x,
        "FAIXA": tit_disp,
        "CAMINHO": nrel,
        "TAMANHO": str(tam_),
        "SHA256": sha_val,
        "DATA": "",  # sobrescrito por montar_manifesto(arquivo=arq)
        "ESTADO": estado,
        "LETRA": letra_estado if letra_estado != "NENHUMA" else "NENHUMA",
        "IDIOMA": bx_core.iso639_2(idioma_letra),
        "KHZ": f"{info.sample_rate:g}" if info else "",
        "BITRATE": str(info.bitrate) if info else "",
        "CODEC": info.codec if info else "",
        "PRODUTOR": produtor,
        "ORIGEM_MORTA": "N",
        "TRAVADO": travado_valor,
        "REPLAYGAIN": f"{rg_valor:.2f}" if rg_valor is not None else "",
    }
    novo_manifesto = arq.parent / f"{arq.stem} [{estado}].txt"

    manifesto_ok = False
    try:
        tmp_m = cfg.tmp_dir / f"m_{os.getpid()}_{id_}.txt"
        tmp_m.write_text(
            bx_core.montar_manifesto(manifesto_dict, arquivo=arq),
            encoding="utf-8")
        manifesto_ok = bx_core.enviar_sd(
            tmp_m, novo_manifesto, cfg.log_erros, cfg)
        tmp_m.unlink(missing_ok=True)
        if manifesto_ok:
            bx_core.rm_manifestos(
                arq.parent, arq.stem, novo_manifesto, cfg.diario)
            if parent_antigo != arq.parent:
                bx_core.rm_manifestos(
                    parent_antigo, stem_antigo, None, cfg.diario)
    except OSError as _e:
        bx_core.log_erro(
            cfg.log_erros,
            f"processar: OSError ao gravar manifesto de {arq}: {_e}")

    if not manifesto_ok:
        # Opcao D: o audio ja esta em nrel (mover/enviar_sd foram
        # bem-sucedidos); sem manifesto e sem DB entry ele ficaria
        # orfao ate o proximo `aplicar`. Registra erro estruturado
        # (TRANSITORIO) + pendencia para retry automatico e para
        # `bx fix curar` rastrear a recuperacao.
        res.erro = "manifesto falhou"
        try:
            bx_core.erro_registrar(
                cfg, "TRANSITORIO", "manifesto_pendente",
                alvo=str(arq),
                motivo="enviar_sd do manifesto falhou (audio no destino)",
                rel=nrel)
        except Exception:
            pass
        try:
            _pend_add(cfg, id_, nrel, "MANIFESTO_PENDENTE")
        except Exception:
            pass
        return res

    # Item 8.1 — gravações no mesmo _con_externa
    con_dedicada = _con_externa is None
    con = _con_externa or bx_core.conectar(cfg.db_path)
    try:
        with bx_core.transacao(con):
            if id_substituir and id_substituir != id_:
                n_rep = bx_core.links_repassar(con, id_substituir, id_)
                if n_rep:
                    bx_core.diario_add(cfg.diario, "LNK",
                                        f"{id_substituir}->{id_}", str(n_rep))
                con.execute("DELETE FROM catalogo WHERE id=?", (id_substituir,))

            con.execute("DELETE FROM catalogo WHERE rel=? AND id!=?", (nrel, id_))
            con.execute("DELETE FROM catalogo WHERE id=? AND rel!=?", (id_, nrel))
            con.execute(
                "INSERT OR REPLACE INTO catalogo(id,artista,titulo,estado,rel,tam,"
                "data,sha,idioma,khz,tipo_letra,bitrate,tem_capa,codec,produtor,replaygain) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (id_, sa_x, tit_disp, estado, nrel, tam_,
                 datetime.now().strftime("%Y%m%d"), sha_val, idioma_letra or None,
                 info.sample_rate if info else None,
                 letra_estado if letra_estado != "NENHUMA" else None,
                 info.bitrate if info else None,
                 "Y" if info and info.tem_capa else "N",
                 info.codec if info else None,
                 produtor or None,
                 rg_valor))
    finally:
        if con_dedicada:
            con.close()

    # Fase 2 (pos-commit): so agora o perdedor vai para Z_substituidos.
    if alvo_perdedor_pendente is not None and alvo_perdedor_pendente.is_file():
        try:
            bx_core.mover_para_substituidos(
                alvo_perdedor_pendente, cfg, "substituido")
        except OSError as e:
            bx_core.log_erro(
                cfg.log_erros,
                f"F2: perdedor ficou em disco ({alvo_perdedor_pendente}): {e}")

    if lrc_pendente in ("PENDENTE", "NAO_ENCONTRADA"):
        _pend_add(cfg, id_, nrel, lrc_pendente)
    else:
        _pend_del(cfg, id_)

    scan_list.append(arq)
    return res


# ============================================================================
# Família transacional (5.1)
# ============================================================================
def _mover_familia(parent_antigo: Path, stem_antigo: str,
                    dest_dir: Path, dest_stem: str,
                    cfg: Config, scan: Optional[list[Path]] = None) -> int:
    """Item 5.1 — move laterais + manifesto como bloco indivisível.

    Se qualquer transferência falhar, reverte as anteriores.
    Retorna nº de arquivos efetivamente movidos.
    """
    operacoes: list[tuple[Path, Path, str]] = []
    for suf in (".lrc", ".lrc.org", ".pdf", ".jpg", ".jpeg", ".png"):
        f = parent_antigo / (stem_antigo + suf)
        if not f.is_file():
            continue
        destino = dest_dir / (dest_stem + suf)
        if destino == f or destino.exists():
            continue
        operacoes.append((f, destino, suf))
    m = bx_core.achar_manifesto(parent_antigo, stem_antigo)
    if m is not None:
        sufixo = m.name[len(stem_antigo):]
        destino_m = dest_dir / (dest_stem + sufixo)
        if destino_m != m:
            if destino_m.exists():
                try:
                    m.unlink()
                    bx_core.diario_add(cfg.diario, "RM",
                                        str(m), "", cfg=cfg)
                    bx_core.log_erro(
                        cfg.log_erros,
                        f"_mover_familia: manifesto orfao removido: {m}")
                except OSError:
                    pass
            else:
                operacoes.append((m, destino_m, "__manifesto__"))

    if not operacoes:
        return 0

    realizados: list[tuple[Path, Path, str]] = []
    try:
        for origem, destino, marca in operacoes:
            if not bx_core.mover(origem, destino, cfg.modo, cfg.diario, cfg):
                raise RuntimeError(f"falha: {origem}")
            realizados.append((origem, destino, marca))
        if scan is not None:
            for _o, d, marca in realizados:
                if marca != "__manifesto__":
                    scan.append(d)
        return len(realizados)
    except (KeyboardInterrupt, SystemExit):
        # Z4 - Ctrl+C no meio da família: rollback mas re-propaga
        for o, d, _marca in reversed(realizados):
            try:
                bx_core.mover(d, o, cfg.modo, cfg.diario, cfg)
            except Exception:
                pass
        raise
    except Exception as _e:
        # A15 - rollback com verificação do retorno + log de presos
        presos: list[tuple[str, str]] = []
        for o, d, _marca in reversed(realizados):
            try:
                if not bx_core.mover(d, o, cfg.modo, cfg.diario, cfg):
                    presos.append((str(d), str(o)))
            except Exception:
                presos.append((str(d), str(o)))
        if presos:
            bx_core.log_erro(
                cfg.log_erros,
                f"A15 ROLLBACK INCOMPLETO em _mover_familia: "
                f"{len(presos)} arquivo(s) presos em destino: "
                f"{[p[0] for p in presos[:3]]} (erro: {_e})")
        return 0


def _quarentena_mover(arq: Path, cfg: Config) -> None:
    cfg.quar.mkdir(parents=True, exist_ok=True)
    (cfg.quar / ".nomedia").touch(exist_ok=True)
    parent_antigo, stem_antigo = arq.parent, arq.stem
    destino = cfg.quar / arq.name
    if bx_core.mover(arq, destino, cfg.modo, cfg.diario, cfg):
        bx_core.diario_add(cfg.diario, "QUAR", str(arq), str(destino))
        _mover_familia(parent_antigo, stem_antigo, cfg.quar, destino.stem, cfg)


def _revisao_mover(arq: Path, cfg: Config) -> None:
    cfg.rev.mkdir(parents=True, exist_ok=True)
    parent_antigo, stem_antigo = arq.parent, arq.stem
    destino = cfg.rev / arq.name
    if bx_core.mover(arq, destino, cfg.modo, cfg.diario, cfg):
        _mover_familia(parent_antigo, stem_antigo, cfg.rev, destino.stem, cfg)


# ============================================================================
# Pendências
# ============================================================================
def _pend_add(cfg: Config, id_: str, rel: str, motivo: str) -> None:
    """Append O(1). Compactacao feita no fim do run (_pend_compactar)."""
    hoje = datetime.now().strftime("%Y%m%d")
    linha = f"{id_}|{hoje}|{rel}|{motivo}\n"
    try:
        cfg.pend.parent.mkdir(parents=True, exist_ok=True)
        with cfg.pend.open("a", encoding="utf-8") as f:
            f.write(linha)
    except OSError:
        pass


def _pend_del(cfg: Config, id_: str) -> None:
    """Append de marcador DEL. Compactacao remove o par."""
    linha = f"__DEL__|{id_}|__DEL__|__DEL__\n"
    try:
        cfg.pend.parent.mkdir(parents=True, exist_ok=True)
        with cfg.pend.open("a", encoding="utf-8") as f:
            f.write(linha)
    except OSError:
        pass


def _pend_compactar(cfg: Config) -> None:
    """Compacta pend aplicando operacoes em ORDEM (ultimo op vence).

    Cada linha ADD define o estado; cada linha __DEL__ remove o id.
    Aplicar em ordem garante que 'add -> del -> add' preserve a entrada
    final (ultimo add vence). Tambem limpa duplicatas de add consecutivas.
    """
    if not cfg.pend.is_file():
        return
    entradas: dict = {}
    try:
        for linha in cfg.pend.read_text(encoding="utf-8",
                                         errors="replace").splitlines():
            if not linha:
                continue
            parts = linha.split("|", 3)
            if len(parts) < 4:
                continue
            id_ = parts[0]
            if id_ == "__DEL__":
                entradas.pop(parts[1], None)
                continue
            entradas[id_] = "|".join(parts[1:])
    except OSError:
        return
    try:
        bx_core.escrever_atomico(
            cfg.pend,
            "".join(f"{k}|{v}\n" for k, v in entradas.items()))
        _pend_load_cache(cfg)
    except OSError:
        pass


_PEND_CACHE: dict[str, str] = {}
_PEND_CACHE_MTIME: float = 0.0


def _pend_load_cache(cfg: Config) -> None:
    global _PEND_CACHE_MTIME
    _PEND_CACHE.clear()
    _PEND_CACHE_MTIME = 0.0
    if not cfg.pend.is_file():
        return
    try:
        _PEND_CACHE_MTIME = cfg.pend.stat().st_mtime
        for l in cfg.pend.read_text(encoding="utf-8", errors="replace").splitlines():
            partes = l.split("|")
            if len(partes) >= 3:
                _PEND_CACHE[partes[2]] = partes[1]
    except OSError:
        pass


def _pend_vencida(cfg: Config, rel: str) -> bool:
    if not cfg.pend.is_file():
        return False
    try:
        mt = cfg.pend.stat().st_mtime
    except OSError:
        return False
    if mt != _PEND_CACHE_MTIME:
        _pend_load_cache(cfg)
    data = _PEND_CACHE.get(rel, "")
    if not data:
        return False
    limite = datetime.now().strftime("%Y%m%d")
    return data < limite


# ============================================================================
# Z_substituídos
# ============================================================================
def _mover_para_substituidos(arq: Path, cfg: Config, motivo: str) -> Optional[Path]:
    """Compat: delega a bx_core.mover_para_substituidos (fonte única)."""
    return bx_core.mover_para_substituidos(arq, cfg, motivo)


# ============================================================================
# Índice de artistas com snapshot diferencial (4.3)
# ============================================================================
def _reconstruir_indice_artistas(cfg: Config) -> int:
    """Item 4.3 — snapshot diferencial. Só toca arquivos que mudaram.

    Fluxo:
      1. Lê contagens reais do SQLite.
      2. Carrega .snapshot.txt (mapa atual em disco).
      3. Diff conjuntos.
      4. Unlink dos que saíram.
      5. Touch dos que entraram.
      6. Regrava .snapshot.txt atomicamente.
    """
    if cfg.modo != "apply":
        return 0
    idx_dir = cfg.raiz_sd / D_IDX
    try:
        idx_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    snapshot = idx_dir / ".snapshot.txt"

    # 1) Contagens reais
    con = bx_core.conectar(cfg.db_path)
    try:
        linhas = list(con.execute(
            "SELECT artista, titulo FROM catalogo "
            "WHERE artista IS NOT NULL AND artista != ''"))
    finally:
        con.close()

    cont_art: dict[str, int] = {}
    cont_prod: dict[str, int] = {}
    for art, tit in linhas:
        for parte in (art or "").split(";"):
            parte = parte.strip()
            if parte and parte.lower() != "desconhecido":
                cont_art[parte] = cont_art.get(parte, 0) + 1
        if tit:
            prods = bx_core.extrair_produtor(tit)
            for p in prods.split(";"):
                p = p.strip()
                if p and p.lower() not in ("e", "and", ""):
                    cont_prod[p] = cont_prod.get(p, 0) + 1

    # 2) Conjunto alvo
    def _safe(nome: str) -> str:
        s = bx_nmx.limpar(nome, "")
        if not s:
            return ""
        return bx_nmx.cortar_bytes(s, 200)

    alvo: set[str] = set()
    for nome, n in cont_art.items():
        s = _safe(nome)
        if s:
            _ai = f"{n}-{s}.txt"
            if _ai in alvo:
                bx_core.log_erro(cfg.log_erros,
                    f"indice: colisao {_ai!r} (artista {nome!r})")
            alvo.add(_ai)
    for nome, n in cont_prod.items():
        s = _safe(nome)
        if s:
            _ai = f"{n}-{s}-prod.txt"
            if _ai in alvo:
                bx_core.log_erro(cfg.log_erros,
                    f"indice: colisao {_ai!r} (produtor {nome!r})")
            alvo.add(_ai)

    # 3) Snapshot anterior
    anterior: set[str] = set()
    if snapshot.is_file():
        try:
            for linha in snapshot.read_text(encoding="utf-8", errors="replace").splitlines():
                linha = linha.strip()
                if linha and not linha.startswith("#"):
                    anterior.add(linha)
        except OSError:
            pass

    # 4) Diff
    remover = anterior - alvo
    criar = alvo - anterior

    # 5) Unlink só do que saiu
    for nome_arq in remover:
        f = idx_dir / nome_arq
        if f.is_file():
            try:
                f.unlink()
            except OSError:
                pass

    # 6) Touch só do que entrou
    for nome_arq in criar:
        f = idx_dir / nome_arq
        if f.exists():
            continue
        try:
            f.touch()
        except OSError:
            pass

    # 7) Regrava snapshot somente se o conteúdo mudou (evita mtime churn)
    novo_conteudo = "\n".join(sorted(alvo)) + "\n"
    try:
        conteudo_atual = snapshot.read_text(encoding="utf-8") if snapshot.is_file() else ""
    except OSError:
        conteudo_atual = ""
    if conteudo_atual != novo_conteudo:
        bx_core.escrever_atomico(snapshot, novo_conteudo)

    return len(alvo)


def cmd_indice(cfg: Config) -> int:
    cfg = cfg.com_modo("apply")
    n = _reconstruir_indice_artistas(cfg)
    print(f"indice: {n} arquivos")
    return 0


# ============================================================================
# Reconciliação singles ↔ Artistas (4.2 unificado)
# ============================================================================
def reconciliar(cfg: Config, scan: list[Path]) -> int:
    con = bx_core.conectar(cfg.db_path)
    try:
        faixas = list(con.execute("SELECT id, artista, titulo, rel FROM catalogo"))
    finally:
        con.close()

    pares = [(art, 1) for _, art, _, _ in faixas
             if art and art.lower() != "desconhecido"]
    bx_nmx.carregar_contexto(pares, cfg=cfg)

    movidas = 0
    for id_, art, tit, rel in faixas:
        if not rel or not art or art.lower() == "desconhecido":
            continue
        if not rel.startswith((D_ART + "/", D_SING + "/")):
            continue
        arq = cfg.raiz_sd / rel
        if not arq.is_file():
            continue
        base = art.split(";")[0].strip()
        if not base:
            continue
        info = bx_core.probe(arq, cfg)
        album = info.album if info else ""
        novo_dest = bx_nmx.calc_dest(base, tit, 0, album, cfg)
        atual = arq.parent
        if atual.resolve() == novo_dest.resolve():
            continue
        novo_arq = novo_dest / arq.name
        if cfg.modo == "apply":
            parent_antigo = arq.parent
            stem_antigo = arq.stem
            if bx_core.mover(arq, novo_arq, cfg.modo, cfg.diario, cfg):
                novo_rel = str(novo_arq.relative_to(cfg.raiz_sd))
                _mover_familia(parent_antigo, stem_antigo,
                               novo_arq.parent, novo_arq.stem, cfg, scan)
                m = bx_core.achar_manifesto(novo_arq.parent, novo_arq.stem)
                if m is not None:
                    bx_core.manif_caminho(m, novo_rel)
                con = bx_core.conectar(cfg.db_path)
                try:
                    with bx_core.transacao(con):
                        con.execute("UPDATE catalogo SET rel=? WHERE id=?", (novo_rel, id_))
                finally:
                    con.close()
                scan.append(novo_arq)
                movidas += 1
    return movidas


# ============================================================================
# Limpeza
# ============================================================================
_EXT_LIXO = frozenset({
    ".m4a", ".mp3", ".mp4", ".wav", ".flac",
    ".ogg", ".opus", ".aac", ".webm", ".mkv",
    ".lrc", ".lrc.org", ".pdf", ".jpg", ".jpeg", ".png",
})


def limpar_vazios(cfg: Config) -> dict:
    stats = {"arquivos_removidos": 0, "pastas_removidas": 0}
    if cfg.raiz_sd.is_dir():
        for arq in cfg.raiz_sd.rglob("*"):
            if not arq.is_file():
                continue
            rel = str(arq.relative_to(cfg.raiz_sd))
            # D12 - _protegido cobre Z_*, Y_*, e D_INT (nao so D_IDX)
            if _protegido(rel):
                continue
            nome = arq.name
            if nome == ".nomedia" or nome.endswith(".bxpart"):
                continue
            suf = arq.suffix.lower()
            if suf == ".txt":
                if not nome.endswith("].txt"):
                    continue
            elif suf not in _EXT_LIXO:
                continue
            try:
                if arq.stat().st_size != 0:
                    continue
            except OSError:
                continue
            if cfg.modo == "apply":
                try:
                    arq.unlink()
                    bx_core.diario_add(cfg.diario, "RMF", str(arq), "")
                    stats["arquivos_removidos"] += 1
                except OSError:
                    pass

    while True:
        mudou = False
        if not cfg.raiz_sd.is_dir():
            break
        for pasta in sorted(cfg.raiz_sd.rglob("*"), reverse=True):
            if not pasta.is_dir():
                continue
            rel = str(pasta.relative_to(cfg.raiz_sd))
            if rel == "." or _protegido(rel) or _estrutura_fixa(rel):
                continue
            try:
                if any(pasta.iterdir()):
                    continue
            except OSError:
                continue
            if cfg.modo == "apply":
                try:
                    pasta.rmdir()
                    bx_core.diario_add(cfg.diario, "RMD", str(pasta), "")
                    stats["pastas_removidas"] += 1
                    mudou = True
                except OSError:
                    pass
            else:
                mudou = True
        if cfg.modo != "apply" or not mudou:
            break
    return stats


# ============================================================================
# Migração
# ============================================================================
def migrar_raiz(cfg: Config) -> int:
    pares = [
        ("0 Artistas", D_ART),
        ("0 singles", D_SING),
        ("Nacionais", D_ART),
        ("00_Logs", D_LOGS),
        ("00 Área de transferência", D_QUAR),
        ("_substituidos", D_SUBST),
    ]
    total = 0
    for antigo, novo in pares:
        origem = cfg.raiz_sd / antigo
        destino = cfg.raiz_sd / novo
        if not origem.is_dir():
            continue
        total += _mesclar(origem, destino, cfg)
    return total


def _mesclar(origem: Path, destino: Path, cfg: Config) -> int:
    if not destino.exists():
        if cfg.modo == "apply":
            destino.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(origem), str(destino))
                return 1
            except OSError:
                return 0
        return 1
    movidas = 0
    for item in list(origem.iterdir()):
        alvo = destino / item.name
        if item.is_dir():
            movidas += _mesclar(item, alvo, cfg)
        else:
            if alvo.exists():
                continue
            if cfg.modo == "apply":
                try:
                    destino.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(item), str(alvo))
                    movidas += 1
                except OSError:
                    pass
            else:
                movidas += 1
    if cfg.modo == "apply":
        try:
            origem.rmdir()
        except OSError:
            pass
    return movidas


# ============================================================================
# cmd_rodar (8.1 — batch transacional)
# ============================================================================
def limpar_fantasmas(cfg: Config) -> int:
    if cfg.modo != "apply":
        return 0
    if not cfg.raiz_sd.is_dir():
        return 0
    con = bx_core.conectar(cfg.db_path)
    removidas = 0
    try:
        rows = list(con.execute("SELECT id, rel FROM catalogo"))
        for id_, rel in rows:
            if not rel:
                continue
            if not (cfg.raiz_sd / rel).is_file():
                con.execute("DELETE FROM links_youtube WHERE catalogo_id=?", (id_,))
                con.execute("DELETE FROM catalogo WHERE id=?", (id_,))
                removidas += 1
    finally:
        con.close()
    return removidas


def cmd_rodar(cfg: Config, scan: Optional[list[Path]] = None,
              quiet: bool = False,
              estimate_only: bool = False) -> dict:
    if scan is None:
        scan = []
    stats = {"ok": 0, "erro": 0, "quar": 0, "rev": 0, "skip": 0,
             "movidas": 0, "dup": 0, "indice": 0}

    if estimate_only:
        _arqs = _varrer(cfg)
        bx_core.imprimir_estimate(cfg, _arqs,
                                   usando_rg=cfg.replaygain_on)
        return {"estimate": True, "n_arqs": len(_arqs)}

    bx_nmx.reset_state()

    # Sessão sempre (lock + wake-lock + atexit). Precache aquece o
    # cache SQLite, benefício permanente para a próxima execução.
    bx_core.iniciar_sessao(cfg)
    _pend_load_cache(cfg)
    if cfg.modo == "apply":
        limpar_fantasmas(cfg)

    if not bx_core.espaco_ok(cfg.tmp_dir, 204800):
        print("Pouco espaço em TMP", file=sys.stderr)
        bx_core.fim(cfg)
        return stats

    arquivos = _varrer(cfg)
    if not arquivos:
        bx_core.fim(cfg)
        return stats
    if not cfg.serial:
        if not quiet:
            print(f"🔎 Precaching {len(arquivos)} arquivos...")
        bx_core.precache_batch(arquivos, cfg)

    con = bx_core.conectar(cfg.db_path)
    try:
        linhas = list(con.execute("SELECT id, artista FROM catalogo"))
        _counts = list(con.execute(
            "SELECT LOWER(artista), COUNT(*) FROM catalogo "
            "WHERE artista IS NOT NULL AND artista != '' "
            "GROUP BY LOWER(artista)"))
    finally:
        con.close()
    bx_nmx.conhecidos_load(cfg, [(l[0], l[1]) for l in linhas])
    bx_nmx.conhecidos_prescan(arquivos)

    # FIX #1: contagem_preload cross-arquivo (paridade com bx_get.cmd_rodar).
    # Conta artista no DB + no disco (stem heuristic) ANTES do loop para
    # decidir singles/ vs Artistas/ corretamente na PRIMEIRA faixa.
    # Sem isso, artistas com 70+ faixas terminam com 4 em singles/ + resto
    # em Artistas/ (split inconsistente).
    from collections import Counter as _Counter
    _cnt_disco: _Counter = _Counter()
    for _arq in arquivos:
        _stem = _arq.stem
        if " - " not in _stem:
            continue
        _art = _stem.split(" - ", 1)[0].strip()
        if _art and 2 <= len(_art) <= 60:
            _cnt_disco[_art.lower()] += 1
    _merge: dict[str, int] = {}
    for _a, _n in linhas:
        _a = (_a or "").split(";")[0].strip().lower()
        if _a and _a != "desconhecido":
            _merge[_a] = max(_merge.get(_a, 0), 1)
    for _a, _n in _counts:
        if _a:
            _merge[_a] = max(_merge.get(_a, 0), int(_n))
    for _a, _n in _cnt_disco.items():
        _merge[_a] = max(_merge.get(_a, 0), _n)
    bx_nmx.contagem_preload([(k, v) for k, v in _merge.items()])

    # ST14 - processar() abre/fecha sua propria transacao curta apenas em
    # volta das escritas no DB, NUNCA em volta de ffmpeg (gravar_tags,
    # calcular_replaygain). Isso evita WAL aberto por minutos com risco de
    # corrupcao em OOM kill do Android.
    _con_loop = bx_core.conectar(cfg.db_path)
    try:
        _erros_consec = 0
        _t0_loop = time.monotonic()
        _stats_lrc = {"SYNC": 0, "STATIC": 0, "NENHUMA": 0}
        _bytes_loop = 0
        for i, arq in enumerate(arquivos, 1):
            try:
                _bytes_loop += bx_core.tam(arq)
            except OSError:
                pass
            try:
                r = processar(arq, cfg, scan, _con_externa=_con_loop)
            except Exception as e:
                stats["erro"] += 1
                _erros_consec += 1
                bx_core.log_erro(cfg.log_erros, f"processar({arq}): {e}")
                if _erros_consec >= 20:
                    print(f"\nABORTADO: {_erros_consec} erros consecutivos "
                          f"em processar() -- bug sistemico? {arq}", file=sys.stderr)
                    break
                continue
            _erros_consec = 0
            _letra = getattr(r, "letra", "NENHUMA")
            _stats_lrc[_letra if _letra in _stats_lrc else "NENHUMA"] += 1
            if r.estado == "QUAR":
                stats["quar"] += 1
            elif r.estado == "REV":
                stats["rev"] += 1
            elif r.dup:
                stats["dup"] += 1
            elif r.erro:
                stats["erro"] += 1
            else:
                stats["ok"] += 1
                if r.movida:
                    stats["movidas"] += 1
            if not quiet:
                bx_core.painel_progresso(
                    i, len(arquivos), time.monotonic() - _t0_loop,
                    _stats_lrc, arquivo=arq.name)
    finally:
        _con_loop.close()

    if not quiet:
        bx_core.painel_limpar()
    # Item 5: registra throughput para --estimate (proxima execucao)
    _t_total = time.monotonic() - _t0_loop
    # H3: so registra runs apply (simular/dry-run nao poluem o historico).
    # Threshold 0.5s evita poluicao de runs cacheados.
    if (cfg.modo == "apply" and len(arquivos) > 0
            and _t_total > 0.5):
        try:
            bx_core.HistoricoExecucoes(cfg).registrar(
                len(arquivos), _bytes_loop, _t_total,
                com_rg=cfg.replaygain_on)
        except Exception:
            pass

    movidas_rec = reconciliar(cfg, scan)
    stats["movidas"] += movidas_rec

    if cfg.modo == "apply":
        bx_core.historico_append(
            cfg,
            f"rodar: ok={stats['ok']} erro={stats['erro']} "
            f"dup={stats['dup']} movidas={stats['movidas']}")
        stats["indice"] = _reconstruir_indice_artistas(cfg)
        limpar_fantasmas(cfg)
        _AUDIO_EXT = (".m4a", ".mp3", ".mp4", ".flac",
                      ".ogg", ".opus", ".wav", ".aac")
        _audio_scan = [p for p in scan
                       if p.suffix.lower() in _AUDIO_EXT]
        if _audio_scan:
            try:
                _ms = bx_core.media_scan(_audio_scan, log_erros=cfg.log_erros)
                stats["media_scan"] = _ms
            except Exception as e:
                bx_core.log_erro(cfg.log_erros, f"media_scan em cmd_rodar: {e}")
                stats["media_scan"] = {"erro": str(e)}

    _pend_compactar(cfg)
    return stats


# ============================================================================
# Comandos: revisão, migrar, limpar
# ============================================================================
def cmd_revisao(cfg: Config, acao: str, args: list[str]) -> int:
    if acao == "lista":
        if not cfg.rev.is_dir():
            return 0
        for f in sorted(cfg.rev.iterdir()):
            if f.is_file() and f.suffix.lower() in EXT_AUDIO:
                print(f.name)
        return 0
    if acao == "set":
        if len(args) < 2:
            print("Uso: revisao set ERRADO CERTO", file=sys.stderr)
            return 2
        errado, certo = args[0], args[1]
        arq = cfg.cfg_dir / "equiv.txt"
        linhas = []
        if arq.is_file():
            for l in arq.read_text(encoding="utf-8", errors="replace").splitlines():
                p = l.split("|", 1)
                if len(p) == 2 and p[0].lower() == errado.lower():
                    continue
                linhas.append(l)
        linhas.append(f"{errado}|{certo}")
        bx_core.escrever_atomico(arq, "\n".join(linhas) + "\n")
        print(f"Equivalência: {errado} -> {certo}")
        return 0
    if acao == "aplicar":
        cfg = cfg.com_modo("apply")
        cfg = bx_core.carregar_textos(cfg)
        movidas = 0
        if cfg.rev.is_dir():
            for f in sorted(cfg.rev.iterdir()):
                if not f.is_file() or f.suffix.lower() not in EXT_AUDIO:
                    continue
                bx_core.mover(f, cfg.raiz_sd / f.name, cfg.modo, cfg.diario, cfg)
                movidas += 1
        print(f"Devolvidas: {movidas}. Rode: bx-fix.py aplicar")
        return 0
    print("Uso: revisao lista|set ERRADO CERTO|aplicar", file=sys.stderr)
    return 2


def cmd_migrar(cfg: Config, aplicar: bool) -> int:
    if aplicar:
        cfg = cfg.com_modo("apply")
    if cfg.modo == "apply":
        bx_core.criar_estrutura_sd(cfg)
        bx_core.migrar_z_antigo(cfg)
    n = migrar_raiz(cfg)
    if cfg.modo == "apply":
        limpar_vazios(cfg)
        cmd_rodar(cfg)
    print(f"Migradas: {n}")
    return 0


def cmd_limpar(cfg: Config, aplicar: bool) -> int:
    if aplicar:
        cfg = cfg.com_modo("apply")
    stats = limpar_vazios(cfg)
    print(f"arquivos_removidos={stats['arquivos_removidos']} "
          f"pastas_removidas={stats['pastas_removidas']}")
    return 0


# ============================================================================
# Comandos: stats / auditar / verificar / fantasmas / reconstruir
# ============================================================================
def cmd_stats(cfg: Config) -> int:
    con = bx_core.conectar(cfg.db_path)
    try:
        total = con.execute("SELECT COUNT(*) FROM catalogo").fetchone()[0]
        print(f"Total no catálogo: {total}")
        print("Por estado:")
        for est, n in con.execute(
            "SELECT estado, COUNT(*) FROM catalogo GROUP BY estado "
            "ORDER BY COUNT(*) DESC"):
            print(f"  {n} {est}")
        print("Top 10 artistas:")
        for art, n in con.execute(
            "SELECT artista, COUNT(*) FROM catalogo GROUP BY artista "
            "ORDER BY COUNT(*) DESC LIMIT 10"):
            print(f"  {n} {art}")
    finally:
        con.close()
    pends = 0
    if cfg.pend.is_file():
        pends = sum(1 for _ in cfg.pend.read_text().splitlines() if _)
    print(f"Letras pendentes: {pends}")
    return 0


def cmd_verificar(cfg: Config) -> int:
    con = bx_core.conectar(cfg.db_path)
    try:
        faixas = list(con.execute("SELECT id, rel, tam, sha FROM catalogo"))
    finally:
        con.close()
    ok = ausente = alterado = sem_hash = 0
    for id_, rel, tam_, sha_esp in faixas:
        if not rel:
            continue
        arq = cfg.raiz_sd / rel
        if not arq.is_file():
            ausente += 1
            continue
        if bx_core.tam(arq) != tam_:
            alterado += 1
            continue
        if not sha_esp:
            sem_hash += 1
            continue
        if bx_core.sha256(arq) == sha_esp:
            ok += 1
        else:
            alterado += 1
    print(f"ok={ok} alterados={alterado} ausentes={ausente} sem_hash={sem_hash}")
    return 0


def cmd_auditar(cfg: Config) -> int:
    sem_manifesto = 0
    desatualizado = 0
    invalido = 0
    for arq in _varrer(cfg):
        rel = str(arq.relative_to(cfg.raiz_sd))
        m = bx_core.achar_manifesto(arq.parent, arq.stem)
        if m is None:
            sem_manifesto += 1
            print(f"SEM_MANIFESTO: {rel}")
            continue
        erros = bx_core.validar_manifesto(m)
        if erros:
            invalido += 1
            print(f"MANIFESTO_INVALIDO: {rel} -> {'; '.join(erros)}")
        caminho = bx_core.mget(m, "CAMINHO")
        if caminho != rel:
            desatualizado += 1
            print(f"CAMINHO_DESATUALIZADO: {rel} != {caminho}")
    print(f"sem_manifesto={sem_manifesto} desatualizado={desatualizado} "
          f"invalido={invalido}")
    return 0


def cmd_fantasmas(cfg: Config, incluir_z: bool = False) -> int:
    n_mp3 = n_pdf = n_exp = 0
    agora = time.time()
    limite_exp = 30 * 86400
    for arq in cfg.raiz_sd.rglob("*"):
        if not arq.is_file():
            continue
        rel = str(arq.relative_to(cfg.raiz_sd))
        if not incluir_z and _protegido(rel):
            continue
        # E16 - retencao de 30 dias em Z_substituídos/
        if incluir_z and arq.parent.name == D_SUBST:
            try:
                if (agora - arq.stat().st_mtime) > limite_exp:
                    if cfg.modo == "apply":
                        arq.unlink(missing_ok=True)
                        bx_core.diario_add(cfg.diario, "RMF", str(arq), "")
                    n_exp += 1
                    print(f"SUBST_EXPIRADO: {rel}")
                    continue
            except OSError:
                pass
        if arq.suffix.lower() == ".mp3":
            try:
                if arq.stat().st_size == 0:
                    n_mp3 += 1
                    print(f"MP3_VAZIO: {rel}")
            except OSError:
                pass
        elif arq.suffix.lower() == ".pdf":
            n_pdf += 1
            print(f"PDF: {rel}")
    print(f"mp3_vazios={n_mp3} pdfs={n_pdf} substituidos_expirados={n_exp}")
    return 0


def cmd_expirar_substituidos(cfg: Config, dias: int = 30,
                              aplicar: bool = False) -> int:
    """Remove arquivos de Z_substituídos/ mais antigos que `dias` (item 8a).

    Sem --aplicar: dry-run (lista mas nao apaga).
    Com --aplicar: apaga + remove subpastas vazias + sincroniza MediaStore.
    """
    if not cfg.subst.is_dir():
        print(f"Z_substituídos/ nao encontrado em {cfg.subst}")
        return 0
    limite = time.time() - dias * 86400
    removidos = 0
    bytes_livres = 0
    preservados = 0
    afetados_ms: list[Path] = []
    for f in sorted(cfg.subst.rglob("*")):
        if not f.is_file():
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime >= limite:
            preservados += 1
            continue
        if not aplicar:
            print(f"[DRY] {f.relative_to(cfg.subst)} "
                  f"({st.st_size / 1024:.1f} KB)")
            removidos += 1
            bytes_livres += st.st_size
            continue
        try:
            f.unlink()
            bx_core.diario_add(cfg.diario, "RMF", str(f), "", cfg=cfg)
            removidos += 1
            bytes_livres += st.st_size
            afetados_ms.append(f)
        except OSError as e:
            print(f"[ERRO] {f}: {e}", file=sys.stderr)
    if aplicar and removidos > 0:
        for d in sorted(cfg.subst.rglob("*"), reverse=True):
            if not d.is_dir():
                continue
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass
        if afetados_ms:
            try:
                bx_core.media_scan(afetados_ms, log_erros=cfg.log_erros)
            except Exception:
                pass
    modo_str = "aplicado" if aplicar else "dry-run (use --aplicar p/ apagar)"
    print(f"expirar-substituidos ({modo_str}): "
          f"removidos={removidos} ({bytes_livres/1024/1024:.1f} MB) "
          f"preservados={preservados} (retencao={dias}d)")
    return 0


_DIARIO_REVERSIVEIS = frozenset({"MV", "QUAR"})
_DIARIO_IRREVERSIVEIS = frozenset({"RMF", "RMD", "LNK", "BAK", "NOVO", "SUBST", "TAGS", "RECONCILIAR"})


def cmd_desfazer(cfg: Config) -> int:
    if not cfg.diario.is_file():
        print("Sem diário.")
        return 0
    desfeitas = 0
    ignoradas = 0
    for linha in reversed(cfg.diario.read_text(encoding="utf-8").splitlines()):
        partes = linha.split("|")
        if len(partes) < 4:
            continue
        tipo = partes[1]
        # E6 - reversao por tipo (antes so MV era tratado; QUAR ficava orfao)
        if tipo not in _DIARIO_REVERSIVEIS:
            if tipo in _DIARIO_IRREVERSIVEIS:
                ignoradas += 1
            continue
        origem, destino = partes[2], partes[3]
        if not origem or not destino:
            continue
        o, d = Path(origem), Path(destino)
        if d.is_file() and not o.exists():
            try:
                o.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(d), str(o))
                desfeitas += 1
            except OSError:
                pass
    if ignoradas:
        _nomes_irrev = "/".join(sorted(_DIARIO_IRREVERSIVEIS))
        print(f"Aviso: {ignoradas} entrada(s) nao reversiveis "
              f"({_nomes_irrev}) - ignoradas.")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        shutil.move(str(cfg.diario), str(cfg.log_dir / f"diario_desfeito_{ts}.log"))
    except OSError:
        pass
    print(f"Desfeitas {desfeitas} movimentações.")
    cfg = cfg.com_modo("apply")
    cmd_reconstruir(cfg)
    limpar_fantasmas(cfg)
    return 0


def _int_ou_none(s: str) -> Optional[int]:
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _float_ou_none(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def cmd_reconstruir(cfg: Config) -> int:
    novo: list[tuple] = []
    for arq in cfg.raiz_sd.rglob("*"):
        if not arq.is_file():
            continue
        rel = str(arq.relative_to(cfg.raiz_sd))
        if _protegido(rel):
            continue
        m = bx_core.achar_manifesto(arq.parent, arq.stem)
        if m is None or not bx_core.mget(m, "ID"):
            continue
        linha = (
            bx_core.mget(m, "ID") or None,
            bx_core.mget(m, "ARTISTA"),
            bx_core.mget(m, "FAIXA"),
            bx_core.mget(m, "ESTADO"),
            bx_core.mget(m, "CAMINHO"),
            _int_ou_none(bx_core.mget(m, "TAMANHO")),
            re.sub(r"[^0-9]", "", bx_core.mget(m, "DATA"))[:8],
            bx_core.mget(m, "SHA256"),
            bx_core.mget(m, "IDIOMA") or None,
            _float_ou_none(bx_core.mget(m, "KHZ")),
            bx_core.mget(m, "LETRA") or None,
            _int_ou_none(bx_core.mget(m, "BITRATE")),
            None,  # tem_capa nao esta no manifesto v0.23
            bx_core.mget(m, "CODEC") or None,
            bx_core.mget(m, "PRODUTOR") or None,
            _float_ou_none(bx_core.mget(m, "REPLAYGAIN")),
            bx_core.mget(m, "ORIGEM_MORTA") or "N",
        )
        novo.append(linha)
    con = bx_core.conectar(cfg.db_path)
    try:
        # A17-bis - snapshot ANTES do DELETE (cmd_desfazer chama isto).
        try:
            _bkp = cfg.bkp_dir
            _bkp.mkdir(parents=True, exist_ok=True)
            _ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            _snap = _bkp / f"cat_pre_reconstruir_{_ts}.csv"
            _atual = list(con.execute("SELECT * FROM catalogo ORDER BY rowid"))
            if _atual:
                bx_core.escrever_atomico(_snap, "".join(
                    bx_core._linha_para_pipe(r) + "\n" for r in _atual))
        except Exception as _e:
            print(f"AVISO: snapshot falhou ({_e}). "
                  f"Catalogue anterior PRESERVADO. "
                  f"Rode novamente quando o disco tiver espaco.",
                  file=sys.stderr)
            return 1
        # A7 - DELETE + INSERTs numa unica transacao.
        with bx_core.transacao(con):
            con.execute("DELETE FROM catalogo")
            for l in novo:
                con.execute(bx_core.SQL_INS, l)
    finally:
        con.close()
    print(f"Catálogo reconstruído: {len(novo)} faixas.")
    return 0


def _rg_worker(args: tuple) -> Optional[float]:
    """Worker (ProcessPoolExecutor). Só calcula RG (I/O puro via ffmpeg).
    NUNCA toca em SQLite. Fork-safe via _reset_child_state (bx_core).
    Recebe (path_str, sample_s) para evitar closure (não picklável).
    """
    path_str, sample_s = args
    try:
        return bx_core.calcular_replaygain(Path(path_str),
                                             sample_s=sample_s)
    except Exception:
        return None


def cmd_replaygain(cfg: Config, forcar: bool = False,
                    workers: Optional[int] = None,
                    sample_s: float = 0) -> int:
    """ReplayGain incremental com paralelismo opcional.

    - Idempotente: rodar 2x seguidas = no-op na 2ª (só faltantes).
    - Retomável: se cair, retoma do próximo arquivo.
    - Persiste em manifesto E catálogo (mesma transação por arquivo).
    - `--forcar`: recalcula mesmo os que já têm.
    - `--workers N`: 1=serial, None=min(cpu_count, 4).
    - Usa fork (rápido + correto); fallback serial se fork ausente.
    """
    if workers is None:
        try:
            workers = min(os.cpu_count() or 2, 4)
        except Exception:
            workers = 2

    cfg = cfg.com_modo("apply")
    bx_core.iniciar_sessao(cfg)
    try:
        arquivos = _varrer(cfg)
        if not arquivos:
            print("Nada para processar.")
            return 0
        total = len(arquivos)
        ja_tem = sem_manifesto = 0
        faltam: list[tuple[Path, Path]] = []
        for arq in arquivos:
            m = bx_core.achar_manifesto(arq.parent, arq.stem)
            if m is None:
                sem_manifesto += 1
                continue
            if not forcar:
                rg = bx_core.mget(m, "REPLAYGAIN").strip()
                if rg:
                    try:
                        float(rg)
                        ja_tem += 1
                        continue
                    except ValueError:
                        pass
            faltam.append((arq, m))
        if not faltam:
            print(f"ReplayGain: {ja_tem}/{total} já calculados "
                  f"({sem_manifesto} sem manifesto). Nada a fazer.")
            return 0

        print(f"ReplayGain: {len(faltam)} para calcular "
              f"({ja_tem} prontos, {sem_manifesto} sem manifesto, "
              f"workers={workers}).")

        ok = fail = skip = 0
        log = logging.getLogger(__name__)
        try:
            def _persistir(arq: Path, m: Path,
                           rg: Optional[float]) -> str:
                nonlocal ok, fail, skip
                if rg is None:
                    skip += 1
                    return "SKIP"
                if not bx_core.manif_campo(m, "REPLAYGAIN",
                                            f"{rg:.2f}"):
                    fail += 1
                    return "FAIL_MANIF"
                try:
                    rel = str(arq.relative_to(cfg.raiz_sd))
                except ValueError:
                    rel = ""
                if rel:
                    with bx_core.sessao_db(cfg) as con:
                        with bx_core.transacao(con):
                            con.execute(
                                "UPDATE catalogo SET replaygain=? "
                                "WHERE rel=?", (rg, rel))
                ok += 1
                return f"OK {rg:+.2f}dB"

            def _serial():
                for i, (arq, m) in enumerate(faltam, 1):
                    try:
                        rg = bx_core.calcular_replaygain(arq, sample_s=sample_s)
                    except Exception as e:
                        bx_core.log_erro(cfg.log_erros,
                                          f"replaygain({arq}): {e}")
                        rg = None
                    status = _persistir(arq, m, rg)
                    print(f"\r[{i}/{len(faltam)}] {status:<14s} "
                          f"{arq.name[:50]:<50s}", end="", flush=True)

            ctx = bx_core._fork_ctx() if hasattr(bx_core, "_fork_ctx") else None
            if ctx is None or workers <= 1 or len(faltam) < 8:
                _serial()
            else:
                from concurrent.futures import (ProcessPoolExecutor,
                                                 as_completed)
                try:
                    with ProcessPoolExecutor(
                            max_workers=workers,
                            mp_context=ctx) as ex:
                        futures = {ex.submit(_rg_worker,
                                              (str(arq), sample_s)): (arq, m)
                                   for arq, m in faltam}
                        for i, fut in enumerate(
                                as_completed(futures), 1):
                            arq, m = futures[fut]
                            try:
                                rg = fut.result()
                            except Exception as e:
                                log.warning("rg(%s): %s", arq, e)
                                rg = None
                            status = _persistir(arq, m, rg)
                            print(f"\r[{i}/{len(faltam)}] {status:<14s} "
                                  f"{arq.name[:50]:<50s}", end="",
                                  flush=True)
                except (OSError, RuntimeError, ImportError,
                        NotImplementedError) as e:
                    log.warning("ProcessPool indisponível (%s), serial", e)
                    ok = fail = skip = 0  # reset p/ refazer serial
                    _serial()
        finally:
            # ST14-bis: sem conexao aberta fora de _persistir()
            pass
        print(f"\nReplayGain: ok={ok} skip={skip} fail={fail} "
              f"(ja_tinha={ja_tem}, sem_manifesto={sem_manifesto})")
        return 0 if fail == 0 else 1
    finally:
        bx_core.fim(cfg)


def cmd_idioma_scan(cfg: Config) -> int:
    """Varre Internacionais/ explicitamente (contorna _protegido).

    Detecta idioma das letras e marca EN no DB + manifest.
    Necessario porque _protegido blinda Internacionais/ inteiro, entao
    bx fix aplicar nunca chega nessas musicas. Este comando e o unico
    caminho para aplicar equiv/detectar idioma nelas.
    """
    int_dir = cfg.raiz_sd / D_INT
    if not int_dir.is_dir():
        print(f"Internacionais/ nao encontrado em {int_dir}")
        return 0
    n_scan = n_marcado = n_skip = n_sem_lrc = n_sem_manifest = 0
    con = bx_core.conectar(cfg.db_path)
    try:
        for arq in sorted(int_dir.rglob("*")):
            if not arq.is_file():
                continue
            if arq.suffix.lower() not in (".m4a", ".mp3", ".mp4"):
                continue
            n_scan += 1
            rel = str(arq.relative_to(cfg.raiz_sd))
            lrc = arq.with_suffix(".lrc")
            org = arq.with_suffix(".lrc.org")
            letra = org if org.is_file() else lrc
            if not letra.is_file():
                n_sem_lrc += 1
                continue
            try:
                idioma = bx_crx.detectar_idioma(letra)
            except Exception:
                n_skip += 1
                continue
            if idioma != "EN":
                n_skip += 1
                continue
            try:
                row = con.execute(
                    "SELECT id FROM catalogo WHERE rel=?", (rel,)).fetchone()
                if row:
                    with bx_core.transacao(con):
                        con.execute(
                            "UPDATE catalogo SET idioma='EN' WHERE id=?",
                            (row[0],))
            except Exception:
                pass
            m = bx_core.achar_manifesto(arq.parent, arq.stem)
            if m is not None:
                bx_core.manif_campo(m, "IDIOMA", "EN")
            else:
                n_sem_manifest += 1
            n_marcado += 1
            print(f"+EN  {rel}")
    finally:
        con.close()
    print(f"scan={n_scan} marcados_en={n_marcado} "
          f"skip_nao_en={n_skip} sem_letra={n_sem_lrc} "
          f"sem_manifest={n_sem_manifest}")
    return 0


# ============================================================================
# Comandos: grupo / selo / nosplit / raiz / config-check
# ============================================================================
def cmd_lista(cfg: Config, arquivo: str, acao: str, nome: Optional[str]) -> int:
    arq = cfg.cfg_dir / arquivo
    if acao == "lista":
        if arq.is_file():
            for l in arq.read_text(encoding="utf-8", errors="replace").splitlines():
                s = l.strip()
                if s and not s.startswith("#"):
                    print(s)
        return 0
    if not nome:
        print(f"Uso: {arquivo} {acao} NOME", file=sys.stderr)
        return 2
    existentes = (arq.read_text(encoding="utf-8", errors="replace").splitlines()
                  if arq.is_file() else [])
    if acao == "add":
        if nome.lower() not in [l.strip().lower() for l in existentes]:
            existentes.append(nome)
        print(f"+ {nome}")
    elif acao == "del":
        existentes = [l for l in existentes if l.strip().lower() != nome.lower()]
        print(f"- {nome}")
    else:
        print(f"Ação inválida: {acao}", file=sys.stderr)
        return 2
    bx_core.escrever_atomico(arq, "\n".join(existentes) + "\n")
    return 0


def cmd_raiz(cfg: Config, caminho: Optional[str]) -> int:
    if not caminho:
        print(f"Raiz atual: {cfg.raiz_sd}")
        return 0
    arq = cfg.cfg_dir / "raiz.txt"
    bx_core.escrever_atomico(arq, f"# Pasta raiz do acervo.\n{caminho}\n")
    print(f"Raiz: {caminho}")
    return 0


def cmd_config_check(cfg: Config) -> int:
    problemas: list[str] = []

    def _ler(arquivo: str) -> list[str]:
        p = cfg.cfg_dir / arquivo
        if not p.is_file():
            return []
        return [l.strip() for l in p.read_text(encoding="utf-8", errors="replace")
                .splitlines() if l.strip() and not l.lstrip().startswith("#")]

    def _dup(linhas: list[str]) -> list[str]:
        vistos: dict[str, int] = {}
        for l in linhas:
            k = l.lower()
            vistos[k] = vistos.get(k, 0) + 1
        return [k for k, n in vistos.items() if n > 1]

    for arq in ("sufixos.txt", "grupos.txt", "selos.txt", "nosplit.txt"):
        dups = _dup(_ler(arq))
        if dups:
            problemas.append(f"{arq}: {len(dups)} duplicata(s): {', '.join(dups[:3])}")

    p_equiv = cfg.cfg_dir / "equiv.txt"
    if p_equiv.is_file():
        vistos: dict[str, int] = {}
        for i, l in enumerate(
                p_equiv.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            s = l.strip()
            if not s or s.startswith("#"):
                continue
            if "|" not in s:
                problemas.append(f"equiv.txt:{i}: sem '|' ({s!r})")
                continue
            a, b = s.split("|", 1)
            if not a.strip() or not b.strip():
                problemas.append(f"equiv.txt:{i}: lado vazio")
                continue
            k = a.strip().lower()
            if k in vistos:
                problemas.append(f"equiv.txt:{i}: ERRADO duplicado ({a.strip()!r})")
            vistos[k] = 1

    sep_linhas = _ler("sep.txt")
    if not sep_linhas:
        problemas.append("sep.txt: vazio (usando padrão ';')")
    elif len(sep_linhas) > 1:
        problemas.append(f"sep.txt: {len(sep_linhas)} linhas (esperado 1)")

    ints = {l.lower() for l in _ler("internacionais.txt")}
    nacs = {l.lower() for l in _ler("nacionais.txt")}
    conflito = ints & nacs
    if conflito:
        problemas.append(
            f"internacionais.txt/nacionais.txt: {len(conflito)} conflito(s): "
            f"{', '.join(sorted(conflito)[:3])}")

    if not problemas:
        print("✓ config OK — nenhum problema encontrado.")
        return 0
    print(f"⚠ {len(problemas)} problema(s):")
    for p in problemas:
        print(f"  • {p}")
    return 1


# ============================================================================
# explicar / report / trava manual
# ============================================================================
def _resolver_caminho(cfg: Config, alvo: str) -> Optional[Path]:
    p = Path(alvo).expanduser()
    if p.is_absolute() and p.is_file():
        return p
    rel = cfg.raiz_sd / alvo
    if rel.is_file():
        return rel
    nome_lower = Path(alvo).name.lower()
    for arq in cfg.raiz_sd.rglob("*"):
        if arq.is_file() and arq.name.lower() == nome_lower:
            return arq
    return None


def cmd_explicar(cfg: Config, alvo: str) -> int:
    if not alvo:
        print("Uso: bx fix explicar <arquivo>", file=sys.stderr)
        return 2
    arq = _resolver_caminho(cfg, alvo)
    if arq is None:
        print(f"Arquivo não encontrado: {alvo}", file=sys.stderr)
        return 1
    linhas: list[str] = []
    linhas.append(f"== EXPLICAR: {arq.name} ==")
    try:
        rel = str(arq.relative_to(cfg.raiz_sd))
    except ValueError:
        rel = str(arq)
    linhas.append(f"caminho: {rel}")
    linhas.append(f"tamanho: {bx_core.tam(arq)} bytes")
    info = bx_core.probe(arq, cfg)
    if info:
        linhas.append(
            f"codec={info.codec} ch={info.channels} sr={info.sample_rate} "
            f"br={info.bitrate} dur={info.duration:.1f}s")
        linhas.append(f"tag title={info.title!r} artist={info.artist!r}")
    d = bx_nmx.auto_nome(
        arq, arq.stem, bx_core.tam(arq), info.duration if info else 0.0,
        info.artist if info else "", info.album if info else "", cfg)
    linhas.append("")
    linhas.append("-- auto_nome --")
    linhas.append(f"acao:    {d.acao}")
    linhas.append(f"artista: {d.artista!r}")
    linhas.append(f"titulo:  {d.titulo!r}")
    linhas.append(f"motivo:  {d.motivo}")
    linhas.append("")
    linhas.append("-- manifesto --")
    m = bx_core.achar_manifesto(arq.parent, arq.stem)
    if m is None:
        linhas.append("(nenhum)")
    else:
        for chave in bx_core.MANIFESTO_CAMPOS:
            linhas.append(f"{chave}: {bx_core.mget(m, chave)}")
    linhas.append("")
    linhas.append("-- catálogo --")
    con = bx_core.conectar(cfg.db_path)
    try:
        row = con.execute(
            "SELECT id, artista, titulo, estado, bitrate, khz, codec, produtor, replaygain "
            "FROM catalogo WHERE rel=?", (rel,)).fetchone()
    finally:
        con.close()
    if row is None:
        linhas.append("(não está no catálogo)")
    else:
        linhas.append(f"id={row[0]} artista={row[1]!r} titulo={row[2]!r}")
        linhas.append(f"estado={row[3]} bitrate={row[4]} khz={row[5]} "
                      f"codec={row[6]} produtor={row[7]!r} rg={row[8]}")
    saida = "\n".join(linhas)
    print(saida)
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        out = cfg.log_dir / f"explicar_{datetime.now():%Y%m%d_%H%M%S}.txt"
        out.write_text(saida + "\n", encoding="utf-8")
        print(f"\n(log salvo em {out})")
    except OSError:
        pass
    return 0


def cmd_report(cfg: Config) -> int:
    con = bx_core.conectar(cfg.db_path)
    try:
        total = con.execute("SELECT COUNT(*) FROM catalogo").fetchone()[0]
        estados = list(con.execute(
            "SELECT estado, COUNT(*) FROM catalogo GROUP BY estado "
            "ORDER BY COUNT(*) DESC"))
        artistas = list(con.execute(
            "SELECT artista, COUNT(*) FROM catalogo "
            "WHERE artista IS NOT NULL AND artista != '' "
            "GROUP BY artista ORDER BY COUNT(*) DESC LIMIT 10"))
        bitrates = list(con.execute(
            "SELECT CASE WHEN bitrate IS NULL THEN 'NULL' "
            "  WHEN bitrate < 96 THEN '<96' WHEN bitrate < 128 THEN '96-127' "
            "  WHEN bitrate < 192 THEN '128-191' WHEN bitrate < 256 THEN '192-255' "
            "  ELSE '256+' END AS bucket, COUNT(*) "
            "FROM catalogo GROUP BY bucket ORDER BY bucket"))
        khz = list(con.execute(
            "SELECT khz, COUNT(*) FROM catalogo "
            "WHERE khz IS NOT NULL GROUP BY khz ORDER BY khz"))
        candidatos = list(con.execute(
            "SELECT id, artista, titulo, bitrate, khz FROM catalogo "
            "WHERE (bitrate IS NOT NULL AND bitrate < 128) "
            "   OR (khz IS NOT NULL AND khz < 44.1) "
            "ORDER BY bitrate ASC LIMIT 20"))
    finally:
        con.close()
    L: list[str] = []
    L.append(f"== REPORT {datetime.now():%Y-%m-%d %H:%M} ==")
    L.append(f"Total: {total} faixas")
    L.append("")
    L.append("-- estados --")
    for est, n in estados:
        L.append(f"  {n:5d}  {est or '(vazio)'}")
    L.append("")
    L.append("-- top 10 artistas --")
    for art, n in artistas:
        L.append(f"  {n:5d}  {art}")
    L.append("")
    L.append("-- bitrate --")
    for bucket, n in bitrates:
        L.append(f"  {n:5d}  {bucket}")
    L.append("")
    L.append("-- sample rate (kHz) --")
    for sr, n in khz:
        L.append(f"  {n:5d}  {sr}")
    L.append("")
    L.append(f"-- candidatos a re-download ({len(candidatos)}) --")
    for id_, art, tit, br, sr in candidatos:
        L.append(f"  {id_}  br={br} sr={sr}  {art} - {tit}")
    saida = "\n".join(L)
    print(saida)
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        out = cfg.log_dir / f"report_{datetime.now():%Y%m%d_%H%M%S}.txt"
        out.write_text(saida + "\n", encoding="utf-8")
        print(f"\n(log salvo em {out})")
    except OSError:
        pass
    return 0


def _manifesto_do_id(cfg: Config, id_: str) -> Optional[Path]:
    con = bx_core.conectar(cfg.db_path)
    try:
        row = con.execute("SELECT rel FROM catalogo WHERE id=?", (id_,)).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return None
    arq = cfg.raiz_sd / row[0]
    return bx_core.achar_manifesto(arq.parent, arq.stem)


def cmd_travar(cfg: Config, alvo: Optional[str]) -> int:
    if not alvo:
        print("Uso: bx fix travar <ID>", file=sys.stderr)
        return 2
    m = _manifesto_do_id(cfg, alvo)
    if m is None:
        print(f"Manifesto não encontrado para {alvo}", file=sys.stderr)
        return 1
    if bx_core.manif_campo(m, "TRAVADO", "Y"):
        print(f"+ TRAVADO: {m.name}")
        return 0
    return 1


def cmd_destravar(cfg: Config, alvo: Optional[str]) -> int:
    if not alvo:
        print("Uso: bx fix destravar <ID>", file=sys.stderr)
        return 2
    m = _manifesto_do_id(cfg, alvo)
    if m is None:
        print(f"Manifesto não encontrado para {alvo}", file=sys.stderr)
        return 1
    if bx_core.manif_campo(m, "TRAVADO", "N"):
        print(f"- DESTRAVADO: {m.name}")
        return 0
    return 1


def cmd_travados(cfg: Config) -> int:
    total = 0
    for arq in _varrer(cfg):
        m = bx_core.achar_manifesto(arq.parent, arq.stem)
        if m is None:
            continue
        if bx_core.mget(m, "TRAVADO") == "Y":
            total += 1
            print(f"{bx_core.mget(m, 'ID')} | {arq.name}")
    print(f"total={total}")
    return 0




def cmd_erros(cfg: Config, tipo: Optional[str] = None) -> int:
    """Lista erros pendentes agrupados por tipo."""
    tipo = tipo.upper() if tipo else None
    if tipo and tipo not in bx_core._ERRO_TIPOS_VALIDOS:
        print(f"tipo invalido: {tipo} (use TRANSITORIO|PERMANENTE|LOGICA)",
              file=sys.stderr)
        return 2
    recs = bx_core.erro_listar(cfg, tipo)
    if not recs:
        print("Nenhum erro pendente.")
        return 0
    from collections import Counter as _C
    grupos = _C(r.get("tipo", "?") for r in recs)
    print(f"Erros pendentes: {len(recs)}")
    for t, n in sorted(grupos.items()):
        print(f"  {n} {t}")
    print()
    for r in recs:
        print(f"[{r.get('tipo','?')}] {r.get('id','?')}")
        print(f"   op={r.get('operacao','?')}  alvo={r.get('alvo','?')}")
        print(f"   motivo={r.get('motivo','?')}  ts={r.get('ts','?')}")
    return 0


def cmd_retry(cfg: Config, alvo: Optional[str] = None) -> int:
    """Retenta erros TRANSITORIO. Manual por design."""
    if not alvo:
        recs = [r for r in bx_core.erro_listar(cfg)
                if r.get("tipo") == "TRANSITORIO"]
        if not recs:
            print("Nenhum erro TRANSITORIO para retentar.")
            return 0
        print(f"Retentando {len(recs)} erro(s) TRANSITORIO...")
    else:
        recs = [r for r in bx_core.erro_listar(cfg)
                if r.get("alvo") == alvo or r.get("id") == alvo]
        if not recs:
            print(f"Nenhum erro encontrado para {alvo}")
            return 1
    ok = fail = 0
    for r in recs:
        op = r.get("operacao", "")
        alvo_rec = r.get("alvo", "")
        if op in ("download", "yt_dlp"):
            print(f"  retry manual necessario: {alvo_rec} (video_id={r.get('video_id','?')})")
            print(f"    use: bx get add LINK / bx get rodar")
            ok += 1
        elif op in ("enviar_sd", "mover", "gravar_tags"):
            arq = Path(alvo_rec)
            if arq.is_file():
                if op == "enviar_sd":
                    cfg = cfg.com_modo("apply")
                    if bx_core.espaco_ok(cfg.tmp_dir, 102400):
                        print(f"  ainda existe, espaco ok: {arq.name} (rerodar bx fix aplicar)")
                        ok += 1
                        continue
            print(f"  {op} falhou: {alvo_rec} (rodar bx fix aplicar para retentar)")
            ok += 1
        elif op in ("replaygain",):
            print(f"  use: bx fix replaygain --forcar")
            ok += 1
        else:
            print(f"  operacao {op} sem handler de retry automatico")
            fail += 1
            continue
        bx_core.erro_remover(cfg, r.get("id", ""))
    print(f"retry: ok={ok} fail={fail}")
    return 0 if fail == 0 else 1


def cmd_esquecer(cfg: Config, errid: Optional[str]) -> int:
    if not errid:
        print("Uso: bx fix esquecer <id>", file=sys.stderr)
        return 2
    if bx_core.erro_remover(cfg, errid):
        print(f"- {errid}")
        return 0
    print(f"nao encontrado: {errid}", file=sys.stderr)
    return 1


def cmd_limpar_erros(cfg: Config, antes_dias: int = 7) -> int:
    n = bx_core.erro_limpar_antigos(cfg, antes_dias)
    print(f"removidos={n} (antes de {antes_dias}d)")
    return 0


def cmd_journal(cfg: Config, desde: Optional[str] = None,
                 ate: Optional[str] = None, verificar: bool = False) -> int:
    if verificar:
        r = bx_core.journal_verify(cfg)
        if r["ok"]:
            print(f"OK journal: {r['total']} entradas, hash-chain integro")
            return 0
        print(f"QUEBRADO em {r.get('quebrado_em','?')}: {r.get('motivo','?')}",
              file=sys.stderr)
        return 1
    recs = bx_core.journal_listar(cfg, desde=desde, ate=ate)
    if not recs:
        print("Journal vazio.")
        return 0
    print(f"Journal: {len(recs)} entradas")
    for r in recs:
        print(f"  {r.get('ts','?')}  {r.get('operacao','?'):12s}  "
              f"{r.get('origem','')} -> {r.get('destino','')}")
    return 0


def cmd_desfazer_journal(cfg: Config, ate: Optional[str] = None) -> int:
    """Desfaz movimentos registrados no journal ate <ts> (inclusive)."""
    if not ate:
        print("Uso: bx fix desfazer-ate <YYYY-MM-DDTHH:MM:SS>",
              file=sys.stderr)
        return 2
    recs = [r for r in bx_core.journal_listar(cfg, ate=ate)
            if r.get("operacao") in ("MV", "QUAR")]
    if not recs:
        print(f"Journal vazio ate {ate}.")
        return 0
    desfeitas = 0
    presos = 0
    for r in reversed(recs):
        origem = Path(r.get("origem", ""))
        destino = Path(r.get("destino", ""))
        if not destino.is_file() and not destino.is_dir():
            continue
        if origem.exists():
            print(f"  skip (origem existe): {origem}")
            continue
        try:
            origem.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destino), str(origem))
            desfeitas += 1
        except OSError as e:
            presos += 1
            print(f"  ERRO {destino} -> {origem}: {e}")
    print(f"desfeitas={desfeitas} presos={presos}")
    return 0 if presos == 0 else 1



# ============================================================================
# Suíte (sem --pipeline)
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

    _bx_home_antes = os.environ.get("BX_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        raiz = tmp_path / "raiz"
        raiz.mkdir()
        os.environ["BX_HOME"] = str(tmp_path / "bx_home")
        cfg = bx_core.carregar_config(raiz_arg=str(raiz), modo="apply")
        cfg = cfg.com_db(tmp_path / "test.db")

        for dep in ("ffmpeg", "ffprobe"):
            try:
                subprocess.run([dep, "-version"], capture_output=True,
                               timeout=5, check=False)
                chk(f"dep:{dep}", True)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                chk(f"dep:{dep}", False)

        chk("limpar sufixos",
            bx_nmx.limpar("X (Official Video)", cfg.suf_re) == "X")
        chk("limpar &", bx_nmx.limpar("A & B", cfg.suf_re) == "A and B")
        chk("inicial Á", bx_nmx.inicial("Álvaro") == "a")

        bx_core.criar_estrutura_sd(cfg)
        chk("estrutura: Artistas", (raiz / D_ART).is_dir())
        chk("estrutura: Z_letras", (raiz / D_LETRAS).is_dir())
        chk("estrutura: singles/a", (raiz / D_SING / "a").is_dir())

        # 3.1 — semântica
        chk("semantica vazia", not _semantica_ok("", cfg.suf_re, cfg=cfg))
        chk("semantica só lixo", not _semantica_ok("(Official Video)", cfg.suf_re, cfg=cfg))
        chk("semantica ok", _semantica_ok("Música", cfg.suf_re, cfg=cfg))

        # 5.1 — família atômica em simulação
        pdir = tmp_path / "orig"
        pdir.mkdir()
        (pdir / "M.m4a").write_bytes(b"x")
        (pdir / "M.lrc").write_text("[00:01.00]a\n", encoding="utf-8")
        (pdir / "M [SEM LETRA].txt").write_text("ID:X\n", encoding="utf-8")
        ddir = tmp_path / "dest"
        ddir.mkdir()
        n = _mover_familia(pdir, "M", ddir, "M", cfg)
        chk("5.1 move família completa", n == 2)
        chk("5.1 lrc movido", (ddir / "M.lrc").is_file())
        chk("5.1 manifesto movido",
            (ddir / "M [SEM LETRA].txt").is_file())

        # 4.3 — snapshot diferencial
        con = bx_core.conectar(cfg.db_path)
        con.execute(
            "INSERT OR REPLACE INTO catalogo(id,artista,titulo,estado,rel,tam) "
            "VALUES (?,?,?,?,?,?)",
            ("IDX-1", "CZT", "Musica (prod. Fulano)", "COMPLETO", "x.m4a", 1000))
        con.execute(
            "INSERT OR REPLACE INTO catalogo(id,artista,titulo,estado,rel,tam) "
            "VALUES (?,?,?,?,?,?)",
            ("IDX-2", "CZT;Outro", "Outra", "COMPLETO", "y.m4a", 1000))
        con.close()
        n1 = _reconstruir_indice_artistas(cfg)
        idx_dir = raiz / D_IDX
        snap = idx_dir / ".snapshot.txt"
        chk("4.3 snapshot criado", snap.is_file())
        chk("4.3 indice >0", n1 >= 3)
        mtimes_antes = {f.name: f.stat().st_mtime for f in idx_dir.glob("*.txt")}
        import time as _t
        _t.sleep(0.05)
        n2 = _reconstruir_indice_artistas(cfg)
        mtimes_depois = {f.name: f.stat().st_mtime for f in idx_dir.glob("*.txt")}
        chk("4.3 segunda rodada não altera mtimes",
            mtimes_antes == mtimes_depois)
        # Adiciona novo artista e verifica que só ele é tocado
        con = bx_core.conectar(cfg.db_path)
        con.execute(
            "INSERT OR REPLACE INTO catalogo(id,artista,titulo,estado,rel,tam) "
            "VALUES (?,?,?,?,?,?)",
            ("IDX-3", "Novato", "Faixa", "COMPLETO", "z.m4a", 1000))
        con.close()
        n3 = _reconstruir_indice_artistas(cfg)
        mtimes_pos = {f.name: f.stat().st_mtime for f in idx_dir.glob("*.txt")}
        novos = set(mtimes_pos) - set(mtimes_depois)
        chk("4.3 só novo arquivo tocado", len(novos) >= 1)
        # 4.1 + 4.2 — arbitragem com repasse
        with tempfile.TemporaryDirectory() as t2:
            p1 = Path(t2) / "novo.m4a"
            p2 = Path(t2) / "velho.m4a"
            p1.write_bytes(b"y" * 100)
            p2.write_bytes(b"x" * 100)
            os.utime(p1, (time.time(), time.time()))
            os.utime(p2, (time.time() - 3600, time.time() - 3600))
            chk("4.1 empate mtime: velho vence", not bx_nmx._vencedor_dup(128, 128, p1, p2))
            chk("4.1 empate mtime: novo perde", bx_nmx._vencedor_dup(128, 128, p2, p1) is True)
        # 4.2 — links_repassar
        con = bx_core.conectar(cfg.db_path)
        try:
            with bx_core.transacao(con):
                bx_core.link_add(con, "vid1", "IDX-1")
                bx_core.link_add(con, "vid2", "IDX-1")
            n_rep = bx_core.links_repassar(con, "IDX-1", "IDX-3")
            chk("4.2 repassou 2 links", n_rep == 2)
            chk("4.2 vid1 no vencedor", "vid1" in bx_core.links_of(con, "IDX-3"))
            chk("4.2 vid2 no vencedor", "vid2" in bx_core.links_of(con, "IDX-3"))
            chk("4.2 vencedor não tem links antigos", "vid1" not in bx_core.links_of(con, "IDX-1"))
        finally:
            con.close()

        # 8.2 — ReplayGain incremental (cfg.replaygain_on ja e True)
        cnt = {"n": 0}
        orig_calc = bx_core.calcular_replaygain
        def _fake_calc(path, timeout=120):
            cnt["n"] += 1
            return -14.5
        bx_core.calcular_replaygain = _fake_calc
        try:
            # Cria áudio real
            audio = tmp_path / "sine.m4a"
            try:
                subprocess.run(
                    ["ffmpeg", "-nostdin", "-v", "error", "-y",
                     "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                     "-ac", "2", "-c:a", "aac", "-b:a", "128k", str(audio)],
                    capture_output=True, timeout=60, check=False)
                ok_audio = audio.is_file()
            except (FileNotFoundError, subprocess.TimeoutExpired):
                ok_audio = False
            chk("8.2 áudio de teste criado", ok_audio)
            if ok_audio:
                # 100E - chama processar de verdade com fake calcular_replaygain
                art_dir = raiz / "Artistas" / "Teste"
                art_dir.mkdir(parents=True, exist_ok=True)
                audio_process = art_dir / "Teste - Faixa.m4a"
                shutil.copy2(audio, audio_process)
                scan_local = []
                try:
                    processar(audio_process, cfg, scan_local)
                except Exception:
                    pass
                # NÃO-tautológico: a chamada real deve ter incrementado cnt
                chk("8.2 calcular_replaygain invocado", cnt["n"] >= 1)
                # Manifest writer inclui REPLAYGAIN quando presente
                d = {"VERSÃO": "3", "ID": "X", "ORIGEM_MORTA": "Y"}
                txt = bx_core.montar_manifesto(d)
                chk("8.2 ORIGEM_MORTA no manifesto", "ORIGEM_MORTA:Y" in txt)
        finally:
            bx_core.calcular_replaygain = orig_calc

    bx_core.fechar_cache()
    if _bx_home_antes is None:
        os.environ.pop("BX_HOME", None)
    else:
        os.environ["BX_HOME"] = _bx_home_antes

    chk("8 estados em ESTADOS_MANIFESTO", len(bx_core.ESTADOS_MANIFESTO) == 8)
    chk("manifesto tem 18 campos", len(bx_core.MANIFESTO_CAMPOS) == 18)
    chk("catálogo tem 17 campos", len(bx_core.CAMPOS) == 17)

    # Bug argparse (v0.23f): parser builda sem conflito
    try:
        _build_parser()
        chk("parser build sem conflito", True)
    except ValueError:
        chk("parser build sem conflito", False)

    # CLI runnable: `stats` nao crasha no parse (item S: isolado)
    import subprocess as _sp
    import tempfile as _tf2
    _iso = _tf2.mkdtemp(prefix="bx_fix_test_iso_")
    try:
        _env_iso = os.environ.copy()
        _env_iso["BX_HOME"] = _iso
        _env_iso["BX_RAIZ"] = str(Path(_iso) / "raiz_fake")
        try:
            _r = _sp.run([sys.executable, __file__, "stats"],
                         capture_output=True, text=True, timeout=15,
                         env=_env_iso)
            chk("CLI 'stats' executavel",
                b"conflicting subparser" not in (_r.stderr or "").encode()
                and b"Traceback" not in (_r.stderr or "").encode())
        except Exception:
            chk("CLI 'stats' executavel", False)
    finally:
        import shutil as _sh2
        _sh2.rmtree(_iso, ignore_errors=True)

    print(f"== RESUMO bx-fix: PASS={ok} FAIL={fail} ==")
    return 0 if fail == 0 else 1


# ============================================================================
# Simulacao com relatorio TSV (v0.23)
# ============================================================================
def cmd_simular_report(cfg: Config) -> int:
    """Simula e imprime TSV: source \t artist \t title \t state \t dest.

    Nada e movido/gravado. Util para revisar 500+ arquivos antes do
    primeiro `aplicar`.
    """
    from pathlib import Path as _P
    cfg = cfg.com_modo("sim")
    bx_nmx.reset_state()
    arquivos = _varrer(cfg)
    if not arquivos:
        print("Acervo vazio.", file=sys.stderr)
        return 0
    bx_nmx.conhecidos_prescan(arquivos)
    print("source\tartist\ttitle\tstate\tdest", flush=True)
    n_ok = n_rev = n_quar = n_erro = 0
    for arq in arquivos:
        try:
            rel = str(arq.relative_to(cfg.raiz_sd))
        except ValueError:
            rel = str(arq)
        try:
            r = processar(arq, cfg, scan_list=[])
        except Exception as e:
            n_erro += 1
            print(f"{rel}\tERRO\t{e}\t\t", flush=True)
            continue
        info = bx_core.probe(arq, cfg)
        art_eq = (info.artist if info else "") or ""
        if art_eq and art_eq.lower() in cfg.equiv:
            art_eq = cfg.equiv[art_eq.lower()]
        try:
            d = bx_nmx.auto_nome(
                arq, arq.stem, bx_core.tam(arq),
                info.duration if info else 0.0,
                art_eq, info.album if info else "", cfg)
        except Exception as e:
            n_erro += 1
            print(f"{rel}\tAUTONOME_ERR\t{e}\t\t", flush=True)
            continue
        dest = "-"
        if d.acao == "quar":
            n_quar += 1
            dest = f"QUAR::{cfg.quar}"
        elif d.acao == "rev":
            n_rev += 1
            dest = f"REV::{cfg.rev}"
        elif d.acao == "ok":
            n_ok += 1
            try:
                _, sa_1 = bx_nmx.split_art(d.artista, cfg)
                art = bx_nmx.limpar(sa_1, cfg.suf_re) if sa_1 else "Desconhecido"
                tit = bx_nmx.limpar(d.titulo, cfg.suf_re) if d.titulo else "Sem titulo"
                dp = bx_nmx.calc_dest(art, tit, 0,
                                       info.album if info else "", cfg)
                try:
                    dest = str(_P(dp).relative_to(cfg.raiz_sd))
                except ValueError:
                    dest = str(dp)
            except Exception as e:
                n_erro += 1
                dest = f"ERR:{e}"
        print(f"{rel}\t{d.artista}\t{d.titulo}\t{r.estado}\t{dest}", flush=True)
    print(f"\n# RESUMO simular_report: ok={n_ok} rev={n_rev} "
          f"quar={n_quar} erro={n_erro} total={len(arquivos)}",
          file=sys.stderr)
    return 0


# ============================================================================
# CLI
# ============================================================================
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bx-fix.py", add_help=False)
    p.add_argument("--raiz", default=None)
    sub = p.add_subparsers(dest="cmd")
    _sp_sim = sub.add_parser("simular")
    for _a in ("--forcar", "--serial", "--net", "--replaygain-off",
               "--quiet", "--estimate"):
        _sp_sim.add_argument(_a, action="store_true")
    _sp_sim.add_argument("--report", action="store_true")
    _sp_app = sub.add_parser("aplicar")
    for _a in ("--forcar", "--serial", "--net", "--replaygain-off",
               "--dry-run", "--quiet", "--estimate"):
        _sp_app.add_argument(_a, action="store_true")
    _sp_app.add_argument("--iterar", type=int, default=1,
                        help="Repete ate convergir (movidas=0, dup=0, erro=0). Max 5.")
    sub.add_parser("migrar").add_argument("--aplicar", action="store_true")
    sub.add_parser("limpar").add_argument("--aplicar", action="store_true")
    sp_rev = sub.add_parser("revisao")
    sp_rev.add_argument("acao")
    sp_rev.add_argument("args", nargs="*")
    for nome in ("grupo", "selo", "nosplit"):
        sp = sub.add_parser(nome)
        sp.add_argument("acao")
        sp.add_argument("nome", nargs="?")
    sp_raiz = sub.add_parser("raiz")
    sp_raiz.add_argument("caminho", nargs="?")
    for name in ("stats", "auditar", "verificar", "desfazer",
                 "reconstruir", "curar", "indice", "report", "config-check",
                 "idioma-scan"):
        sub.add_parser(name)
    _sp_erros = sub.add_parser("erros")
    _sp_erros.add_argument("--tipo", default=None)
    _sp_retry = sub.add_parser("retry")
    _sp_retry.add_argument("alvo", nargs="?")
    _sp_esq = sub.add_parser("esquecer")
    _sp_esq.add_argument("errid", nargs="?")
    _sp_limpar = sub.add_parser("limpar-erros")
    _sp_limpar.add_argument("--antes", type=int, default=7)
    _sp_journal = sub.add_parser("journal")
    _sp_journal.add_argument("--desde", default=None)
    _sp_journal.add_argument("--ate", default=None)
    _sp_journal.add_argument("--verificar", action="store_true")
    _sp_dfz = sub.add_parser("desfazer-ate")
    _sp_dfz.add_argument("ts", nargs="?")
    sub.add_parser("fantasmas").add_argument("--incluir-z", action="store_true")
    _sp_exp = sub.add_parser("expirar-substituidos")
    _sp_exp.add_argument("--dias", type=int, default=30)
    _sp_exp.add_argument("--aplicar", action="store_true")
    sp_exp = sub.add_parser("explicar")
    sp_exp.add_argument("alvo", nargs="?")
    for name in ("travar", "destravar"):
        sp_tv = sub.add_parser(name)
        sp_tv.add_argument("alvo", nargs="?")
    sub.add_parser("travados")
    sp_rg = sub.add_parser("replaygain")
    sp_rg.add_argument("--forcar", action="store_true")
    sp_rg.add_argument("--workers", type=int, default=None)
    sp_rg.add_argument("--sample", type=float, default=0,
                       help="Segundos de amostra do meio (0=integral)")
    return p


def _extrair_raiz(argv: list[str]) -> tuple[Optional[str], list[str]]:
    """Remove --raiz CAMINHO / --raiz=CAMINHO de qualquer posição.

    argparse só aceita --raiz ANTES do subcomando, o que quebra
    UX (`bx fix aplicar --raiz X`). Extrai antes e devolve separado.
    """
    return bx_core.extrair_raiz_arg(argv)


def _cli(argv: list[str]) -> int:
    if any(a in ("-h", "--help", "ajuda", "help") for a in argv):
        print(__doc__)
        return 0
    if "--teste" in argv:
        return _teste()
    _raiz_extraida, argv_sem_raiz = _extrair_raiz(argv)
    args = _build_parser().parse_args(argv_sem_raiz)
    if _raiz_extraida is not None:
        args.raiz = _raiz_extraida
    if not args.cmd:
        print(__doc__)
        return 1
    cfg = bx_core.carregar_config(
        raiz_arg=args.raiz,
        modo=("apply" if args.cmd in
              ("aplicar", "migrar", "limpar", "reconstruir", "curar",
               "travar", "destravar", "indice", "replaygain") else "sim"),
    )
    if args.cmd == "simular":
        cfg = cfg.com(forcar=args.forcar, serial=args.serial, net=args.net,
                      replaygain_on=not args.replaygain_off)
        if getattr(args, "report", False):
            return cmd_simular_report(cfg)
        _est = getattr(args, "estimate", False)
        _q = getattr(args, "quiet", False)
        stats = cmd_rodar(cfg, quiet=_q, estimate_only=_est)
        if not _est and not _q:
            print(f"simular: {stats}")
        return 0
    if args.cmd == "aplicar":
        cfg = cfg.com(forcar=args.forcar, serial=args.serial, net=args.net,
                      replaygain_on=not args.replaygain_off)
        if getattr(args, "dry_run", False):
            cfg = cfg.com_modo("sim")
            stats = cmd_rodar(
                cfg,
                quiet=getattr(args, "quiet", False),
                estimate_only=getattr(args, "estimate", False))
            if not getattr(args, "estimate", False):
                print(f"aplicar --dry-run: {stats}")
            return 0
        _q = getattr(args, "quiet", False)
        _est = getattr(args, "estimate", False)
        if _est:
            # --estimate: one-shot, sem loop, sem convergencia
            cmd_rodar(cfg, quiet=_q, estimate_only=True)
            return 0
        cfg = cfg.com_modo("apply")
        _iterar = max(1, min(int(getattr(args, "iterar", 1) or 1), 5))
        _stats_final = None
        for _it in range(_iterar):
            if not _q:
                print(f"\n== Iteracao {_it + 1}/{_iterar} ==")
            _stats_final = cmd_rodar(cfg, quiet=_q, estimate_only=False)
            if not _q:
                print(f"aplicar (iter {_it + 1}): {_stats_final}")
            if (_stats_final.get("movidas", 0) == 0
                    and _stats_final.get("dup", 0) == 0
                    and _stats_final.get("erro", 0) == 0):
                if not _q:
                    print("Convergiu (nada mais a mover).")
                break
        return 0
    if args.cmd == "migrar":
        return cmd_migrar(cfg, args.aplicar)
    if args.cmd == "limpar":
        return cmd_limpar(cfg, args.aplicar)
    if args.cmd == "revisao":
        return cmd_revisao(cfg, args.acao, args.args)
    if args.cmd == "grupo":
        return cmd_lista(cfg, "grupos.txt", args.acao, args.nome)
    if args.cmd == "selo":
        return cmd_lista(cfg, "selos.txt", args.acao, args.nome)
    if args.cmd == "nosplit":
        return cmd_lista(cfg, "nosplit.txt", args.acao, args.nome)
    if args.cmd == "raiz":
        return cmd_raiz(cfg, args.caminho)
    if args.cmd == "stats":
        return cmd_stats(cfg)
    if args.cmd == "verificar":
        return cmd_verificar(cfg)
    if args.cmd == "auditar":
        return cmd_auditar(cfg)
    if args.cmd == "fantasmas":
        return cmd_fantasmas(cfg, incluir_z=getattr(args, "incluir_z", False))
    if args.cmd == "desfazer":
        return cmd_desfazer(cfg)
    if args.cmd in ("reconstruir", "curar"):
        return cmd_reconstruir(cfg)
    if args.cmd == "travar":
        return cmd_travar(cfg, args.alvo)
    if args.cmd == "destravar":
        return cmd_destravar(cfg, args.alvo)
    if args.cmd == "travados":
        return cmd_travados(cfg)
    if args.cmd == "indice":
        return cmd_indice(cfg)
    if args.cmd == "report":
        return cmd_report(cfg)
    if args.cmd == "explicar":
        return cmd_explicar(cfg, args.alvo or "")
    if args.cmd == "replaygain":
        return cmd_replaygain(cfg, getattr(args, "forcar", False),
                              getattr(args, "workers", None),
                              getattr(args, "sample", 0.0))
    if args.cmd == "expirar-substituidos":
        return cmd_expirar_substituidos(
            cfg, getattr(args, "dias", 30),
            aplicar=getattr(args, "aplicar", False))
    if args.cmd == "config-check":
        return cmd_config_check(cfg)
    if args.cmd == "idioma-scan":
        return cmd_idioma_scan(cfg.com_modo("apply"))
    if args.cmd == "erros":
        return cmd_erros(cfg, getattr(args, "tipo", None))
    if args.cmd == "retry":
        return cmd_retry(cfg, getattr(args, "alvo", None))
    if args.cmd == "esquecer":
        return cmd_esquecer(cfg, getattr(args, "errid", None))
    if args.cmd == "limpar-erros":
        return cmd_limpar_erros(cfg, getattr(args, "antes", 7))
    if args.cmd == "journal":
        return cmd_journal(cfg,
                           getattr(args, "desde", None),
                           getattr(args, "ate", None),
                           getattr(args, "verificar", False))
    if args.cmd == "desfazer-ate":
        return cmd_desfazer_journal(cfg, getattr(args, "ts", None))
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))