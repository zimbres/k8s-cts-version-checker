#!/bin/sh
set -e

# ── Auto-login to private registries ─────────────────────────────────────────
# Define up to 9 registries via environment variables:
#   REG_1_URL, REG_1_USER, REG_1_PASS
#   REG_2_URL, REG_2_USER, REG_2_PASS  ... etc.

i=1
while [ "$i" -le 9 ]; do
  eval url="\$REG_${i}_URL"
  eval user="\$REG_${i}_USER"
  eval pass="\$REG_${i}_PASS"

  if [ -n "$url" ] && [ -n "$user" ] && [ -n "$pass" ]; then
    echo "[registry] Logging into $url as $user"
    printf '%s' "$pass" | docker login "$url" -u "$user" --password-stdin \
      && echo "[registry] Login OK: $url" \
      || echo "[registry] Login FAILED: $url (continuing)"
  fi

  i=$((i + 1))
done

exec "$@"
