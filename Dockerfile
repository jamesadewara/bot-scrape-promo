FROM python:3.13-slim

# tzdata: max_runs_per_day counts runs per local day, so the container needs a real timezone
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Africa/Lagos

WORKDIR /app

# pinned dependencies first (cached layer), then the code
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pyproject.toml review_app.py ./
COPY promoscout ./promoscout
COPY .streamlit ./.streamlit
RUN pip install --no-deps .

RUN useradd --create-home --uid 1000 scout \
    && mkdir -p /app/data \
    && chown -R scout:scout /app
USER scout

VOLUME ["/app/data"]

CMD ["python", "-m", "promoscout", "auto", "--quiet"]
