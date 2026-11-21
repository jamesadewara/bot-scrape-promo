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

```powershell
python -m promoscout run
python -m promoscout run --resume
python -m promoscout auto
python -m promoscout auto --resume
```

## Check

```powershell
python -m promoscout check
```

## Review

```powershell
streamlit run review_app.py
# http://localhost:8501
python -m promoscout export --out approved.csv
```

## Settings (.env)

```
PROMOSCOUT_MAX_PRICE_NGN=20000
PROMOSCOUT_MAX_OLD_PRICE_NGN=1000000
PROMOSCOUT_MAX_LINKS_PER_RUN=300
PROMOSCOUT_MAX_RUNTIME_MINUTES=30
PROMOSCOUT_AUTO_MIN_COOLDOWN_MINUTES=15
PROMOSCOUT_AUTO_MAX_COOLDOWN_MINUTES=60
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
git clone <your-repo-url> bot-scrape-promo
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
```

## Server review (from your PC)

```powershell
ssh -i your-key.pem -L 8501:localhost:8501 <user>@<server-ip>
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

## Server update

```bash
git pull
docker compose up -d --build
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
