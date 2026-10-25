#!/bin/bash
# Install the App Store Connect API key for TestFlight invites (app/appstore.py). Run once the .p8 is on the server:
#   tools/install_asc_key.sh ~/AuthKey_ABCDE12345.p8
# Like tools/install_apns_key.sh: moves the key into secrets/ next to .env as ASC_AuthKey_<KEYID>.p8 (mode 600, owned
# by the container's app user 10001; secrets/ is mounted read-only at /app/secrets, is in .gitignore and is NOT in
# tools/backup.sh: keep a copy of the .p8 in your password manager), sets ASC_KEY_PATH / ASC_KEY_ID in .env, then
# deploys (tools/deploy.sh). The Key ID comes from the file name (AuthKey_<KEYID>.p8). The Issuer ID, App ID and beta
# group go on the Platform page (Phone apps).
set -euo pipefail
cd "$(dirname "$0")/.."
src="${1:?usage: tools/install_asc_key.sh /path/to/AuthKey_<KEYID>.p8}"
name=$(basename "$src")
[[ "$name" =~ ^AuthKey_([A-Z0-9]{10})\.p8$ ]] || { echo "Expected a file named AuthKey_<10-character Key ID>.p8, got $name"; exit 1; }
key_id="${BASH_REMATCH[1]}"
grep -q "BEGIN PRIVATE KEY" "$src" || { echo "$src doesn't look like an Apple .p8 key"; exit 1; }
dest="ASC_$name"
mkdir -p secrets && chmod 711 secrets
mv "$src" "secrets/$dest"
docker run --rm -v "$PWD/secrets:/s" alpine:3.20 sh -c "chown 10001:10001 /s/$dest && chmod 600 /s/$dest"
python3 - "$key_id" "$dest" <<'PY'
import os, sys
key_id, name = sys.argv[1], sys.argv[2]
want = {"ASC_KEY_PATH": f"/app/secrets/{name}", "ASC_KEY_ID": key_id}
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
ls -ln "secrets/$dest" | awk '{print "key:", $1, "owner uid", $3, $NF}'
echo "Deploying so the portal reads the key..."
exec tools/deploy.sh
