#!/usr/bin/env bash
# Starts the Leitor de Partituras (Docker) and waits until it answers.
#
#   ./start.sh            build if needed, start, wait, print the URL
#   ./start.sh --open     ...and open it in the browser
#   ./start.sh --no-build start the existing image without rebuilding
#
# Environment: PORT (default 8000).
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8000}"
OPEN=0
BUILD=1
for arg in "$@"; do
  case "$arg" in
    --open) OPEN=1 ;;
    --no-build) BUILD=0 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Opção desconhecida: $arg (use --help)" >&2; exit 2 ;;
  esac
done

fail() { echo "Erro: $*" >&2; exit 1; }

command -v docker >/dev/null || fail "Docker não encontrado. Instale o Docker para rodar a aplicação."
docker compose version >/dev/null 2>&1 || fail "O plugin 'docker compose' não está disponível."
docker info >/dev/null 2>&1 || fail "O Docker não está rodando (ou seu usuário não tem acesso a ele)."

# The container runs as the host user so files in ./data stay yours. Create the folder
# ourselves: if Docker creates it for the bind mount, it would belong to root.
mkdir -p data
export HOST_UID HOST_GID PORT
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

if [ "$BUILD" = 1 ]; then
  echo "Construindo e iniciando (a primeira vez pode levar alguns minutos)…"
  docker compose up -d --build
else
  docker compose up -d
fi

URL="http://localhost:${PORT}"
echo -n "Aguardando a aplicação responder em ${URL} "
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "${URL}/api/scores" 2>/dev/null; then
    echo
    echo "Pronto: ${URL}"
    echo "Logs: docker compose logs -f app   ·   Parar: docker compose down"
    if [ "$OPEN" = 1 ]; then
      if command -v wslview >/dev/null; then wslview "$URL"
      elif grep -qi microsoft /proc/version 2>/dev/null; then cmd.exe /c start "" "$URL" >/dev/null 2>&1
      elif command -v xdg-open >/dev/null; then xdg-open "$URL" >/dev/null 2>&1
      elif command -v open >/dev/null; then open "$URL"
      else echo "Abra no navegador: ${URL}"
      fi
    fi
    exit 0
  fi
  echo -n "."
  sleep 1
done

echo
docker compose logs --tail 30 app >&2
fail "a aplicação não respondeu em 60 s (veja os logs acima)."
