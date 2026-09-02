FROM python:3.14-slim

# Node from NodeSource, not Debian: Debian ships Node 18 + npm 9, and npm 9 rewrites
# lockfiles it regenerates (strips libc/license metadata, shrinks trees) → junk diffs.
# NodeSource gives Node 22 + a current npm, so lockfile regen stays surgical.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates ripgrep curl gnupg jq openssh-client \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g npm@latest @anthropic-ai/claude-code \
 && npm cache clean --force

# gh (PR creation) and docker-ce-cli (rebuilding images to re-scan) from upstream repos,
# so versions track the vendor rather than a pin that rots.
RUN install -m0755 -d /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh docker-ce-cli \
 && rm -rf /var/lib/apt/lists/*

# trivy: vuln scanning for filesystem and built images
RUN curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh \
      | sh -s -- -b /usr/local/bin

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app/main.py app/entrypoint.sh ./
# Glob so a fresh clone (example only) still builds; real projects.json is gitignored.
COPY app/projects*.json ./
COPY app/bin/ /app/bin/
COPY app/skills/ /app/skills/

# Claude Code refuses --dangerously-skip-permissions (what permission_mode=
# "bypassPermissions" sends) under root, so the server runs unprivileged.
RUN useradd -u 1000 -m -d /home/agent agent

ENV HOME=/home/agent
# .claude.json lives beside $HOME/.claude by default, i.e. OUTSIDE the mounted
# volume — so onboarding state was lost every start and `claude -p` refused to run.
# Pointing the config dir at the volume keeps credentials and config together.
ENV CLAUDE_CONFIG_DIR=/home/agent/.claude
RUN mkdir -p /home/agent/.claude /home/agent/workspace /home/agent/state /home/agent/.ssh \
 && chmod 700 /home/agent/.ssh \
 && chmod +x /app/entrypoint.sh \
 && chown -R agent:agent /home/agent /app

ENV PATH=/app/bin:$PATH

EXPOSE 9999
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9999"]
