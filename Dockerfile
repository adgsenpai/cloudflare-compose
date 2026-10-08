FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home connector && mkdir /state && chown connector:connector /state
USER connector
ENV CF_COMPOSE_HOME=/state
ENTRYPOINT ["python", "-m", "cloudflare_compose.server"]
