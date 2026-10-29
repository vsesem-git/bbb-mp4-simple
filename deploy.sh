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
printf 'Готово: %s\nРезультаты: %s\n' "$repo_dir" "$COPY_TO_LOCATION"
