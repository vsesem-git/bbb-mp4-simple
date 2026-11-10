#!/bin/sh
# Adapted from manishkatyan/bbb-mp4 (MIT, see LICENSE.upstream).
# Chrome/Xvfb and x11grab use exactly the selected output size.
set -eu
DURATION=$1
EXPORT_NAME=$2
DISPLAY_NUMBER=$3
case "${BBB_MP4_SIZE:-}" in
    640x360|854x480|1280x720|1280x800|1920x1080|2560x1440|2560x1080|3440x1440) ;;
    *) echo 'Unsupported capture size' >&2; exit 1 ;;
esac

case "${BBB_MP4_CODEC:-h264}" in
    h264) CRF=22; set -- -c:v libx264 ;;
    h265) CRF=26; set -- -c:v libx265 -tag:v hvc1 -x265-params log-level=error ;;
    *) echo 'Unsupported codec' >&2; exit 1 ;;
esac
if [ "$BBB_MP4_VIDEO_BITRATE" = auto ]; then
    set -- "$@" -crf "$CRF"
else
    set -- "$@" -b:v "${BBB_MP4_VIDEO_BITRATE}k"
fi

ffmpeg -y -nostats -progress /usr/src/app/processed/.ffmpeg-progress -draw_mouse 0 -s "$BBB_MP4_SIZE" \
    -framerate "${BBB_MP4_FPS:-30}" -f x11grab -thread_queue_size 8 -i ":$DISPLAY_NUMBER" \
    -f alsa -thread_queue_size 128 -itsoffset 0.57 -i pulse -ac 2 \
    -vf "setsar=1" \
    "$@" \
    -c:a aac -b:a "${BBB_MP4_AUDIO_BITRATE}k" \
    -pix_fmt yuv420p -preset "$BBB_MP4_PRESET" -movflags faststart \
    -t "$DURATION" "/usr/src/app/processing/$EXPORT_NAME.mp4"

echo "Moving video to processed folder"
mv "/usr/src/app/processing/$EXPORT_NAME.mp4" "/usr/src/app/processed/$EXPORT_NAME.mp4"
