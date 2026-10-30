#!/usr/bin/env python3
"""bx.py — Orquestrador do BX Audio Manager v0.2 (Atualização 02).

Aplicado nesta versão:
  • 9.1 + 9.2 — barra de progresso ciente do estado da letra e do lote
                (delegado a bx_get.py; aqui só ficam os utilitários).
  • bx-fix.sh — incorporado: `bx fix <args>` é o wrapper definitivo.
  • `bx teste` — agrega PASS/FAIL de todos os módulos, mostra falhas
                 por módulo e o total geral no final.

imp/exp/diff continuam disponíveis.
"""
from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

VERSAO = "0.23.0"
AQUI = Path(__file__).resolve().parent
HOME = Path.home()

SCRIPTS_PY = (
    "bx.py", "bx_core.py", "bx_dbx.py", "bx_nmx.py", "bx_crx.py",
    "bx_fix.py", "bx_get.py", "bx_testkit.py",
    "bx_kernel.py", "bx_io.py", "bx_media.py",
)

DOWNLOADS_ENV = "BX_DOWNLOADS"
DOWNLOADS_DEFAULT = "/storage/emulated/0/Download"

ALIAS_FIX = {
    "sim": "simular", "apl": "aplicar", "mig": "migrar",
    "lim": "limpar", "rev": "revisao", "gru": "grupo",
    "sel": "selo", "nos": "nosplit", "sta": "stats",
    "aud": "auditar", "ver": "verificar", "fan": "fantasmas",
    "des": "desfazer", "rec": "reconstruir", "trv": "travar",
    "dtr": "destravar", "tra": "travados", "ind": "indice",
    "rep": "report", "exp": "explicar", "err": "erros",
    "ret": "retry", "esq": "esquecer", "jou": "journal",
    "rpg": "replaygain", "raiz": "raiz", "curar": "curar",
    "exp sub": "expirar-substituidos",
    "cfg che": "config-check",
    "idi sca": "idioma-scan",
    "des ate": "desfazer-ate",
    "lim err": "limpar-erros",
}

ALIAS_GET = {
    "rod": "rodar", "add": "add", "pla": "playlist",
    "com": "completar", "aut": "auto", "fila": "fila",
    "aud lin": "auditar-links",
    "pla rar": "playlist-raridades",
}

ALIAS_CRX = {
    "idi": "idioma", "tra": "traduzir", "let": "letras",
    "int": "internacional", "nac": "nacional", "bus": "buscar",
    "des": "deslocar",
    "let sol": "letras-soltas",
}


def _traduzir_trigram(args, alias):
    """Traduz 1 ou 2 trigramas iniciais para nome canonico.

    Excecoes de 4 letras (raiz/fila/curar) ficam como estao -- mapeamento
    identitario no dict, sem efeito. Nomes canonicos passam inalterados.
    """
    if not args:
        return args
    if len(args) >= 2:
        chave2 = f"{args[0]} {args[1]}"
        if chave2 in alias:
            return [alias[chave2]] + args[2:]
    if args[0] in alias:
        return [alias[args[0]]] + args[1:]
    return args


MAPA_FIX = {
    "simular", "aplicar", "migrar", "limpar",
    "grupo", "selo", "nosplit", "raiz", "revisao",
    "stats", "auditar", "verificar", "fantasmas", "desfazer", "reconstruir",
    "curar",
    "travar", "destravar", "travados", "indice", "report", "explicar",
    "config-check", "replaygain", "idioma-scan", "erros", "retry",
    "esquecer", "limpar-erros", "journal", "desfazer-ate",
    "expirar-substituidos",
}
MAPA_GET = {"rodar", "add", "playlist", "completar", "auto", "fila",
            "auditar-links", "playlist-raridades"}
MAPA_CRX = {
    "idioma", "traduzir", "letras", "letras-soltas",
    "internacional", "nacional", "buscar", "deslocar",
}

# Trigramas canonicos (3 letras) -- aliases aditivos.
# 4-letras sao excecao legitima (raiz, fila, show, curar, auto).
MAPA_FIX_TRIG = {k for k in ALIAS_FIX if " " not in k}
MAPA_GET_TRIG = {k for k in ALIAS_GET if " " not in k}
MAPA_CRX_TRIG = {k for k in ALIAS_CRX if " " not in k}


def _p(*partes: str) -> Path:
    normalizado = []
    for p in partes:
        if p.startswith("bx-") and p.endswith(".py"):
            p = "bx_" + p[3:]
        normalizado.append(p)
    return AQUI.joinpath(*normalizado)


def _executar(script: str, args: list[str]) -> int:
    destino = _p(script)
    if not destino.is_file():
        print(f"❌ Script não encontrado: {destino}", file=sys.stderr)
        return 2
    try:
        return subprocess.run([sys.executable, str(destino)] + args).returncode
    except KeyboardInterrupt:
        print("\nInterrompido.", file=sys.stderr)
        return 130


def _cfg():
    import bx_core
    return bx_core.Config(
        bx_home=bx_core.resolver_bx_home(),
        raiz_sd=bx_core.resolver_raiz(None),
    )


def _pasta_downloads() -> Path:
    p = os.environ.get(DOWNLOADS_ENV, "").strip()
    if p:
        return Path(p).expanduser()
    for c in (DOWNLOADS_DEFAULT, str(HOME / "Download"), str(HOME)):
        if Path(c).is_dir():
            return Path(c)
    return Path.cwd()


def _expandir_nome(arg: str) -> str:
    a = arg.strip()
    if not a:
        return ""
    if a in ("instalar", "bx-instalar", "bx_instalar"):
        return "bx-instalar.sh"
    if a in ("core", "bx_core", "bx-core"):
        return "bx_core.py"
    if a in ("dbx", "bx_dbx", "bx-dbx"):
        return "bx_dbx.py"
    if a in ("testkit", "bx_testkit", "bx-testkit"):
        return "bx_testkit.py"
    if a in ("kernel", "bx_kernel", "bx-kernel"):
        return "bx_kernel.py"
    if a in ("io", "bx_io", "bx-io"):
        return "bx_io.py"
    if a in ("media", "bx_media", "bx-media"):
        return "bx_media.py"
    if (a.endswith(".sh") or a.endswith(".patch")
            or a.endswith(".tar.gz") or a.endswith(".tgz")):
        return a
    if a.endswith(".py"):
        return "bx_" + a[3:] if a.startswith("bx-") else a
    base = a if a.startswith("bx") else f"bx_{a}"
    if base.startswith("bx-"):
        base = "bx_" + base[3:]
    return f"{base}.py"


def _aplicar_patch(patch_file: Path, base_dir: Path) -> tuple[bool, str]:
    if not shutil.which("patch"):
        return False, "comando 'patch' não instalado (pkg install patch)"
    for strip in ("1", "0"):
        try:
            r = subprocess.run(
                ["patch", f"-p{strip}", "--forward", "--batch",
                 "--no-backup-if-mismatch",
                 "-d", str(base_dir), "-i", str(patch_file)],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                return True, f"-p{strip}"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    return False, "patch não aplicou (-p1 e -p0 falharam)"


# ============================================================================
# imp / exp / diff
# ============================================================================
def _cmd_imp(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv or "--teste" in argv
    manter = "--manter" in argv
    origem = _pasta_downloads()
    nomes: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--para="):
            origem = Path(a.split("=", 1)[1]).expanduser()
        elif a == "--para" and i + 1 < len(argv):
            origem = Path(argv[i + 1]).expanduser()
            i += 1
        elif not a.startswith("--"):
            nomes.append(a)
        i += 1

    arquivos: list[Path] = []
    patches: list[Path] = []
    tarballs: list[Path] = []
    if nomes:
        for n in nomes:
            f = origem / n
            if not f.is_file():
                f = origem / _expandir_nome(n)
            if not f.is_file():
                print(f"⚠ Não encontrado: {f}")
                continue
            if f.suffix == ".patch":
                patches.append(f)
            elif f.name.endswith(".tar.gz") or f.suffix == ".tgz":
                tarballs.append(f)
            else:
                arquivos.append(f)
        if not (arquivos or patches or tarballs):
            return 1
    else:
        arquivos = [origem / s for s in SCRIPTS_PY if (origem / s).is_file()]
        inst = origem / "bx-instalar.sh"
        if inst.is_file():
            arquivos.append(inst)
    if not (arquivos or patches or tarballs):
        print(f"Nada para importar em {origem}")
        return 0

    print(f"→ Importando de {origem}:")
    for a in arquivos:
        print(f"  📄 {a.name}")
    for p in patches:
        print(f"  🩹 {p.name}")
    for t in tarballs:
        print(f"  📦 {t.name}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    test_dir = HOME / ".bx_teste" / f"imp_{ts}"
    if test_dir.exists():
        shutil.rmtree(test_dir, ignore_errors=True)
    test_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Fix 11 (v2): desempacotar tarballs do bx exp.
        for _tb in tarballs:
            try:
                with tarfile.open(_tb) as _tar:
                    try:
                        _tar.extractall(test_dir, filter="data")
                    except TypeError:
                        _tar.extractall(test_dir)
                print(f"  v {_tb.name} desempacotado")
            except (tarfile.TarError, OSError) as _e:
                print(f"\nx Tarball {_tb.name}: {_e}")
                return 1
        # A11 - copia arquivos do usuario PRIMEIRO; patches por ULTIMO.
        # Assim o patch e aplicado sobre a versao final, nao sobrescrito.
        for a in arquivos:
            shutil.copy2(a, test_dir / a.name)
        # Base: arquivos do AQUI entram apenas se o usuario nao enviou
        for p in AQUI.glob("bx*.py"):
            destino = test_dir / p.name
            if not destino.is_file():
                shutil.copy2(p, destino)
        for p in patches:
            ok, msg = _aplicar_patch(p, test_dir)
            if not ok:
                print(f"\n❌ Patch {p.name}: {msg}")
                return 1
            print(f"  ✔ Patch {p.name} ({msg})")
        if (test_dir / "bx.py").is_file():
            (test_dir / "bx.py").chmod(0o755)

        print(f"\n→ Testando em {test_dir} (isolado)")
        try:
            r = subprocess.run(
                [sys.executable, str(test_dir / "bx.py"), "teste"],
                timeout=900,
            )
        except subprocess.TimeoutExpired:
            print("❌ Teste timeout (15min)")
            return 1
        if r.returncode != 0:
            print("\n❌ Teste falhou. Nada foi alterado.")
            return 1
        if dry_run:
            print("\n✅ Teste OK (dry-run: nada foi substituído)")
            return 0
        backup_dir = HOME / ".bx_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"pre_imp_{ts}.tar.gz"
        try:
            # A10 - backup so de codigo (.py/.sh/.patch); sem biblioteca.db,
            # temp_bx/, logs/ e backups/ (evita 1 GB+ em Termux).
            with tarfile.open(backup, "w:gz") as tar:
                for pattern in ("bx*.py", "bx*.sh", "*.patch",
                                "pyproject.toml", "LICENSE",
                                "README.md", ".gitignore"):
                    for p in AQUI.glob(pattern):
                        tar.add(p, arcname=p.name)
            print(f"✔ Backup: {backup}")
        except OSError as e:
            print(f"⚠ Backup falhou: {e}")
        modificados: list[str] = []
        # A12 - sincroniza .py E .sh (evita dessincronizar bx-instalar.sh).
        for pattern in ("bx*.py", "bx*.sh"):
            for f in test_dir.glob(pattern):
                destino = AQUI / f.name
                if not destino.is_file() or f.read_bytes() != destino.read_bytes():
                    shutil.copy2(f, destino)
                    destino.chmod(0o755)
                    modificados.append(f.name)
        # Metadados exportados via --incluir-meta/--incluir-tudo
        # tambem sao restaurados no import.
        for meta in ("pyproject.toml", "LICENSE", "README.md", ".gitignore"):
            src_m = test_dir / meta
            if not src_m.is_file():
                continue
            dst_m = AQUI / meta
            if not dst_m.is_file() or src_m.read_bytes() != dst_m.read_bytes():
                shutil.copy2(src_m, dst_m)
                modificados.append(meta)
        print(f"✔ {len(modificados)} arquivo(s) substituído(s):")
        for m in modificados:
            print(f"  • {m}")
        if not manter:
            for a in arquivos + patches:
                try:
                    a.unlink()
                except OSError:
                    pass
        shutil.rmtree(test_dir, ignore_errors=True)
        return 0
    except Exception as e:
        print(f"❌ Erro: {e}", file=sys.stderr)
        return 1


def _cmd_exp(argv: list[str]) -> int:
    modo_txt = "--txt" in argv
    incluir_db = "--incluir-db" in argv or "--incluir-tudo" in argv
    incluir_meta = "--incluir-meta" in argv or "--incluir-tudo" in argv
    incluir_config = "--incluir-tudo" in argv
    destino_dir = _pasta_downloads()
    nomes: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--para="):
            destino_dir = Path(a.split("=", 1)[1]).expanduser()
        elif a == "--para" and i + 1 < len(argv):
            destino_dir = Path(argv[i + 1]).expanduser()
            i += 1
        elif not a.startswith("--"):
            nomes.append(a)
        i += 1
    if nomes:
        arquivos: list[Path] = []
        for n in nomes:
            f = AQUI / _expandir_nome(n)
            if f.is_file():
                arquivos.append(f)
            else:
                print(f"⚠ Não encontrado: {f}")
        if not arquivos:
            return 1
    else:
        arquivos = [AQUI / s for s in SCRIPTS_PY if (AQUI / s).is_file()]
    if not arquivos:
        print("Nada para exportar.")
        return 1
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    destino_dir.mkdir(parents=True, exist_ok=True)
    if modo_txt:
        out = destino_dir / f"bx-exportado_{ts}.txt"
        try:
            with out.open("w", encoding="utf-8") as fh:
                for p in arquivos:
                    fh.write(f"\n# --- ARQUIVO: {p.name} ---\n")
                    try:
                        fh.write(p.read_text(encoding="utf-8", errors="replace"))
                    except OSError as e:
                        fh.write(f"# Erro: {e}\n")
                    fh.write("\n")
        except OSError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        print(f"✔ {out}")
        print(f"   {len(arquivos)} arquivo(s), {out.stat().st_size:,} bytes")
        return 0
    out = destino_dir / f"bx-exportado_{ts}.tar.gz"
    try:
        with tarfile.open(out, "w:gz") as tar:
            for p in arquivos:
                tar.add(p, arcname=p.name)
            if incluir_db:
                db = AQUI / "biblioteca.db"
                if db.is_file():
                    tar.add(db, arcname="biblioteca.db")
            if incluir_meta:
                for meta in ("pyproject.toml", "LICENSE",
                             "README.md", ".gitignore"):
                    mp = AQUI / meta
                    if mp.is_file():
                        tar.add(mp, arcname=meta)
            if incluir_config:
                cfg_dir = AQUI / "config"
                if cfg_dir.is_dir():
                    for f in cfg_dir.rglob("*"):
                        if f.is_file():
                            tar.add(f, arcname=str(f.relative_to(AQUI)))
    except OSError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    print(f"✔ {out}")
    print(f"   {len(arquivos)} arquivo(s), {out.stat().st_size:,} bytes")
    return 0


def _cmd_diff(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    origem = _pasta_downloads()
    nomes = [a for a in argv if not a.startswith("--") and a != "-v"]
    arquivos = [_expandir_nome(n) for n in nomes] if nomes else list(SCRIPTS_PY)
    diferente = 0
    for nome in arquivos:
        a = AQUI / nome
        b = origem / nome
        if not a.is_file() and not b.is_file():
            print(f"? {nome} (não em nenhum dos dois)")
            continue
        if not a.is_file():
            print(f"+ {nome} (só em Downloads)")
            diferente += 1
            continue
        if not b.is_file():
            print(f"- {nome} (só instalado)")
            continue
        if a.read_bytes() == b.read_bytes():
            print(f"= {nome}")
        else:
            diferente += 1
            print(f"≠ {nome}")
            if verbose:
                diff = difflib.unified_diff(
                    a.read_text(encoding="utf-8", errors="replace").splitlines(),
                    b.read_text(encoding="utf-8", errors="replace").splitlines(),
                    fromfile=f"instalado/{nome}",
                    tofile=f"Downloads/{nome}",
                    lineterm="",
                )
                for linha in diff:
                    print(linha)
    print(f"\n{diferente} arquivo(s) diferem")
    return 0 if diferente == 0 else 1


# ============================================================================
# Teste agregado (por módulo + total + lista de falhas)
# ============================================================================
def _run_module_test(mod: str) -> tuple[int, int, int, float, list[str]]:
    """Roda <mod> --teste. Retorna (PASS, FAIL, SKIP, duracao_s, falhas)."""
    caminho = _p(mod)
    if not caminho.is_file():
        print(f"AVISO {mod} ausente, pulando.")
        return (0, 0, 1, 0.0, [])
    ok = fail = skip = 0
    fail_names: list[str] = []
    import tempfile as _tf
    home_iso = _tf.mkdtemp(
        prefix=f"bx_teste_{mod.replace('.py', '').replace('.', '_')}_")
    env_iso = os.environ.copy()
    env_iso["BX_HOME"] = home_iso
    env_iso.pop("BX_RAIZ", None)
    t0 = time.time()
    proc = None
    try:
        try:
            proc = subprocess.Popen(
                [sys.executable, str(caminho), "--teste"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env_iso,
            )
        except FileNotFoundError:
            print(f"AVISO {mod}: python3 nao encontrado")
            return (0, 1, 0, 0.0, [f"{mod}: python3 missing"])
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if line.startswith("[PASS]"):
                ok += 1
            elif line.startswith("[FAIL]"):
                fail += 1
                name = line[len("[FAIL]"):].strip()
                if "|" in name:
                    name = name.split("|", 1)[0].strip()
                fail_names.append(name)
            elif line.startswith("[SKIP]"):
                skip += 1
        proc.wait()
    finally:
        shutil.rmtree(home_iso, ignore_errors=True)
    duracao = time.time() - t0
    if proc is not None and proc.returncode != 0 and fail == 0:
        fail = 1
        fail_names.append(f"{mod}: rc={proc.returncode}")
    return (ok, fail, skip, duracao, fail_names)

def _teste_bx_py() -> tuple[int, int, list[str]]:
    """imp/exp/diff internos. Retorna (PASS, FAIL, falhas)."""
    ok = fail = 0
    fail_names: list[str] = []

    def chk(nome: str, cond: bool) -> None:
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"[PASS] {nome}")
        else:
            fail += 1
            print(f"[FAIL] {nome}")
            fail_names.append(nome)

    testes_exp = [
        ("nmx", "bx_nmx.py"),
        ("bx-nmx", "bx_nmx.py"),
        ("bx-nmx.py", "bx_nmx.py"),
        ("core", "bx_core.py"),
        ("dbx", "bx_dbx.py"),
        ("testkit", "bx_testkit.py"),
        ("fix.patch", "fix.patch"),
    ]
    for inp, esperado in testes_exp:
        obtido = _expandir_nome(inp)
        chk(f"expandir {inp!r}", obtido == esperado)

    testes_exp2 = [
        ("kernel", "bx_kernel.py"),
        ("bx-kernel", "bx_kernel.py"),
        ("io", "bx_io.py"),
        ("bx-io", "bx_io.py"),
        ("media", "bx_media.py"),
        ("bx-media", "bx_media.py"),
    ]
    for inp, esperado in testes_exp2:
        chk(f"expandir {inp!r}", _expandir_nome(inp) == esperado)

    import tempfile
    import tarfile as _tar
    with tempfile.TemporaryDirectory() as tmp:
        r = _cmd_exp(["--txt", f"--para={tmp}"])
        chk("exp --txt",
            r == 0 and len(list(Path(tmp).glob("bx-exportado_*.txt"))) == 1)
        r = _cmd_exp([f"--para={tmp}"])
        tars = list(Path(tmp).glob("bx-exportado_*.tar.gz"))
        chk("exp .tar.gz", r == 0 and len(tars) == 1)
        if tars:
            with _tar.open(tars[0]) as _t:
                _names = _t.getnames()
            _n_py = sum(1 for _n in _names if _n.endswith(".py"))
            chk("exp tar contem 11 .py", _n_py == 11)
            chk("exp tar sem metadata", "pyproject.toml" not in _names)
        r = _cmd_exp([f"--para={tmp}", "--incluir-meta"])
        tars = list(Path(tmp).glob("bx-exportado_*.tar.gz"))
        if tars:
            with _tar.open(tars[-1]) as _t:
                _names = _t.getnames()
            chk("exp --incluir-meta: pyproject.toml",
                "pyproject.toml" in _names)
            chk("exp --incluir-meta: LICENSE", "LICENSE" in _names)
            chk("exp --incluir-meta: README.md", "README.md" in _names)
            chk("exp --incluir-meta: .gitignore", ".gitignore" in _names)

    return (ok, fail, fail_names)


def _teste(pipeline: bool = False) -> int:
    """Roda --teste em todos os modulos + bx.py e agrega os resultados."""
    modulos = [
        "bx_core.py", "bx_dbx.py", "bx_nmx.py", "bx_crx.py",
        "bx_fix.py", "bx_get.py", "bx_testkit.py",
    ]
    resultados: dict[str, tuple[int, int, int, float, list[str]]] = {}

    inicio = time.time()
    for mod in modulos:
        print(f"\n{'=' * 62}")
        print(f"  {mod}")
        print(f"{'=' * 62}")
        resultados[mod] = _run_module_test(mod)

    print(f"\n{'=' * 62}")
    print(f"  bx.py (imp/exp/diff)")
    print(f"{'=' * 62}")
    t0 = time.time()
    res_bx = _teste_bx_py()
    resultados["bx.py"] = (res_bx[0], res_bx[1], 0,
                            time.time() - t0, list(res_bx[2]))
    total_geral = time.time() - inicio

    total_ok = sum(r[0] for r in resultados.values())
    total_fail = sum(r[1] for r in resultados.values())
    total_skip = sum(r[2] for r in resultados.values())

    sep = "=" * 62
    print(f"\n{sep}")
    print("RESUMO GERAL")
    print(sep)
    print(f"  {'Modulo':<20s} {'PASS':>4s} {'FAIL':>4s} {'SKIP':>4s} {'Tempo':>9s}")
    print("-" * 62)
    for mod, (ok, fail, skip, dur, _f) in resultados.items():
        marca = "OK" if fail == 0 else "XX"
        print(f"  {marca} {mod:<17s} {ok:>4d} {fail:>4d} {skip:>4d} {dur:>8.2f}s")
    print("-" * 62)
    print(f"  {'TOTAL GERAL':<20s} {total_ok:>4d} {total_fail:>4d} "
          f"{total_skip:>4d} {total_geral:>8.2f}s")
    print(sep)

    if total_fail > 0:
        print("\nFALHAS DETALHADAS (modulo -> teste):")
        for mod, (_ok, fail, _sk, _dur, fail_names) in resultados.items():
            if fail == 0:
                continue
            print(f"\n  [X] {mod} ({fail} falha(s)):")
            for name in fail_names:
                print(f"      - {name}")
        print(f"\nFALHOU: {total_fail} falha(s) em "
              f"{sum(1 for r in resultados.values() if r[1] > 0)} modulo(s)")
        return 1

    print("\nOK: todos os modulos passaram.")
    if total_skip > 0:
        print(f"   {total_skip} teste(s) pulado(s) por dependencia ausente "
              f"(ffmpeg/ffprobe). Instale com: pkg install ffmpeg")
    return 0

# ============================================================================
# Help
# ============================================================================
def _help() -> int:
    print(f"""BX AUDIO MANAGER v{VERSAO} — Python

Uso: bx <comando> [args]

TRIGRAMAS 3 LETRAS sao canonicos (nomes completos tambem aceitos).
Excecoes de 4 letras preservadas: raiz, fila, curar.

═══════════ ORGANIZAÇÃO (fix) ═══════════
  bx fix sim            Simula. Precaching incluso (aquece cache SQLite).
  bx fix apl            Aplica (renomeia, move, taggeia, reconcilia).
    Flags: --forcar --serial --net --replaygain-off
           --dry-run --quiet --estimate --iterar N
  bx fix rpg            RG incremental (idempotente, retomavel).
    Flags: --forcar --workers N --sample SEG
           --sample 15: amostra 15s do meio (~10x rapido, ±1-2 dB)
  bx fix mig | lim      migrar | limpar [--aplicar]
  bx fix rev            revisao lista|set ERR CERTO|aplicar

═══════════ CADASTROS ═══════════
  bx fix gru | sel | nos    add|del|lista NOME
  bx fix raiz [CAM]         Excecao 4 letras

═══════════ IDIOMA / LETRAS ═══════════
  bx idi [--apply]          Detecta idioma (move p/ Internacionais/)
  bx tra ORG LRC [s] [t]    Traduz LRC (src=auto, tgt=pt)
  bx let                    Lista letras | bx let sol aplica Z_letras/
  bx int | nac              add|del|lista NOME
  bx bus ART TIT DST        Busca letra no LRCLIB

═══════════ DOWNLOADS (get) ═══════════
  bx get rod [--lote N] [--serial] [--quiet] [--estimate]
  bx get add LINK           Enfileira link/video do YouTube
  bx get pla [LINK] [--baixar]
  bx get com [N]            Completa RESGATADOS (ytsearch, default 20)
  bx get aut [--lote N] [--sem-fix] [--serial]
  bx get fila               Status da fila (4 letras)
  bx get aud lin            Audita links mortos (origem_morta=Y)
  bx get pla rar [DST]      Gera .m3u de raridades

═══════════ IMPORT / EXPORT / DIFF ═══════════
  bx imp [...]   Importa de Downloads (testa isolado, backup).
  bx exp [...]   Exporta .tar.gz (ou --txt).
    Flags: --incluir-meta (pyproject/LICENSE/README/.gitignore)
           --incluir-db   (biblioteca.db)
           --incluir-tudo (meta + db + config)
  bx diff [...]  Compara instalado x Downloads.

═══════════ MANUTENÇÃO (fix) ═══════════
  bx fix sta | aud | ver    stats / auditar / verificar
  bx fix fan                fantasmas [--incluir-z]
  bx fix des                desfazer | des ate TS (journal)
  bx fix rec | curar        reconstruir / curar (4 letras)
  bx fix exp sub            expirar-substituidos [--dias N] [--aplicar]
  bx fix ind | rep          indice / report
  bx fix trv | dtr | tra    travar / destravar / travados
  bx fix exp ARQ            explicar
  bx fix err | ret | esq    erros / retry / esquecer
  bx fix lim err            limpar-erros [--antes N]
  bx fix jou                journal [--desde] [--ate] [--verificar]
  bx fix cfg che            config-check
  bx fix idi sca            idioma-scan

═══════════ SISTEMA ═══════════
  bx teste              Roda --teste em todos os modulos + resumo
  bx db <cmd> [args]    bx_dbx.py cru (debug)
  bx instalar [flags]   bx-instalar.sh
  bx versao | -v        Versao
  bx help | -h          Esta tela

═══════════ OBSERVAÇÕES ═══════════
  • --raiz CAMINHO aceito em QUALQUER posicao (ou BX_RAIZ=)
  • Raiz padrao: config/raiz.txt (altere com: bx fix raiz CAMINHO)
  • Pastas protegidas: Z/, Y/, Z_*, Y_*, Internacionais/
  • Manifesto: 18 campos canonicos 'CHAVE:valor' (REPLAYGAIN ultimo)
  • COMPLETO = m4a/mp4+aac estereo; mp3 = RESGATADO
  • --estimate: historico_execucoes.jsonl (hibrido 70/30 byte/arq)
  • --quiet: silencia barra/painel (logs e stats finais mantidos)
  • RG lento? Use --sample 15 (amostragem, ±1-2 dB)
  • bx-fix.sh removido: use `bx fix <sub>`
""")
    return 0


# ============================================================================
# CLI
# ============================================================================
def _extrair_raiz_env(argv: list[str]) -> list[str]:
    """Promove --raiz CAMINHO (ou --raiz=X) para a env BX_RAIZ.

    Permite `bx fix aplicar --raiz X` funcionar mesmo quando o wrapper
    bx.py repassa para bx_fix.py via subprocess: a env é herdada e o
    subcomando já enxerga a raiz correta.
    """
    import bx_core as _bc
    raiz, novo = _bc.extrair_raiz_arg(argv)
    if raiz is not None:
        os.environ["BX_RAIZ"] = raiz
    return novo


def _cli(argv: list[str]) -> int:
    # E1 - instala panic hook global (traceback em CRASH_CRITICO_*.log)
    try:
        import bx_core as _bc
        _bc.instalar_panic_hook()
    except Exception:
        pass
    # Compat: se invocado como `bx-fix` (via symlink), roteia para bx fix.
    prog = Path(sys.argv[0]).name
    if prog in ALIAS_FIX:
        argv = ["fix"] + argv

    # --raiz em qualquer posição -> env BX_RAIZ (herdada pelo subprocess)
    argv = _extrair_raiz_env(argv)

    if not argv:
        return _help()
    if argv and argv[0] in ("--teste", "teste"):
        return _teste("--pipeline" in argv[1:])

    cmd = argv[0]
    resto = argv[1:]

    if cmd in ("help", "-h", "--help"):
        return _help()
    if cmd in ("versao", "-v", "--version", "version"):
        print(f"BX Audio Manager v{VERSAO}")
        return 0
    if cmd == "instalar":
        script = _p("bx-instalar.sh")
        if not script.is_file():
            print(f"❌ {script} não encontrado.", file=sys.stderr)
            return 2
        try:
            return subprocess.run(["bash", str(script)] + resto).returncode
        except FileNotFoundError:
            print("❌ bash não encontrado.", file=sys.stderr)
            return 2
    if cmd == "teste":
        return _teste("--pipeline" in resto)
    if cmd == "imp":
        return _cmd_imp(resto)
    if cmd == "exp":
        return _cmd_exp(resto)
    if cmd == "diff":
        return _cmd_diff(resto)
    if cmd == "fix":
        if not resto:
            return _help()
        return _executar("bx-fix.py",
                         _traduzir_trigram(resto, ALIAS_FIX))
    if cmd == "get":
        if not resto:
            return _help()
        return _executar("bx-get.py",
                         _traduzir_trigram(resto, ALIAS_GET))
    if cmd == "db":
        if not resto:
            print("Uso: bx db <cmd> [args]", file=sys.stderr)
            return 2
        if not _p("bx_dbx.py").is_file():
            print(f"❌ {_p('bx_dbx.py')} não encontrado.", file=sys.stderr)
            return 2
        if resto[0] == "show":
            return _executar("bx_dbx.py", list(resto))
        cfg = _cfg()
        return _executar("bx_dbx.py", [str(cfg.db_path)] + list(resto))
    if cmd in MAPA_CRX or cmd in MAPA_CRX_TRIG:
        if cmd == "letras":
            return _executar("bx-crx.py", ["letras-soltas"] + resto)
        return _executar("bx-crx.py",
                         _traduzir_trigram([cmd] + resto, ALIAS_CRX))
    if cmd in MAPA_FIX or cmd in MAPA_FIX_TRIG:
        return _executar("bx-fix.py",
                         _traduzir_trigram([cmd] + resto, ALIAS_FIX))
    if cmd in MAPA_GET or cmd in MAPA_GET_TRIG:
        return _executar("bx-get.py",
                         _traduzir_trigram([cmd] + resto, ALIAS_GET))

    print(f"Comando desconhecido: {cmd}", file=sys.stderr)
    return _help()


if __name__ == "__main__":
    try:
        sys.exit(_cli(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nInterrompido.", file=sys.stderr)
        sys.exit(130)
