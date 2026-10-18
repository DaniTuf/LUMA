# One-container LUMA for free cloud hosts (Hugging Face Spaces, Render, Koyeb, ...).
# The backend serves the web pages itself, so no nginx is needed. API keys come from the host's
# "secrets / environment variables" settings, never from a file in the image (.env is excluded).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=7860
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend

# Markdown/maths libraries served by LUMA itself (public CDNs are blocked on some networks)
RUN python - <<'EOF'
import io, shutil, tarfile, urllib.request
def pkg(url):
    return tarfile.open(fileobj=io.BytesIO(urllib.request.urlopen(url).read()))
t = pkg("https://registry.npmjs.org/katex/-/katex-0.16.11.tgz")
t.extractall("/tmp/katex", filter="data")
shutil.copytree("/tmp/katex/package/dist", "frontend/vendor/katex")
t = pkg("https://registry.npmjs.org/marked/-/marked-12.0.0.tgz")
t.extractall("/tmp/marked", filter="data")
shutil.copy("/tmp/marked/package/marked.min.js", "frontend/vendor/marked.min.js")
EOF

# Hugging Face runs the container as user 1000; the database folder must be writable by it
RUN useradd -m -u 1000 luma && mkdir -p /app/data && chown -R luma /app
USER luma

EXPOSE 7860
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]

