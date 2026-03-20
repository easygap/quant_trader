# Oracle Cloud Free Tier (ARM) 배포 가이드

Ubuntu 22.04 기준으로 **퀀트 트레이더**를 24시간 구동하기 위한 설정입니다.  
인스턴스 shape는 **VM.Standard.A1.Flex** (ARM, Always Free eligible)를 권장합니다.

---

## 1. Oracle Cloud에서 인스턴스 만들기

1. [Oracle Cloud Console](https://cloud.oracle.com/)에 로그인합니다.
2. **Compute → Instances → Create instance** 로 이동합니다.
3. **Image**에서 **Canonical Ubuntu 22.04** (aarch64) 이미지를 선택합니다.
4. **Shape**에서 **Ampere A1** (`VM.Standard.A1.Flex`)을 선택하고, OCPU 1~4, 메모리 6~24GB 범위에서 Free Tier 한도 내로 지정합니다.
5. **Networking**: 새 VCN 또는 기존 VCN에 퍼블릭 서브넷을 둡니다.
6. **SSH keys**: 공개 키를 등록합니다 (로컬에서 `ssh-keygen`으로 생성한 `~/.ssh/id_rsa.pub` 등).
7. 인스턴스가 **Running** 이 되면 퍼블릭 IP를 확인합니다.

### 보안 목록(Ingress)

- **SSH(22)** 는 본인 IP만 허용하는 것이 안전합니다.
- 매매/대시보드용으로 **추가 포트**를 열 경우, Oracle **Networking → Virtual Cloud Networks → Security Lists / NSG**에서 규칙을 추가합니다.

---

## 2. SSH 접속

로컬 터미널에서 (사용자명은 이미지에 따라 `ubuntu` 또는 `opc` — Ubuntu 22.04는 보통 `ubuntu`):

```bash
ssh -i ~/.ssh/your_key ubuntu@<퍼블릭_IP>
```

---

## 3. 저장소 클론 및 초기 설정 (`setup.sh`)

GitHub 등에 푸시한 뒤 **저장소 URL**을 준비합니다.

### 방법 A — 먼저 클론한 뒤 스크립트 실행 (권장)

```bash
cd /home/ubuntu
git clone https://github.com/YOUR_ORG/quant_trader.git
cd quant_trader
chmod +x deploy/setup.sh deploy/install_service.sh
./deploy/setup.sh
```

`/home/ubuntu/quant_trader/requirements.txt` 가 있으면 **추가 클론 없이** 시스템 패키지 + venv + `pip install` 만 수행합니다.

### 방법 B — URL 한 번에 클론 + venv

`/home/ubuntu/quant_trader` 가 **없을 때**만 사용합니다.

```bash
# 예: 로컬에 스크립트만 복사해 실행
bash deploy/setup.sh https://github.com/YOUR_ORG/quant_trader.git
```

`setup.sh`가 수행하는 작업:

- 시스템 패키지 업데이트
- **deadsnakes** PPA로 **Python 3.11** 설치 (Ubuntu 22.04 기본 저장소에 3.11 없음)
- (모드 B) `git clone` → `/home/ubuntu/quant_trader`
- `python3.11 -m venv .venv` 및 `pip install -r requirements.txt`
- `logs/` 생성, `.env` 없으면 `.env.example` 복사 또는 빈 `.env` 생성

> **압축만 풀어 올린 경우**  
> 디렉터리를 `/home/ubuntu/quant_trader` 로 두고 방법 A처럼 `./deploy/setup.sh` (인자 없음)를 실행하면 됩니다.

---

## 4. 환경 변수 (`.env`)

```bash
nano /home/ubuntu/quant_trader/.env
```

`.env.example`을 참고해 **KIS**, **디스코드**, **SMTP** 등 필요한 값을 채웁니다.  
`systemd` 유닛은 `EnvironmentFile=-/home/ubuntu/quant_trader/.env` 로 읽습니다 (파일 없어도 기동은 가능하나, 실제 매매/알림에는 설정이 필요합니다).

---

## 5. systemd 서비스 등록 (`install_service.sh`)

```bash
cd /home/ubuntu/quant_trader
chmod +x deploy/install_service.sh
./deploy/install_service.sh
```

내부 동작:

- `deploy/quant_trader.service` → `/etc/systemd/system/quant_trader.service`
- `daemon-reload`, `enable`, `restart`/`start`, `status`

### 유용한 명령

```bash
sudo journalctl -u quant_trader -f          # systemd 저널(설정에 따라)
tail -f /home/ubuntu/quant_trader/logs/service.log
tail -f /home/ubuntu/quant_trader/logs/service_error.log
sudo systemctl stop quant_trader
sudo systemctl start quant_trader
```

### 실행 모드 변경

유닛 기본값은 **`--mode schedule`** 입니다. `config/settings.yaml` 의 `trading.mode` 가 **paper** 일 때 장전·장중·장마감 스케줄을 **무한 루프**로 돌립니다 (systemd `Restart=always` 와 맞음).

| CLI | 동작 |
|-----|------|
| `--mode schedule` | 스케줄러 무한 루프 (모의, **서비스 권장**) |
| `--mode paper` | 워치리스트 **한 바퀴만** 돌고 종료 (수동 점검·크론 단발에 적합) |
| `--mode live` | 실전 (`ENABLE_LIVE_TRADING=true` + `--confirm-live` 필요) |

`trading.mode` 가 **live** 인 상태에서 `schedule` 을 쓰면 프로세스가 거부됩니다. 실전은 반드시 `--mode live` 로 실행하세요.

```bash
sudo nano /etc/systemd/system/quant_trader.service
# ExecStart: schedule(기본) / paper(1회) / live(실전) 등
sudo systemctl daemon-reload
sudo systemctl restart quant_trader
```

---

## 6. logrotate (선택)

애플리케이션 로그와 서비스 로그가 `logs/*.log` 에 쌓입니다.

```bash
sudo cp /home/ubuntu/quant_trader/deploy/logrotate.conf /etc/logrotate.d/quant_trader
sudo logrotate -d /etc/logrotate.d/quant_trader   # 드라이런
```

`copytruncate` 는 쓰는 중인 로그 파일을 잘라도 **systemd append**와 충돌을 줄이기 위해 넣었습니다. 필요 없으면 제거해도 됩니다.

---

## 7. ARM / Free Tier 유의사항

- **아키텍처**: 대부분의 Python 휠(aarch64)은 정상 설치됩니다. 빌드 실패 시 `apt install` 로 `-dev` 패키지를 추가해야 할 수 있습니다.
- **유휴 정지**: Free Tier는 크레딧·정책에 따라 인스턴스가 중지될 수 있으니, 장기 무인 운용 시 Oracle 정책과 알림을 확인하세요.
- **시간 동기화**: `timedatectl` 로 NTP 동기화가 켜져 있는지 확인하면 주문·로그 타임스탬프에 유리합니다.

---

## 8. 체크리스트

- [ ] 인스턴스 Ubuntu 22.04 ARM, SSH 접속 확인  
- [ ] `./deploy/setup.sh <저장소 URL>` 완료  
- [ ] `.env` 설정  
- [ ] `./deploy/install_service.sh` 후 `systemctl status quant_trader` **active (running)**  
- [ ] (선택) `/etc/logrotate.d/quant_trader` 설치  

문제가 있으면 `logs/service_error.log` 와 앱 로그 `logs/quant_trader_*.log` 를 먼저 확인하세요.
