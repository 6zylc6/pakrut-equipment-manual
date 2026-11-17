FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY server.py ./server.py
COPY index.html ./index.html

ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production
ENV PORT=8080

EXPOSE 8080
CMD ["sh", "-c", "exec python server.py --host 0.0.0.0 --port \"${PORT:-8080}\""]
