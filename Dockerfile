# Image du service « pont » (webhooks LNbits -> backend, annuaire LNURL regtest).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

WORKDIR /app
COPY pyproject.toml README.md ./
COPY tontine_lightning ./tontine_lightning

# Utilisateur non root, uid 1000 = même uid que « lnd » dans les images polarlightning :
# permet de lire invoice.macaroon de lnd-membres sans élargir ses permissions.
RUN pip install ".[pont]" \
    && useradd --uid 1000 --create-home pont \
    && mkdir /donnees && chown pont:pont /donnees

USER pont
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/vivant', timeout=4)"

# --no-access-log : l'URL du webhook contient son jeton secret, elle ne doit pas finir dans les logs.
CMD ["uvicorn", "tontine_lightning.pont.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
