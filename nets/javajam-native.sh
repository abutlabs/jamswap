#!/usr/bin/env bash
# JavaJAM as a NATIVE process on the host, for the nets where its Docker images can't
# run: under Docker Desktop on Apple silicon they (and the Linux release zip in a
# container) die with SIGILL in their native crypto libraries (jamswap#2). ./dex uses
# this for every javajam index when JAVAJAM_RUNNER=native (the default on macOS).
#
#   nets/javajam-native.sh fetch
#   nets/javajam-native.sh start STATE SPEC INDEX PORT RPC FINALITY [EXTERNAL_IP]
#   nets/javajam-native.sh stop STATE        stop every JavaJAM node of this net
#   nets/javajam-native.sh status STATE
#
# STATE is the net's state dir (./dex: ~/.cache/jamswap/nets/<net>); node i keeps its
# data, logs and pid in STATE/jj<i>/. SPEC is the shared chain spec, copied out of the
# net's volume. The node listens for JAMNP-S on 0.0.0.0:PORT (its genesis address is
# EXTERNAL_IP:PORT) and serves JIP-2 RPC on 127.0.0.1:RPC.
#
# COMPLIANCE: nothing is redistributed. The release zip (javajamio/javajam-releases,
# published without a license) and a Temurin JRE 25 (Adoptium) are downloaded at run
# time into the user's cache ($JAMSWAP_CACHE, default ~/.cache/jamswap), each checked
# against a pinned or vendor-published sha256. JavaJAM is run as a black box.
set -euo pipefail

CACHE="${JAMSWAP_CACHE:-$HOME/.cache/jamswap}"
HERE="$(cd "$(dirname "$0")" && pwd)"
JJ_RELEASE="${JJ_RELEASE:-0.4.3}"
JRE_RELEASE="${JRE_RELEASE:-jdk-25.0.4.1+1}"
# sha256 of the 0.4.3 release zips (GitHub release-asset digests, 2026-09-26)
JJ_PINS="0.4.3:macos-aarch64:d7e1c3f3110aacfae6a6ec9c017fdd7f185cc890a03e98f5388f2de0a31e4108
0.4.3:macos-x86_64:4dc2d94f80cd25e19c482dd430e1901b81a8a99ab8362cefb641bdb003d54531
0.4.3:linux-aarch64:701d657580933baef81357fe3261548b947b41c8ca4d85ef92702904693ad45f
0.4.3:linux-x86_64:91041dd241e6b143131edd2f1501420bb7c12a62de4dd211aed0f947022bf75a"

case "$(uname -s)" in Darwin) os=macos; jos=mac;; Linux) os=linux; jos=linux;;
  *) echo "javajam-native: unsupported OS $(uname -s)" >&2; exit 1;; esac
case "$(uname -m)" in arm64|aarch64) arch=aarch64;; x86_64|amd64) arch=x86_64;;
  *) echo "javajam-native: unsupported arch $(uname -m)" >&2; exit 1;; esac
jarch=$([ "$arch" = x86_64 ] && echo x64 || echo aarch64)
JJ_DIR="$CACHE/javajam-$JJ_RELEASE-$os-$arch"
JRE_DIR="$CACHE/temurin-$JRE_RELEASE-jre-$jos-$jarch"

sha256() { shasum -a 256 "$1" | awk '{print $1}'; }

fetch() {
  mkdir -p "$CACHE"
  if [ ! -f "$JJ_DIR/javajam-$os-$arch.jar" ]; then
    want=$(echo "$JJ_PINS" | awk -F: -v k="$JJ_RELEASE:$os-$arch" '$1":"$2==k {print $3}')
    url="https://github.com/javajamio/javajam-releases/releases/download/$JJ_RELEASE/javajam-$os-$arch.zip"
    echo "fetching $url"
    curl -fsSL -o "$CACHE/jj.zip.part" "$url"
    got=$(sha256 "$CACHE/jj.zip.part")
    if [ -n "$want" ] && [ "$got" != "$want" ]; then
      echo "javajam-native: sha256 $got != pinned $want" >&2; rm -f "$CACHE/jj.zip.part"; exit 1
    fi
    [ -n "$want" ] || echo "WARNING: no pin for JavaJAM $JJ_RELEASE/$os-$arch (sha256 $got)"
    rm -rf "$JJ_DIR.part" && mkdir -p "$JJ_DIR.part"
    unzip -q "$CACHE/jj.zip.part" -d "$JJ_DIR.part" && rm "$CACHE/jj.zip.part"
    mv "$JJ_DIR.part" "$JJ_DIR"
  fi
  if [ -z "$(java_home 2>/dev/null)" ]; then
    rel=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$JRE_RELEASE")
    meta=$(curl -fsSL "https://api.adoptium.net/v3/assets/release_name/eclipse/$rel?architecture=$jarch&image_type=jre&os=$jos")
    read -r url want < <(echo "$meta" | python3 -c \
      'import sys,json;p=json.load(sys.stdin)["binaries"][0]["package"];print(p["link"],p["checksum"])')
    echo "fetching $url"
    curl -fsSL -o "$CACHE/jre.tgz.part" "$url"
    got=$(sha256 "$CACHE/jre.tgz.part")
    [ "$got" = "$want" ] || { echo "javajam-native: JRE sha256 $got != $want" >&2; exit 1; }
    rm -rf "$JRE_DIR" && mkdir -p "$JRE_DIR"
    tar xzf "$CACHE/jre.tgz.part" -C "$JRE_DIR" && rm "$CACHE/jre.tgz.part"
  fi
  echo "JavaJAM $JJ_RELEASE: $JJ_DIR; JRE: $(java_home)"
}

java_home() {   # the directory holding bin/java (macOS bundles nest it in Contents/Home)
  local j; j=$(find "$JRE_DIR" -maxdepth 5 -path '*/bin/java' -type f 2>/dev/null | head -1)
  [ -n "$j" ] && dirname "$(dirname "$j")"
}

alive() { [ -s "$1/pid" ] && kill -0 "$(cat "$1/pid")" 2>/dev/null; }

start() {
  local state=$1 spec=$2 i=$3 port=$4 rpc=$5 finality=$6 ext=${7:-}
  local dir="$state/jj$i" jh
  mkdir -p "$dir"
  if alive "$dir"; then echo "jj$i already running (pid $(cat "$dir/pid"))"; return; fi
  jh=$(java_home) || { echo "javajam-native: no JRE; run fetch first" >&2; exit 1; }
  args=(run --chain "$spec" --dev-validator "$i" --listen-ip 0.0.0.0 --port "$port"
        --rpc --rpc-port "$rpc" --finality-mode "$finality"
        --data-path "$dir/data" --log-file "$dir/javajam.log")
  [ -n "$ext" ] && args+=(--external-ip "$ext")
  echo "java -jar $JJ_DIR/javajam-$os-$arch.jar ${args[*]}" > "$dir/cmdline"
  # The release's bin/javajam launcher pins -Xms6g -Xmx6g -XX:+AlwaysPreTouch (seen on
  # the java command line it starts) and ignores JAVA_OPTS, so three nodes would commit
  # 18 GB up front. Start the release jar with that same command line instead, minus the
  # pre-touch and NUMA flags, with the heap capped at JAVAJAM_HEAP. From the release dir:
  # it loads its native libraries from there (java.library.path=.:..).
  # nets/supervise.py keeps it running like a container with `restart: unless-stopped`
  # (0.4.3 sometimes shuts itself down right after its first outbound connections; the
  # restart is logged as a [supervise] line), in a session of its own with every fd
  # redirected: a closed terminal or a Ctrl-C of ./dex leaves it running.
  (
    cd "$JJ_DIR"
    nohup python3 "$HERE/supervise.py" "$dir/stdout.log" -- \
      "$jh/bin/java" -Xms256m "-Xmx${JAVAJAM_HEAP:-2g}" -XX:+UseZGC -XX:ReservedCodeCacheSize=512m \
      -XX:+TieredCompilation -Xss512k -XX:+UseFastJNIAccessors -XX:+UseThreadPriorities \
      -XX:+UseSignalChaining -Djava.library.path=.:.. --enable-native-access=ALL-UNNAMED \
      -jar "$JJ_DIR/javajam-$os-$arch.jar" "${args[@]}" > /dev/null 2>&1 < /dev/null &
    echo $! > "$dir/pid"
  )
  echo "jj$i: JavaJAM $JJ_RELEASE native, pid $(cat "$dir/pid"), udp $port, rpc 127.0.0.1:$rpc, log $dir/stdout.log"
}

stop() {
  local state=$1 dir pid
  for dir in "$state"/jj*; do
    [ -d "$dir" ] || continue
    if alive "$dir"; then
      pid=$(cat "$dir/pid")
      kill "$pid" 2>/dev/null || true               # the supervisor forwards it to the node
      for _ in $(seq 1 25); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    fi
    pkill -f -- "--data-path $dir/data" 2>/dev/null || true   # anything left over
    rm -f "$dir/pid"
    echo "stopped $(basename "$dir")"
  done
}

status() {
  local state=$1 dir
  for dir in "$state"/jj*; do
    [ -d "$dir" ] || continue
    if alive "$dir"; then echo "$(basename "$dir") running (pid $(cat "$dir/pid"))"
    else echo "$(basename "$dir") stopped"; fi
  done
}

cmd=${1:-help}; shift || true
case "$cmd" in
  fetch) fetch ;;
  start) [ $# -ge 6 ] || { echo "usage: $0 start STATE SPEC INDEX PORT RPC FINALITY [EXTERNAL_IP]" >&2; exit 2; }
         start "$@" ;;
  stop) stop "${1:?STATE}" ;;
  status) status "${1:?STATE}" ;;
  *) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0" ;;
esac
