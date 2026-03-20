#!/bin/bash
# quant_trader 저장소 루트 또는 deploy/ 에서 실행 가능
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_SRC="$SCRIPT_DIR/quant_trader.service"

if [ ! -f "$SERVICE_SRC" ]; then
  echo "quant_trader.service 를 찾을 수 없습니다: $SERVICE_SRC"
  exit 1
fi

sudo cp "$SERVICE_SRC" /etc/systemd/system/quant_trader.service
sudo systemctl daemon-reload
sudo systemctl enable quant_trader
sudo systemctl restart quant_trader || sudo systemctl start quant_trader
sudo systemctl status quant_trader --no-pager
