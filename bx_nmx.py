#!/usr/bin/env python3
"""
bx-nmx.py — Nomes do BX Audio Manager (Atualização 02).

Strings, decisão automática de nome, parsing de artista, destinos.
Depende apenas de bx_core.py (mesma pasta).

Aplicado nesta versão:
  • 6.1 — split_art() converte universalmente underlines soltos em ';' e
          tenta quebra modular antes de mandar para revisão.
  • 3.2 — extrair_produtor() exposto para reaproveitamento por bx_fix.
  • 4.1 — _vencedor_dup() aceita paths para desempate por st_mtime.
"""
from __future__ import annotations

import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from threading import RLock
from pathlib import Path
from typing import Iterable, Optional

import bx_core


# ----------------------------------------------------------------------------
# Estado do acervo (por sessão) — encapsulado em ContextoAcervo
# ----------------------------------------------------------------------------
@dataclass
class ContextoAcervo:
    """Estado mutável do acervo (known/cntk/trk).

    Substitui os antigos dicionários globais soltos (_KNOWN/_CNTK/_TRK).
    Pode ser criado, inspecionado e substituído atomicamente para testes
    e para reidratação por bx_get/bx_fix via carregar_contexto().
    """
    known: dict[str, str] = field(default_factory=dict)
    cntk: dict[str, int] = field(default_factory=dict)
    trk: dict[str, int] = field(default_factory=dict)


_CTX: ContextoAcervo = ContextoAcervo()
_CTX_LOCK = RLock()


def get_contexto() -> ContextoAcervo:
    """Retorna o contexto ativo (imutável de fato para o caller)."""
    return _CTX


def set_contexto(novo: ContextoAcervo) -> None:
    """Substitui o contexto atomicamente."""
    global _CTX
    with _CTX_LOCK:
        _CTX = novo


def reset_state() -> None:
    """Compat: limpa tudo. Equivale a set_contexto(ContextoAcervo())."""
    set_contexto(ContextoAcervo())


# ----------------------------------------------------------------------------
# Limpeza de strings
# ----------------------------------------------------------------------------
def limpar_exib(texto: str, suf_re: str) -> str:
    s = texto
    if suf_re:
        s = re.sub(rf"[ \t]*[(\[]({suf_re})[])]", "", s, flags=re.IGNORECASE)
    # B11-bis: remove completamente feat/ft/part/prod e o nome que segue.
    # Se houver um parêntese de fechamento (ex: (Remix)), preserva a partir dele.
    s = re.sub(
        r"\s*[\(\[]?\s*(?:(?:feat|ft)\.?|(?:part|prod)\.)\s+[^()\[\]]+[\)\]]?",
        "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*-\s*Topic$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*VEVO$", "", s, flags=re.IGNORECASE)
    s = s.replace("|", "｜")
    s = "".join(c for c in s if ord(c) >= 32)
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .")
    return s


def _ascii_safe(texto: str) -> str:
    """Mantém só ASCII + Latin-1/Ext-A/Ext-B (PT-BR e ñ).
    Descarta emoji, CJK, cirílico, grego, árabe, hebraico, tailandês,
    símbolos Unicode e bidi marks.
    """
    out: list[str] = []
    for c in texto:
        o = ord(c)
        if o < 0x80 or 0xC0 <= o <= 0x24F:
            out.append(c)
    return "".join(out)


def limpar(texto: str, suf_re: str) -> str:
    s = limpar_exib(texto, suf_re)
    s = re.sub(r"\s*&\s*", " and ", s)
    s = s.replace("\u201c", "").replace("\u201d", "")
    s = s.replace("\u2018", "").replace("\u2019", "")
    for ch in "\"'`*/:<>?\\|":
        s = s.replace(ch, "")
    s = "".join(
        c for c in s
        if unicodedata.category(c) not in ("Cc", "Cs", "Co")
        and not ("\u202a" <= c <= "\u202e")
        and not ("\u2066" <= c <= "\u2069")
    )
    s = _ascii_safe(s)
    s = re.sub(r"\s*[\(\[]\s*[\)\]]", "", s)
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .")
    return s


def inicial(nome: str) -> str:
    if not nome:
        return "#"
    nfd = unicodedata.normalize("NFD", nome)
    ascii_str = "".join(c for c in nfd if not unicodedata.combining(c))
    if not ascii_str:
        return "#"
    c = ascii_str[0].lower()
    return c if "a" <= c <= "z" else "#"


def nome_pasta(nome: str, suf_re: str) -> str:
    limpo = limpar(nome, suf_re)
    if not limpo:
        return ""
    # S7 - defesa contra path traversal
    if limpo in (".", ".."):
        return ""
    if "/" in limpo or "\\" in limpo:
        limpo = limpo.replace("/", "_").replace("\\", "_").strip("_ .")
        if not limpo or limpo in (".", ".."):
            return ""
    # v0.23: remove marca manual (C)/(S)/(BR) de 1-2 letras maiusculas
    limpo = re.sub(r"\s*\([A-Z]{1,2}\)\s*$", "", limpo).strip()
    if not limpo:
        return ""
    return limpo[0].upper() + limpo[1:].lower()


def carregar_contexto(pares: Iterable[tuple[str, int]],
                       conhecidos_extra: Iterable[str] = (),
                       cfg=None) -> None:
    """Reidrata contexto a partir do catálogo. API unificada.

    Substitui a sequência:
        bx_nmx.reset_state()
        for art, tit, rel in faixas: bx_nmx._CNTK[...] += 1
        bx_nmx.conhecidos_load(...)
        bx_nmx.contagem_preload(...)

    Chamado por bx_get.cmd_rodar e bx_fix.reconciliar. Preserva a
    independência de módulo: quem chama só precisa de (artista, count).
    """
    novo = ContextoAcervo()
    for art, n in pares:
        base = (art or "").split(";")[0].strip()
        if not base or base.lower() == "desconhecido":
            continue
        k = base.lower()
        try:
            novo.cntk[k] = novo.cntk.get(k, 0) + int(n)
        except (ValueError, TypeError):
            continue
        for parte in (art or "").split(";"):
            p = parte.strip()
            if p and p.lower() != "desconhecido":
                conhecidos_add_ctx(novo, p)
    for a in conhecidos_extra:
        conhecidos_add_ctx(novo, a)
    if cfg is not None:
        for g in cfg.grupos:
            conhecidos_add_ctx(novo, g)
        for certo in cfg.equiv.values():
            for parte in certo.split(";"):
                conhecidos_add_ctx(novo, parte.strip())
    set_contexto(novo)


def cortar_bytes(texto: str, max_bytes: int) -> str:
    encoded = texto.encode("utf-8")
    if len(encoded) <= max_bytes:
        return texto
    cortado = encoded[:max_bytes]
    while cortado and (cortado[-1] & 0b11000000) == 0b10000000:
        cortado = cortado[:-1]
    try:
        return cortado.decode("utf-8").rstrip(" .")
    except UnicodeDecodeError:
        while cortado:
            try:
                return cortado.decode("utf-8").rstrip(" .")
            except UnicodeDecodeError:
                cortado = cortado[:-1]
        return ""


def extrair_featuring(artista: str, titulo: str) -> tuple[str, str, str]:
    """B12: extrai feat/ft/part/prod do titulo e do artista.

    Retorna (artista_principal, titulo_limpo, featuring_str).
    featuring_str = "A; B" (nomes separados por ;).
    """
    # B12-bis: \. obrigatorio (evita falso positivo em "Sem Feat X"),
    # greedy [^)\]]+ (pega "Stéfano Loscalzo" inteiro, nao só "Stéfano"),
    # remove [)\] opcional no fim.
    feat_re = re.compile(
        r"\s*[\[(]?\s*(?:(?:feat|ft)\.?|(?:part|prod)\.)\s+([^()\[\]]+)[)\]]?",
        re.IGNORECASE)
    def _extrai_de(s: str) -> tuple[str, list[str]]:
        nomes: list[str] = []
        novo = s
        for m in feat_re.finditer(s):
            for p in re.split(r"\s*[&,]\s*|\s+e\s+", m.group(1)):
                p = p.strip()
                if p and p.lower() not in ("e", "and", ""):
                    nomes.append(p)
        novo = feat_re.sub("", s).strip(" .-")
        novo = re.sub(r"\s+", " ", novo)
        return novo, nomes
    art_limpo, nomes_art = _extrai_de(artista)
    tit_limpo2, nomes_tit = _extrai_de(titulo)
    todos = nomes_art + nomes_tit
    base = (art_limpo or artista).lower()
    feat = "; ".join(dict.fromkeys(
        n for n in todos if n and n.lower() != base))
    return (art_limpo or artista, tit_limpo2 or titulo, feat)


def tit_limpo(texto: str) -> str:
    s = texto
    s = re.sub(r"(^|\s)_([^_\s](?:[^_]*[^_\s])?)_(\s|$|[)\]])", r"\1\2\3", s)
    s = re.sub(r"([a-zA-Z])_(m|s|t|d|ll|re|ve)(\s|$)", r"\1'\2\3", s, flags=re.IGNORECASE)
    s = re.sub(r"(^|\s)_(\d)", r"\1#\2", s)
    s = re.sub(r"_+(\s+-\s+|\s*\(|\s*$)", r"\1", s)
    s = re.sub(r"\s+-\s+(prod|feat|ft|part)\.?\s+([^()]+)$",
               r" (\1. \2)", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*--+\s*$", "", s)
    return s


# ----------------------------------------------------------------------------
# Grupos / selos / Topic
# ----------------------------------------------------------------------------
def grupo_de(artista, titulo, album, cfg) -> Optional[str]:
    a, t, al = artista.lower(), titulo.lower(), (album or "").lower()
    for g in cfg.grupos:
        gl = g.lower()
        if gl in a or gl in t or (al and gl in al):
            return g
    return None


def grupo_pasta(rel: str, cfg) -> Optional[str]:
    if "/" not in rel:
        return None
    pasta = rel.rsplit("/", 1)[0]
    nome = pasta.rsplit("/", 1)[-1].lower()
    for g in cfg.grupos:
        if g.lower() == nome:
            return g
    return None


def tag_topic(tag: str, cfg) -> Optional[str]:
    if not tag:
        return None
    m = re.match(r"^(.*\S)\s+-\s+topic$", tag, re.IGNORECASE)
    if not m:
        return None
    a = m.group(1).strip()
    al = a.lower()
    for lixo in ("various", "unknown", "desconhecido", "youtube"):
        if lixo in al:
            return None
    for selo in cfg.selos:
        if al == selo.lower():
            return None
    return a


def tag_artista_ok(tag: str, cfg) -> bool:
    if not tag:
        return False
    al = tag.split(";")[0].strip().lower()
    for lixo in ("topic", "vevo", "various", "unknown", "desconhecido", "youtube",
                 "official", "oficial"):
        if lixo in al:
            return False
    for selo in cfg.selos:
        if al == selo.lower():
            return False
    return eh_conhecido(al)


# ----------------------------------------------------------------------------
# Conhecidos
# ----------------------------------------------------------------------------
def conhecidos_add_ctx(ctx: ContextoAcervo, nome: str) -> None:
    t = nome.strip()
    if not (2 <= len(t) <= 60):
        return
    t = nome_pasta(t, "")
    if not t:
        return
    ctx.known[t.lower()] = t


def conhecidos_add(nome: str) -> None:
    conhecidos_add_ctx(_CTX, nome)


def eh_conhecido(nome: str) -> bool:
    if not nome:
        return False
    return nome.strip().lower() in _CTX.known


def conhecidos_load(cfg, linhas_catalogo, artistas_das_pastas=()) -> None:
    _CTX.known.clear()
    for linha in linhas_catalogo:
        if len(linha) < 2:
            continue
        art = (linha[1] or "").strip()
        if not art or art.lower() == "desconhecido":
            continue
        for parte in re.split(r";", art):
            parte = parte.strip()
            if parte:
                conhecidos_add(parte)
    for a in artistas_das_pastas:
        conhecidos_add(a)
    for g in cfg.grupos:
        conhecidos_add(g)
    for certo in cfg.equiv.values():
        for parte in re.split(r";", certo):
            parte = parte.strip()
            if parte:
                conhecidos_add(parte)


def conhecidos_prescan(arquivos: Iterable[Path]) -> None:
    contagem: dict[str, int] = {}
    for arq in arquivos:
        base = arq.stem
        if " - " not in base:
            continue
        a = base.split(" - ", 1)[0]
        if "_" in a:
            continue
        if re.fullmatch(r"\d+[.)]?", a):
            continue
        if re.fullmatch(r"[Ee][Pp]\.?\d+", a):
            continue
        a = a.split(";")[0].strip().lower()
        if 2 <= len(a) <= 40:
            contagem[a] = contagem.get(a, 0) + 1
    for k, v in contagem.items():
        if v >= 2:
            conhecidos_add(k)


# ----------------------------------------------------------------------------
# Produtor (item 3.2, delegando ao core)
# ----------------------------------------------------------------------------
def extrair_produtor(titulo: str) -> str:
    """Delegação para bx_core.extrair_produtor (fonte única da regex)."""
    return bx_core.extrair_produtor(titulo)


# ----------------------------------------------------------------------------
# Parsing de artista
# ----------------------------------------------------------------------------
def art_pasta(artista: str, cfg) -> str:
    a = artista.split(";", 1)[0]
    for ch in "\"'`*/:<>?\\|\u201c\u201d\u2018\u2019":
        a = a.replace(ch, "")
    a = a.replace("&", " and ").strip()
    return nome_pasta(a, cfg.suf_re)


# Regex de underscore "solto" no bloco de autoria.
# Underscore solto = cercado por espaços OU entre tokens alfanuméricos válidos,
# sem conectar sílabas individuais (letras espaçadas ficam de fora — quem lida
# com isso é desesp_lado() mais adiante em auto_nome).
_UNDERSCORE_SOLTO = re.compile(r"(?<=[A-Za-z0-9À-ÿ)\]])\s*_\s*(?=[A-Za-z0-9À-ÿ(\[])")


def _quebrar_underscores(bloco: str) -> str:
    """Converte underscores soltos em ';' de forma universal (item 6.1).

    D6 - preserva letras espaçadas ("K_a_m_a_i_t_a_c_h_i"): 4+ tokens
    todos de 1 caractere não são quebrados (nome próprio estilizado).
    """
    if "_" not in bloco:
        return bloco
    # D6 - letras espaçadas: 4+ tokens de 1 char → preserva intacto
    partes_us = [p.strip() for p in bloco.split("_") if p.strip()]
    if len(partes_us) >= 4 and all(len(p) == 1 for p in partes_us):
        return bloco
    # Normaliza "__" e "_+" nas pontas primeiro (decorativos)
    s = re.sub(r"__+", " _ ", bloco).strip(" _")
    # Quebra universal
    s = _UNDERSCORE_SOLTO.sub(";", s)
    # Compacta ";;" residuais e normaliza espaços
    s = re.sub(r";\s*;+", ";", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _split_por_delimitadores(texto: str) -> list[str]:
    """Divide por ; & / , e conector ' e ' (feat natural).

    v0.23: ' e ' quebra quando o proximo token comeca com maiuscula
    (nome proprio). "Baco Exu do Blues e Péricles" quebra em 2;
    "Cidade Dormitório" (sem ' e ') fica intacto.
    """
    t = re.sub(r"\s*[&/]\s*", ";", texto)
    t = re.sub(r"\s*,\s*", ";", t)
    t = re.sub(r"\s+e\s+(?=[A-ZÀ-Ý])", ";", t)
    return [p.strip() for p in t.split(";") if p.strip()]


def split_art(artista: str, cfg) -> tuple[str, str]:
    """Retorna (sa_x, sa_1): tag canônica + primeiro artista (pasta).

    Item 6.1 — undersores soltos são universalmente convertidos em ';' antes
    do split. Se a quebra modular resultar em tokens, aceita; senão delega
    ao art_decode; só manda para revisão em último caso (o retorno "" é sinal
    de ambiguidade e deve ser tratado pelo chamador).

    Nunca sobrescreve o comportamento de 'nosplit'.
    """
    a = artista
    for ns in cfg.nosplit:
        if ns.lower() in a.lower():
            v = limpar_exib(a, cfg.suf_re)
            return (v, v)

    # Passo 1 — quebra universal de underscores (6.1)
    a_quebrado = _quebrar_underscores(a)

    # Passo 2 — split pelos delimitadores canônicos
    partes = _split_por_delimitadores(a_quebrado)
    partes = [limpar_exib(p, cfg.suf_re) for p in partes]
    partes = [p for p in partes if p]

    # Passo 3 — se alguma parte ainda contém tokens internos do tipo
    # "A_B_C" com underscores não quebrados, tenta de novo via art_decode
    if len(partes) <= 1 and "_" in a_quebrado:
        out, _extra, ok = art_decode(a_quebrado, True, cfg)
        if ok and out:
            partes = [p.strip() for p in out.split(";") if p.strip()]

    sa_x = cfg.sep.join(partes) if partes else ""
    sa_1 = partes[0] if partes else ""
    return (sa_x, sa_1)


def ratio_known(artista: str, cfg) -> tuple[int, int]:
    s = artista.strip()
    m = re.match(r"^(.*\S)\s*[\[(][^\])]*[\])]$", s)
    if m:
        s = m.group(1)
    s = re.sub(r"^(.*\S)\s+(feat|ft|part)\.?\s+.*$", r"\1", s, flags=re.IGNORECASE)
    s = s.replace("_ ", ";").replace(" & ", ";").replace(" / ", ";").replace(", ", ";")
    while s.endswith("_"):
        s = s[:-1]
    tokens = [t.strip() for t in s.split(";") if t.strip()]
    n = len(tokens)
    k = sum(1 for t in tokens if eh_conhecido(t))
    return (n, k)


def art_decode(artista: str, simples: bool, cfg) -> tuple[str, str, bool]:
    # v0.23: limpa (M4A_128K) e sufixos antes do parse
    s = limpar_exib(artista, cfg.suf_re).strip()
    lead = False
    if s.startswith("_"):
        lead = True
        s = s[1:]
    while s.endswith("_"):
        s = s[:-1]
    if re.search(r"[^\s_]_[^\s_]", s):
        return ("", "", False)
    if re.search(r"_[0-9]", s):
        return ("", "", False)
    sep = " _ " in s
    s = s.replace("_ ", ";")
    partes = s.split(";")
    out_parts: list[str] = []
    extra = ""
    maxw = 0
    n = 0
    kn = 0
    for p in partes:
        p = p.strip()
        if not p:
            continue
        m = re.match(r"^(prod|feat|ft|part)\.?\s+(.+)$", p, re.IGNORECASE)
        if m:
            kw = m.group(1).lower().rstrip(".")
            if kw in ("feat", "ft"):
                kw = "part"
            extra += f" ({kw}. {m.group(2)})"
            continue
        words = p.split()
        maxw = max(maxw, len(words))
        if len(words) > 4:
            return ("", "", False)
        if not re.search(r"[^0-9]", p):
            return ("", "", False)
        last = words[-1] if words else ""
        if len(last) < 2 and sep:
            return ("", "", False)
        if len(p) < 3 and not eh_conhecido(p):
            return ("", "", False)
        n += 1
        out_parts.append(p)
        if eh_conhecido(p):
            kn += 1
    out = ";".join(out_parts)
    ok = n >= 1
    if lead and (n != 1 or kn < 1):
        ok = False
    if sep and kn < 1:
        if not (n >= 2 and simples and maxw <= 3):
            ok = False
    return (out, extra, ok)


# ----------------------------------------------------------------------------
# Letras espaçadas
# ----------------------------------------------------------------------------
def desesp(texto: str, modo: str = "art") -> tuple[str, bool, bool]:
    tokens = texto.split()
    if len(tokens) < 4:
        return (texto, True, False)
    if not all(len(t) == 1 for t in tokens):
        return (texto, True, False)
    junto = "".join(tokens)
    if not any(c.isalpha() for c in junto):
        return (texto, True, False)
    if modo == "art":
        return (junto, True, True)
    out = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", junto)
    ok = True
    if out == junto and len(junto) > 7:
        ok = False
    return (out, ok, True)


def desesp_lado(texto: str) -> tuple[str, bool, bool]:
    out, ok, sp = desesp(texto, "art")
    if sp and not eh_conhecido(out):
        return desesp(texto, "tit")
    return (out, ok, sp)


# ----------------------------------------------------------------------------
# Pastas e destino
# ----------------------------------------------------------------------------
def pasta_existente(pai: Path, nome_display: str, cfg) -> str:
    proposta = nome_pasta(nome_display, cfg.suf_re)
    if not proposta:
        return ""
    proposta = cortar_bytes(proposta, 255)
    alvo = pai / proposta
    if alvo.is_dir():
        return proposta
    try:
        for d in pai.iterdir():
            if d.is_dir() and d.name.lower() == proposta.lower():
                return d.name
    except OSError:
        pass
    return proposta


def _cnt_add(chave: str, titulo_lower: str) -> None:
    k = f"{chave}|{titulo_lower}"
    if k in _CTX.trk:
        return
    _CTX.trk[k] = 1
    _CTX.cntk[chave] = _CTX.cntk.get(chave, 0) + 1


def contagem_preload(pares) -> None:
    """Reidrata _CNTK a partir do catalogo (Fix 1 v2).

    Preserva _KNOWN intacto (usa so _CNTK). Chamado por
    bx_get.cmd_rodar para que calc_dest() considere as faixas
    ja existentes no DB ao decidir entre singles/ e Artistas/.
    Sem isso, um lote de downloads conta so as faixas desta
    sessao e joga artista com 3 singles no DB + 2 baixados
    para singles/ com 5 total (regra MIN_ART=4 pediria Artistas/).
    """
    _CTX.cntk.clear()
    for art, n in pares:
        base = (art or "").split(";")[0].strip()
        if not base or base.lower() == "desconhecido":
            continue
        k = base.lower()
        _CTX.cntk[k] = _CTX.cntk.get(k, 0) + int(n)


def calc_dest(artista: str, titulo: str, contar: bool, album: str, cfg) -> Path:
    if not artista or artista.lower() == "desconhecido":
        return cfg.desc
    grupo = grupo_de(artista, titulo, album, cfg)
    nome = grupo if grupo else artista
    chave = nome.lower()
    if contar:
        _cnt_add(chave, titulo.lower())
    n = _CTX.cntk.get(chave, 0)
    if n >= bx_core.MIN_ART:
        return cfg.raiz_sd / bx_core.D_ART / pasta_existente(
            cfg.raiz_sd / bx_core.D_ART, nome, cfg)
    ini = inicial(nome)
    return cfg.raiz_sd / bx_core.D_SING / ini / pasta_existente(
        cfg.raiz_sd / bx_core.D_SING / ini, nome, cfg)


# ----------------------------------------------------------------------------
# Arbitragem de duplicados (item 4.1)
# ----------------------------------------------------------------------------
def _st_mtime(path: Optional[Path]) -> float:
    """Compat: delega a bx_core._st_mtime."""
    return bx_core._st_mtime(path)


def _formato_score(path: Optional[Path]) -> int:
    """Compat: delega a bx_core._formato_score."""
    return bx_core._formato_score(path)


def _vencedor_dup(
    bitrate_novo: int, bitrate_existente: int,
    path_novo: Optional[Path] = None,
    path_existente: Optional[Path] = None,
) -> bool:
    """B11 — delega a bx_core._vencedor_dup (fonte única).

    Mantido aqui como wrapper público para retrocompatibilidade.
    """
    return bx_core._vencedor_dup(
        bitrate_novo, bitrate_existente, path_novo, path_existente)


# ----------------------------------------------------------------------------
# Suspeito
# ----------------------------------------------------------------------------
def rev_suspeito(artista: str, base: str, tamanho: int) -> bool:
    if "_" in artista:
        return True
    if re.match(r"^[Ee][Pp]\.?\d+$", artista):
        return True
    if re.fullmatch(r"\d+[.)]?", artista):
        return True
    if re.match(r"^--|^【", artista):
        return True
    if re.search(r"\((M4A|MP3|MP4)_\d+K", artista, re.IGNORECASE):
        return True
    return False


# ----------------------------------------------------------------------------
# Decisão automática
# ----------------------------------------------------------------------------
@dataclass
class Decisao:
    acao: str = "ok"
    artista: str = ""
    titulo: str = ""
    motivo: str = ""
    track: Optional[int] = None


_PREFIXOS = (
    r"^ep\.?\s*\d+\s*-\s+(.+)$",
    r"^\d{1,3}[.)]?\s*-\s+(.+)$",
    r"^[12]\d{3}\s*-\s+(.+)$",
    r"^\d{1,3}[.)]\s+(.+)$",
)

_SUFIXO_LIXO = re.compile(
    r"^(.*\S)\s+_\s+("
    r"lyric\s+video|lyric\s+vídeo|lyric|"
    r"edit|"
    r"official\s+video|official\s+music\s+video|official\s+audio|"
    r"official\s+lyric\s+video|official\s+visualizer|official|"
    r"oficial\s+video|oficial\s+vídeo|vídeo\s+oficial|"
    r"clipe\s+oficial|clipe|"
    r"animação\s+oficial|animação|"
    r"audio|áudio|"
    r"visualizer|"
    r"letra\s+oficial|letra|"
    r"com\s+letra"
    r")$",
    re.IGNORECASE,
)


def auto_nome(arquivo: Path, base: str, tamanho: int, duracao_s: float,
              tag_artist: str, tag_album: str, cfg) -> Decisao:
    d = Decisao()

    if tamanho > 6_291_456:
        if duracao_s > bx_core.MAX_MIN * 60:
            d.acao = "quar"
            d.motivo = f"{int(duracao_s)}s"
            return d
        if duracao_s == 0 and tamanho > 15_728_640:
            d.acao = "rev"
            d.motivo = "duração ilegível, arquivo grande"
            return d

    b = base
    m = re.match(r"^(.*\S)\s*\((m4a|mp3|mp4)_[0-9]+k?\)$", b, re.IGNORECASE)
    if m:
        b = m.group(1)
    b = re.sub(r"(feat|ft|part|prod)\.([^\s.])", r"\1. \2", b, flags=re.IGNORECASE)
    b = re.sub(r"^【[^】]*】", "", b)
    b = b.strip(" -")
    # v0.23: remove extensao duplicada (.gif.m4a, .mp3.m4a)
    b = re.sub(r"\.(gif|mp3|m4a|mp4|wav|flac|opus|png|jpg|jpeg|webm)$",
               "", b, flags=re.IGNORECASE)
    # Normaliza travessões longos ANTES de qualquer split
    b = b.replace(" – ", " - ").replace(" — ", " - ")

    # WhatsApp: AUD-YYYYMMDD-WAxxxx - irrecuperável por automação
    if re.match(r"^AUD-\d{8}-WA\d+", b, re.IGNORECASE):
        d.acao = "quar"
        d.motivo = "whatsapp_audio"
        return d

    # Número de faixa colado/espaçado no final: "... (03)" ou "...(03)"
    m_track = re.search(r"\s*\((\d{1,2})\)\s*$", b)
    if m_track:
        d.track = int(m_track.group(1))
        b = b[:m_track.start()].rstrip(" -")

    for padrao in _PREFIXOS:
        m = re.match(padrao, b, re.IGNORECASE)
        if m:
            b = m.group(1)
            break

    m = _SUFIXO_LIXO.match(b)
    if m:
        b = m.group(1)

    L, R = "", ""
    if " - " in b:
        L, R = b.split(" - ", 1)
    elif " – " in b or " — " in b:
        fp = b.replace(" – ", " - ").replace(" — ", " - ")
        if " - " in fp:
            l_c, r_c = fp.split(" - ", 1)
            _, k_l = ratio_known(l_c, cfg)
            _, k_r = ratio_known(r_c, cfg)
            if k_l > 0 or k_r > 0:
                L, R = l_c, r_c
            else:
                R = b
        else:
            R = b
    else:
        R = b

    if L:
        L, ok_l, _ = desesp_lado(L)
        if not ok_l:
            d.acao = "rev"
            d.motivo = f"letras espaçadas ambíguas: {L}"
            return d
        R, ok_r, _ = desesp_lado(R)
        if not ok_r:
            d.acao = "rev"
            d.motivo = f"letras espaçadas ambíguas: {R}"
            return d

    if not L:
        if R.count(" _ ") == 1:
            l_c, r_m = R.split(" _ ", 1)
            l_c, ok_l, _ = desesp_lado(l_c)
            if not ok_l:
                d.acao = "rev"
                d.motivo = f"letras espaçadas ambíguas: {l_c}"
                return d
            r_m, ok_r, _ = desesp_lado(r_m)
            if not ok_r:
                d.acao = "rev"
                d.motivo = f"letras espaçadas ambíguas: {r_m}"
                return d
            if eh_conhecido(l_c):
                d.artista = l_c
                d.titulo = tit_limpo(r_m)
            elif eh_conhecido(r_m):
                d.artista = r_m
                d.titulo = tit_limpo(l_c)
        if not d.artista:
            m = re.match(r"^([^_]*\S)\s+(_.+)$", R)
            if m and eh_conhecido(m.group(1)):
                d.artista = m.group(1)
                d.titulo = tit_limpo(m.group(2))
        if not d.artista:
            r_des, ok_r, sp_r = desesp(R, "tit")
            if sp_r and not ok_r:
                d.acao = "rev"
                d.motivo = f"título com letras espaçadas: {R}"
                return d
            d.titulo = tit_limpo(r_des)
            top = tag_topic(tag_artist, cfg)
            if top:
                d.artista = top
                d.motivo = "tag Topic"
            elif tag_artista_ok(tag_artist, cfg):
                d.artista = tag_artist.split(";")[0].strip()
        return d

    tmp = re.sub(r"\([^)]*\)", "", R)
    tmp = re.sub(r"\[[^]]*\]", "", tmp)
    simples = " - " not in tmp

    _, k_l = ratio_known(L, cfg)

    rb = ""
    rmain = R
    while True:
        m = re.match(r"^(.*\S)\s*([\[(][^\])]*[\])])$", rmain)
        if not m:
            break
        rb = m.group(2) + (" " + rb if rb else "")
        rmain = m.group(1)

    rfeat = ""
    m = re.match(r"^(.*\S)\s+(feat|ft|part)\.?\s+(.+)$", rmain, re.IGNORECASE)
    if m:
        rmain = m.group(1)
        feat_name = m.group(3).split(" _ ")[0]
        rfeat = f" (part. {feat_name})"

    n_r, k_r = ratio_known(rmain, cfg)

    if k_l == 0 and n_r >= 1 and k_r == n_r:
        ad_out, ad_extra, ad_ok = art_decode(rmain, True, cfg)
        if ad_ok:
            d.artista = ad_out
            extra = ad_extra + rfeat
            d.titulo = tit_limpo(L) + (f" {rb}" if rb else "") + extra
            d.motivo = "invertido"
            return d

    if "_" in L:
        if re.search(r"[A-Za-z0-9à-ÿ]_ [A-Z0-9]", tmp):
            d.acao = "rev"
            d.motivo = f"lista de artistas dos dois lados: {L}"
            return d
        ad_out, ad_extra, ad_ok = art_decode(L, simples, cfg)
        if not ad_ok:
            d.acao = "rev"
            d.motivo = f"artista ambíguo: {L}"
            return d
        d.artista = ad_out
        xtra = ad_extra
    else:
        d.artista = L.strip()
        xtra = ""

    if "_" in R or " - " in R or "--" in R:
        d.titulo = tit_limpo(R)
    else:
        d.titulo = R
    d.titulo += xtra

    if rev_suspeito(d.artista, b, tamanho):
        d.acao = "rev"
        d.motivo = f"suspeito: {d.artista}"

    return d


# ----------------------------------------------------------------------------
# Testes
# ----------------------------------------------------------------------------
def _mk_cfg():
    return bx_core.carregar_config(raiz_arg="/tmp/bx_teste_nmx", modo="sim")


def _teste() -> int:
    ok = 0
    fail = 0

    def chk(nome: str, obtido, esperado) -> None:
        nonlocal ok, fail
        if obtido == esperado:
            ok += 1
            print(f"[PASS] {nome}")
        else:
            fail += 1
            print(f"[FAIL] {nome} | obtido={obtido!r} esperado={esperado!r}")

    cfg = _mk_cfg()
    reset_state()

    chk("limpar sufixos", limpar("Música (Official Video) [HD]", cfg.suf_re), "Música")
    chk("limpar &", limpar("A & B", cfg.suf_re), "A and B")
    chk("limpar feat removido (B11-bis)", limpar("X feat. Y", cfg.suf_re), "X")
    chk("extrair_featuring preserva nome",
        extrair_featuring("X", "Song feat. Y")[2], "Y")
    chk("limpar aspas", limpar("D'a \u201cb\u201d", cfg.suf_re), "Da b")
    chk("limpar exFAT", limpar("a/b:c?d", cfg.suf_re), "abcd")
    chk("inicial Á", inicial("Álvaro"), "a")
    chk("inicial número", inicial("50 Cent"), "#")

    chk("nome_pasta CZT", nome_pasta("CZT", cfg.suf_re), "Czt")
    chk("nome_pasta THEO KANT", nome_pasta("THEO KANT", cfg.suf_re), "Theo kant")

    chk("tit_limpo _X_", tit_limpo("_Condor_"), "Condor")
    chk("tit_limpo I_m", tit_limpo("I_m going"), "I'm going")
    chk("tit_limpo _4", tit_limpo("Faixa _4"), "Faixa #4")

    # 6.1 — split_art com underscores
    sa_x, sa_1 = split_art("Mc Sid_ Krawk", cfg)
    chk("split_art _ -> ;", (sa_x, sa_1), ("Mc Sid;Krawk", "Mc Sid"))
    sa_x, sa_1 = split_art("Tarta 081_ Don Eme_ Czt", cfg)
    chk("split_art 3 com _", sa_x, "Tarta 081;Don Eme;Czt")
    sa_x, sa_1 = split_art("A&B/C", cfg)
    chk("split_art & /", sa_x, "A;B;C")
    # nosplit permanece intocado
    cfg_ns = cfg.com(nosplit=("Poesia Acústica",))
    sa_x, _ = split_art("Poesia Acústica #1", cfg_ns)
    chk("split_art nosplit", sa_x, "Poesia Acústica #1")

    # 100D - letras espaçadas não viram 9 artistas falsos
    sa_x, sa_1 = split_art("K_a_m_a_i_t_a_c_h_i", cfg)
    chk("100D split_art letras espaçadas (não quebra)",
        (sa_x, sa_1), ("K_a_m_a_i_t_a_c_h_i", "K_a_m_a_i_t_a_c_h_i"))

    # 4.1 — _vencedor_dup com mtime
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p1 = Path(tmp) / "novo.m4a"
        p2 = Path(tmp) / "velho.m4a"
        p1.write_bytes(b"x")
        p2.write_bytes(b"y")
        os.utime(p1, (time.time(), time.time()))
        os.utime(p2, (time.time() - 3600, time.time() - 3600))
        chk("_vencedor_dup bitrate >", _vencedor_dup(128, 96), True)
        chk("_vencedor_dup bitrate <", _vencedor_dup(96, 128), False)
        chk("_vencedor_dup empate + mtime: novo antigo vence",
            _vencedor_dup(128, 128, p2, p1), True)
        chk("_vencedor_dup empate + mtime: novo recente perde",
            _vencedor_dup(128, 128, p1, p2), False)
        chk("_vencedor_dup sem paths: empate = existente",
            _vencedor_dup(128, 128), False)

    print(f"== RESUMO bx-nmx: PASS={ok} FAIL={fail} ==")
    return 0 if fail == 0 else 1


def cmd_test_name(argv: list[str]) -> int:
    if not argv:
        print("Uso: bx-nmx.py test_name <nome_arquivo>", file=sys.stderr)
        return 2
    nome = " ".join(argv)
    cfg = bx_core.carregar_config(modo="sim")
    d = auto_nome(Path(nome), nome, 0, 0, "", "", cfg)
    print(f"ação:    {d.acao}")
    print(f"artista: {d.artista!r}")
    print(f"título:  {d.titulo!r}")
    print(f"motivo:  {d.motivo}")
    return 0


def main(argv: list[str]) -> int:
    if "--teste" in argv:
        return _teste()
    if argv and argv[0] == "test_name":
        return cmd_test_name(argv[1:])
    print(__doc__)
    return 0


if __name__ == "__main__":
    import time  # noqa: E402 — necessário apenas em _teste()
    sys.exit(main(sys.argv[1:]))