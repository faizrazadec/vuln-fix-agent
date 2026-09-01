#!/bin/sh
# Copies the three SSH keys named in .env into the ssh-keys volume.
# Re-run after rotating a key. Key material is streamed through the docker daemon;
# nothing is printed and nothing is written into this directory.
set -e
cd "$(dirname "$0")/.."
. ./.env

: "${SSH_KEY_DIR:?set SSH_KEY_DIR in .env}"
: "${SSH_AUTH_KEY:?}" "${SSH_SIGN_KEY:?}" "${SSH_SIGN_KEY_PRIVATE:?}"

VOL=a2claude_ssh-keys
docker volume create "$VOL" >/dev/null

# A throwaway container is the only way to write into a named volume from the host.
docker rm -f a2claude-keyload >/dev/null 2>&1 || true
docker run -d --name a2claude-keyload -v "$VOL":/ssh-keys alpine sleep 300 >/dev/null

for k in "$SSH_AUTH_KEY" "$SSH_SIGN_KEY_PRIVATE" "$SSH_SIGN_KEY"; do
  if [ ! -f "$SSH_KEY_DIR/$k" ]; then
    docker rm -f a2claude-keyload >/dev/null
    echo "missing: $SSH_KEY_DIR/$k" >&2
    exit 1
  fi
  docker cp "$SSH_KEY_DIR/$k" a2claude-keyload:/ssh-keys/"$k"
  echo "loaded $k"
done

# uid 1000 = the `agent` user the server runs as.
docker exec a2claude-keyload sh -c 'chown -R 1000:1000 /ssh-keys && chmod 600 /ssh-keys/* && chmod 644 /ssh-keys/*.pub'
docker rm -f a2claude-keyload >/dev/null
echo "done — restart with: docker compose up -d a2claude-api"
