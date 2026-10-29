#!/bin/sh
# Adapted from manishkatyan/bbb-mp4 (MIT, see LICENSE.upstream).
# Chrome/Xvfb still uses the upstream 1280x800 display; scale/pad the output.
set -eu
DURATION=$1
EXPORT_NAME=$2
DISPLAY_NUMBER=$3
WIDTH=${BBB_MP4_SIZE%x*}
HEIGHT=${BBB_MP4_SIZE#*x}

ffmpeg -y -nostats -draw_mouse 0 -s 1280x800 \
    -framerate 30 -f x11grab -thread_queue_size 1024 -i ":$DISPLAY_NUMBER" \
    -f alsa -thread_queue_size 1024 -itsoffset 0.57 -i pulse -ac 2 \
    -vf "scale=${WIDTH}:${HEIGHT}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=${WIDTH}:${HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1" \
    -c:v libx264 -b:v "${BBB_MP4_VIDEO_BITRATE}k" \
    -c:a aac -b:a "${BBB_MP4_AUDIO_BITRATE}k" \
    -pix_fmt yuv420p -preset "$BBB_MP4_PRESET" -movflags faststart \
    -t "$DURATION" "/usr/src/app/processing/$EXPORT_NAME.mp4"

echo "Moving video to processed folder"
mv "/usr/src/app/processing/$EXPORT_NAME.mp4" "/usr/src/app/processed/$EXPORT_NAME.mp4"
