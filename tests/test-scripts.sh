#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/repo with spaces" "$tmp/home"
cp "$root/convert.sh" "$root/deploy.sh" "$tmp/repo with spaces/"
git -C "$tmp/repo with spaces" init -q
cat > "$tmp/bin/docker" <<'MOCK'
#!/usr/bin/env bash
printf '%s\0' "$@" > "$DOCKER_ARGS"
exit "${DOCKER_EXIT:-0}"
MOCK
chmod +x "$tmp/bin/docker"
export PATH="$tmp/bin:$PATH" HOME="$tmp/home" DOCKER_ARGS="$tmp/docker-args"
repo="$tmp/repo with spaces"

bash "$repo/deploy.sh"
[[ -d "$repo/recordings" && -x "$repo/convert.sh" ]]
python3 - "$DOCKER_ARGS" <<'PY'
import sys
assert open(sys.argv[1], 'rb').read().split(b'\0') == [b'pull', b'manishkatyan/bbb-mp4', b'']
PY
# Existing configuration and files must survive repeated deployment.
printf 'COPY_TO_LOCATION=%q\n' "$tmp/other recordings" > "$repo/.env"
echo keep > "$repo/recordings/existing.mp4"
bash "$repo/deploy.sh"
[[ -d "$tmp/other recordings" && $(cat "$repo/recordings/existing.mp4") == keep ]]

url='https://vks.vsesem.ru/playback/presentation/2.3/f678f4a23ecaf2ac77135e744f2c9da8764c32dd-1793270594439?token=a&x=b#test'
bash "$repo/convert.sh" "$url"
python3 - "$DOCKER_ARGS" "$tmp/other recordings" "$url" <<'PY'
import sys
args = open(sys.argv[1], 'rb').read().split(b'\0')[:-1]
assert [a.decode() for a in args] == ['run', '--rm', '-v', sys.argv[2] + ':/usr/src/app/processed', '--env', 'REC_URL=' + sys.argv[3], 'manishkatyan/bbb-mp4']
PY
rm "$DOCKER_ARGS"
for invalid in '' 'https://vks.vsesem.ru/other' 'https://vks.vsesem.ru/playback/presentation/2.3/../../x' 'https://vks.vsesem.ru/playback/presentation/2.3/f678f4a23ecaf2ac77135e744f2c9da8764c32dd-1793270594439/extra'; do
    if bash "$repo/convert.sh" "$invalid"; then echo 'Invalid URL accepted' >&2; exit 1; fi
    [[ ! -e $DOCKER_ARGS ]]
done
if bash "$repo/convert.sh" "$url" extra; then echo 'Extra argument accepted' >&2; exit 1; fi
if DOCKER_EXIT=42 bash "$repo/convert.sh" "$url"; then echo 'Docker failure ignored' >&2; exit 1; else [[ $? == 42 ]]; fi
# Exercise the standalone bootstrap without contacting GitHub.
mkdir -p "$tmp/bootstrap" "$tmp/bootstrap-home"
cp "$root/deploy.sh" "$tmp/bootstrap/deploy.sh"
real_git=$(command -v git)
export REAL_GIT="$real_git" TEST_ROOT="$root" GIT_CLONE_LOG="$tmp/clone-log"
cat > "$tmp/bin/git" <<'MOCK'
#!/usr/bin/env bash
if [[ $1 == clone ]]; then
    printf '%s\n' "$*" >> "$GIT_CLONE_LOG"
    dest=${@: -1}
    mkdir -p "$dest"
    cp "$TEST_ROOT/convert.sh" "$TEST_ROOT/deploy.sh" "$dest/"
    "$REAL_GIT" -C "$dest" init -q
else
    exec "$REAL_GIT" "$@"
fi
MOCK
chmod +x "$tmp/bin/git"
HOME="$tmp/bootstrap-home" bash "$tmp/bootstrap/deploy.sh"
HOME="$tmp/bootstrap-home" bash "$tmp/bootstrap/deploy.sh"
[[ $(wc -l < "$GIT_CLONE_LOG") == 1 ]]
[[ -f "$tmp/bootstrap-home/bbb-mp4-simple/.env" ]]
[[ -d "$tmp/bootstrap-home/bbb-mp4-simple/recordings" ]]
echo 'All tests passed.' 
