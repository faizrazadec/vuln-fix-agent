#!/bin/sh
# Sets up git/ssh identity from mounted keys, then hands off to the server.
# Everything here is idempotent — the container gets recreated on every rebuild.
set -e

KEYDIR=/ssh-keys           # volume holding the keys, loaded by ./load-keys.sh
AGENT_UID=1000
export HOME=/home/agent
SSHDIR="$HOME/.ssh"

# The forwarded ssh-agent socket arrives root-owned, so this script starts as root,
# hands the socket to `agent`, and drops privileges before exec'ing the server —
# Claude Code refuses --dangerously-skip-permissions when running as root.
if [ "$(id -u)" = "0" ]; then
  [ -S "$SSH_AUTH_SOCK" ] && chown "$AGENT_UID" "$SSH_AUTH_SOCK" 2>/dev/null || true
  # Let the unprivileged agent talk to the mounted Docker socket (build/scan images).
  [ -S /var/run/docker.sock ] && chmod a+rw /var/run/docker.sock 2>/dev/null || true
  exec setpriv --reuid="$AGENT_UID" --regid="$AGENT_UID" --init-groups "$0" "$@"
fi

if [ -d "$KEYDIR" ]; then
  # Copy rather than use in place: ssh rejects keys whose ownership/mode it dislikes,
  # and a bind mount carries the host's uid and permissions.
  mkdir -p "$SSHDIR" && chmod 700 "$SSHDIR"
  cp -f "$KEYDIR"/* "$SSHDIR"/ 2>/dev/null || true
  chmod 600 "$SSHDIR"/* 2>/dev/null || true
  chmod 644 "$SSHDIR"/*.pub 2>/dev/null || true

  # Pin GitHub's host keys so the first clone doesn't hang on an unknown-host prompt.
  ssh-keyscan -t rsa,ecdsa,ed25519 github.com > "$SSHDIR/known_hosts" 2>/dev/null || true
  chmod 644 "$SSHDIR/known_hosts" 2>/dev/null || true

fi

# The key is passphrase-protected, so an ssh-agent inside the container unlocks it once
# at startup and holds it for the container's lifetime. The passphrase reaches ssh-add
# through SSH_ASKPASS rather than a tty, since there is no tty here.
if [ -n "$SSH_AUTH_KEY" ] && [ -f "$SSHDIR/$SSH_AUTH_KEY" ]; then
  eval "$(ssh-agent -s)" >/dev/null

  if [ -n "$SSH_KEY_PASSPHRASE" ]; then
    ASKPASS=$(mktemp)
    printf '#!/bin/sh\nprintf %%s "$SSH_KEY_PASSPHRASE"\n' > "$ASKPASS"
    chmod 700 "$ASKPASS"
    # SSH_ASKPASS_REQUIRE=force is what makes modern OpenSSH use the helper with no tty.
    SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force DISPLAY=none \
      ssh-add "$SSHDIR/$SSH_AUTH_KEY" < /dev/null 2>&1 | head -2
    rm -f "$ASKPASS"
  else
    ssh-add "$SSHDIR/$SSH_AUTH_KEY" < /dev/null 2>&1 | head -2
  fi

  ssh-add -l > /dev/null 2>&1 \
    && echo "[entrypoint] ssh-agent holds $(ssh-add -l | wc -l) key(s)" \
    || echo "[entrypoint] WARNING: ssh-agent has no keys — push and signing will fail"

  # Pin the identity but keep the agent in play, so the unlocked copy is what gets used.
  export GIT_SSH_COMMAND="ssh -o IdentitiesOnly=yes -i $SSHDIR/$SSH_AUTH_KEY.pub"
fi

# $CLAUDE_CONFIG_DIR is a named volume, so image content does not appear there on
# rebuild — copy skills in on every start instead.
if [ -d /app/skills ]; then
  mkdir -p "$CLAUDE_CONFIG_DIR/skills"
  cp -R /app/skills/. "$CLAUDE_CONFIG_DIR/skills/"
  echo "[entrypoint] skills: $(ls "$CLAUDE_CONFIG_DIR/skills" | tr '\n' ' ')"
fi

git config --global user.name  "${GIT_USER_NAME:-Vuln Fix Agent}"
git config --global user.email "${GIT_USER_EMAIL:-agent@localhost}"
git config --global init.defaultBranch main
git config --global --add safe.directory '*'

# SSH-signed commits. The key must ALSO be registered on GitHub as a Signing Key
# (a separate entry from the same key added as an Auth Key) for the Verified badge.
if [ -n "$SSH_SIGN_KEY" ] && [ -f "$SSHDIR/$SSH_SIGN_KEY" ]; then
  git config --global gpg.format ssh
  git config --global user.signingkey "$SSHDIR/$SSH_SIGN_KEY"
  git config --global commit.gpgsign true
  git config --global tag.gpgsign true
  # Lets `git log --show-signature` verify locally instead of reporting "no signature".
  printf '%s %s\n' "${GIT_USER_EMAIL:-agent@localhost}" "$(cat "$SSHDIR/$SSH_SIGN_KEY")" \
    > "$SSHDIR/allowed_signers"
  git config --global gpg.ssh.allowedSignersFile "$SSHDIR/allowed_signers"
fi

if [ -n "$GH_TOKEN" ]; then
  gh auth setup-git 2>/dev/null || true
fi

# Authenticate npm/pnpm to GitHub Packages so private @ember-ai-engineering/* deps resolve.
# Host-level auth in ~/.npmrc applies to every request to npm.pkg.github.com regardless of a
# repo's own scope→registry mapping, so lockfiles that pull those packages can regenerate.
if [ -n "$GH_READ_PACKAGES_TOKEN" ]; then
  printf '//npm.pkg.github.com/:_authToken=%s\n' "$GH_READ_PACKAGES_TOKEN" > "$HOME/.npmrc"
  chmod 600 "$HOME/.npmrc"
fi

exec "$@"
