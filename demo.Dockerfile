# The hosted demo (fly.demo.toml): `quanty demo --live --public` on synthetic markets. No keys, no
# data: every start is a fresh day, and it exits every few hours for Fly to start a fresh one.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && rm -rf /root/.cache
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
# 6 hours of history: a quick start on a shared CPU
CMD ["quanty", "demo", "--live", "--public", "--history", "6", "--port", "8080"]
