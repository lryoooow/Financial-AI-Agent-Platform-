# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS frontend
WORKDIR /build/fronted
RUN npm install -g pnpm@10.33.0
COPY fronted/package.json fronted/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY fronted/index.html fronted/vite.config.ts fronted/tsconfig*.json ./
COPY fronted/src ./src
COPY fronted/public ./public
RUN pnpm typecheck && pnpm build

FROM python:3.13-slim-bookworm
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TQDM_DISABLE=1 FIN_WEB=1 \
    FINRESEARCH_DATA_DIR=/data FRONTEND_DIST=/app/static FIN_HOST=0.0.0.0 PORT=9527
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates fonts-noto-cjk fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 jingyuan && useradd -r -u 10001 -g jingyuan -M jingyuan \
    && mkdir -p /data && chown jingyuan:jingyuan /data
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN uv sync --locked --no-dev --extra server --no-install-project
COPY backend/src ./src
COPY backend/run_web.py ./
RUN uv sync --locked --no-dev --extra server --no-editable
COPY --from=frontend /build/fronted/dist /app/static
USER 10001:10001
VOLUME ["/data"]
EXPOSE 9527
HEALTHCHECK --interval=30s --timeout=4s --start-period=35s \
    CMD ["/app/.venv/bin/python", "-c", "import json,os,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/api/health',timeout=3))['db']=='ok'"]
CMD ["/app/.venv/bin/python", "run_web.py"]
