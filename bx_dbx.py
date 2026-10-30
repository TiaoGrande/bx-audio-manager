#!/usr/bin/env python3
"""
bx_dbx.py — CLI fina (router) do banco SQLite do BX Audio Manager.

Sem SQL nem lógica de dados: só captura argumentos, chama o bx_core e
imprime o resultado. Requer bx_core.py na mesma pasta.
O comando `show` usa bx_core diretamente (sem bx_ncx.py).

Uso:
    bx_dbx.py <db> <comando> [args]
    bx_dbx.py show [--raiz CAMINHO] [--bx-home CAMINHO] [--apply]
    bx_dbx.py --teste        executa a suíte local (banco temporário)
    bx_dbx.py help | -h      mostra esta ajuda

Comandos:
    show                                   diagnóstico da config (bx_home, raiz, db, contagens)
    init                                   cria schema (idempotente)
    count                                  imprime total de faixas
    ins <17 campos>                        INSERT OR REPLACE
    dump [arquivo]                         dump pipe-separated (stdout se vazio)
    boot <csv>                             importa CSV se o catálogo estiver vazio
    csvlines                               substitui catálogo inteiro (stdin)
    setrel <id> <rel>                      atualiza coluna rel
    setcol <coluna> <valor> <id>           atualiza coluna (whitelist)
    idrel <rel>                            imprime id ou rc=1
    get <id>                               imprime linha pipe-separated
    getrel <rel>                           imprime linha pipe-separated
    delete <id>                            apaga faixa + vínculos
    link_add <video_id> <catalogo_id>      INSERT OR REPLACE
    links_bulk                             stdin: video_id|catalogo_id
    link_rel <video_id>                    imprime rel ou rc=1 se não vinculado
    links_all                              imprime rel|video_id,video_id,...
    links_of <catalogo_id>                 imprime video_id,video_id,...
    trad_get <hash>                        imprime tradução ou rc=1
    trad_put <hash> <orig> <trad> <io> <id> armazena tradução
    probe_get <chave> <tam> <mtime>        imprime JSON se bater, rc=1 senão
    probe_put <chave> <tam> <mtime> [json] armazena JSON (stdin se vazio)

Códigos de retorno: 0 ok | 1 não encontrado | 2 uso/argumentos inválidos
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Sequence

import bx_core


# ============================================================================
# show: diagnóstico da configuração (usa bx_core diretamente)
# ============================================================================

def _show(args: Sequence[str]) -> int:
    """Imprime a configuração resolvida. Item R: restaura BX_HOME no fim."""
    raiz = None
    apply_ = False
    _bx_home_antes = os.environ.get("BX_HOME")
    _bx_home_alterado = False
    i = 0
    try:
        while i < len(args):
            if args[i] == "--raiz" and i + 1 < len(args):
                raiz = args[i + 1]
                i += 2
            elif args[i] == "--bx-home" and i + 1 < len(args):
                os.environ["BX_HOME"] = args[i + 1]
                _bx_home_alterado = True
                i += 2
            elif args[i] == "--apply":
                apply_ = True
                i += 1
            else:
                sys.stderr.write(f"show: argumento inválido: {args[i]}\n")
                return 2
        cfg = bx_core.carregar_config(
            raiz_arg=raiz, modo="apply" if apply_ else "sim",
        )
        prev = cfg.suf_re[:80] + ("..." if len(cfg.suf_re) > 80 else "")
        print(f"bx_home  = {cfg.bx_home}")
        print(f"raiz_sd  = {cfg.raiz_sd}")
        print(f"modo     = {cfg.modo}")
        print(f"db       = {cfg.db_path}")
        print(f"cfg_dir  = {cfg.cfg_dir}")
        print(f"tmp_dir  = {cfg.tmp_dir}")
        print(f"log_dir  = {cfg.log_dir}")
        print(f"letras   = {cfg.letras}")
        print(f"sufixos  = {len(cfg.sufixos)}")
        print(f"grupos   = {len(cfg.grupos)}")
        print(f"selos    = {len(cfg.selos)}")
        print(f"nosplit  = {len(cfg.nosplit)}")
        print(f"equiv    = {len(cfg.equiv)}")
        print(f"sep      = {cfg.sep!r}")
        print(f"suf_re   = {prev}")
        return 0
    finally:
        if _bx_home_alterado:
            if _bx_home_antes is None:
                os.environ.pop("BX_HOME", None)
            else:
                os.environ["BX_HOME"] = _bx_home_antes


# ============================================================================
# Handlers (cada um: (con, args, db_path) -> rc)
# ============================================================================

def _c_help(con, a, db) -> int:
    print(__doc__)
    return 0


def _c_init(con, a, db) -> int:
    sys.stderr.write("ok: schema pronto\n")
    return 0


def _c_count(con, a, db) -> int:
    print(bx_core.db_contar(con))
    return 0


def _c_ins(con, a, db) -> int:
    bx_core.db_inserir(con, a)
    return 0


def _c_dump(con, a, db) -> int:
    if a and a[0] and a[0] != "-":
        bx_core.db_dump(con, Path(a[0]))
    else:
        sys.stdout.write(bx_core.db_dump(con))
    return 0


def _mk_cfg(db_path: str):
    """Item 11: Config minimo apontando para o DB do CLI."""
    try:
        return bx_core.Config(
            bx_home=bx_core.resolver_bx_home(),
            raiz_sd=bx_core.resolver_raiz(None),
        ).com_db(Path(db_path))
    except Exception:
        return None

def _c_boot(con, a, db) -> int:
    if not a:
        return 0
    n = bx_core.db_boot_csv(con, Path(a[0]), cfg=_mk_cfg(db))
    if n:
        sys.stderr.write(f"boot: importadas {n} faixas de {Path(a[0]).name}\n")
    return 0


def _c_csvlines(con, a, db) -> int:
    n = bx_core.db_substituir_catalogo(con, sys.stdin, cfg=_mk_cfg(db))
    sys.stderr.write(f"csvlines: {n} faixas importadas\n")
    return 0


def _c_setrel(con, a, db) -> int:
    if len(a) < 2:
        return 2
    bx_core.db_set_rel(con, a[0], a[1])
    return 0


def _c_setcol(con, a, db) -> int:
    if len(a) < 3:
        return 2
    raiz = None
    try:
        raiz = bx_core.resolver_raiz(None)
    except Exception:
        pass
    return 0 if bx_core.db_set_col(con, a[0], a[1], a[2], raiz_sd=raiz) else 2

def _c_idrel(con, a, db) -> int:
    if not a:
        return 2
    r = bx_core.db_id_por_rel(con, a[0])
    if r is None:
        return 1
    print(r)
    return 0


def _c_get(con, a, db) -> int:
    if not a:
        return 2
    r = bx_core.db_obter_row(con, a[0])
    if r is None:
        return 1
    print(_fmt_linha(r))
    return 0


def _c_getrel(con, a, db) -> int:
    if not a:
        return 2
    r = bx_core.db_obter_por_rel_row(con, a[0])
    if r is None:
        return 1
    print(_fmt_linha(r))
    return 0


def _c_delete(con, a, db) -> int:
    if not a:
        return 2
    bx_core.db_apagar(con, a[0])
    return 0


def _c_link_add(con, a, db) -> int:
    if len(a) < 2:
        return 2
    bx_core.link_add(con, a[0], a[1])
    return 0


def _c_links_bulk(con, a, db) -> int:
    bx_core.links_bulk(con, sys.stdin)
    return 0


def _c_link_rel(con, a, db) -> int:
    if not a:
        return 2
    r = bx_core.link_rel(con, a[0])
    if r is None:
        return 1
    print(r)
    return 0


def _c_links_all(con, a, db) -> int:
    for linha in bx_core.links_all(con):
        print(linha)
    return 0


def _c_links_of(con, a, db) -> int:
    if not a:
        return 2
    print(",".join(bx_core.links_of(con, a[0])))
    return 0


def _c_trad_get(con, a, db) -> int:
    if not a:
        return 2
    r = bx_core.trad_get(con, a[0])
    if r is None:
        return 1
    sys.stdout.write(r)
    return 0


def _c_trad_put(con, a, db) -> int:
    if len(a) < 5:
        return 2
    bx_core.trad_put(con, a[0], a[1], a[2], a[3], a[4])
    return 0


def _fmt_linha(row: Sequence[object]) -> str:
    """X2 - formatting de view no CLI (não no core)."""
    partes: list[str] = []
    for i, v in enumerate(row):
        if v is None:
            partes.append("")
        elif i in (bx_core.I_KHZ, bx_core.I_REPLAYGAIN):
            partes.append(f"{float(v):.1f}")
        else:
            partes.append(str(v))
    return "|".join(partes)


def _parse_probe(a: Sequence[str]):
    """Valida chave/tam/mtime. Retorna (chave, tam, mtime) ou None."""
    if len(a) < 3:
        return None
    try:
        return a[0], int(a[1]), float(a[2])
    except ValueError:
        return None


def _c_probe_get(con, a, db) -> int:
    p = _parse_probe(a)
    if p is None:
        return 2
    row = con.execute(
        "SELECT json FROM probe_cache WHERE chave=? AND tamanho=? AND mtime=?",
        (p[0], int(p[1]), float(p[2])),
    ).fetchone()
    if row is None:
        return 1
    sys.stdout.write(row[0])
    return 0



def _c_probe_put(con, a, db) -> int:
    p = _parse_probe(a)
    if p is None:
        return 2
    js = a[3] if len(a) >= 4 else sys.stdin.read()
    # A13 - INSERT em transacao (WAL fsync garantido; sem indice orfao em kill)
    with bx_core.transacao(con):
        con.execute(
            "INSERT OR REPLACE INTO probe_cache(chave, tamanho, mtime, json) "
            "VALUES (?,?,?,?)",
            (p[0], int(p[1]), float(p[2]), js),
        )
    return 0



ROTAS: dict[str, Callable[..., int]] = {
    "init": _c_init, "count": _c_count, "ins": _c_ins, "dump": _c_dump,
    "boot": _c_boot, "csvlines": _c_csvlines, "setrel": _c_setrel,
    "setcol": _c_setcol, "idrel": _c_idrel, "get": _c_get,
    "getrel": _c_getrel, "delete": _c_delete, "link_add": _c_link_add,
    "links_bulk": _c_links_bulk, "link_rel": _c_link_rel,
    "links_all": _c_links_all, "links_of": _c_links_of,
    "trad_get": _c_trad_get, "trad_put": _c_trad_put,
    "probe_get": _c_probe_get, "probe_put": _c_probe_put, "help": _c_help,
}


# ============================================================================
# CLI
# ============================================================================

def main(argv: Sequence[str]) -> int:
    """argv = [prog, db, comando, args...] (ou [prog, show, args...]).

    Aceita `show` em argv[1] (forma direta, sem db) e em argv[2]
    (forma injetada por `bx db show`, onde argv[1] é o db_path).
    """
    if len(argv) >= 2 and argv[1] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    if len(argv) >= 2 and argv[1] == "--teste":
        return _teste()
    if len(argv) >= 2 and argv[1] == "show":
        return _show(list(argv[2:]))
    if len(argv) >= 3 and argv[2] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    if len(argv) >= 3 and argv[2] == "show":
        return _show(list(argv[3:]))
    if len(argv) < 3:
        sys.stderr.write("uso: bx_dbx.py <db> <comando> [args]  (bx_dbx.py help)\n")
        return 2
    db, cmd, args = argv[1], argv[2], list(argv[3:])
    rota = ROTAS.get(cmd)
    if rota is None:
        sys.stderr.write(f"comando desconhecido: {cmd}\n")
        return 2
    import sqlite3 as _sq
    try:
        con = bx_core.conectar(db)
    except _sq.Error as e:
        sys.stderr.write(f"DB erro: {e}\n")
        return 3
    try:
        return rota(con, args, db)
    finally:
        con.close()



# ============================================================================
# Suíte de testes (só roda com --teste) — formato [PASS]/[FAIL] padrão BX
# ============================================================================

def _teste() -> int:
    import contextlib
    import io
    import tempfile

    total = [0, 0]  # [passou, falhou]

    def run(db: str, *args: str, stdin: str = "") -> tuple[int, str]:
        buf = io.StringIO()
        antigo = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = main(["bx_dbx.py", db, *args])
        finally:
            sys.stdin = antigo
        return rc, buf.getvalue()

    def ok(nome: str, cond: bool) -> None:
        if cond:
            total[0] += 1
            print(f"[PASS] {nome}")
        else:
            total[1] += 1
            print(f"[FAIL] {nome}")

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "t.db")
        ok("init", run(db, "init")[0] == 0)
        ok("count vazio", run(db, "count") == (0, "0\n"))

        faixa = ["id1", "Art", "Tit", "COMPLETO", "Artistas/Art/Tit.m4a",
                 "1000", "20260101", "abc", "pt", "44.1", "", "128", "sim"]
        ok("ins", run(db, "ins", *faixa)[0] == 0)
        ok("count 1", run(db, "count")[1] == "1\n")
        rc, out = run(db, "get", "id1")
        ok("get", rc == 0 and out.startswith("id1|Art|Tit|"))
        ok("get inexistente -> rc=1", run(db, "get", "zzz")[0] == 1)
        ok("getrel", run(db, "getrel", "Artistas/Art/Tit.m4a")[0] == 0)
        ok("idrel", run(db, "idrel", "Artistas/Art/Tit.m4a") == (0, "id1\n"))
        ok("idrel inexistente -> rc=1", run(db, "idrel", "nada")[0] == 1)
        ok("setrel", run(db, "setrel", "id1", "singles/t/Tit.m4a")[0] == 0)
        ok("idrel após setrel", run(db, "idrel", "singles/t/Tit.m4a")[1] == "id1\n")
        ok("setcol válida", run(db, "setcol", "bitrate", "256", "id1")[0] == 0)
        ok("setcol fora da whitelist -> rc=2", run(db, "setcol", "tam", "1", "id1")[0] == 2)
        ok("setcol args insuficientes -> rc=2", run(db, "setcol", "khz")[0] == 2)

        ok("link_add", run(db, "link_add", "vid1", "id1")[0] == 0)
        ok("link_rel", run(db, "link_rel", "vid1") == (0, "singles/t/Tit.m4a\n"))
        ok("link_rel inexistente -> rc=1", run(db, "link_rel", "x")[0] == 1)
        run(db, "links_bulk", stdin="vid2|id1\n")
        ok("links_of", run(db, "links_of", "id1") == (0, "vid1,vid2\n"))
        ok("links_all", run(db, "links_all")[1].startswith("singles/t/Tit.m4a|"))

        ok("trad_put", run(db, "trad_put", "h1", "hello", "olá", "en", "pt")[0] == 0)
        ok("trad_get", run(db, "trad_get", "h1") == (0, "olá"))
        ok("trad_get inexistente -> rc=1", run(db, "trad_get", "h2")[0] == 1)

        ok("probe_put", run(db, "probe_put", "k", "10", "5.0", '{"a":1}')[0] == 0)
        ok("probe_get", run(db, "probe_get", "k", "10", "5.0") == (0, '{"a":1}'))
        ok("probe_get tam diferente -> rc=1", run(db, "probe_get", "k", "11", "5.0")[0] == 1)
        ok("probe_put via stdin", run(db, "probe_put", "k2", "1", "1", stdin="{}")[0] == 0)
        ok("probe_get número inválido -> rc=2", run(db, "probe_get", "k", "x", "y")[0] == 2)

        rc, dump = run(db, "dump")
        ok("dump stdout", rc == 0 and dump.count("\n") == 1)
        arq = str(Path(tmp) / "d.csv")
        ok("dump arquivo", run(db, "dump", arq)[0] == 0 and Path(arq).read_text() == dump)

        db2 = str(Path(tmp) / "t2.db")
        ok("csvlines", run(db2, "csvlines", stdin=dump)[0] == 0)
        ok("csvlines contagem", run(db2, "count")[1] == "1\n")
        db3 = str(Path(tmp) / "t3.db")
        run(db3, "boot", arq)
        ok("boot", run(db3, "count")[1] == "1\n")

        ok("delete", run(db, "delete", "id1")[0] == 0)
        ok("delete contagem", run(db, "count")[1] == "0\n")
        ok("delete remove vínculos", run(db, "link_rel", "vid1")[0] == 1)
        ok("comando inválido -> rc=2", run(db, "xyz")[0] == 2)

        # show: usa bx_core direto (sem _carregar_ncx)
        antigo_home = os.environ.get("BX_HOME")
        os.environ["BX_HOME"] = str(Path(tmp) / "home")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["bx_dbx.py", "show", "--raiz", str(Path(tmp) / "raiz")])
            saida = buf.getvalue()
            ok("show (argv[1], sem db)",
               rc == 0 and "bx_home  =" in saida and "suf_re   =" in saida)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["bx_dbx.py", db, "show", "--raiz", str(Path(tmp) / "raiz")])
            ok("show (argv[2], via bx db show)",
               rc == 0 and "raiz_sd  =" in buf.getvalue())
            with contextlib.redirect_stderr(io.StringIO()):
                ok("show argumento inválido -> rc=2",
                   main(["bx_dbx.py", "show", "--x"]) == 2)
        finally:
            if antigo_home is None:
                os.environ.pop("BX_HOME", None)
            else:
                os.environ["BX_HOME"] = antigo_home

    print(f"\nTOTAL: {total[0]} PASS, {total[1]} FAIL")
    return 1 if total[1] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
