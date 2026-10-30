#!/usr/bin/env bash
set -euo pipefail

repo_url=https://github.com/vsesem-git/bbb-mp4-simple.git
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

# When run from an existing checkout, configure that checkout. A standalone
# downloaded deploy.sh bootstraps the repository into the user's home.
if [[ -f $script_dir/convert.sh ]] && git -C "$script_dir" rev-parse --show-toplevel >/dev/null 2>&1; then
    repo_dir=$script_dir
else
    repo_dir=$HOME/bbb-mp4-simple
    if [[ ! -e $repo_dir ]]; then
        git clone -- "$repo_url" "$repo_dir"
    elif [[ ! -f $repo_dir/convert.sh ]] || ! git -C "$repo_dir" rev-parse --show-toplevel >/dev/null 2>&1; then
        echo "Путь $repo_dir уже существует и не является checkout bbb-mp4-simple." >&2
        exit 1
    fi
fi

# Do not overwrite local configuration or update an existing checkout.
if [[ ! -e $repo_dir/.env ]]; then
    printf 'COPY_TO_LOCATION=%q\n' "$repo_dir/recordings" > "$repo_dir/.env"
fi
unset COPY_TO_LOCATION
source "$repo_dir/.env"
if [[ ${COPY_TO_LOCATION:-} != /* || $COPY_TO_LOCATION == *:* ]]; then
    echo 'COPY_TO_LOCATION должен быть абсолютным путём без двоеточия.' >&2
    exit 1
fi
mkdir -p -- "$COPY_TO_LOCATION"
chmod +x "$repo_dir/deploy.sh" "$repo_dir/convert.sh"

# Pause queue intake and refuse to interrupt an active conversion. Older
# versions have no maintenance endpoint, so check their saved state and ask
# operators not to submit new jobs during this first migration.
maintenance=0
unit_file=
cleanup() {
    [[ -z ${unit_file:-} ]] || rm -f -- "$unit_file"
    if [[ $maintenance == 1 ]]; then
        python3 -c 'import urllib.request; r=urllib.request.Request("http://127.0.0.1:8080/maintenance", b"{\"enabled\":false}", {"Content-Type":"application/json"}); urllib.request.urlopen(r,timeout=3).close()' 2>/dev/null || true
    fi
}
trap cleanup EXIT
if python3 - "$repo_dir/state/jobs.json" <<'PYMAINT'
import json, sys, urllib.request, urllib.error
request = urllib.request.Request('http://127.0.0.1:8080/maintenance', b'{"enabled":true}', {'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(request, timeout=3) as response:
        if response.status != 200: raise RuntimeError('Не удалось приостановить очередь')
except urllib.error.HTTPError as exc:
    if exc.code != 404:
        print('Идёт конвертация или сервис отклонил обновление. Дождитесь завершения.', file=sys.stderr)
        sys.exit(1)
    # Older service: no atomic pause; require an empty queue and no submissions.
    try:
        jobs = json.load(open(sys.argv[1], encoding='utf-8'))
    except FileNotFoundError:
        jobs = []
    if any(j.get('status') in ('running','stopping','queued') for j in jobs):
        print('Очередь не пуста. Дождитесь завершения перед первым обновлением.', file=sys.stderr); sys.exit(1)
    print('Старая версия сервиса: не добавляйте задания во время обновления.', file=sys.stderr)
    sys.exit(2)
except urllib.error.URLError:
    try:
        jobs = json.load(open(sys.argv[1], encoding='utf-8'))
    except FileNotFoundError:
        jobs = []
    if any(j.get('status') in ('running','stopping') for j in jobs):
        print('В состоянии есть активная конвертация. Проверьте сервис перед обновлением.', file=sys.stderr); sys.exit(1)
    sys.exit(2)
PYMAINT
then
    maintenance=1
else
    maintenance_result=$?
    [[ $maintenance_result == 2 ]] || exit "$maintenance_result"
fi

# Pin the currently installed image on first migration. This inspects image
# metadata, not Docker installation or daemon configuration.
if [[ $# -gt 1 || ( $# -eq 1 && $1 != --update-image ) ]]; then
    echo 'Использование: ./deploy.sh [--update-image]' >&2; exit 1
fi
mkdir -p "$repo_dir/state"
chmod 700 "$repo_dir/state"
image_file=$repo_dir/state/image.ref
image_pattern='^manishkatyan/bbb-mp4@sha256:[a-f0-9]{64}$'
image_digest() {
    docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' manishkatyan/bbb-mp4:latest 2>/dev/null |
        grep -E "$image_pattern" | head -n 1 || true
}
if [[ ${1:-} == --update-image ]]; then
    docker pull manishkatyan/bbb-mp4:latest
    image=$(image_digest)
elif [[ -f $image_file ]]; then
    IFS= read -r image < "$image_file" || true
    [[ $image =~ $image_pattern ]] || { echo 'Некорректный state/image.ref' >&2; exit 1; }
    docker pull "$image"
else
    image=$(image_digest)
    if [[ -z $image ]]; then
        docker pull manishkatyan/bbb-mp4:latest
        image=$(image_digest)
    fi
fi
[[ $image =~ $image_pattern ]] || { echo 'Не удалось определить digest образа; закрепление отменено.' >&2; exit 1; }
tmp_image=$(mktemp "$repo_dir/state/.image.ref.XXXXXX")
printf '%s\n' "$image" > "$tmp_image"
chmod 600 "$tmp_image"
mv -f "$tmp_image" "$image_file"
printf 'Образ закреплён: %s\n' "$image"

# Install the web service as the deploying user (who must have Docker access).
# Quoted systemd values need escaping independently of shell quoting.
if [[ $repo_dir == *$'\n'* || $repo_dir == *$'\r'* ]]; then
    echo 'Путь репозитория не должен содержать переводы строк.' >&2
    exit 1
fi
systemd_escape() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//%/%%}
    printf '%s' "$value"
}
unit_file=$(mktemp)
# WorkingDirectory is a literal path, not an ExecStart-style quoted argument.
# Only systemd specifiers require escaping here.
working_dir=${repo_dir//%/%%}
escaped_script=$(systemd_escape "$repo_dir/web.py")
cat > "$unit_file" <<EOF
[Unit]
Description=BBB MP4 web interface
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$working_dir
ExecStart=/usr/bin/python3 "$escaped_script"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
if [[ $(id -u) -eq 0 ]]; then
    privileged=()
else
    privileged=(sudo)
fi
"${privileged[@]}" install -m 644 "$unit_file" /etc/systemd/system/bbb-mp4-web.service
"${privileged[@]}" systemctl daemon-reload
"${privileged[@]}" systemctl enable bbb-mp4-web.service
"${privileged[@]}" systemctl restart bbb-mp4-web.service
printf 'Веб-интерфейс: http://<IP-VM>:8080 (только для доверенной сети)\n'
printf 'Готово: %s\nРезультаты: %s\n' "$repo_dir" "$COPY_TO_LOCATION"
