#!/usr/bin/env python3
"""bx_kernel.py -- Constantes e helpers puros (sem I/O, sem DB, sem subprocess).

Importado por bx_core, bx_io e bx_media. Nao importa nenhum outro modulo do
projeto. Todo valor nao-ASCII e escrito com escapes \\uXXXX para blindar
contra corrupcao de encoding em copy-paste.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Sequence


# ============================================================================
# PASTAS
# ============================================================================
D_ART = "Artistas"
D_SING = "singles"
D_DESC = "Z_desconhecidos"
D_INT = "Internacionais"
D_QUAR = "Z_\u00e1rea de transfer\u00eancia"
D_LOGS = "Z_logs"
D_SUBST = "Z_substitu\u00eddos"
D_IDX = "Z_indice_artistas"
D_REV = "Z_revisao"
D_LETRAS = "Z_letras"
D_LINKS = "Z_links"
D_Z = "Z"
D_Y = "Y"
PASTAS_BLINDADAS: tuple[str, ...] = (D_Z, D_Y)


# ============================================================================
# LIMITES
# ============================================================================
MIN_ART = 4
MAX_MIN = 20
TIMEOUT = 20
RETRY_DIAS = 7
MAX_PAR = 2
LOTE_MB = 100
LOTE_N = 20
LOTE_SQL = 100

SUF_DEFAULT = "@@nunca@@"
SEP_DEFAULT = ";"
SUFIXO_PARCIAL = ".bxpart"

RAIZ_PADRAO = "/storage/emulated/0/Music/A m\u00fasicas"

SUFIXOS_PADRAO: tuple[str, ...] = (
    "Official Video", "Official Music Video", "Official Audio",
    "Official Lyric Video", "Official Visualizer", "Official",
    "Lyric Video", "Lyrics Video", "Lyrics", "Letra",
    "Audio Oficial", "\u00c1udio Oficial", "Clipe Oficial", "Clipe",
    "Video Oficial", "V\u00eddeo Oficial", "M\u00fasica Oficial",
    "Visualizer", "Audio", "\u00c1udio", "HD", "Full HD", "4K",
    "1080p", "720p", "Legendado", "Legendado PT-BR", "Com Letra",
    "Videoclipe Oficial", "Videoclipe", "Anima\u00e7\u00e3o Oficial", "Anima\u00e7\u00e3o",
    "Lyric Video oficial", "Letra Oficial", "Lyric Oficial",
    "Lyric V\u00eddeo", "Lyric Video Oficial", "Lyrics Oficial", "Edit",
)
GRUPOS_PADRAO: tuple[str, ...] = ("CYPHER", "Poesia Ac\u00fastica")
SELOS_PADRAO: tuple[str, ...] = ("Sadstation", "SadStudio", "Sadnation")
NOSPLIT_PADRAO: tuple[str, ...] = (
    "Cypher", "Poesia Ac\u00fastica", "Perfil", "Sotam",
)


# ============================================================================
# CATALOGO (17 campos)
# ============================================================================
CAMPOS: tuple[str, ...] = (
    "id", "artista", "titulo", "estado", "rel", "tam", "data", "sha",
    "idioma", "khz", "tipo_letra", "bitrate", "tem_capa",
    "codec", "produtor", "replaygain", "origem_morta",
)
N_CAMPOS: int = len(CAMPOS)
I_TAM = 5
I_KHZ = 9
I_BITRATE = 11
I_CODEC = 13
I_PRODUTOR = 14
I_REPLAYGAIN = 15
I_ORIGEM_MORTA = 16
I_NULOS = (8, 10, 12, 13, 14)
MAX_TRAD = 50_000

COLUNAS_PERMITIDAS = frozenset({
    "idioma", "tipo_letra", "tem_capa", "khz", "bitrate", "estado",
    "codec", "produtor", "replaygain",
})
SQL_INS = ("INSERT OR REPLACE INTO catalogo VALUES (%s)"
           % ",".join("?" * N_CAMPOS))


# ============================================================================
# MANIFESTO (18 campos)
# ============================================================================
MANIFESTO_CAMPOS: tuple[str, ...] = (
    "VERS\u00c3O", "ID", "ARTISTA", "FAIXA", "CAMINHO", "TAMANHO", "SHA256",
    "DATA", "ESTADO", "LETRA", "IDIOMA", "KHZ", "BITRATE", "CODEC",
    "PRODUTOR", "ORIGEM_MORTA", "REPLAYGAIN", "TRAVADO",
)
MANIFESTO_VERSAO = "3"

ISO639_2: dict[str, str] = {
    "PT": "por", "EN": "eng", "ES": "spa", "FR": "fra",
    "DE": "deu", "IT": "ita", "JA": "jpn", "KO": "kor",
    "ZH": "zho", "RU": "rus", "AR": "ara",
    "NA": "und", "NAO": "und",
}

ESTADOS_MANIFESTO: tuple[str, ...] = (
    "COMPLETO", "LETRADO", "LETRA ESTATICA", "SEM LETRA",
    "RESGATADO LETRA ESTATICA", "RESGATADO LETRADO", "RESGATADO",
    "PRESERVADO",
)

PADROES_TEMP: tuple[str, ...] = (
    "l_*.lrc", "t_*.m4a", "m_*.txt", "n_*.lrc", "n_*.lrc.2",
    "lista_*.bin", "cat_snap_*.csv", "mc_*.txt", "fila_snap_*.txt",
    "idioma_*.lst", "idioma_*.res", "vd_*", "pc", "sbx_*",
    "dl_*.res", "aac_*.m4a", "rg_*.txt",
)

PRODUTOR_RE = re.compile(
    r"[\(\[]\s*(?:prod\.?|produzido\s+por)\s+([^\)\]]+)[\)\]]",
    re.IGNORECASE,
)


# ============================================================================
# HELPERS PUROS
# ============================================================================
def iso639_2(codigo: str) -> str:
    if not codigo:
        return "und"
    return ISO639_2.get(codigo.strip().upper(),
                        codigo.strip().lower()[:3] or "und")


# --- Mojibake: mapa de substituicao manual (fallback cascata) ---
_MOJIBAKE_MAP: dict[str, str] = {
    "\u00c3\u00a3": "\u00e3",
    "\u00c3\u00a1": "\u00e1",
    "\u00c3 ": "\u00e0",
    "\u00c3\u00a2": "\u00e2",
    "\u00c3\u00a4": "\u00e4",
    "\u00c3\u00a9": "\u00e9",
    "\u00c3\u00aa": "\u00ea",
    "\u00c3\u00a8": "\u00e8",
    "\u00c3\u00ad": "\u00ed",
    "\u00c3\u00ac": "\u00ec",
    "\u00c3\u00ae": "\u00ee",
    "\u00c3\u00b3": "\u00f3",
    "\u00c3\u00b4": "\u00f4",
    "\u00c3\u00b2": "\u00f2",
    "\u00c3\u00b5": "\u00f5",
    "\u00c3\u00ba": "\u00fa",
    "\u00c3\u00b9": "\u00f9",
    "\u00c3\u00bc": "\u00fc",
    "\u00c3\u00a7": "\u00e7",
    "\u00c3\u00b1": "\u00f1",
    "\u00c3\u0081": "\u00c1",
    "\u00c3\u2030": "\u00c9",
    "\u00c3\u008d": "\u00cd",
    "\u00c3\u008e": "\u00ce",
    "\u00c3\u201c": "\u00d3",
    "\u00c3\u201d": "\u00d4",
    "\u00c3\u2022": "\u00d5",
    "\u00c3\u0161": "\u00da",
    "\u00c3\u0153": "\u00dc",
    "\u00c3\u2021": "\u00c7",
}


def _fix_mojibake(s: str) -> str:
    """Tenta decodificar mojibake em ordem de robustez."""
    if "\u00c3" not in s and "\u00c2" not in s:
        return s
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
    try:
        fixed = s.encode("cp1252").decode("utf-8")
        if fixed and len(fixed) <= len(s) + 2:
            return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    try:
        fixed = s.encode("latin-1").decode("utf-8")
        if fixed and len(fixed) <= len(s) + 2:
            return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
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
    import logging
    if len(campos) > N_CAMPOS:
        logging.getLogger(__name__).warning(
            "_normalizar: %d campos truncados para %d",
            len(campos) - N_CAMPOS, N_CAMPOS)
    r: list[object] = [(_limpo(x) if x is not None else "")
                       for x in list(campos)[:N_CAMPOS]]
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


def normalizar_unicode(s: str) -> str:
    """NFD + remocao de diacriticos + lower. Uso: comparacao canonica."""
    if not s:
        return ""
    nfd = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in nfd if not unicodedata.combining(c))


def extrair_produtor(titulo: str) -> str:
    """Extrai produtores do titulo via PRODUTOR_RE. Retorna ';'-separado."""
    if not titulo:
        return ""
    nomes: list[str] = []
    for m in PRODUTOR_RE.finditer(titulo):
        for p in re.split(r"\s*[&,]\s*|\s+e\s+", m.group(1)):
            p = p.strip()
            if p and p.lower() not in ("e", "and"):
                nomes.append(p)
    return ";".join(dict.fromkeys(nomes))
