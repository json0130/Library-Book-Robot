#!/usr/bin/env bash
# Start, stop or check the robot's local AI: llama-server (Qwen3-1.7B) and tools/local_ai_server.py (whisper.cpp + Piper).
#
#   scripts/start_local_ai.sh                     start both, wait until they answer, run the end-to-end check
#   scripts/start_local_ai.sh start --stt small.en    hear better in a noisy room (+0.4 s, +0.2 GB); the default is base.en
#   scripts/start_local_ai.sh stop | restart | status | check
#
# Then run the robot with AI_MODE=local, or set `ai_server.mode: local` in config/robot.yaml (docs/local_ai.md).
# Models and builds are expected in $LOCAL_AI_HOME (default ~/local-ai); nothing is downloaded or built here.
# Settings are the ones measured in docs/local_ai_benchmark.md: the context is 2048 tokens and one slot (a bigger context or
# more slots cost memory the board does not have), everything on the GPU except Piper, and nothing listens beyond this computer.
set -uo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
LOCAL_AI_HOME="${LOCAL_AI_HOME:-$HOME/local-ai}"
LLAMA_BIN="${LLAMA_BIN:-$LOCAL_AI_HOME/src/llama.cpp/build/bin/llama-server}"
LLM_MODEL="${LLM_MODEL:-$LOCAL_AI_HOME/models/Qwen3-1.7B-Q4_K_M.gguf}"
SERVER_PYTHON="${SERVER_PYTHON:-$LOCAL_AI_HOME/venv-piper/bin/python}"    # the venv with piper-tts; the robot itself uses the system python3
LLAMA_PORT="${LLAMA_PORT:-8081}"
WHISPER_PORT="${WHISPER_PORT:-8090}"
TEMPERATURE="${LOCAL_AI_TEMPERATURE:-0.7}"                               # LLM sampling temperature; lower is more literal (docs/local_ai.md)
RUN_DIR="${RUN_DIR:-$LOCAL_AI_HOME/run}"                                 # pid files and logs
STT_MODEL="base.en"
FORCE=0
NEED_MB_BASE=2300                                                        # what the AI needs on top of everything else (docs/local_ai_benchmark.md)

# The address the robot will use, from config/robot.yaml (ai_server.local); LOCAL_AI_HOST / LOCAL_AI_PORT override it.
read -r CFG_HOST CFG_PORT < <(AI_MODE=local python3 -c "from robot.config import load_config; c = load_config(); print(c.ai_host, c.ai_port)" 2>/dev/null || echo "127.0.0.1 7898")
AI_HOST="${LOCAL_AI_HOST:-$CFG_HOST}"
AI_PORT="${LOCAL_AI_PORT:-$CFG_PORT}"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
alive() { [[ -f "$RUN_DIR/$1.pid" ]] && kill -0 "$(cat "$RUN_DIR/$1.pid")" 2>/dev/null; }
mem_available_mb() { awk '/MemAvailable/ {print int($2 / 1024)}' /proc/meminfo; }
swap_used_mb() { awk '/SwapTotal/ {t=$2} /SwapFree/ {f=$2} END {print int((t - f) / 1024)}' /proc/meminfo; }

command="start"
if [[ $# -gt 0 && "$1" != -* ]]; then command="$1"; shift; fi
while [[ $# -gt 0 ]]; do
    case "$1" in
        --stt) STT_MODEL="${2:?--stt needs base.en or small.en}"; shift 2 ;;
        --force) FORCE=1; shift ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done
[[ "$STT_MODEL" == "base.en" || "$STT_MODEL" == "small.en" ]] || die "--stt must be base.en or small.en"
mkdir -p "$RUN_DIR"

wait_for() {          # wait_for NAME SECONDS COMMAND...  (the command succeeds when the service is ready)
    local name="$1" limit="$2"; shift 2
    for ((i = 0; i < limit * 2; i++)); do
        "$@" && return 0
        if ! alive "$name"; then say "  $name exited; its log ($RUN_DIR/$name.log) ends:"; tail -n 8 "$RUN_DIR/$name.log" | sed 's/^/    /'; return 1; fi
        sleep 0.5
    done
    say "  $name did not answer within ${limit} s (log: $RUN_DIR/$name.log)"; return 1
}
llama_ready() { curl -fs "http://127.0.0.1:$LLAMA_PORT/health" 2>/dev/null | grep -q '"ok"'; }
server_ready() { grep -q "^Listening on" "$RUN_DIR/local-ai-server.log" 2>/dev/null; }

check_memory() {
    local need=$NEED_MB_BASE have; have=$(mem_available_mb)
    [[ "$STT_MODEL" == "small.en" ]] && need=$((need + 400))
    say "Memory: ${have} MB available, swap used $(swap_used_mb) MB; the local AI needs about ${need} MB."
    if (( have < need )); then
        say "  Not enough free memory. The biggest users right now:"
        ps -eo rss=,comm= --sort=-rss | head -6 | awk '{printf "    %5d MB  %s\n", $1 / 1024, $2}'
        say "  Close the desktop apps (VS Code, browsers) or boot without the desktop (docs/local_ai.md); --force starts anyway."
        (( FORCE )) || exit 1
    fi
}

do_start() {
    [[ -x "$LLAMA_BIN" ]] || die "llama-server not found at $LLAMA_BIN (set LOCAL_AI_HOME or LLAMA_BIN)"
    [[ -f "$LLM_MODEL" ]] || die "LLM model not found at $LLM_MODEL (set LLM_MODEL)"
    [[ -x "$SERVER_PYTHON" ]] || die "python with piper-tts not found at $SERVER_PYTHON (set SERVER_PYTHON)"
    alive llama-server && alive local-ai-server || check_memory

    if alive llama-server; then say "llama-server already running (pid $(cat "$RUN_DIR/llama-server.pid"))"
    else
        say "Starting llama-server ($(basename "$LLM_MODEL"), 2048-token context, one slot) ..."
        PATH="/usr/local/cuda/bin:$PATH" setsid nohup "$LLAMA_BIN" -m "$LLM_MODEL" -ngl 99 -c 2048 -np 1 -fa on --jinja -t 4 \
            --no-webui --host 127.0.0.1 --port "$LLAMA_PORT" > "$RUN_DIR/llama-server.log" 2>&1 < /dev/null &
        echo $! > "$RUN_DIR/llama-server.pid"
        wait_for llama-server 120 llama_ready || die "llama-server did not start"
    fi

    if alive local-ai-server; then say "local AI server already running (pid $(cat "$RUN_DIR/local-ai-server.pid"))"
    else
        say "Starting the local AI server (whisper $STT_MODEL, Piper) on $AI_HOST:$AI_PORT ..."
        : > "$RUN_DIR/local-ai-server.log"
        setsid nohup "$SERVER_PYTHON" "$ROOT/tools/local_ai_server.py" serve --host "$AI_HOST" --port "$AI_PORT" \
            --llm-url "http://127.0.0.1:$LLAMA_PORT" --stt-model "$STT_MODEL" --whisper-port "$WHISPER_PORT" --temperature "$TEMPERATURE" \
            --log-dir "$RUN_DIR" >> "$RUN_DIR/local-ai-server.log" 2>&1 < /dev/null &
        echo $! > "$RUN_DIR/local-ai-server.pid"
        wait_for local-ai-server 180 server_ready || die "the local AI server did not start"
    fi
    do_check
}

do_check() {
    say "Checking the local AI at $AI_HOST:$AI_PORT ..."
    local failed=0
    "$SERVER_PYTHON" "$ROOT/tools/local_ai_server.py" check --host "$AI_HOST" --port "$AI_PORT" || failed=1
    AI_MODE=local AI_HOST="$AI_HOST" AI_PORT="$AI_PORT" python3 -m robot check || failed=1
    say "Memory now: $(mem_available_mb) MB available, swap used $(swap_used_mb) MB."
    return $failed
}

stop_one() {          # stop_one NAME: TERM, then KILL after 15 s
    local name="$1"
    if alive "$name"; then
        local pid; pid=$(cat "$RUN_DIR/$name.pid")
        kill "$pid" 2>/dev/null
        for ((i = 0; i < 30; i++)); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
        kill -9 "$pid" 2>/dev/null
        say "stopped $name"
    fi
    rm -f "$RUN_DIR/$name.pid"
}

do_stop() {
    stop_one local-ai-server
    pkill -f "whisper-server .*--port $WHISPER_PORT" 2>/dev/null || true       # only if the server died without stopping its child
    stop_one llama-server
}

do_status() {
    for name in llama-server local-ai-server; do
        if alive "$name"; then say "$name: running (pid $(cat "$RUN_DIR/$name.pid"))"; else say "$name: not running"; fi
    done
    say "robot address: $AI_HOST:$AI_PORT | memory: $(mem_available_mb) MB available, swap used $(swap_used_mb) MB"
}

case "$command" in
    start) do_start ;;
    stop) do_stop ;;
    restart) do_stop; do_start ;;
    status) do_status ;;
    check) do_check ;;
    *) die "unknown command $command (start, stop, restart, status, check)" ;;
esac
