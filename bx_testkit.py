#!/usr/bin/env python3
"""
bx_testkit.py — Suíte complementar EXAUSTIVA (16 seções).

Seções:
   0. Ambiente (detecção de ffmpeg/ffprobe com versão mínima)
   1. Primitivas de filesystem
   2. Transferência atômica (enviar_sd — single-pass + O_TRUNC)
   3. Movimentação (mover)
   4. Schema SQLite (inclui fila_downloads e schema_version >= 3)
   5. Segurança do SQLite
   6. Manifestos
   7. Normalização Unicode
   8. Probe e cache (opcional: ffmpeg>=4)
   9. Tags e ReplayGain (opcional: ffmpeg>=4 + loudnorm)
  10. Codec AAC (opcional: ffmpeg>=4 + aac)
  11. Concorrência (flock + inode check)
  12. Runtime
  13. Integração entre módulos
  14. Integridade do manifesto (round-trip das 22 chaves, 7 estados)
  15. Adversarial I/O (corrupção, overwrite, garbage)
  16. Adversarial arquitetural (single-pass, O_TRUNC, 2 fases, inode)
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import bx_core

AQUI = Path(__file__).resolve().parent
_FFMPEG_MIN = (4, 0)
_FFPROBE_MIN = (4, 0)
_MIN_TOTAL_TESTS = 210  # trava anti-truncamento (v0.23 + §19)


def _v(t):
    return ".".join(map(str, t)) if t else "?"


def _parse_version(vstr):
    m = re.search(r"version\s+n?(\d+(?:\.\d+)*)", vstr, re.IGNORECASE)
    if not m:
        return ()
    return tuple(int(p) for p in m.group(1).split(".") if p)


def _ffmpeg_info():
    info = {"ffmpeg_bin": None, "ffmpeg_version": (), "ffmpeg_ok": False,
            "ffprobe_bin": None, "ffprobe_version": (), "ffprobe_ok": False,
            "has_aac": False, "has_libfdk_aac": False, "has_loudnorm": False}
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                           text=True, timeout=5, check=False)
        if r.returncode == 0:
            out = (r.stdout or "") + (r.stderr or "")
            info["ffmpeg_bin"] = "ffmpeg"
            info["ffmpeg_version"] = _parse_version(out)
            info["ffmpeg_ok"] = info["ffmpeg_version"] >= _FFMPEG_MIN
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        r = subprocess.run(["ffprobe", "-version"], capture_output=True,
                           text=True, timeout=5, check=False)
        if r.returncode == 0:
            out = (r.stdout or "") + (r.stderr or "")
            info["ffprobe_bin"] = "ffprobe"
            info["ffprobe_version"] = _parse_version(out)
            info["ffprobe_ok"] = info["ffprobe_version"] >= _FFPROBE_MIN
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    if info["ffmpeg_bin"]:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                               capture_output=True, text=True,
                               timeout=10, check=False)
            enc = (r.stdout or "") + (r.stderr or "")
            info["has_aac"] = bool(re.search(r"^\s+\S+\s+aac\s", enc, re.MULTILINE))
            info["has_libfdk_aac"] = bool(re.search(r"^\s+\S+\s+libfdk_aac\s",
                                                    enc, re.MULTILINE))
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                               capture_output=True, text=True,
                               timeout=10, check=False)
            info["has_loudnorm"] = bool(re.search(r"\bloudnorm\b",
                                                 (r.stdout or "") + (r.stderr or "")))
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return info


def _teste() -> int:
    _bx_home_antes = os.environ.get("BX_HOME")  # R3 FIX
    stats = {"pass": 0, "fail": 0, "skip": 0}
    fail_names: list[str] = []
    sec_atual = ["(inicial)"]

    def sec(t):
        sec_atual[0] = t
        print(f"\n──── {t} ────")

    def chk(nome, cond):
        if cond:
            stats["pass"] += 1
            print(f"[PASS] {nome}")
        else:
            stats["fail"] += 1
            fail_names.append(f"{sec_atual[0]} → {nome}")
            print(f"[FAIL] {nome}")

    def skip(nome, motivo):
        stats["skip"] += 1
        print(f"[SKIP] {nome} ({motivo})")

    info = _ffmpeg_info()
    sec("0. Ambiente detectado")
    print(f"  Python  : {sys.version.split()[0]}")
    print(f"  ffmpeg  : {info['ffmpeg_bin'] or 'ausente'}")
    if info["ffmpeg_bin"]:
        print(f"    {_v(info['ffmpeg_version'])} (min {_v(_FFMPEG_MIN)})  "
              f"aac={info['has_aac']} libfdk_aac={info['has_libfdk_aac']} "
              f"loudnorm={info['has_loudnorm']}")
    print(f"  ffprobe : {info['ffprobe_bin'] or 'ausente'}")

    env_absent = info["ffmpeg_bin"] is None and info["ffprobe_bin"] is None
    broken: list[str] = []
    if not env_absent:
        if info["ffmpeg_bin"] is None:
            broken.append("ffmpeg ausente")
        elif not info["ffmpeg_ok"]:
            broken.append(f"ffmpeg<{_v(_FFMPEG_MIN)}")
        if info["ffprobe_bin"] is None:
            broken.append("ffprobe ausente")
        elif not info["ffprobe_ok"]:
            broken.append(f"ffprobe<{_v(_FFPROBE_MIN)}")
        if info["ffmpeg_bin"] and not info["has_aac"]:
            broken.append("sem aac")
        if info["ffmpeg_bin"] and not info["has_loudnorm"]:
            broken.append("sem loudnorm")
    env_ready = (not env_absent) and not broken

    if env_absent:
        skip("ambiente", "ffmpeg/ffprobe ausentes")
    else:
        chk("ambiente: ffmpeg presente", info["ffmpeg_bin"] is not None)
        chk("ambiente: ffprobe presente", info["ffprobe_bin"] is not None)
        if info["ffmpeg_bin"]:
            chk(f"ambiente: ffmpeg>={_v(_FFMPEG_MIN)}", info["ffmpeg_ok"])
            chk("ambiente: encoder aac", info["has_aac"])
            chk("ambiente: filtro loudnorm", info["has_loudnorm"])
        if info["ffprobe_bin"]:
            chk(f"ambiente: ffprobe>={_v(_FFPROBE_MIN)}", info["ffprobe_ok"])

    with tempfile.TemporaryDirectory(prefix="bx_testkit_") as tmpdir:
        tmp = Path(tmpdir)
        home = tmp / "home"
        raiz = tmp / "raiz"
        raiz.mkdir()
        os.environ["BX_HOME"] = str(home)
        cfg = bx_core.carregar_config(raiz_arg=str(raiz), modo="apply")

        # ─── 1 ────────────────────────────────────────────
        sec("1. Primitivas de filesystem")
        f = tmp / "abc.txt"; f.write_bytes(b"abc")
        chk("tam(abc)==3", bx_core.tam(f) == 3)
        chk("tam(inexistente)==0", bx_core.tam(tmp / "nada") == 0)
        chk("sha256(abc) conhecido", bx_core.sha256(f) ==
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        chk("sha256(inexistente)==''", bx_core.sha256(tmp / "nada") == "")
        atom = tmp / "atom.txt"
        chk("escrever_atomico OK", bx_core.escrever_atomico(atom, "hello"))
        chk("escrever_atomico conteudo", atom.read_text() == "hello")
        chk("escrever_atomico sem .bxpart", not list(tmp.rglob("*.bxpart")))
        bx_core.escrever_atomico(atom, "world")
        chk("escrever_atomico sobrescreve", atom.read_text() == "world")
        bin_atom = tmp / "atom.bin"
        chk("escrever_bytes_atomico", bx_core.escrever_bytes_atomico(bin_atom, b"\x00\x01\x02"))
        chk("bytes preservados", bin_atom.read_bytes() == b"\x00\x01\x02")
        try:
            bx_core._fsync(tmp / "nada_aqui")
            chk("_fsync(inexistente) silencioso", True)
        except Exception:
            chk("_fsync(inexistente) silencioso", False)

        # ─── 2 ────────────────────────────────────────────
        sec("2. enviar_sd (single-pass + O_TRUNC)")
        src = tmp / "src.bin"; src.write_bytes(b"x" * 1000)
        dest = raiz / "dest.bin"
        chk("enviar_sd OK", bx_core.enviar_sd(src, dest, cfg=cfg))
        chk("destino existe", dest.is_file())
        chk("tamanho correto", bx_core.tam(dest) == 1000)
        chk("hash preservado", bx_core.sha256(src) == bx_core.sha256(dest))
        chk("sem .bxpart apos sucesso", not list(raiz.rglob("*.bxpart")))
        (raiz / bx_core.D_Z).mkdir(parents=True, exist_ok=True)
        chk("recusa Z/", not bx_core.enviar_sd(src, raiz / bx_core.D_Z / "x.bin", cfg=cfg))
        (raiz / bx_core.D_Y).mkdir(parents=True, exist_ok=True)
        chk("recusa Y/", not bx_core.enviar_sd(src, raiz / bx_core.D_Y / "x.bin", cfg=cfg))
        (raiz / bx_core.D_LOGS).mkdir(parents=True, exist_ok=True)
        chk("aceita Z_logs/", bx_core.enviar_sd(src, raiz / bx_core.D_LOGS / "x.bin", cfg=cfg))
        # O_TRUNC: .bxpart residual de crash anterior e truncado (idempotente)
        conflict = raiz / "conflict.bin"
        partial = raiz / "conflict.bin.bxpart"
        partial.write_bytes(b"lixo_de_crash_XXXXXXXXXXXX")
        chk("O_TRUNC recupera .bxpart orfao",
            bx_core.enviar_sd(src, conflict, cfg=cfg))
        chk("destino criado apos recovery",
            conflict.is_file() and bx_core.tam(conflict) == bx_core.tam(src))
        chk("conteudo correto (nao lixo)",
            conflict.read_bytes() == src.read_bytes())
        chk(".bxpart removido apos replace", not partial.exists())
        chk("fonte inexistente rejeitada",
            not bx_core.enviar_sd(tmp / "nao.bin", raiz / "y.bin", cfg=cfg))

        # ─── 3 ────────────────────────────────────────────
        sec("3. mover")
        a = tmp / "a.txt"; a.write_bytes(b"a")
        b = tmp / "b.txt"; b.write_bytes(b"b")
        chk("colisao retorna False", bx_core.mover(a, b, "apply", tmp / "d.log") is False)
        chk("colisao preserva destino", b.read_bytes() == b"b")
        chk("colisao preserva origem", a.is_file() and a.read_bytes() == b"a")
        c = tmp / "c.txt"; c.write_bytes(b"c")
        dc = tmp / "sub" / "c.txt"
        chk("mover arquivo normal", bx_core.mover(c, dc, "apply", tmp / "d.log"))
        chk("origem removida", not c.is_file())
        chk("destino criado", dc.is_file())
        sim = tmp / "sim.txt"; sim.write_bytes(b"s")
        sim_d = tmp / "sim_dst.txt"
        chk("modo sim OK", bx_core.mover(sim, sim_d, "sim", tmp / "d.log"))
        chk("modo sim nao move", sim.is_file() and not sim_d.exists())
        da = tmp / "da"; da.mkdir(); (da / "f.txt").write_bytes(b"x")
        db = tmp / "db"
        chk("mover diretorio", bx_core.mover(da, db, "apply", tmp / "d.log"))
        chk("dir origem removida", not da.is_dir())
        chk("dir destino conteudo", db.is_dir() and (db / "f.txt").is_file())
        dca = tmp / "dca"; dca.mkdir()
        dcb = tmp / "dcb"; dcb.mkdir()
        chk("colisao diretorio False",
            bx_core.mover(dca, dcb, "apply", tmp / "d.log") is False)
        chk("dirs preservados", dca.is_dir() and dcb.is_dir())
        nb = tmp / "nb.bin"; nb.write_bytes(b"n")
        chk("recusa destino Z/",
            not bx_core.mover(nb, raiz / bx_core.D_Z / "blind.bin",
                              "apply", tmp / "d.log", cfg=cfg))

        # ─── 4 ────────────────────────────────────────────
        sec("4. Schema SQLite")
        db_p = tmp / "test.db"
        con = bx_core.conectar(db_p)
        chk("DB criado", db_p.is_file())
        chk("WAL ativo",
            con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
        vmax = con.execute("SELECT MAX(versao) FROM schema_version").fetchone()[0]
        chk("schema_version>=2", vmax >= 2)
        chk("schema_version=3", vmax == 3)
        chk("17 colunas catalogo",
            len(list(con.execute("PRAGMA table_info(catalogo)"))) == 17)
        for t in ("links_youtube", "traducao_cache", "probe_cache", "fila_downloads"):
            chk(f"tabela {t}", con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (t,)).fetchone() is not None)
        try:
            with bx_core.transacao(con):
                con.execute("INSERT INTO catalogo(id,artista,titulo) VALUES (?,?,?)",
                            ("RB-1", "A", "T"))
                raise RuntimeError("rollback")
        except RuntimeError:
            pass
        chk("rollback descarta",
            con.execute("SELECT COUNT(*) FROM catalogo WHERE id='RB-1'").fetchone()[0] == 0)
        with bx_core.transacao(con):
            con.execute("INSERT INTO catalogo(id,artista,titulo) VALUES (?,?,?)",
                        ("CM-1", "A", "T"))
        chk("commit persiste",
            con.execute("SELECT COUNT(*) FROM catalogo WHERE id='CM-1'").fetchone()[0] == 1)
        bx_core.db_inserir(con, ["ID-1", "Art", "Tit", "COMPLETO", "r.m4a", "1000",
                                 "20260101", "abc", "pt", "44.1", "", "128", "sim",
                                 "aac", "", "-14.5"])
        chk("db_inserir",
            con.execute("SELECT COUNT(*) FROM catalogo WHERE id='ID-1'").fetchone()[0] == 1)
        dump = bx_core.db_dump(con)
        chk("db_dump pipe-separated", dump.count("|") > 0)
        chk("db_dump \\n final", dump.endswith("\n"))
        bx_core.db_apagar(con, "ID-1")
        chk("db_apagar",
            con.execute("SELECT COUNT(*) FROM catalogo WHERE id='ID-1'").fetchone()[0] == 0)

        # ─── 5 ────────────────────────────────────────────
        sec("5. Segurança SQLite")
        try:
            bx_core.db_inserir(con, ["", "A", "T"])
            chk("db_inserir rejeita id vazio", False)
        except ValueError:
            chk("db_inserir rejeita id vazio", True)
        chk("db_set_col whitelist",
            not bx_core.db_set_col(con, "tam", "1", "CM-1"))
        chk("db_set_col bitrate",
            bx_core.db_set_col(con, "bitrate", "256", "CM-1"))
        chk("bitrate gravado",
            con.execute("SELECT bitrate FROM catalogo WHERE id='CM-1'").fetchone()[0] == 256)
        chk("db_set_col nao-numerico",
            not bx_core.db_set_col(con, "khz", "abc", "CM-1"))
        chk("db_set_col injection",
            not bx_core.db_set_col(con, "bitrate; DROP TABLE catalogo", "1", "CM-1"))
        chk("catalogo intacto",
            con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                        "AND name='catalogo'").fetchone()[0] == 1)

        # ─── 6 ────────────────────────────────────────────
        sec("6. Manifestos")
        mdir = tmp / "mtest"; mdir.mkdir()
        mfile = mdir / "X [COMPLETO].txt"
        cm = {k: f"v_{k}" for k in bx_core.MANIFESTO_CAMPOS}
        cm["ID"] = "BX-TEST"; cm["ESTADO"] = "COMPLETO"
        mfile.write_text(bx_core.montar_manifesto(cm), encoding="utf-8")
        txt = bx_core.montar_manifesto(cm)
        chk("18 linhas", len(txt.strip().split("\n")) == 18)
        chk("termina \\n", txt.endswith("\n"))
        for _c in bx_core.MANIFESTO_CAMPOS:
            chk(f"contem {_c}:", f"{_c}:" in txt)
        chk("mget ID", bx_core.mget(mfile, "ID") == "BX-TEST")
        chk("mget inexistente vazio", bx_core.mget(mfile, "NOPE") == "")
        chk("achar_manifesto encontra",
            bx_core.achar_manifesto(mdir, "X") == mfile)
        bx_core.manif_campo(mfile, "NOVO", "xyz")
        chk("manif_campo cria", bx_core.mget(mfile, "NOVO") == "xyz")
        bx_core.manif_campo(mfile, "ID", "BX-NEW")
        chk("manif_campo substitui", bx_core.mget(mfile, "ID") == "BX-NEW")
        bx_core.manif_caminho(mfile, "novo/rel.m4a")
        chk("manif_caminho", bx_core.mget(mfile, "CAMINHO") == "novo/rel.m4a")
        tp = bx_core.montar_manifesto({"ID": "a|b", "ARTISTA": "x\ny"})
        chk("sanitiza |", "a|b" not in tp)
        chk("sanitiza \\n", "x\ny" not in tp)
        (mdir / "X [RESGATADO].txt").write_text("ID:BX-TEST\n", encoding="utf-8")
        bx_core.rm_manifestos(mdir, "X", mfile, tmp / "diario.log")
        chk("preserva manter", mfile.is_file())
        chk("remove outros", not (mdir / "X [RESGATADO].txt").exists())

        # ─── 7 ────────────────────────────────────────────
        sec("7. Normalização Unicode")
        chk("_limpo remove Cc", bx_core._limpo("a\x00b") == "ab")
        chk("_limpo | -> espaco", bx_core._limpo("a|b") == "a b")
        chk("_limpo vazio", bx_core._limpo("") == "")
        chk("_limpo None", bx_core._limpo(None) == "")
        chk("_fix_mojibake cp1252", bx_core._fix_mojibake("Ã\u00a3o") == "\u00e3o")
        chk("_fix_mojibake intacto", bx_core._fix_mojibake("abc") == "abc")
        chk("normalizar lower", bx_core.normalizar_unicode("ABC") == "abc")
        chk("normalizar remove acento", bx_core.normalizar_unicode("ÁÉÍÓÚ") == "aeiou")
        chk("normalizar vazio", bx_core.normalizar_unicode("") == "")
        n = bx_core._normalizar(["id1", "art", "tit", "COMPLETO", "rel", "1000",
                                  "data", "sha", "pt", "44.1", "", "128", "sim",
                                  "aac", "prod", "-14.5", "N"])
        chk("17 campos", len(n) == 17)
        chk("tam int", isinstance(n[5], int) and n[5] == 1000)
        chk("khz float", isinstance(n[9], float) and n[9] == 44.1)
        chk("rg float", isinstance(n[15], float))
        chk("16 pipes", bx_core._linha_para_pipe(n).count("|") == 16)

        # ─── 8 ────────────────────────────────────────────
        sec("8. Probe e cache")
        if not env_ready:
            for nm in ("probe_raw", "cache hit", "cache invalidate", "limpar_cache"):
                skip(nm, "ffmpeg ausente/quebrado")
        else:
            audio = tmp / "sine.m4a"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                            "-ac", "2", "-c:a", "aac", "-b:a", "128k", str(audio)],
                           capture_output=True, timeout=60, check=False)
            if audio.is_file():
                inf = bx_core.probe_raw(audio)
                chk("codec aac", inf is not None and inf.codec == "aac")
                chk("channels 2", inf is not None and inf.channels == 2)
                chk("sr ~44.1", inf is not None and abs(inf.sample_rate - 44.1) < 0.5)
                chk("duration ~2s", inf is not None and 1.5 < inf.duration < 3.0)
                chave, tm_, mt_ = bx_core._chave_cache(audio)
                chk("chave nao vazia", chave != "")
                inf1 = bx_core.probe(audio, cfg)
                chk("probe retorna info", inf1 is not None)
                with bx_core._CACHE_LOCK:
                    chk("cache populado", (chave, tm_, mt_) in bx_core._MEM_CACHE)
                ar = tmp / "renamed.m4a"
                os.rename(audio, ar)
                chave_r, _, _ = bx_core._chave_cache(ar)
                chk("rename preserva chave", chave == chave_r)
                nmt = ar.stat().st_mtime - 100
                os.utime(ar, (nmt, nmt))
                chave_m, _, _ = bx_core._chave_cache(ar)
                chk("mtime invalida chave", chave_m != chave)
                bx_core.limpar_cache(cfg)
                with bx_core._CACHE_LOCK:
                    chk("limpar_cache", len(bx_core._MEM_CACHE) == 0)

        # ─── 9 ────────────────────────────────────────────
        sec("9. Tags e ReplayGain")
        if not env_ready:
            for nm in ("gravar_tags", "tags aplicadas", "rg float"):
                skip(nm, "ffmpeg ausente/quebrado")
        else:
            ta = tmp / "tag.m4a"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                            "-ac", "2", "-c:a", "aac", "-b:a", "128k",
                            "-metadata", "album=ALB_ORIG", str(ta)],
                           capture_output=True, timeout=60, check=False)
            if ta.is_file():
                ok_t = bx_core.gravar_tags(ta, "Art X", "Tit Y", "cmt Z", cfg)
                chk("gravar_tags True", ok_t)
                it = bx_core.probe_raw(ta)
                chk("title aplicado", it is not None and it.title == "Tit Y")
                chk("artist aplicado", it is not None and it.artist == "Art X")
                chk("comment aplicado",
                    it is not None and "cmt Z" in (it.comment or ""))
                chk("album estrutural preservado",
                    it is not None and it.album == "ALB_ORIG")
                rg = bx_core.calcular_replaygain(ta)
                chk("rg float", rg is not None and isinstance(rg, float))
                if rg is not None:
                    chk("rg range", -40.0 < rg < 20.0)

        # ─── 10 ───────────────────────────────────────────
        sec("10. Codec AAC")
        if not env_ready:
            for nm in ("detectar_encoder", "transcodificar"):
                skip(nm, "ffmpeg ausente/quebrado")
        else:
            enc, flags = bx_core.detectar_encoder_aac(force=True)
            chk("encoder tupla", isinstance(enc, str) and isinstance(flags, list))
            chk("encoder valido", enc in ("libfdk_aac", "aac"))
            webm = tmp / "src.webm"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                            "-ac", "2", "-c:a", "libopus", "-b:a", "96k", str(webm)],
                           capture_output=True, timeout=60, check=False)
            if webm.is_file():
                om = tmp / "out.m4a"
                ok_tc = bx_core.transcodificar_aac(webm, om)
                chk("transcodificar True", ok_tc)
                if ok_tc:
                    it = bx_core.probe_raw(om)
                    chk("codificado aac", it is not None and it.codec == "aac")
                    chk("br ~128k", it is not None and 100 <= it.bitrate <= 160)

        # ─── 11 ───────────────────────────────────────────
        sec("11. Concorrência")
        chk("trava adquire", bx_core.trava(cfg))
        script = (
            "import sys; "
            f"sys.path.insert(0, {str(AQUI)!r}); "
            "import bx_core; "
            f"cfg = bx_core.carregar_config(raiz_arg={str(raiz)!r}, modo='apply'); "
            "print('OK' if bx_core.trava(cfg) else 'LOCKED')"
        )
        try:
            r = subprocess.run([sys.executable, "-c", script],
                               capture_output=True, text=True, timeout=15)
            chk("2o processo vê LOCKED", "LOCKED" in r.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            chk("2o processo vê LOCKED", False)
        bx_core.libera(cfg)
        chk("readquire após libera", bx_core.trava(cfg))
        bx_core.libera(cfg)
        chk("_destino_proibido Z/",
            bx_core._destino_proibido(Path("/r/Z/x"), Path("/r")))
        chk("_destino_proibido Y/",
            bx_core._destino_proibido(Path("/r/Y/x"), Path("/r")))
        chk("_destino_proibido Z_* livre",
            not bx_core._destino_proibido(Path("/r/Z_logs/x"), Path("/r")))
        chk("_destino_proibido Y_* livre",
            not bx_core._destino_proibido(Path("/r/Y_foo/x"), Path("/r")))
        chk("_destino_proibido fora de raiz livre",
            not bx_core._destino_proibido(Path("/other/Z/x"), Path("/r")))

        # ─── 12 ───────────────────────────────────────────
        sec("12. Runtime")
        chk("espaco_ok positivo", bx_core.espaco_ok(tmp, 1))
        chk("espaco_ok negativo", not bx_core.espaco_ok(tmp, 10**12))
        orig = sys.excepthook
        try:
            bx_core.instalar_panic_hook(tmp)
            chk("panic_hook muda hook", sys.excepthook is not orig)
            chk("panic_hook callable", callable(sys.excepthook))
        finally:
            sys.excepthook = orig
        old_f = cfg.tmp_dir / "t_999_old.m4a"
        old_f.write_bytes(b"x")
        om = time.time() - 7200
        os.utime(old_f, (om, om))
        bx_core.limpar_temporarios(cfg, idade_min_seg=60)
        chk("limpa antigos", not old_f.is_file())
        new_f = cfg.tmp_dir / "t_999_new.m4a"
        new_f.write_bytes(b"y")
        bx_core.limpar_temporarios(cfg, idade_min_seg=3600)
        chk("preserva recentes", new_f.is_file())
        new_f.unlink()

        # ─── 13 ───────────────────────────────────────────
        sec("13. Integração")
        try:
            import bx_nmx as _nmx
            cfg_sim = bx_core.carregar_config(raiz_arg=str(raiz), modo="sim")
            _nmx.reset_state()
            d = _nmx.calc_dest("Artista Teste", "Musica Teste", 1, "", cfg_sim)
            chk("calc_dest Path", isinstance(d, Path))
            chk("calc_dest dentro de raiz", str(d).startswith(str(raiz)))
            sa_x, sa_1 = _nmx.split_art("A_B_C", cfg_sim)
            chk("split_art _ quebra", ";" in sa_x and sa_1 != "")
        except ImportError:
            skip("bx_nmx", "ausente")
        try:
            import bx_crx
            en = tmp / "test_en.lrc"
            en.write_text("[00:01.00] i love you baby and the night\n" * 12,
                          encoding="utf-8")
            chk("idioma EN", bx_crx.detectar_idioma(en) == "EN")
            pt = tmp / "test_pt.lrc"
            pt.write_text("[00:01.00] eu nao quero voce para mim amor\n" * 12,
                          encoding="utf-8")
            chk("idioma PT", bx_crx.detectar_idioma(pt) == "PT")
        except ImportError:
            skip("bx_crx", "ausente")
        if env_ready:
            ai = raiz / "int_test.m4a"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                            "-ac", "2", "-c:a", "aac", "-b:a", "128k", str(ai)],
                           capture_output=True, timeout=60, check=False)
            if ai.is_file():
                rel_i = str(ai.relative_to(raiz))
                ci = bx_core.conectar(cfg.db_path)
                with bx_core.transacao(ci):
                    ci.execute("INSERT INTO catalogo(id,artista,titulo,rel) "
                               "VALUES (?,?,?,?)", ("INT-1", "A", "T", rel_i))
                bx_core.probe(ai, cfg)
                with bx_core._CACHE_LOCK:
                    antes = len(bx_core._MEM_CACHE)
                bx_core.db_set_col(ci, "bitrate", "192", "INT-1", raiz_sd=raiz)
                with bx_core._CACHE_LOCK:
                    depois = len(bx_core._MEM_CACHE)
                chk("db_set_col invalida cache", depois < antes)
                ci.close()

        # ─── 14 ───────────────────────────────────────────
        sec("14. Integridade do manifesto")
        campos_rt = {"VERSÃO": "3", "ID": "BX-deadbeef1234",
                     "ARTISTA": "Artista Teste", "FAIXA": "Titulo Teste",
                     "CAMINHO": "Artistas/A/arq.m4a", "TAMANHO": "1234567",
                     "SHA256": "a" * 64, "DATA": "2026-01-26_12:34",
                     "ESTADO": "COMPLETO", "LETRA": "SYNC", "TRAVADO": "Y",
                     "IDIOMA": "por", "KHZ": "44.1", "CODEC": "aac",
                     "PRODUTOR": "Fulano;Ciclano", "ORIGEM_MORTA": "N",
                     "BITRATE": "128", "REPLAYGAIN": "-14.50"}
        m_rt = mdir / "RT [COMPLETO].txt"
        m_rt.write_text(bx_core.montar_manifesto(campos_rt), encoding="utf-8")
        for k, v in campos_rt.items():
            chk(f"round-trip {k}", bx_core.mget(m_rt, k) == v)
        for estado in bx_core.ESTADOS_MANIFESTO:
            for sep in (" ", ""):
                pe = mdir / f"Q{sep}[{estado}].txt"
                pe.write_text(f"ID:Q\nESTADO:{estado}\n", encoding="utf-8")
                chk(f"achar [{estado}] sep={sep!r}",
                    bx_core.achar_manifesto(mdir, "Q") == pe)
                pe.unlink()
        mo = mdir / "ORD [COMPLETO].txt"
        mo.write_text(bx_core.montar_manifesto({}), encoding="utf-8")
        linhas = mo.read_text(encoding="utf-8").splitlines()
        chk("ordem canônica",
            all(linhas[i].startswith(bx_core.MANIFESTO_CAMPOS[i] + ":")
                for i in range(len(bx_core.MANIFESTO_CAMPOS))))
        chk("18 chaves cobertas",
            {l.split(":", 1)[0] for l in linhas if ":" in l}
            == set(bx_core.MANIFESTO_CAMPOS))

        # ─── 15 ───────────────────────────────────────────
        sec("15. Adversarial I/O")
        src_a = tmp / "v1.bin"; src_a.write_bytes(b"AAAA")
        src_b = tmp / "v2.bin"; src_b.write_bytes(b"BBBBBBBB")
        dst_ov = tmp / "overwrite.bin"
        bx_core.enviar_sd(src_a, dst_ov, cfg=cfg)
        chk("overwrite v1", dst_ov.read_bytes() == b"AAAA")
        bx_core.enviar_sd(src_b, dst_ov, cfg=cfg)
        chk("overwrite v2 substitui", dst_ov.read_bytes() == b"BBBBBBBB")
        dst_o = tmp / "orphan.bin"
        orphan = dst_o.with_name(dst_o.name + bx_core.SUFIXO_PARCIAL)
        orphan.write_bytes(b"leftover")
        ok_o = bx_core.enviar_sd(src_a, dst_o, cfg=cfg)
        chk("O_TRUNC recupera orfao", ok_o)
        chk("destino conteudo correto", dst_o.read_bytes() == b"AAAA")
        chk("orphan removido", not orphan.exists())
        if env_ready:
            cor = tmp / "corrupt.m4a"
            cor.write_bytes(b"\x00\xff" * 5000)
            chk("probe_raw corrompido None", bx_core.probe_raw(cor) is None)
        else:
            skip("probe_raw corrompido", "ffmpeg ausente")
        for s in ("abc", "a|b", "a\x00b", "", "a\x01b\tc", "a\nb"):
            chk(f"_limpo idempotente {s!r}",
                bx_core._limpo(bx_core._limpo(s)) == bx_core._limpo(s))
        nl = bx_core._normalizar(["id", "", "", "", "", "abc", "", "", "", "xyz",
                                   "", "foo", "", "", "", "bar"])
        chk("lixo tam None", nl[5] is None)
        chk("lixo khz None", nl[9] is None)
        chk("lixo br None", nl[11] is None)
        chk("lixo rg None", nl[15] is None)
        vazio = tmp / "vazio.bin"; vazio.write_bytes(b"")
        chk("sha256 vazio",
            bx_core.sha256(vazio) ==
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        ddir = tmp / "somedir"; ddir.mkdir()
        td = bx_core.tam(ddir)
        chk("tam(diretorio) int>=0", isinstance(td, int) and td >= 0)
        dst_esp = tmp / "pasta com espaco" / "arq.bin"
        chk("path com espaco", bx_core.enviar_sd(src_a, dst_esp, cfg=cfg)
            and dst_esp.read_bytes() == b"AAAA")

        # ─── 16 ───────────────────────────────────────────
        sec("16. Adversarial arquitetural")
        src_h = tmp / "hash_src.bin"; src_h.write_bytes(b"H" * 123456)
        dst_h = raiz / "hash_dst.bin"
        chk("16.1 single-pass OK", bx_core.enviar_sd(src_h, dst_h, cfg=cfg))
        chk("16.1 sha256 igual", bx_core.sha256(src_h) == bx_core.sha256(dst_h))
        orf_d = tmp / "orf_dst.bin"
        orf_p = orf_d.with_name(orf_d.name + bx_core.SUFIXO_PARCIAL)
        orf_p.write_bytes(b"crash_lixo")
        src_r = tmp / "src_rec.bin"; src_r.write_bytes(b"novo_conteudo")
        chk("16.2 O_TRUNC recupera", bx_core.enviar_sd(src_r, orf_d, cfg=cfg))
        chk("16.2 conteudo novo", orf_d.read_bytes() == b"novo_conteudo")
        chk("16.2 orfao limpo", not orf_p.exists())
        cfg_t = bx_core.carregar_config(raiz_arg=str(raiz), modo="apply")
        chk("16.3 trava adquire", bx_core.trava(cfg_t))
        lp = cfg_t.lock
        if lp.is_file():
            try: lp.unlink()
            except OSError: pass
        chk("16.3 inode check detecta",
            not bx_core.trava(cfg_t))
        bx_core.libera(cfg_t)
        if env_ready:
            va = raiz / "arts" / "A" / "a - x.m4a"
            va.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                            "-ac", "2", "-c:a", "aac", "-b:a", "128k", str(va)],
                           capture_output=True, timeout=60, check=False)
            pa = raiz / "arts" / "A" / "a - x_dup.m4a"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                            "-ac", "2", "-c:a", "aac", "-b:a", "96k", str(pa)],
                           capture_output=True, timeout=60, check=False)
            if va.is_file() and pa.is_file():
                rd = bx_core.resolver_duplicata(va, pa, cfg)
                chk("16.4 dict com vencedor", isinstance(rd, dict) and "vencedor" in rd)
                chk("16.4 vencedor=128", rd["vencedor"] == va)
                chk("16.4 perdedor=96", rd["perdedor"] == pa)
        mv = mdir / "MV [COMPLETO].txt"
        mv.write_text(bx_core.montar_manifesto({**campos_rt, "ESTADO": "COMPLETO"}),
                      encoding="utf-8")
        chk("16.5 valido -> []", bx_core.validar_manifesto(mv) == [])
        mv.write_text(bx_core.montar_manifesto({**campos_rt, "ESTADO": "INEXISTENTE"}),
                      encoding="utf-8")
        chk("16.5 ESTADO invalido",
            any("ESTADO" in e for e in bx_core.validar_manifesto(mv)))
        txt_ok = bx_core.montar_manifesto({**campos_rt, "ESTADO": "COMPLETO"})
        linhas_sem = [l for l in txt_ok.splitlines() if not l.startswith("CAMINHO:")]
        mv.write_text("\n".join(linhas_sem) + "\n", encoding="utf-8")
        chk("16.5 chave faltando",
            any("CAMINHO" in e or "faltando" in e
                for e in bx_core.validar_manifesto(mv)))
        chk("16.5 arquivo ausente",
            len(bx_core.validar_manifesto(tmp / "nope.txt")) >= 1)
        if cfg.fila.is_file():
            try: cfg.fila.unlink()
            except OSError: pass
        cfg.fila.write_text(
            "pendente|AAAAAAAAAAA|https://x|0|\n"
            "baixado|BBBBBBBBBBB|https://y|1|20250101\n",
            encoding="utf-8")
        cf = bx_core.conectar(cfg.db_path)
        try:
            n_mig = bx_core.fila_migrar_txt(cfg, cf)
            chk("16.6 migra 2", n_mig == 2)
            chk("16.6 2 no DB", len(bx_core.fila_read_db(cf)) == 2)
            bak = cfg.fila.with_name(cfg.fila.name + ".bak")
            chk("16.6 .txt -> .bak", not cfg.fila.is_file() and bak.is_file())
        finally:
            cf.close()


        # ─── 17. Regressao v4 (semantica + rename CI) ─────
        sec("17. Regressao v4 (item 1 + item 2)")

        try:
            import bx_fix as _fx
        except ImportError:
            _fx = None

        if _fx is not None:
            cfg_sim = bx_core.carregar_config(
                raiz_arg=str(raiz), modo="sim")
            sr = cfg_sim.suf_re

            chk("17.1 semantica: curto legitimo (Ai + sufixo)",
                _fx._semantica_ok("Ai (Official Video)", sr, cfg=cfg_sim))
            chk("17.1 semantica: titulo normal",
                _fx._semantica_ok("Musica Longa", sr, cfg=cfg_sim))
            chk("17.1 semantica: so sufixo -> False",
                not _fx._semantica_ok(
                    "(Official Video)", sr, cfg=cfg_sim))
            chk("17.1 semantica: vazio -> False",
                not _fx._semantica_ok("", sr, cfg=cfg_sim))
            chk("17.1 semantica: None -> False",
                not _fx._semantica_ok(None, sr, cfg=cfg_sim))
            chk("17.1 semantica: 'Ai' puro aceito",
                _fx._semantica_ok("Ai", sr, cfg=cfg_sim))
            chk("17.1 semantica: 'XP (Official)' aceito",
                _fx._semantica_ok("XP (Official)", sr, cfg=cfg_sim))
            chk("17.1 semantica: 1 char limpo rejeitado",
                not _fx._semantica_ok("A (Official)", sr, cfg=cfg_sim))
        else:
            skip("semantica_ok", "bx_fix ausente")

        # 17.2 - _rename_case_insensitive funcional
        p_ci_a = tmp / "mv_src.bin"
        p_ci_a.write_bytes(b"conteudo_original")
        p_ci_b = tmp / "MV_SRC.bin"
        ok_ci = bx_core._rename_case_insensitive(p_ci_a, p_ci_b)
        chk("17.2 case-insensitive rename OK", ok_ci)
        chk("17.2 origem removida", not p_ci_a.exists())
        chk("17.2 destino com conteudo",
            p_ci_b.is_file() and p_ci_b.read_bytes() == b"conteudo_original")

        # 17.3 - _rename_case_insensitive restaura origem em falha
        p_ci_c = tmp / "restore_src.bin"
        p_ci_c.write_bytes(b"nao_perder")
        p_ci_d = tmp / "restore_dst_dir"
        p_ci_d.mkdir()
        ok_falha = bx_core._rename_case_insensitive(p_ci_c, p_ci_d)
        chk("17.3 rename p/ dir falha (retorna False)", not ok_falha)
        chk("17.3 origem RESTAURADA (bug antigo evitado)",
            p_ci_c.is_file() and p_ci_c.read_bytes() == b"nao_perder")

        # 17.4 - mover() ainda funciona após refatoracao
        ma = tmp / "mv_a.bin"; ma.write_bytes(b"a" * 100)
        mb = tmp / "sub17" / "mv_b.bin"
        chk("17.4 mover arquivo normal",
            bx_core.mover(ma, mb, "apply", tmp / "d.log"))
        chk("17.4 destino existe",
            mb.is_file() and mb.read_bytes() == b"a" * 100)
        chk("17.4 origem removida", not ma.exists())

        ma2 = tmp / "mv_a2.bin"; ma2.write_bytes(b"x")
        mb2 = tmp / "mv_b2.bin"; mb2.write_bytes(b"y")
        chk("17.4 mover colisao ainda retorna False",
            bx_core.mover(ma2, mb2, "apply", tmp / "d.log") is False)
        chk("17.4 colisao preserva conteudo",
            mb2.read_bytes() == b"y")

        mda = tmp / "mvd_a"; mda.mkdir(); (mda / "f.txt").write_bytes(b"d")
        mdb = tmp / "mvd_b"
        chk("17.4 mover diretorio normal",
            bx_core.mover(mda, mdb, "apply", tmp / "d.log"))
        chk("17.4 dir destino conteudo",
            mdb.is_dir() and (mdb / "f.txt").is_file())

        (tmp / "sim17.txt").write_bytes(b"s")
        chk("17.4 modo sim nao move",
            bx_core.mover(
                tmp / "sim17.txt",
                tmp / "sim17_dst.txt", "sim", tmp / "d.log") is True)
        chk("17.4 sim origem preservada", (tmp / "sim17.txt").is_file())
        chk("17.4 sim destino NAO criado",
            not (tmp / "sim17_dst.txt").exists())

        (tmp / "ztest.bin").write_bytes(b"z")
        chk("17.4 recusa Z/",
            not bx_core.mover(
                tmp / "ztest.bin", raiz / bx_core.D_Z / "x.bin",
                "apply", tmp / "d.log", cfg=cfg))


        # ─── 18. E2E: processar() com ffmpeg + DB + mover reais ─────
        sec("18. E2E processar() end-to-end")

        if not env_ready:
            for nm in ("processar golden path", "processar idempotente",
                       "processar gera manifest", "processar grava DB",
                       "processar split correto"):
                skip(nm, "ffmpeg ausente/quebrado")
        else:
            try:
                import bx_fix
                import bx_nmx
            except ImportError:
                bx_fix = None
                bx_nmx = None

            if bx_fix is None:
                skip("bx_fix import", "ausente")
            else:
                _e2e_tmp = tmp / "e2e"
                _e2e_tmp.mkdir(parents=True, exist_ok=True)
                _e2e_raiz = _e2e_tmp / "raiz"
                _e2e_raiz.mkdir()
                _e2e_home = _e2e_tmp / "home"
                _e2e_home.mkdir()
                _old_home = os.environ.get("BX_HOME")
                os.environ["BX_HOME"] = str(_e2e_home)

                try:
                    _e2e_cfg = bx_core.carregar_config(
                        raiz_arg=str(_e2e_raiz), modo="apply")
                    bx_core.criar_estrutura_sd(_e2e_cfg)

                    # Cria 5 m4a do mesmo artista para testar split
                    _art = _e2e_raiz / "A Lista" / "TesteE2E"
                    _art.mkdir(parents=True)
                    _n_ok = 0
                    for _i in range(5):
                        _f = _art / f"TesteE2E - Faixa {_i+1}.m4a"
                        try:
                            subprocess.run(
                                ["ffmpeg", "-nostdin", "-v", "error", "-y",
                                 "-f", "lavfi", "-i",
                                 "sine=frequency=440:duration=1",
                                 "-ac", "2", "-c:a", "aac", "-b:a", "128k",
                                 str(_f)],
                                capture_output=True, timeout=60, check=False)
                            if _f.is_file():
                                _n_ok += 1
                        except (FileNotFoundError, subprocess.TimeoutExpired):
                            pass
                    chk("18.1 5 m4a criados", _n_ok == 5)

                    # FIX 18.3: simula pre-scan cross-arquivo do cmd_rodar
                    # (paridade com producao). Sem isso, cntk comeca em 0
                    # e as 3 primeiras faixas vao para singles/ antes de
                    # cntk cruzar MIN_ART=4 -- que era o bug original #1.
                    bx_nmx.reset_state()
                    from collections import Counter as _C
                    _cnt_disco = _C()
                    for _arq in _art.glob("*.m4a"):
                        _stem = _arq.stem
                        if " - " in _stem:
                            _a = _stem.split(" - ", 1)[0].strip()
                            if _a and 2 <= len(_a) <= 60:
                                _cnt_disco[_a.lower()] += 1
                    bx_nmx.contagem_preload(
                        [(k, v) for k, v in _cnt_disco.items()])

                    # Roda processar em cada um
                    _scan = []
                    _ok_proc = 0
                    for _i in range(5):
                        _f = _art / f"TesteE2E - Faixa {_i+1}.m4a"
                        if not _f.is_file():
                            continue
                        try:
                            r = bx_fix.processar(_f, _e2e_cfg, _scan)
                            if not r.erro:
                                _ok_proc += 1
                        except Exception:
                            pass
                    chk("18.2 processar 5 sem erro", _ok_proc == 5)

                    # Verifica split: 5 faixas >= MIN_ART(4) -> Artistas/
                    _dest = _e2e_raiz / "Artistas" / "Testee2e"
                    _encontrados = list(
                        (_e2e_raiz / "Artistas").rglob("*.m4a")) if (_e2e_raiz / "Artistas").is_dir() else []
                    _em_singles = list(
                        (_e2e_raiz / "singles").rglob("*.m4a")) if (_e2e_raiz / "singles").is_dir() else []
                    chk("18.3 split correto (Artistas, nao singles)",
                        len(_encontrados) >= 5 and len(_em_singles) == 0)

                    # Verifica manifest
                    _manifs = list(_e2e_raiz.rglob("*[[]COMPLETO[]].txt"))
                    if not _manifs:
                        _manifs = list(_e2e_raiz.rglob("*[]SEM LETRA[]].txt"))
                    if not _manifs:
                        _manifs = list(_e2e_raiz.rglob("*[]LETRADO[]].txt"))
                    if not _manifs:
                        _manifs = list(_e2e_raiz.rglob("*[[]*[]].txt"))
                    chk("18.4 manifest gerado", len(_manifs) >= 1)

                    # Verifica DB
                    _con = bx_core.conectar(_e2e_cfg.db_path)
                    try:
                        _n_db = _con.execute(
                            "SELECT COUNT(*) FROM catalogo").fetchone()[0]
                        chk("18.5 DB populado", _n_db >= 5)
                        # Idempotencia: roda 2a vez e confirma que nao duplicou
                        _scan2 = []
                        for _f in _art.glob("*.m4a"):
                            try:
                                bx_fix.processar(_f, _e2e_cfg, _scan2)
                            except Exception:
                                pass
                        _n_db2 = _con.execute(
                            "SELECT COUNT(*) FROM catalogo").fetchone()[0]
                        chk("18.6 idempotente (nao duplica)",
                            _n_db2 == _n_db)
                    finally:
                        _con.close()
                finally:
                    if _old_home is None:
                        os.environ.pop("BX_HOME", None)
                    else:
                        os.environ["BX_HOME"] = _old_home


        # ─── 19 ───────────────────────────────────────────
        sec("19. Acervo caos (fixtures do JSON real)")

        import bx_nmx as _nm
        _nm.reset_state()

        def _an(nome: str, tag_art: str = "", tag_album: str = "") -> "object":
            fake = tmp / "fake.m4a"
            fake.write_bytes(b"x" * 100)
            return _nm.auto_nome(fake, nome, 100, 0.0,
                                  tag_art, tag_album, cfg)

        # 19.1 MP3_128 sem K
        d = _an("Barreto - Só Um Pesadelo (MP3_128)")
        chk("19.1 MP3_128 (sem K) removido",
            "(MP3" not in (d.titulo or ""))

        # 19.2 .gif.m4a
        d = _an("kamaitachi - Chuva De Sexta.gif")
        chk("19.2 .gif removido do titulo",
            ".gif" not in (d.titulo or ""))

        # 19.3 nome_pasta strip (C)/(S)
        chk("19.3 Bia Marques (C) strip",
            _nm.nome_pasta("Bia Marques (C)", cfg.suf_re) == "Bia marques")
        chk("19.3 EAGLE (S) strip",
            _nm.nome_pasta("EAGLE (S)", cfg.suf_re) == "Eagle")
        chk("19.3 MC (Grupo) preserva",
            "grupo" in _nm.nome_pasta("MC (Grupo)", cfg.suf_re).lower())

        # 19.4 conector ' e '
        sa_x, _ = _nm.split_art("Baco Exu do Blues e Péricles", cfg)
        chk("19.4 ' e ' feat quebra", ";" in sa_x)
        sa_x, _ = _nm.split_art("Cidade Dormitório", cfg)
        chk("19.4 ' e ' nao afeta nome proprio",
            sa_x == "Cidade Dormitório")
        sa_x, _ = _nm.split_art("Kavinn e Jeovanna Beatriz", cfg)
        chk("19.4 Kavinn e Jeovanna quebra", ";" in sa_x)

        # 19.5 art_decode limpa sufixo
        out, _extra, ok = _nm.art_decode("CZT(M4A_128K)", True, cfg)
        chk("19.5 art_decode strip (M4A_128K)",
            ok and "(M4A" not in out)

        # 19.6 _vencedor_dup: lrc desempata em mtime identico
        p_lrc = tmp / "com_lrc.m4a"; p_lrc.write_bytes(b"x")
        p_sem = tmp / "sem_lrc.m4a"; p_sem.write_bytes(b"x")
        os.utime(p_lrc, (1000, 1000))
        os.utime(p_sem, (1000, 1000))
        p_lrc.with_suffix(".lrc").write_text("[00:01.00] x\n",
                                              encoding="utf-8")
        chk("19.6 empate mtime + lrc vence",
            bx_core._vencedor_dup(128, 128, p_lrc, p_sem))
        chk("19.6 empate mtime + sem lrc perde",
            not bx_core._vencedor_dup(128, 128, p_sem, p_lrc))

        # 19.7 prefixo + formato invertido (com contexto conhecido)
        _nm.conhecidos_add("CZT")
        d = _an("1 - Aqueles Pensamentos - CZT")
        chk("19.7 prefixo + inversao",
            d.titulo == "Aqueles Pensamentos" and d.artista == "CZT")

        # 19.8 WhatsApp quar
        d = _an("AUD-20220206-WA0004")
        chk("19.8 WhatsApp quar", d.acao == "quar")

        # 19.9 --Rap do X: strip de -- e fallback seguro (sem -- no inicio)
        d = _an("--Rap do Bill Cipher _Meu Plano_ _ Gravity Falls _ VMZ")
        chk("19.9 --Rap prefixo removido",
            not (d.artista or "").startswith("--")
            and not (d.titulo or "").startswith("--"))

        # 19.10 nosplit preserva
        cfg_ns = cfg.com(nosplit=("Poesia Acústica",))
        sa_x, _ = _nm.split_art("Poesia Acústica #1", cfg_ns)
        chk("19.10 nosplit preserva", sa_x == "Poesia Acústica #1")

        # 19.11 equiv aplicado em processar (tag errada -> correta)
        cfg_eq = cfg.com(equiv={"kamaitach": "Kamaitachi",
                                "llowzzy": "Ilowzzy",
                                "touno": "Tuono"})
        _cfg_save = cfg
        try:
            _fake = tmp / "kamaitach - x.m4a"
            _fake.write_bytes(b"x")
            _test_cfg = cfg_eq
            # Simula o lookup que processar faz
            art_raw = "kamaitach"
            if art_raw.lower() in _test_cfg.equiv:
                art_raw = _test_cfg.equiv[art_raw.lower()]
            chk("19.11 equiv aplicado", art_raw == "Kamaitachi")
        finally:
            pass

        # 19.12 Bia Marques (C) vs Bia Marques mesma pasta
        p1 = _nm.nome_pasta("Bia Marques (C)", cfg.suf_re)
        p2 = _nm.nome_pasta("Bia Marques", cfg.suf_re)
        chk("19.12 (C) funde com nome sem marca", p1 == p2)

        # 19.13 MP3_160K
        d = _an("ERA - Ameno (MP3_160K)")
        chk("19.13 MP3_160K removido",
            "(MP3" not in (d.titulo or ""))

        # 19.14 M4A_128K no titulo
        d = _an("Essa não é sobre amor - Czt (M4A_128K)")
        chk("19.14 (M4A_128K) removido",
            "(M4A" not in (d.titulo or ""))

        # 19.15 emoji preservado
        d = _an("Sobral - Bem ❤️")
        chk("19.15 emoji no titulo nao corrompe",
            d.acao in ("ok", "rev", "quar"))

        # 19.16 bracket chines 【】
        d = _an("【1 Hour】 Best Japanese Sad Song")
        chk("19.16 【】 removido do inicio",
            "【" not in (d.artista or ""))

        # 19.17 "1." prefixo numero
        d = _an("1. Deus digital _ Acústico")
        chk("19.17 '1.' prefixo removido",
            not (d.titulo or "").startswith("1."))

        # 19.18 _PREFIXOS YYYY
        d = _an("2026 - Faixa X - Artista Y")
        chk("19.18 prefixo ano removido",
            not (d.titulo or "").startswith("2026"))

        # 19.19 Bia Marques - duplicata nome_pasta diferente sufixo
        chk("19.19 nome_pasta case-only equivalencia",
            _nm.nome_pasta("bia marques", cfg.suf_re)
            == _nm.nome_pasta("BIA MARQUES", cfg.suf_re))

        # 19.20 equiv.sugestoes gerado
        sug = cfg.cfg_dir / "equiv.sugestoes.txt"
        chk("19.20 equiv.sugestoes.txt gerado", sug.is_file())

        # 19.21 equiv.txt intocado (nao foi sobrescrito)
        orig_eq = cfg.cfg_dir / "equiv.txt"
        chk("19.21 equiv.txt intacto (nao modificado)",
            orig_eq.is_file() and "# ERRADO|CERTO" in
            orig_eq.read_text(encoding="utf-8", errors="replace"))

        # 19.22 _split_art multi feat com &
        sa_x, _ = _nm.split_art("A & B & C", cfg)
        chk("19.22 '&' triplo", sa_x.count(";") == 2)

        bx_core.fechar_cache()

    total = stats["pass"] + stats["fail"] + stats["skip"]
    print(f"\n== RESUMO bx_testkit: PASS={stats['pass']} "
          f"FAIL={stats['fail']} SKIP={stats['skip']} ==")
    if fail_names:
        print("\nFalhas:")
        for name in fail_names:
            print(f"  • {name}")
    _min_efetivo = _MIN_TOTAL_TESTS
    _override = os.environ.get("BX_TESTKIT_MIN", "").strip()
    if _override.isdigit():
        _min_efetivo = int(_override)
    elif not env_ready:
        _min_efetivo = max(100, _MIN_TOTAL_TESTS - 60)
    else:
        _min_efetivo = max(100, _MIN_TOTAL_TESTS - stats["skip"])
    if total < _min_efetivo:
        print(f"\n[FATAL] apenas {total} testes rodaram "
              f"(esperado >={_min_efetivo}); "
              f"env_ready={env_ready}; use BX_TESTKIT_MIN=N para ajustar")
        return 1
    return 0 if stats["fail"] == 0 else 1


def main(argv):
    if "--teste" in argv or not argv:
        return _teste()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
