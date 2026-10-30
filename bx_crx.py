#!/usr/bin/env python3
"""
bx_crx.py — Caracteres/Letras do BX Audio Manager (Atualização v0.23).

Aplicado nesta versão:
  • 2 (v0.23) — buscar_letra() usa LRCLIB via urllib.request direto,
    enviando artista, título e duração decimal (info.duration).
  • 5.2 — aplicar_letras_soltas() varre Z_letras/ recursivamente.
  • 5.3 — fusão automática de LRC em colisão.
  • 7.1 — auto_traduzir_se_en() dispara tradução quando idioma == EN.
  • 7.2 — backup .lrc.org atômico antes de traduzir.
  • 7.3 — chave do cache inclui par de idiomas.
  • 7.5 — idioma_rodar() NUNCA rebaixa "NA" para nacionais.

Manifesto v0.23: IDIOMA usa ISO 639-2 (por/eng/und). Conversão via
bx_core.iso639_2(). detectar_idioma() continua retornando PT/EN/NA
(curto) e é convertido por quem escreve o manifesto.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error as _ue
import urllib.parse as _up
import urllib.request as _u
from pathlib import Path
from typing import Callable, Optional

import bx_core

Config = bx_core.Config
D_ART = bx_core.D_ART
D_INT = bx_core.D_INT
MIN_ART = bx_core.MIN_ART

TIMEOUT_BUSCA = 20
EXT_AUDIO_LETRAS = (".m4a", ".mp3", ".mp4", ".flac", ".ogg", ".opus", ".wav", ".aac")
EXT_AUDIO = EXT_AUDIO_LETRAS

TIMESTAMP_RE = re.compile(
    r"^\[(\d{1,3}):(\d{2})(?::(\d{2}))?(?:[.:](\d{1,3}))?\]")

from bx_nmx import (  # noqa: F401
    limpar_exib, limpar, inicial, nome_pasta, cortar_bytes,
    pasta_existente, grupo_pasta,
)


# ----------------------------------------------------------------------------
# Heurística PT/EN
# ----------------------------------------------------------------------------
PT_WORDS: frozenset[str] = frozenset({
    "que", "não", "voce", "você", "meu", "minha", "pra", "para", "com", "uma",
    "mais", "ja", "já", "so", "só", "eu", "nos", "nós", "ela", "dele", "dela",
    "esta", "está", "ta", "tá", "entao", "então", "mas", "ate", "até", "tambem",
    "também", "quando", "como", "vou", "to", "tô", "aqui", "nunca", "sempre",
    "tudo", "nada", "coracao", "coração", "amor", "vida", "pro", "seu", "sua",
    "dos", "das", "num", "pelo", "pela", "nem", "sao", "são", "sou", "foi", "ser",
    "tem", "tinha", "vem", "quer", "fazer", "dizer", "porque", "pois", "ainda",
    "depois", "sem", "mim", "ti", "tua", "teu", "nosso", "eles", "elas", "isso",
    "esse", "essa", "aquele", "muito", "todo", "toda", "cada", "coisa", "ficar",
    "estar", "olhos", "noite", "dia", "saudade", "melhor", "pior", "bem", "mal",
})

EN_WORDS: frozenset[str] = frozenset({
    "the", "and", "you", "your", "i'm", "i'll", "i've", "don't", "it's", "that",
    "this", "with", "for", "what", "when", "are", "was", "but", "not", "all",
    "just", "know", "love", "like", "baby", "they", "have", "from", "she", "her",
    "his", "him", "will", "can", "got", "one", "there", "now", "never", "about",
    "would", "could", "been", "were", "into", "over", "than", "then", "them",
    "these", "those", "who", "why", "how", "because", "cause", "gonna", "wanna",
    "ain't", "feel", "night", "heart", "eyes", "time", "away", "down", "let",
    "make", "want", "need", "see", "say", "said", "going", "yeah", "okay",
})


# ----------------------------------------------------------------------------
# Tradutor injetavel (urllib puro, sem deep_translator)
# ----------------------------------------------------------------------------
_MYMEMORY_URL = "https://api.mymemory.translated.net/get"
_MYMEMORY_UA = "BX-Audio-Manager/0.23 (urllib)"
_MYMEMORY_MAX_Q = 500
_MYMEMORY_TIMEOUT = 15


def _tr_urllib_mymemory(texto: str, src: str, tgt: str) -> str:
    """Traducao via MyMemory publico (urllib puro, sem deep_translator).

    Retorna "" em qualquer falha (rede, rate limit, JSON invalido,
    responseStatus != 200, texto vazio). O chamador (traduzir_um)
    mantem o original quando recebe "".
    """
    if not texto or not texto.strip():
        return ""
    q = texto.strip()
    if len(q) > _MYMEMORY_MAX_Q:
        corte = q[:_MYMEMORY_MAX_Q].rsplit(" ", 1)[0]
        q = corte or q[:_MYMEMORY_MAX_Q]
    src_use = "Autodetect" if src in ("", "auto", "NA", None) else src.lower()
    tgt_use = (tgt or "pt").lower()
    params = _up.urlencode({"q": q, "langpair": f"{src_use}|{tgt_use}"})
    url = f"{_MYMEMORY_URL}?{params}"
    try:
        req = _u.Request(url, headers={
            "User-Agent": _MYMEMORY_UA,
            "Accept": "application/json",
        })
        with _u.urlopen(req, timeout=_MYMEMORY_TIMEOUT) as r:
            if getattr(r, "status", 200) != 200:
                return ""
            raw = r.read(64 * 1024)
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (_ue.HTTPError, _ue.URLError, OSError,
            json.JSONDecodeError, ValueError):
        return ""
    status = data.get("responseStatus")
    if status is not None:
        try:
            if int(status) != 200:
                return ""
        except (TypeError, ValueError):
            return ""
    rd = data.get("responseData") or {}
    trad = str(rd.get("translatedText") or "").strip()
    low = trad.lower()
    if not trad or "my memory" in low or "quota" in low:
        return ""
    return trad


_TRANSLATOR_FN: Callable[[str, str, str], str] = _tr_urllib_mymemory


def _get_translator() -> Callable[[str, str, str], str]:
    return _TRANSLATOR_FN


def injetar_tradutor(fn: Optional[Callable[[str, str, str], str]]) -> None:
    global _TRANSLATOR_FN
    _TRANSLATOR_FN = fn if fn is not None else _tr_urllib_mymemory


def _resetar_tradutor() -> None:
    global _TRANSLATOR_FN
    _TRANSLATOR_FN = _tr_urllib_mymemory


# ----------------------------------------------------------------------------
# .lrc: normalização e verificação
# ----------------------------------------------------------------------------
def lrc_normalizar(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        bruto = path.read_bytes()
    except OSError:
        return False
    original = bruto
    if bruto.startswith(b"\xef\xbb\xbf"):
        bruto = bruto[3:]
    bruto = bruto.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    try:
        bruto.decode("utf-8")
    except UnicodeDecodeError:
        texto_repl = bruto.decode("utf-8", errors="replace")
        try:
            log_lateral = path.with_name(path.name + ".encoding.log")
            log_lateral.write_text(
                f"encoding=nao-utf8 original_bytes={len(bruto)}\n",
                encoding="utf-8")
        except OSError:
            pass
        try:
            bruto = texto_repl.encode("utf-8")
        except UnicodeEncodeError:
            return False
    if bruto == original:
        return False
    return bx_core.escrever_bytes_atomico(path, bruto)


def lrc_tem_tempo(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for linha in f:
                if TIMESTAMP_RE.match(linha):
                    return True
    except OSError:
        return False
    return False


def _frac_para_float(frac: str) -> float:
    if not frac:
        return 0.0
    return int(frac[:3]) / (10 ** len(frac[:3]))


def _timestamp_para_segundos(linha: str) -> Optional[float]:
    m = TIMESTAMP_RE.match(linha)
    if not m:
        return None
    mm, ss = int(m.group(1)), int(m.group(2))
    g3 = int(m.group(3)) if m.group(3) else None
    frac = _frac_para_float(m.group(4) or "")
    if g3 is not None:
        if mm >= 60:
            return mm * 3600 + ss * 60 + g3 + frac
        return mm * 60 + ss + g3 / 100.0
    return mm * 60 + ss + frac


def lrc_ordem(path: Path) -> bool:
    if not path.is_file():
        return True
    anterior = -1.0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for linha in f:
                seg = _timestamp_para_segundos(linha)
                if seg is None:
                    continue
                if seg < anterior:
                    return False
                anterior = seg
    except OSError:
        return False
    return True


# ----------------------------------------------------------------------------
# Idioma
# ----------------------------------------------------------------------------
def detectar_idioma(lrc_path: Path) -> str:
    if not lrc_path.is_file():
        return "NA"
    try:
        texto = lrc_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "NA"
    texto = re.sub(r"\[[^\]]*\]", "", texto)
    linhas = [l for l in texto.splitlines() if l.strip()]
    palavras = re.findall(r"[a-zà-ÿ']+", texto.lower())
    pt = sum(1 for p in palavras if p in PT_WORDS)
    en = sum(1 for p in palavras if p in EN_WORDS)
    if len(linhas) < 5:
        return "NA"
    if len(linhas) >= 10 and pt + en >= 8:
        return "PT" if pt >= en else "EN"
    total = pt + en
    if total >= 15:
        dom = max(pt, en) / total
        if dom >= 0.75:
            return "PT" if pt > en else "EN"
    return "NA"


# ----------------------------------------------------------------------------
# Busca via LRCLIB direto (urllib)
# ----------------------------------------------------------------------------
_LRCLIB_UA = "BX-Audio-Manager/0.23 (https://github.com/local/bx)"


def _lrclib_get(artist: str, track: str, duration: Optional[float],
                timeout: int) -> Optional[str]:
    """Tenta /api/get (match exato). Retorna syncedLyrics ou None."""
    params = {"artist_name": artist, "track_name": track}
    if duration is not None and duration > 0:
        params["duration"] = f"{duration:.2f}"
    url = "https://lrclib.net/api/get?" + _up.urlencode(params)
    try:
        req = _u.Request(url, headers={
            "User-Agent": _LRCLIB_UA,
            "Accept": "application/json",
        })
        with _u.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        return data.get("syncedLyrics") or None
    except (_ue.HTTPError, _ue.URLError, OSError, json.JSONDecodeError):
        return None


def _lrclib_search(artist: str, track: str, timeout: int) -> Optional[str]:
    """Fallback /api/search. Prefere syncedLyrics > plainLyrics."""
    q = f"{artist} {track}".strip()
    if not q:
        return None
    url = "https://lrclib.net/api/search?" + _up.urlencode({"q": q})
    try:
        req = _u.Request(url, headers={
            "User-Agent": _LRCLIB_UA,
            "Accept": "application/json",
        })
        with _u.urlopen(req, timeout=timeout) as r:
            results = json.loads(r.read().decode("utf-8", errors="replace"))
    except (_ue.HTTPError, _ue.URLError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(results, list) or not results:
        return None
    # Ordena por duração mais próxima se a duração for conhecida?
    # Por simplicidade: primeiro com syncedLyrics, depois plain.
    for item in results:
        sl = item.get("syncedLyrics") or ""
        if sl:
            return sl
    for item in results:
        pl = item.get("plainLyrics") or ""
        if pl:
            return pl
    return None


def buscar_letra(artista: str, titulo: str, destino: Path,
                 timeout: int = TIMEOUT_BUSCA, log_erros=None,
                 duracao: Optional[float] = None) -> bool:
    """Busca LRC em lrclib.net via urllib.request (v0.23, item 2).

    Tenta /api/get (match exato, com duração decimal), cai para
    /api/search. Escreve o resultado atomicamente.
    """
    if not artista or not titulo:
        return False
    texto = _lrclib_get(artista, titulo, duracao, timeout)
    if not texto:
        texto = _lrclib_search(artista, titulo, timeout)
    if not texto or not texto.strip():
        bx_core.log_erro(log_erros, f"LRCLIB: sem letra para {artista} - {titulo}")
        return False
    if not bx_core.escrever_atomico(destino, texto):
        return False
    lrc_normalizar(destino)
    return True


# ----------------------------------------------------------------------------
# Tradução — hash com idiomas (item 7.3)
# ----------------------------------------------------------------------------
_HASH_SEP = "\x1f"


def _trad_hash(texto: str, src: str, tgt: str) -> str:
    base = (
        texto.lower().strip() + _HASH_SEP
        + (src or "auto").lower() + _HASH_SEP
        + (tgt or "pt").lower()
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _backup_org(lrc_dest: Path, cfg: Optional[Config] = None) -> Optional[Path]:
    if not lrc_dest.is_file():
        return None
    org = lrc_dest.with_name(lrc_dest.name + ".org")
    if org.is_file() and org.stat().st_size > 0:
        return org
    tmp = org.with_name(org.name + bx_core.SUFIXO_PARCIAL)
    try:
        shutil.copy2(lrc_dest, tmp)
        bx_core._fsync(tmp)
        os.replace(tmp, org)
        bx_core._fsync_dir(org.parent)
        if cfg is not None:
            bx_core.diario_add(cfg.diario, "BAK", str(lrc_dest), str(org))
        return org
    except OSError:
        tmp.unlink(missing_ok=True)
        return None


def traduzir_um(texto: str, cfg: Config, src: str = "auto", tgt: str = "pt",
                con=None) -> str:
    t = texto.strip()
    if not t:
        return texto
    chave = _trad_hash(t, src, tgt)
    if con is not None:
        em_cache = bx_core.trad_get(con, chave)
    else:
        with bx_core.sessao_db(cfg) as _con:
            em_cache = bx_core.trad_get(_con, chave)
    if em_cache is not None:
        return em_cache
    fn = _get_translator()
    if fn is None:
        return texto
    try:
        trad = (fn(t, src, tgt) or "").strip()
    except Exception:
        return texto
    if not trad:
        return texto
    if con is not None:
        bx_core.trad_put(con, chave, t, trad, src, tgt)
    else:
        with bx_core.sessao_db(cfg) as _con:
            bx_core.trad_put(_con, chave, t, trad, src, tgt)
    return trad


def traduzir_letra(lrc_org: Path, lrc_dest: Path, cfg: Config,
                    src: str = "auto", tgt: str = "pt") -> int:
    if not lrc_org.is_file() and lrc_dest.is_file():
        if _backup_org(lrc_dest, cfg) is None:
            return 0
        lrc_org = lrc_dest.with_name(lrc_dest.name + ".org")
    if not lrc_org.is_file():
        return 0
    try:
        linhas = lrc_org.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    saida: list[str] = []
    traduzidos = 0
    with bx_core.sessao_db(cfg) as con:
        for linha in linhas:
            m = TIMESTAMP_RE.match(linha)
            if not m:
                saida.append(linha)
                continue
            prefixo = m.group(0)
            corpo = linha[len(prefixo):].strip()
            saida.append(linha)
            if not corpo:
                continue
            trad = traduzir_um(corpo, cfg, src, tgt, con=con)
            if trad and trad != corpo:
                saida.append(f"{prefixo} {trad}")
                traduzidos += 1
    if not bx_core.escrever_atomico(lrc_dest, "\n".join(saida) + "\n"):
        return 0
    return traduzidos


def auto_traduzir_se_en(lrc: Path, cfg: Config,
                        src: str = "en", tgt: str = "pt") -> int:
    if not lrc.is_file():
        return 0
    try:
        linhas = lrc.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    pares = 0
    for i in range(len(linhas) - 1):
        m1 = TIMESTAMP_RE.match(linhas[i])
        m2 = TIMESTAMP_RE.match(linhas[i + 1])
        if m1 and m2 and m1.group(0) == m2.group(0) and linhas[i] != linhas[i + 1]:
            pares += 1
    if pares >= 3:
        return 0
    org = _backup_org(lrc, cfg)
    if org is None:
        return 0
    return traduzir_letra(org, lrc, cfg, src=src, tgt=tgt)


# ----------------------------------------------------------------------------
# Deslocar timestamps
# ----------------------------------------------------------------------------
def _fmt_ts(segundos: float) -> str:
    cs_total = max(0, int(round(segundos * 100)))
    seg, cs = divmod(cs_total, 100)
    return f"[{seg // 60:02d}:{seg % 60:02d}.{cs:02d}]"


def cmd_deslocar(lrc_path: str, segundos: float) -> int:
    p = Path(lrc_path)
    if not p.is_file():
        print(f"Arquivo não encontrado: {lrc_path}", file=sys.stderr)
        return 1
    linhas = p.read_text(encoding="utf-8", errors="replace").splitlines()
    saida: list[str] = []
    for linha in linhas:
        m = TIMESTAMP_RE.match(linha)
        if not m:
            saida.append(linha)
            continue
        total = _timestamp_para_segundos(linha) or 0.0
        saida.append(_fmt_ts(total + segundos) + linha[len(m.group(0)):])
    if not bx_core.escrever_atomico(p, "\n".join(saida) + "\n"):
        print(f"Falha ao escrever: {lrc_path}", file=sys.stderr)
        return 1
    print(f"deslocado {segundos:+.2f}s: {lrc_path}")
    return 0


# ----------------------------------------------------------------------------
# Varredura do acervo
# ----------------------------------------------------------------------------
def _pasta_ignorada(nome: str) -> bool:
    return nome in bx_core.PASTAS_BLINDADAS or nome.startswith("Z_")


def _varrer(raiz: Path):
    for dp, dns, fns in os.walk(raiz):
        if Path(dp) == raiz:
            dns[:] = [d for d in dns if not _pasta_ignorada(d)]
        yield Path(dp), fns


def _indice_audio(raiz: Path) -> dict[str, Path]:
    idx: dict[str, Path] = {}
    for d, fns in _varrer(raiz):
        for fn in fns:
            if fn.lower().endswith(EXT_AUDIO):
                idx.setdefault(Path(fn).stem.lower(), d)
    return idx


# ----------------------------------------------------------------------------
# Fusão automática de LRC (item 5.3)
# ----------------------------------------------------------------------------
def _parse_lrc(linhas: list[str]) -> tuple[list[str], list[tuple[float, str]]]:
    header: list[str] = []
    versos: list[tuple[float, str]] = []
    for ln in linhas:
        m = TIMESTAMP_RE.match(ln)
        if not m:
            if ln.strip():
                header.append(ln)
            continue
        seg = _timestamp_para_segundos(ln)
        if seg is None:
            continue
        corpo = ln[len(m.group(0)):].strip()
        if corpo:
            versos.append((seg, corpo))
    return header, versos


def _fmt_ts_full(seg: float) -> str:
    return _fmt_ts(seg)


def fundir_lrc(origem: Path, destino: Path) -> bool:
    try:
        linhas_o = origem.read_text(encoding="utf-8", errors="replace").splitlines()
        linhas_d = destino.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    h_o, v_o = _parse_lrc(linhas_o)
    h_d, v_d = _parse_lrc(linhas_d)
    union: dict[float, list[str]] = {}
    for seg, txt in v_d:
        union.setdefault(round(seg, 1), []).append(txt)
    for seg, txt in v_o:
        k = round(seg, 1)
        lst = union.setdefault(k, [])
        if not lst:
            lst.append(txt)
        elif txt and txt not in lst:
            lst.append(txt)
    header = list(dict.fromkeys(h_d + h_o))
    saida: list[str] = list(header)
    for k in sorted(union):
        seg = float(k)
        for txt in union[k]:
            saida.append(f"{_fmt_ts_full(seg)} {txt}")
    return bx_core.escrever_atomico(destino, "\n".join(saida) + "\n")


# ----------------------------------------------------------------------------
# Z_letras ↔ acervo
# ----------------------------------------------------------------------------
def _iter_letras_soltas(cfg: Config):
    if not cfg.letras.is_dir():
        return
    for p in cfg.letras.rglob("*.lrc"):
        if p.is_file():
            yield p


def aplicar_letras_soltas(cfg: Config, modo: str = "apply") -> dict[str, int]:
    stats = {"aplicadas": 0, "orfas": 0, "sem_correspondencia": 0,
             "fundidas": 0, "erros": 0}
    raiz = cfg.raiz_sd
    if not raiz.is_dir():
        return stats
    idx = _indice_audio(raiz)
    for f in sorted(_iter_letras_soltas(cfg)):
        pasta = idx.get(f.stem.lower())
        if pasta is None:
            stats["sem_correspondencia"] += 1
            continue
        alvo = pasta / f.name
        if alvo.exists() and alvo != f:
            if modo == "apply":
                if fundir_lrc(f, alvo):
                    stats["fundidas"] += 1
                    org_origem = f.with_name(f.name + ".org")
                    if org_origem.is_file():
                        destino_org = pasta / org_origem.name
                        if not destino_org.exists():
                            bx_core.mover(org_origem, destino_org, modo,
                                           cfg.diario, cfg)
                    try:
                        f.unlink()
                    except OSError:
                        stats["erros"] += 1
                else:
                    stats["erros"] += 1
            else:
                stats["fundidas"] += 1
            continue
        if bx_core.mover(f, alvo, modo, cfg.diario, cfg):
            org = f.with_name(f.name + ".org")
            if org.is_file():
                bx_core.mover(org, pasta / org.name, modo, cfg.diario, cfg)
            stats["aplicadas"] += 1
    for d, fns in list(_varrer(raiz)):
        for fn in fns:
            if not fn.lower().endswith(".lrc"):
                continue
            stem = Path(fn).stem.lower()
            if any((d / x).is_file() for x in os.listdir(d)
                   if x.lower().endswith(EXT_AUDIO) and Path(x).stem.lower() == stem):
                continue
            dest = cfg.letras / fn
            if bx_core.mover(d / fn, dest, modo, cfg.diario, cfg):
                org = d / (fn + ".org")
                if org.is_file():
                    bx_core.mover(org, cfg.letras / org.name, modo, cfg.diario, cfg)
                stats["orfas"] += 1
    return stats


# ----------------------------------------------------------------------------
# Listas de artistas
# ----------------------------------------------------------------------------
def _lista_arquivo(cfg: Config, nome: str) -> Path:
    return cfg.cfg_dir / f"{nome}.txt"


def _cmd_lista(cfg: Config, nome_lista: str, acao: str, nome: Optional[str]) -> int:
    arq = _lista_arquivo(cfg, nome_lista)
    itens = bx_core._ler_lista(arq)
    if acao == "lista":
        for x in itens:
            print(x)
        return 0
    if acao not in ("add", "del") or not nome:
        print("Uso: <internacional|nacional> add|del|lista [NOME]", file=sys.stderr)
        return 1
    nome = nome.strip()
    baixos = [x.lower() for x in itens]
    if acao == "add":
        if nome.lower() in baixos:
            return 0
        itens.append(nome)
    else:
        itens = [x for x in itens if x.lower() != nome.lower()]
    cab = "# Artistas forçados para Internacionais/.\n" if nome_lista == "internacionais" \
        else "# Artistas forçados como nacionais.\n"
    if not bx_core.escrever_atomico(arq, cab + "".join(x + "\n" for x in itens)):
        print(f"Falha ao gravar {arq}", file=sys.stderr)
        return 1
    return 0


def _pastas_artistas(raiz: Path) -> list[Path]:
    base = raiz / D_ART
    if not base.is_dir():
        return []
    res: list[Path] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        if len(d.name) == 1:
            res.extend(sorted(x for x in d.iterdir() if x.is_dir()))
        else:
            res.append(d)
    return res


def _gravar_lista(cfg: Config, nome_lista: str, artista: str) -> bool:
    itens = bx_core._ler_lista(_lista_arquivo(cfg, nome_lista))
    if artista.lower() in (x.lower() for x in itens):
        return False
    return _cmd_lista(cfg, nome_lista, "add", artista) == 0


# ----------------------------------------------------------------------------
# Idioma por artista (item 7.5)
# ----------------------------------------------------------------------------
def idioma_rodar(cfg: Config, modo: str = "sim") -> dict:
    rel: dict = {"internacionais": [], "duvida": [], "indefinido": [],
                 "nacionais": [], "na": [], "movidas": 0, "gravadas": 0}
    raiz = cfg.raiz_sd
    forc_int = {x.lower() for x in cfg.internacionais}
    forc_nac = {x.lower() for x in cfg.nacionais}
    for pasta in _pastas_artistas(raiz):
        nome = pasta.name
        pt = en = 0
        total_letras = 0
        for l in pasta.rglob("*.lrc"):
            org = l.with_name(l.name + ".org")
            lang = detectar_idioma(org if org.is_file() else l)
            if lang == "PT":
                pt += 1
                total_letras += 1
            elif lang == "EN":
                en += 1
                total_letras += 1
        tot = pt + en
        desc = f"{nome} (PT={pt} EN={en} NA={total_letras == 0})"
        if nome.lower() in forc_int:
            cat = "internacionais"
        elif nome.lower() in forc_nac:
            cat = "nacionais"
        elif tot == 0:
            cat = "na"
        elif en / tot >= 0.8:
            cat = "internacionais"
        elif pt / tot >= 0.8:
            cat = "nacionais"
        else:
            cat = "duvida"
        rel[cat].append(desc)
        if modo != "apply" or cat != "internacionais":
            continue
        faixas = [f for f in pasta.rglob("*")
                  if f.is_file() and f.name.lower().endswith(EXT_AUDIO)]
        int_dir = raiz / D_INT
        if len(faixas) >= MIN_ART:
            dest = int_dir / pasta_existente(int_dir, nome, cfg)
            if bx_core.mover(pasta, dest, modo, cfg.diario, cfg):
                rel["movidas"] += 1
        else:
            for f in [x for x in pasta.rglob("*") if x.is_file()]:
                if bx_core.mover(f, int_dir / f.name, modo, cfg.diario, cfg):
                    rel["movidas"] += 1
            for dp, _, _ in os.walk(pasta, topdown=False):
                try:
                    Path(dp).rmdir()
                except OSError:
                    pass
        if _gravar_lista(cfg, "internacionais", nome):
            rel["gravadas"] += 1
    return rel


# ----------------------------------------------------------------------------
# Testes
# ----------------------------------------------------------------------------
def _mudo(fn, *a):
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a)


def _teste() -> int:
    falhas: list[str] = []
    total = 0

    def chk(nome: str, cond: bool) -> None:
        nonlocal total
        total += 1
        print(("[PASS] " if cond else "[FAIL] ") + nome)
        if not cond:
            falhas.append(nome)

    tmp = Path(tempfile.mkdtemp(prefix="bxcrx_"))
    try:
        cfg = Config(bx_home=tmp / "home", raiz_sd=tmp / "sd", modo="apply")
        bx_core.init_dirs(cfg)
        cfg = bx_core.carregar_textos(cfg)
        (cfg.raiz_sd).mkdir(parents=True, exist_ok=True)
        bx_core.criar_estrutura_sd(cfg)

        f = tmp / "a.lrc"
        f.write_bytes(b"\xef\xbb\xbf[00:01.00] caf\xe9\r\n[00:02.00] ok\r\n")
        chk("normalizar modifica", lrc_normalizar(f))
        txt = f.read_text(encoding="utf-8")
        chk("normalizar utf8/lf/sem BOM",
            "\ufffd" in txt and "\r" not in txt and not txt.startswith("\ufeff"))
        log_lat = tmp / "a.lrc.encoding.log"
        chk("S13 log lateral criado", log_lat.is_file())
        chk("normalizar idempotente", not lrc_normalizar(f))

        chk("tem_tempo", lrc_tem_tempo(f))
        g = tmp / "b.lrc"
        g.write_text("sem tempo\n", encoding="utf-8")
        chk("sem tempo", not lrc_tem_tempo(g))
        chk("ordem ok", lrc_ordem(f))
        h = tmp / "c.lrc"
        h.write_text("[00:05.00] a\n[00:02.00] b\n", encoding="utf-8")
        chk("ordem quebrada", not lrc_ordem(h))

        d = tmp / "d.lrc"
        d.write_text("[00:10.50] x\n[00:01.00] y\n", encoding="utf-8")
        _mudo(cmd_deslocar, str(d), 1.5)
        lin = d.read_text(encoding="utf-8").splitlines()
        chk("deslocar +1.5", lin[0].startswith("[00:12.00]"))
        _mudo(cmd_deslocar, str(d), -5)
        lin = d.read_text(encoding="utf-8").splitlines()
        chk("deslocar não negativo", lin[1].startswith("[00:00.00]"))

        en = tmp / "en.lrc"
        en.write_text("[00:01.00] i love you baby and the night\n" * 12,
                      encoding="utf-8")
        pt = tmp / "pt.lrc"
        pt.write_text("[00:01.00] eu não quero você para mim amor\n" * 12,
                      encoding="utf-8")
        chk("idioma EN", detectar_idioma(en) == "EN")
        chk("idioma PT", detectar_idioma(pt) == "PT")
        chk("idioma NA", detectar_idioma(g) == "NA")

        chk("limpar sufixo", limpar("Song (Official Video)", cfg.suf_re) == "Song")
        chk("inicial", inicial("Ágata") == "a" and inicial("7x") == "#")
        chk("nome_pasta", nome_pasta("BEATLES", cfg.suf_re) == "Beatles")

        h1 = _trad_hash("hello", "en", "pt")
        h2 = _trad_hash("hello", "en", "es")
        chk("7.3 hash muda com tgt", h1 != h2)
        h3 = _trad_hash("hello", "en", "pt")
        chk("7.3 hash estável", h1 == h3)

        injetar_tradutor(lambda t, s, g_: f"{g_.upper()} {t}")
        org = tmp / "t.lrc.org"
        dst = tmp / "t.lrc"
        org.write_text("[00:01.00] hello\n[00:02.00] world\n", encoding="utf-8")
        n = traduzir_letra(org, dst, cfg)
        saida = dst.read_text(encoding="utf-8").splitlines()
        chk("traduzir conta", n == 2)
        chk("traduzir 2 linhas por verso",
            len(saida) == 4 and saida[1] == "[00:01.00] PT hello")

        # Q: mock com tgt para distinguir hit/miss real (nao tautologico)
        _ = traduzir_um("hello2", cfg, "en", "pt")  # popula cache
        chk("cache hit", traduzir_um("hello2", cfg, "en", "pt") == "PT hello2")
        chk("cache miss para tgt diferente",
            traduzir_um("hello2", cfg, "en", "es") == "ES hello2")

        bob = tmp / "bob.lrc"
        bob.write_text("[00:01.00] hi\n", encoding="utf-8")
        org_b = _backup_org(bob)
        chk("7.2 backup .org criado",
            org_b is not None and org_b.is_file()
            and org_b.read_text() == bob.read_text())

        injetar_tradutor(lambda t, s, g_: "PT " + t)
        enl = tmp / "music.lrc"
        enl.write_text("[00:01.00] hello\n[00:02.00] world\n[00:03.00] test\n",
                        encoding="utf-8")
        n_auto = auto_traduzir_se_en(enl, cfg)
        chk("7.1 auto traduz EN", n_auto == 3)
        n_auto2 = auto_traduzir_se_en(enl, cfg)
        chk("7.1 idempotente (já traduzido)", n_auto2 == 0)

        orig = tmp / "orig.lrc"
        orig.write_text("[00:01.00] linha A\n[00:02.00] linha B\n", encoding="utf-8")
        dest = tmp / "dest.lrc"
        dest.write_text("[00:01.00] linha A\n[00:02.00] linha B traduzida\n[00:03.00] só destino\n",
                        encoding="utf-8")
        okf = fundir_lrc(orig, dest)
        content = dest.read_text(encoding="utf-8")
        chk("5.3 fundiu sem perder destino", okf and "só destino" in content)
        chk("5.3 manteve texto destino (não duplicou)",
            content.count("linha B traduzida") == 1)

        art = cfg.raiz_sd / D_ART / "Zed"
        art.mkdir(parents=True, exist_ok=True)
        (art / "Musica.m4a").write_bytes(b"x")
        sub = cfg.letras / "subfolder"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "Musica.lrc").write_text("[00:01.00] a\n", encoding="utf-8")
        (cfg.letras / "Nada.lrc").write_text("[00:01.00] a\n", encoding="utf-8")
        (art / "Orfa.lrc").write_text("[00:01.00] a\n", encoding="utf-8")
        st = aplicar_letras_soltas(cfg, "apply")
        chk("5.2 aplicada de subpasta",
            st["aplicadas"] == 1 and (art / "Musica.lrc").is_file())
        chk("5.2 sem correspondência", st["sem_correspondencia"] == 1)
        chk("5.2 órfã → Z_letras",
            st["orfas"] == 1 and (cfg.letras / "Orfa.lrc").is_file())

        ing = cfg.raiz_sd / D_ART / "Inglesa"
        ing.mkdir(parents=True, exist_ok=True)
        for i in range(4):
            (ing / f"s{i}.m4a").write_bytes(b"x")
            (ing / f"s{i}.lrc").write_text(en.read_text(encoding="utf-8"),
                                            encoding="utf-8")
        r = idioma_rodar(cfg, "sim")
        chk("idioma relatório",
            any("Inglesa" in x for x in r["internacionais"]))
        r = idioma_rodar(cfg, "apply")
        chk("idioma apply move",
            (cfg.raiz_sd / D_INT / "Inglesa").is_dir() and r["movidas"] == 1)

        na_art = cfg.raiz_sd / D_ART / "Instrumental"
        na_art.mkdir(parents=True, exist_ok=True)
        (na_art / "track.m4a").write_bytes(b"x")
        r_na = idioma_rodar(cfg, "apply")
        chk("7.5 NA retido (não move)",
            any("Instrumental" in x for x in r_na["na"])
            and na_art.is_dir())
        nac = bx_core._ler_lista(_lista_arquivo(cfg, "nacionais"))
        chk("7.5 NA não grava em nacionais",
            not any("instrumental" in x.lower() for x in nac))

        _cmd_lista(cfg, "internacionais", "add", "Foo")
        chk("lista add", "Foo" in bx_core._ler_lista(_lista_arquivo(cfg, "internacionais")))
        _cmd_lista(cfg, "internacionais", "del", "foo")
        chk("lista del", "Foo" not in bx_core._ler_lista(_lista_arquivo(cfg, "internacionais")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"== RESUMO bx-crx: PASS={total - len(falhas)} FAIL={len(falhas)} ==")
    return 1 if falhas else 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    if "--teste" in argv:
        return _teste()

    # Item 10: parser canonico (aceita --raiz X e --raiz=X em qualquer pos)
    raiz, argv = bx_core.extrair_raiz_arg(argv)

    if not argv or argv[0] in ("-h", "--help", "ajuda"):
        print(__doc__)
        return 0

    cmd = argv[0]
    if cmd == "deslocar" and len(argv) >= 3:
        try:
            return cmd_deslocar(argv[1], float(argv[2].replace(",", ".")))
        except ValueError:
            print("segundos inválido", file=sys.stderr)
            return 1

    apply_ = "--apply" in argv
    modo_apply = cmd in ("traduzir", "letras-soltas") or (cmd == "idioma" and apply_)
    cfg = bx_core.carregar_config(
        raiz_arg=raiz, modo="apply" if modo_apply else "sim")
    if modo_apply:
        bx_core.iniciar_sessao(cfg)

    if cmd == "letras-soltas":
        s = aplicar_letras_soltas(cfg, modo="apply")
        print(f"aplicadas={s['aplicadas']} fundidas={s['fundidas']} "
              f"orfas={s['orfas']} sem_correspondencia={s['sem_correspondencia']}")
        return 0
    if cmd == "buscar" and len(argv) >= 4:
        ok = buscar_letra(argv[1], argv[2], Path(argv[3]),
                          log_erros=cfg.log_erros)
        print("OK" if ok else "FALHOU")
        return 0 if ok else 1
    if cmd == "traduzir" and len(argv) >= 3:
        src = argv[3] if len(argv) > 3 else "auto"
        tgt = argv[4] if len(argv) > 4 else "pt"
        n = traduzir_letra(Path(argv[1]), Path(argv[2]), cfg, src=src, tgt=tgt)
        print(f"traduzidos={n}")
        return 0
    if cmd == "idioma":
        rel = idioma_rodar(cfg, "apply" if apply_ else "sim")
        for secao in ("internacionais", "duvida", "indefinido", "nacionais", "na"):
            itens = rel.get(secao, [])
            if itens:
                print(f"\n== {secao.upper()} ==")
                for x in itens:
                    print(f"  {x}")
        print(f"\nmovidas={rel['movidas']} gravadas={rel['gravadas']}")
        return 0
    if cmd in ("internacional", "nacional") and len(argv) >= 2:
        nome = argv[2] if len(argv) > 2 else None
        return _cmd_lista(cfg, cmd + "s", argv[1], nome)

    print(f"Comando desconhecido em crx: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))