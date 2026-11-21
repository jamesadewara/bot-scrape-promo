# PromoScout RUNBOOK

## Setup

```powershell
cd C:\Users\pc\Documents\WORKSPACE\PROJECTS\bot-scrape-promo
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env
```

```powershell
# only if Activate.ps1 is blocked
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## New terminal

```powershell
cd C:\Users\pc\Documents\WORKSPACE\PROJECTS\bot-scrape-promo
.\.venv\Scripts\Activate.ps1
```

## Run

Pick one:

```powershell
# normal: batches forever, rests in between (Ctrl+C to stop)
python -m promoscout auto

# continue auto after Ctrl+C mid-batch
python -m promoscout auto --resume

# one batch only
python -m promoscout run

# finish one batch stopped with Ctrl+C
python -m promoscout run --resume
```

## Check

```powershell
python -m promoscout check
```

## Review

```powershell
streamlit run review_app.py
# http://localhost:8501  (HOT banner = 95%+ finds, refreshes every 30 s)
python -m promoscout export --out approved.csv
Get-Content data/alerts.log -Wait
```

## Settings (.env)

```
PROMOSCOUT_MAX_PRICE_NGN=20000
PROMOSCOUT_MAX_OLD_PRICE_NGN=1000000
PROMOSCOUT_ALERT_MIN_DISCOUNT=95
PROMOSCOUT_FOCUS_BONUS=40
PROMOSCOUT_PHONE_BONUS=20
PROMOSCOUT_REVIEW_KEEP_MIN_DISCOUNT=90
PROMOSCOUT_REVIEW_HIDE_AFTER_HOURS=48
PROMOSCOUT_MAX_LINKS_PER_RUN=300
PROMOSCOUT_MAX_RUNTIME_MINUTES=30
PROMOSCOUT_TARGET_CANDIDATES=300
PROMOSCOUT_AUTO_MIN_COOLDOWN_MINUTES=3
PROMOSCOUT_AUTO_MAX_COOLDOWN_MINUTES=60
PROMOSCOUT_AUTO_RETRY_MINUTES=2
```

## Email alerts (Gmail)

1. Google Account > Security > 2-Step Verification: on
2. Google Account > Security > App passwords: create one, copy the 16 letters
3. `.env`:

```
PROMOSCOUT_EMAIL_ENABLED=true
PROMOSCOUT_SMTP_USER=yourbot@gmail.com
PROMOSCOUT_SMTP_PASSWORD=abcdabcdabcdabcd
PROMOSCOUT_EMAIL_TO=jamesadewara1@gmail.com,jamesadewara3@gmail.com
PROMOSCOUT_REVIEW_URL=http://localhost:8501
```

```powershell
python -m promoscout test-email
```

```bash
# server
docker compose up -d
docker compose exec bot python -m promoscout test-email
```

## Event days (Black Friday, 11.11, 12.12, anniversary)

```
PROMOSCOUT_AUTO_ALIGN_TO_HOUR=true
PROMOSCOUT_RECHECK_LISTING_HOURS=1
PROMOSCOUT_RECHECK_PRODUCT_QUALIFYING_HOURS=1
```

```bash
# server: apply .env changes
docker compose up -d
```

## Windows: start at logon

```powershell
schtasks /create /tn PromoScout /sc onlogon /tr "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\pc\Documents\WORKSPACE\PROJECTS\bot-scrape-promo\run_auto.ps1"
schtasks /delete /tn PromoScout /f
powercfg /change standby-timeout-ac 0
```

## Docker (local)

```powershell
docker compose up -d --build
docker compose logs -f bot
docker compose exec bot python -m promoscout check
docker compose down
```

## EC2 instance (AWS free plan)

| Item | Pick |
| --- | --- |
| Instance | `t3.small` (2 vCPU, 2 GB, x86) |
| Cheaper | `t3.micro` (1 GB): bot only, review on demand |
| AMI | Amazon Linux 2023 |
| Disk | 20 GB gp3 |
| Region | `us-east-1` (N. Virginia) |
| Security group | inbound 22 (SSH) and 8501 (review page) |
| Elastic IP | yes |

```bash
# confirm free-plan types in your region
aws ec2 describe-instance-types --filters Name=free-tier-eligible,Values=true --query "InstanceTypes[].InstanceType" --region us-east-1
```

## Server setup (Amazon Linux / EC2)

```bash
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -sSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
exit
```

## Server setup (Ubuntu)

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER
exit
```

## Server deploy

```bash
git clone https://github.com/jamesadewara/bot-scrape-promo.git
cd bot-scrape-promo
cp .env.example .env
docker compose up -d --build
```

## Server with 1 GB RAM (bot always on, review only when needed)

```bash
docker compose up -d --build bot
docker compose up -d review
docker compose stop review
```

## Server status

```bash
docker compose ps
docker compose logs -f bot
docker compose exec bot python -m promoscout check
docker compose exec bot tail -f data/alerts.log
```

## Server review (public IP)

```
# server .env
PROMOSCOUT_REVIEW_PASSWORD=<long password>
PROMOSCOUT_REVIEW_URL=http://<elastic-ip>:8501
```

```bash
docker compose up -d
```

EC2 > Security groups > inbound rules > add: Custom TCP, port 8501, source 0.0.0.0/0

```
http://<elastic-ip>:8501
```

## Server review (SSH tunnel instead of public)

```powershell
ssh -i your-key.pem -L 8501:localhost:8501 <user>@<elastic-ip>
# http://localhost:8501
```

## Server export

```bash
docker compose exec bot python -m promoscout export --out data/approved.csv
docker compose cp bot:/app/data/approved.csv ./approved.csv
```

```powershell
scp -i your-key.pem <user>@<server-ip>:bot-scrape-promo/approved.csv .
```

## Server redeploy (after pushing a bot update)

```powershell
# PC
git add -A
git commit -m "update"
git push
```

```bash
# server
cd ~/bot-scrape-promo
bash deploy.sh
```

```bash
# rollback to the previous version
git log --oneline -5
git checkout <commit>
docker compose up -d --build
git checkout main
```

## Server stop / start

```bash
docker compose stop
docker compose start
```

## Server backup

```bash
docker run --rm -v bot-scrape-promo_promoscout-data:/data -v "$PWD":/backup alpine tar czf /backup/promoscout-data.tgz -C /data .
```

## Tests

```powershell
python -m pytest -q
```
