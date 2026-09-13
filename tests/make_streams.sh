#!/bin/bash
# Builds the HLS/DASH fixtures for stream_test_cdp.py with ffmpeg: a 4 s test pattern with a tone as
#   hls_ts/index.m3u8      plain MPEG-TS media playlist (audio muxed in)
#   hls_aes/index.m3u8     the same, AES-128 encrypted (clear key served next to it)
#   hls_fmp4/master.m3u8   fMP4 master: video variants + a separate audio rendition group
#   dash/stream.mpd        static MPD, video + audio adaptation sets (SegmentTemplate + timeline)
# Usage: tests/make_streams.sh <out-dir>
set -e
OUT="${1:?out dir}"; FF="${FFMPEG:-$(command -v ffmpeg || echo /opt/homebrew/bin/ffmpeg)}"
SRC=(-f lavfi -i "testsrc=size=320x240:rate=25:duration=4" -f lavfi -i "sine=frequency=440:duration=4")
ENC=(-c:v libx264 -preset ultrafast -pix_fmt yuv420p -g 25 -c:a aac -b:a 64k -shortest)
mkdir -p "$OUT"/hls_ts "$OUT"/hls_aes "$OUT"/hls_fmp4 "$OUT"/dash
"$FF" -y -loglevel error "${SRC[@]}" "${ENC[@]}" -f hls -hls_time 1 -hls_playlist_type vod "$OUT/hls_ts/index.m3u8"
head -c 16 /dev/urandom > "$OUT/hls_aes/key.bin"
printf 'key.bin\n%s/hls_aes/key.bin\n' "$OUT" > "$OUT/hls_aes/key.info"
"$FF" -y -loglevel error "${SRC[@]}" "${ENC[@]}" -f hls -hls_time 1 -hls_playlist_type vod -hls_key_info_file "$OUT/hls_aes/key.info" "$OUT/hls_aes/index.m3u8"
"$FF" -y -loglevel error "${SRC[@]}" -map 0:v -map 0:v -map 1:a -c:v libx264 -preset ultrafast -pix_fmt yuv420p -g 25 \
  -filter:v:1 scale=160:120 -c:a aac -b:a 64k -shortest -f hls -hls_time 1 -hls_playlist_type vod -hls_segment_type fmp4 \
  -var_stream_map "a:0,agroup:aud,default:yes v:0,agroup:aud v:1,agroup:aud" -master_pl_name master.m3u8 "$OUT/hls_fmp4/v%v.m3u8"
"$FF" -y -loglevel error "${SRC[@]}" "${ENC[@]}" -f dash -seg_duration 1 -use_timeline 1 -use_template 1 "$OUT/dash/stream.mpd"
echo "fixtures in $OUT"
