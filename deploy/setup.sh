#!/bin/bash
# Ubuntu 22.04 기준 (Oracle Cloud ARM Free Tier 권장)
#
# 모드 1) 이미 클론된 경우 (/home/ubuntu/quant_trader/requirements.txt 존재)
#   cd /home/ubuntu/quant_trader && ./deploy/setup.sh
#
# 모드 2) 빈 홈에서 한 번에 클론 + venv
#   curl -sSL .../deploy/setup.sh | bash -s -- https://github.com/you/quant_trader.git
#   또는: bash deploy/setup.sh https://github.com/you/quant_trader.git

set -euo pipefail

TARGET="/home/ubuntu/quant_trader"
REPO_URL="${1:-}"

install_system_python() {
  sudo apt update && sudo apt upgrade -y
  sudo apt install -y software-properties-common
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt update
  sudo apt install -y python3.11 python3.11-venv python3.11-distutils git curl
  if ! python3.11 -m pip --version &>/dev/null; then
    curl -sS https://bootstrap.pypa.io/get-pip.py | sudo python3.11
  fi
}

setup_venv_and_env() {
  mkdir -p "$TARGET/logs"
  cd "$TARGET"
  python3.11 -m venv .venv
  # shellcheck source=/dev/null
  source .venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.txt
  if [ ! -f .env ]; then
    if [ -f .env.example ]; then
      cp .env.example .env
      echo ".env.example 을 .env 로 복사했습니다. 값을 수정하세요."
    else
      touch .env
      echo "빈 .env 파일을 생성했습니다."
    fi
  fi
}

install_system_python

if [ -f "$TARGET/requirements.txt" ]; then
  echo "기존 저장소 사용: $TARGET"
  setup_venv_and_env
elif [ -n "$REPO_URL" ]; then
  if [ -d "$TARGET" ]; then
    echo "오류: $TARGET 가 이미 있으나 requirements.txt 가 없습니다. 디렉터리를 정리한 뒤 다시 실행하세요."
    exit 1
  fi
  cd /home/ubuntu
  git clone "$REPO_URL" quant_trader
  setup_venv_and_env
else
  echo "사용법:"
  echo "  (A) 저장소를 이미 /home/ubuntu/quant_trader 에 클론한 뒤:"
  echo "      cd $TARGET && ./deploy/setup.sh"
  echo "  (B) 홈에서 URL로 클론까지 한 번에:"
  echo "      bash deploy/setup.sh https://github.com/YOUR_ORG/quant_trader.git"
  exit 1
fi

echo ""
echo "초기 설정 완료. 다음:"
echo "  1) nano $TARGET/.env"
echo "  2) cd $TARGET && ./deploy/install_service.sh"
echo ""
