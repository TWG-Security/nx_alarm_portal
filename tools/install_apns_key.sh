#!/bin/bash
# Install Apple's APNs key for the iPhone app's notifications (app/apns.py). Run once the .p8 is on the server:
#   tools/install_apns_key.sh ~/AuthKey_ABCDE12345.p8
# It moves the key into secrets/ next to .env (mode 600, owned by the container's app user 10001 so only the portal
# can read it; secrets/ is mounted read-only at /app/secrets, is in .gitignore and is NOT in tools/backup.sh: keep a
# copy of the .p8 in your password manager), sets APNS_KEY_PATH / APNS_KEY_ID / APNS_TEAM_ID / APNS_TOPIC in .env,
# then deploys (tools/deploy.sh) so the portal picks it up. The Key ID comes from the file name (AuthKey_<KEYID>.p8).
set -euo pipefail
cd "$(dirname "$0")/.."
src="${1:?usage: tools/install_apns_key.sh /path/to/AuthKey_<KEYID>.p8}"
name=$(basename "$src")
[[ "$name" =~ ^AuthKey_([A-Z0-9]{10})\.p8$ ]] || { echo "Expected a file named AuthKey_<10-character Key ID>.p8, got $name"; exit 1; }
key_id="${BASH_REMATCH[1]}"
grep -q "BEGIN PRIVATE KEY" "$src" || { echo "$src doesn't look like an Apple .p8 key"; exit 1; }
mkdir -p secrets && chmod 711 secrets
mv "$src" "secrets/$name"
# Readable only by the container's app user (uid 10001); docker does the chown, no sudo needed.
docker run --rm -v "$PWD/secrets:/s" alpine:3.20 sh -c "chown 10001:10001 /s/$name && chmod 600 /s/$name"
python3 - "$key_id" "$name" <<'PY'
import os, sys
key_id, name = sys.argv[1], sys.argv[2]
want = {"APNS_KEY_PATH": f"/app/secrets/{name}", "APNS_KEY_ID": key_id, "APNS_TEAM_ID": "NY8H9358QD",
        "APNS_TOPIC": "com.twgsecurity.alarms"}
lines = open(".env").read().splitlines()
out, seen = [], set()
for l in lines:
    k = l.split("=", 1)[0].strip()
    if k in want:
        out.append(f"{k}={want[k]}"); seen.add(k)
    else:
        out.append(l)
out += [f"{k}={v}" for k, v in want.items() if k not in seen]
mode = os.stat(".env").st_mode & 0o777
open(".env", "w").write("\n".join(out) + "\n")
os.chmod(".env", mode)
print("set in .env:", ", ".join(f"{k}={v}" for k, v in want.items()))
PY
ls -ln "secrets/$name" | awk '{print "key:", $1, "owner uid", $3, $NF}'
echo "Deploying so the portal reads the key..."
exec tools/deploy.sh
