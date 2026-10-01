FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# python:slim ships no CA bundle, so every outbound HTTPS call (Scrap.io,
# SAM.gov, Google, Stripe) fails with CERTIFICATE_VERIFY_FAILED. ca-certificates
# fixes apt/openssl; certifi (pinned in requirements) is symlinked over
# /etc/ssl/cert.pem, the path Python's ssl module looks at, so urllib and
# requests share one trust store and neither depends on the other being present.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# ca-certificates already owns /etc/ssl/cert.pem, so replace it rather than
# letting os.symlink raise FileExistsError and fail the build.
RUN python -c "import certifi, os, pathlib; \
t = pathlib.Path('/etc/ssl/cert.pem'); \
t.unlink(missing_ok=True); \
t.symlink_to(certifi.where())" \
 && python -c "import ssl, urllib.request; \
assert ssl.create_default_context().get_ca_certs(), 'empty trust store'; \
print(urllib.request.urlopen('https://pypi.org', timeout=20).status)"
COPY . .
CMD ["sh", "-c", "uvicorn construction_main:app --host 0.0.0.0 --port ${PORT:-8000}"]
