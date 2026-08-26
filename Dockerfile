FROM python:3.14-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm git ca-certificates ripgrep \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code \
 && npm cache clean --force

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY main.py ./

# Claude Code refuses --dangerously-skip-permissions (what permission_mode=
# "bypassPermissions" sends) under root, so the server runs unprivileged.
RUN useradd -u 1000 -m -d /home/agent agent

ENV HOME=/home/agent
# .claude.json lives beside $HOME/.claude by default, i.e. OUTSIDE the mounted
# volume — so onboarding state was lost every start and `claude -p` refused to run.
# Pointing the config dir at the volume keeps credentials and config together.
ENV CLAUDE_CONFIG_DIR=/home/agent/.claude
RUN mkdir -p /home/agent/.claude /home/agent/workspace \
 && chown -R agent:agent /home/agent /app

USER agent

EXPOSE 9999
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9999"]
