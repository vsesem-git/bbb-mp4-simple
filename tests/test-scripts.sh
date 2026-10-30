#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/repo with spaces" "$tmp/home"
cp "$root/convert.sh" "$root/deploy.sh" "$root/web.py" "$root/web.html" "$root/ffmpeg-cmd.sh" "$root/bbb-mp4.js" "$root/operations.py" "$tmp/repo with spaces/"
git -C "$tmp/repo with spaces" init -q
cat > "$tmp/bin/docker" <<'MOCK'
#!/usr/bin/env bash
printf '%s\0' "$@" > "$DOCKER_ARGS"
if [[ ${1:-} == image && ${2:-} == inspect ]]; then printf 'manishkatyan/bbb-mp4@sha256:%064d\n' 0; fi
exit "${DOCKER_EXIT:-0}"
MOCK
chmod +x "$tmp/bin/docker"
# Do not alter the test host's systemd installation.
cat > "$tmp/bin/sudo" <<'MOCK'
#!/usr/bin/env bash
exec "$@"
MOCK
cat > "$tmp/bin/install" <<'MOCK'
#!/usr/bin/env bash
cp "$3" "$UNIT_COPY"
MOCK
cat > "$tmp/bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
MOCK
chmod +x "$tmp/bin/sudo" "$tmp/bin/install" "$tmp/bin/systemctl"
export UNIT_COPY="$tmp/bbb-mp4-web.service" SYSTEMCTL_LOG="$tmp/systemctl-log"
export PATH="$tmp/bin:$PATH" HOME="$tmp/home" DOCKER_ARGS="$tmp/docker-args"
repo="$tmp/repo with spaces"
sed -i 's/127.0.0.1:8080/127.0.0.1:1/g' "$repo/deploy.sh"

bash "$repo/deploy.sh"
[[ -d "$repo/recordings" && -x "$repo/convert.sh" ]]
grep -Fx "WorkingDirectory=$repo" "$UNIT_COPY" >/dev/null
grep -F 'ExecStart=/usr/bin/python3 "'"$repo"'/web.py"' "$UNIT_COPY" >/dev/null
grep -Fx 'enable bbb-mp4-web.service' "$SYSTEMCTL_LOG" >/dev/null
grep -Fx 'restart bbb-mp4-web.service' "$SYSTEMCTL_LOG" >/dev/null
# Parse the actual generated unit too; mocked systemctl alone misses syntax errors.
if command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze verify "$UNIT_COPY"
fi
python3 - "$DOCKER_ARGS" <<'PY'
import sys
assert open(sys.argv[1], 'rb').read().split(b'\0')[:2] == [b'image', b'inspect']
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
assert [a.decode() for a in args] == ['run', '--rm', '-v', str(__import__('pathlib').Path(sys.argv[2]).parent / 'repo with spaces' / 'bbb-mp4.js') + ':/usr/src/app/bbb-mp4.js:ro', '-v', sys.argv[2] + ':/usr/src/app/processed', '--env', 'REC_URL=' + sys.argv[3], 'manishkatyan/bbb-mp4@sha256:' + '0'*64]
PY
BBB_MP4_CONTAINER_NAME="bbb-mp4-11111111111111111111111111111111" BBB_MP4_SIZE=1280x720 \
BBB_MP4_VIDEO_BITRATE=4000 BBB_MP4_AUDIO_BITRATE=192 BBB_MP4_PRESET=slow bash "$repo/convert.sh" "$url"
python3 - "$DOCKER_ARGS" "$repo" <<'PYARGS'
import sys
args = [a.decode() for a in open(sys.argv[1], 'rb').read().split(b'\0')[:-1]]
assert '--name' in args and 'bbb-mp4-11111111111111111111111111111111' in args
assert sys.argv[2] + '/ffmpeg-cmd.sh:/usr/src/app/ffmpeg-cmd.sh:ro' in args
assert all(x in args for x in ['BBB_MP4_SIZE=1280x720','BBB_MP4_VIDEO_BITRATE=4000','BBB_MP4_AUDIO_BITRATE=192','BBB_MP4_PRESET=slow'])
PYARGS
BBB_MP4_SIZE=1280x800 BBB_MP4_VIDEO_BITRATE=auto BBB_MP4_AUDIO_BITRATE=128 BBB_MP4_PRESET=medium BBB_MP4_CODEC=h265 bash "$repo/convert.sh" "$url"
python3 - "$DOCKER_ARGS" <<'PYARGS'
import sys
args = [a.decode() for a in open(sys.argv[1], 'rb').read().split(b'\0')[:-1]]
assert 'BBB_MP4_VIDEO_BITRATE=auto' in args and 'BBB_MP4_CODEC=h265' in args
PYARGS
rm "$DOCKER_ARGS"
if BBB_MP4_SIZE=1280x720 BBB_MP4_VIDEO_BITRATE=auto BBB_MP4_AUDIO_BITRATE=128 BBB_MP4_PRESET=medium BBB_MP4_CODEC=unsafe bash "$repo/convert.sh" "$url"; then echo 'Invalid codec accepted' >&2; exit 1; fi
[[ ! -e $DOCKER_ARGS ]]
if BBB_MP4_SIZE=1280x720 BBB_MP4_VIDEO_BITRATE=0 BBB_MP4_AUDIO_BITRATE=192 BBB_MP4_PRESET=slow bash "$repo/convert.sh" "$url"; then echo 'Invalid bitrate accepted' >&2; exit 1; fi
[[ ! -e $DOCKER_ARGS ]]
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
    cp "$TEST_ROOT/convert.sh" "$TEST_ROOT/deploy.sh" "$TEST_ROOT/web.py" "$TEST_ROOT/web.html" "$TEST_ROOT/ffmpeg-cmd.sh" "$TEST_ROOT/bbb-mp4.js" "$TEST_ROOT/operations.py" "$dest/"
    sed -i 's/127.0.0.1:8080/127.0.0.1:1/g' "$dest/deploy.sh"
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
