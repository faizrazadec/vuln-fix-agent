FROM python:3.14-slim

# Pinned, not "latest". An unpinned rebuild could change the agent's own runtime under it —
# a new claude-code can move the SDK contract, a new npm rewrites lockfiles differently
# (see the NodeSource note below), a new trivy changes what the PR gate reports. Bump these
# deliberately, rebuild, and run tests/test_protocol.py — never as a side effect of an
# unrelated rebuild. Current as of 2026-09-15.
ARG NPM_VERSION=12.0.2
ARG CLAUDE_CODE_VERSION=2.1.267
ARG TRIVY_VERSION=0.74.0

# Node from NodeSource, not Debian: Debian ships Node 18 + npm 9, and npm 9 rewrites
# lockfiles it regenerates (strips libc/license metadata, shrinks trees) → junk diffs.
# NodeSource gives Node 22 + a current npm, so lockfile regen stays surgical.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates ripgrep curl gnupg jq openssh-client \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g "npm@${NPM_VERSION}" "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
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

# trivy: vuln scanning for filesystem and built images. The installer is fetched from a
# tagged ref, not `main` — piping a moving branch straight into sh makes every rebuild a
# fresh trust decision.
RUN curl -sfL "https://raw.githubusercontent.com/aquasecurity/trivy/v${TRIVY_VERSION}/contrib/install.sh" \
      | sh -s -- -b /usr/local/bin "v${TRIVY_VERSION}"

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
# .cache/trivy is a mounted volume (see compose.yml): trivy's DB is ~1.3GB and lived in
# the container's writable layer, so every rebuild threw it away and re-downloaded it.
RUN mkdir -p /home/agent/.claude /home/agent/workspace /home/agent/state /home/agent/.ssh \
      /home/agent/.cache/trivy \
 && chmod 700 /home/agent/.ssh \
 && chmod +x /app/entrypoint.sh \
 && chown -R agent:agent /home/agent /app

ENV PATH=/app/bin:$PATH

EXPOSE 9999
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9999"]
