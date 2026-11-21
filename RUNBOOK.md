# PromoScout RUNBOOK

## Setup (once)

```powershell
cd C:\Users\pc\Documents\WORKSPACE\PROJECTS\bot-scrape-promo
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

If `Activate.ps1` is blocked, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

## Every new terminal

```powershell
cd C:\Users\pc\Documents\WORKSPACE\PROJECTS\bot-scrape-promo
.\.venv\Scripts\Activate.ps1
```

The prompt must start with `(.venv)`.

## Run

```powershell
python -m promoscout run
```

First run only, small (50 pages):

```powershell
$env:PROMOSCOUT_MAX_LINKS_PER_RUN = "50"; python -m promoscout run; Remove-Item Env:PROMOSCOUT_MAX_LINKS_PER_RUN
```

Continue after `Ctrl+C`:

```powershell
python -m promoscout run --resume
```

Long session (2 hours, 1500 pages):

```powershell
$env:PROMOSCOUT_MAX_LINKS_PER_RUN = "1500"; $env:PROMOSCOUT_MAX_RUNTIME_MINUTES = "120"; python -m promoscout run
```

## Is it working?

```powershell
python -m promoscout check
```

Ends with `OVERALL: OK`.

## Review

```powershell
streamlit run review_app.py
```

Open http://localhost:8501, set Decision, click Save decisions.

Export approved:

```powershell
python -m promoscout export --out approved.csv
```

## Tests

```powershell
python -m pytest -q
```
