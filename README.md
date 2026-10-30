# BX Audio Manager

Organizador de acervo musical para Android/Termux.
Renomeia, move, tagueia e mantém catálogo SQLite com durabilidade
(POSIX atômico, fsync, journal hash-chain, lock flock).

## Requisitos

- Termux (Android) ou Linux com Python ≥ 3.11
- `ffmpeg` + `ffprobe` (para probe de áudio)
- `termux-api` (opcional, para MediaStore scan)
- `yt-dlp` (opcional, para downloads)

```bash
pkg install python ffmpeg termux-api curl
cd /data/data/com.termux/files/home/.bx_manager || exit 1

echo "=== compileall (sanity global) ==="
python3 -m compileall -q bx*.py && echo "  [OK] todos compilam" || {
  echo "  [FAIL] compileall falhou — não seguimos"; exit 1; }

echo ""
echo "=== .gitignore ==="
cat > .gitignore << 'EOF'
# Runtime / estado local
.bx_backups/
.bx_teste/
temp_bx/
logs/
*.db
*.db-wal
*.db-shm
biblioteca.db*
diario_renomeacoes.log
pendentes.txt
fila_downloads.txt*
historico_execucoes.jsonl
historico.txt
erros_pendentes.jsonl
journal.jsonl
raridades.m3u
*.bxpart
*.lrc.org
*.encoding.log
*.bxtmp*

# Config do usuário
config/

# Backups locais (pré-bloco*, .bak, .bak2, .bak3)
*.bak*
*.pre_bloco*

# Ferramentas
.venv/
venv/
.mypy_cache/
.ruff_cache/
.pytest_cache/
__pycache__/
*.pyc
*.pyo

# Sistema
.DS_Store
Thumbs.db
