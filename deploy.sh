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

docker pull manishkatyan/bbb-mp4

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
trap 'rm -f -- "$unit_file"' EXIT
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
