# For hosts that run containers (Koyeb, Cloud Run, Fly.io, Hugging Face Spaces).
# Not needed on Render, which builds from requirements.txt.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd --system --uid 10001 veriklaim && mkdir /data && chown veriklaim /data
USER veriklaim
ENV APP_ENV=production DATABASE_URL=sqlite:////data/veriklaim.db PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
