#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Chutes Deploy Script
# =============================================================================
# Interactive deployment tool with optional flags for automation
# GUIDES: https://chutes.ai/docs/getting-started/first-chute
#         https://github.com/chutesai/chutes?tab=readme-ov-file#-deploying-a-chute
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
CHUTES_CONFIG="$HOME/.chutes/config.ini"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Flags
SHOW_HELP=false
LIST_IMAGES=false
LIST_CHUTES=false
BUILD_MODULE=""
DEPLOY_MODULE=""
STATUS_CHUTE=""
RUN_MODULE=""
RUN_DOCKER_MODULE=""
DELETE_CHUTE=""
LOGS_CHUTE=""
DISCOVER_MODULE=""
LOCAL_BUILD=false
ACCEPT_FEE=false
PUBLIC_DEPLOY=false
DEV_MODE=false
DEBUG_MODE=false
PORT=8000

DEFAULT_STARTUP_DELAY=180
DEFAULT_PROBE_TIMEOUT=60
DEFAULT_DISCOVER_GPUS="all"

# =============================================================================
# Helper Functions
# =============================================================================

print_header() {
    echo -e "\n${CYAN}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║${NC} ${BLUE}$1${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════════════════════════╝${NC}\n"
}

print_info() {
    echo -e "${BLUE}ℹ${NC}  $1"
}

print_success() {
    echo -e "${GREEN}✓${NC}  $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC}  $1"
}

print_error() {
    echo -e "${RED}✗${NC}  $1"
}

print_cmd() {
    echo -e "${YELLOW}\$${NC} $1"
}

confirm() {
    local prompt="$1"
    local default="${2:-n}"
    
    if [[ "$default" == "y" ]]; then
        read -rp "$prompt [Y/n]: " response
        [[ -z "$response" || "$response" =~ ^[Yy] ]]
    else
        read -rp "$prompt [y/N]: " response
        [[ "$response" =~ ^[Yy] ]]
    fi
}

ensure_venv() {
    if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
        print_error "Virtual environment not found. Run setup.sh first."
        exit 1
    fi
    source "$VENV_DIR/bin/activate"
}

ensure_chutes_config() {
    if [[ ! -f "$CHUTES_CONFIG" ]]; then
        print_error "Chutes config not found at $CHUTES_CONFIG"
        print_info "Run setup.sh to register with Chutes first."
        exit 1
    fi
}

get_username() {
    grep -E "^username\s*=" "$CHUTES_CONFIG" 2>/dev/null | cut -d'=' -f2 | tr -d ' '
}

show_usage() {
    cat << EOF
${CYAN}Chutes Deploy Script${NC}

${YELLOW}Usage:${NC}
  ./deploy.sh [OPTIONS]

${YELLOW}Options:${NC}
  -h, --help              Show this help message
  
  ${BLUE}Listing:${NC}
  --list-images           List all built images
  --list-chutes           List all deployed chutes
  --status NAME           Get status of a specific chute
  
  ${BLUE}Building:${NC}
  --build MODULE          Build a chute (e.g., deploy_xtts_whisper)
  --discover MODULE       Run route discovery (generate routes manifest)
  --local                 Build locally (no remote upload)
  
  ${BLUE}Running:${NC}
  --run-docker MODULE     Run built image in Docker with GPU
  --run MODULE            Run chute in dev mode (on host)
  --dev                   Enable dev mode
  --port PORT             Port for local run (default: 8000)
  
  ${BLUE}Deploying:${NC}
  --deploy MODULE         Deploy a chute
  --accept-fee            Accept deployment fees
  --public                Make deployment public
  
  ${BLUE}Management:${NC}
  --delete NAME           Delete a chute (interactive confirmation)
  --logs NAME             Check instance logs for a chute
  --debug                 Enable debug output

${YELLOW}Available Modules:${NC}
$(list_modules_quiet)

${YELLOW}Examples:${NC}
  ./deploy.sh                                    # Interactive mode
  ./deploy.sh --list-chutes                      # List deployed chutes
  ./deploy.sh --build deploy_xtts_whisper --local
  ./deploy.sh --run-docker deploy_xtts_whisper    # Run in Docker with GPU
  ./deploy.sh --run deploy_xtts_whisper           # Run on host (dev mode)
  ./deploy.sh --deploy deploy_xtts_whisper --accept-fee
  ./deploy.sh --status xtts-whisper

EOF
}

# =============================================================================
# Module Discovery
# =============================================================================

list_modules_quiet() {
    for f in "$SCRIPT_DIR"/deploy_*.py; do
        if [[ -f "$f" ]]; then
            basename "$f" .py
        fi
    done
}

list_modules() {
    print_header "Available Deploy Modules"
    local i=1
    for f in "$SCRIPT_DIR"/deploy_*.py; do
        if [[ -f "$f" ]]; then
            local name=$(basename "$f" .py)
            echo -e "  ${GREEN}$i)${NC} $name"
            i=$((i + 1))
        fi
    done
    echo ""
}

select_module() {
    local prompt="${1:-Select module}"
    local modules=()
    
    for f in "$SCRIPT_DIR"/deploy_*.py; do
        if [[ -f "$f" ]]; then
            modules+=("$(basename "$f" .py)")
        fi
    done
    
    if [[ ${#modules[@]} -eq 0 ]]; then
        print_error "No deploy_*.py modules found"
        return 1
    fi
    
    # Output menu to stderr so it doesn't get captured
    echo "" >&2
    local i=1
    for m in "${modules[@]}"; do
        echo -e "  ${GREEN}$i)${NC} $m" >&2
        i=$((i + 1))
    done
    echo "" >&2
    
    read -rp "$prompt (1-${#modules[@]}): " choice
    
    if [[ "$choice" =~ ^[0-9]+$ ]] && [[ "$choice" -ge 1 ]] && [[ "$choice" -le ${#modules[@]} ]]; then
        echo "${modules[$((choice - 1))]}"
        return 0
    else
        print_error "Invalid selection"
        return 1
    fi
}

run_route_discovery_for_module() {
    local module="$1"
    local chute_file="$SCRIPT_DIR/${module}.py"
    local tool_path="$SCRIPT_DIR/tools/discover_routes.py"

    if [[ ! -f "$chute_file" ]]; then
        print_error "Module file not found: $chute_file"
        return 1
    fi
    if [[ ! -f "$tool_path" ]]; then
        print_error "Route discovery tool not found at $tool_path"
        return 1
    fi

    print_header "Route Discovery: $module"
    print_info "This will run the base image and probe HTTP routes automatically."
    print_info "Press Enter to accept defaults."
    echo ""

    local startup_delay
    local probe_timeout
    local docker_gpus

    read -rp "Startup delay seconds [${DEFAULT_STARTUP_DELAY}]: " startup_delay
    startup_delay=${startup_delay:-$DEFAULT_STARTUP_DELAY}

    read -rp "Probe timeout seconds [${DEFAULT_PROBE_TIMEOUT}]: " probe_timeout
    probe_timeout=${probe_timeout:-$DEFAULT_PROBE_TIMEOUT}

    read -rp "Docker --gpus value [${DEFAULT_DISCOVER_GPUS}]: " docker_gpus
    docker_gpus=${docker_gpus:-$DEFAULT_DISCOVER_GPUS}

    ensure_venv
    pushd "$SCRIPT_DIR" >/dev/null || return 1

    local cmd=(python tools/discover_routes.py --chute-file "$chute_file" --startup-delay "$startup_delay" --probe-timeout "$probe_timeout")
    if [[ -n "$docker_gpus" ]]; then
        cmd+=(--docker-gpus "$docker_gpus")
    fi

    print_cmd "${cmd[*]}"
    echo ""
    "${cmd[@]}"
    local status=$?
    popd >/dev/null || true

    if [[ $status -eq 0 ]]; then
        local manifest="$SCRIPT_DIR/${module}.routes.json"
        print_success "Route discovery complete."
        if [[ -f "$manifest" ]]; then
            print_info "Manifest saved to $manifest"
        fi
    else
        print_error "Route discovery failed (exit $status)"
    fi
    return $status
}

prompt_route_discovery_before_build() {
    local module="$1"
    echo ""
    if confirm "Run route discovery for $module before building?"; then
        if ! run_route_discovery_for_module "$module"; then
            if ! confirm "Discovery failed. Continue to build anyway?"; then
                return 1
            fi
        fi
    fi
    return 0
}

# =============================================================================
# Chutes Operations
# =============================================================================

do_list_images() {
    print_header "Built Images"
    ensure_venv
    print_cmd "chutes images list"
    echo ""
    chutes images list || print_warning "No images found or error listing"
}

do_list_chutes() {
    print_header "Deployed Chutes"
    ensure_venv
    print_cmd "chutes chutes list"
    echo ""
    chutes chutes list || print_warning "No chutes found or error listing"
}

do_chute_status() {
    local chute_name="$1"
    print_header "Chute Status: $chute_name"
    ensure_venv
    print_cmd "chutes chutes get \"$chute_name\""
    echo ""
    chutes chutes get "$chute_name" || print_error "Could not get chute status"
}

list_chute_names_from_modules() {
    for f in "$SCRIPT_DIR"/deploy_*.py; do
        if [[ -f "$f" ]]; then
            local name
            name=$(grep -oP "CHUTE_NAME\s*=\s*['\"]\\K[^'\"]*" "$f" 2>/dev/null | head -1)
            if [[ -z "$name" ]]; then
                name=$(basename "$f" .py)
                name="${name#deploy_}"
            fi
            [[ -n "$name" ]] && echo "$name"
        fi
    done
}

module_to_chute_name() {
    local module="$1"
    local py_file="$SCRIPT_DIR/${module}.py"
    local chute_name=""

    if [[ -f "$py_file" ]]; then
        chute_name=$(grep -oP "CHUTE_NAME\s*=\s*['\"]\\K[^'\"]*" "$py_file" 2>/dev/null | head -1)
    fi

    if [[ -z "$chute_name" ]]; then
        chute_name="${module#deploy_}"
    fi

    echo "$chute_name"
}

select_chute_for_status() {
    # Send all UI to stderr so command substitution only captures the name
    print_header "Select Chute for Status" >&2

    local module
    module=$(select_module "Select module for chute status") || return 1

    local chute_name
    chute_name=$(module_to_chute_name "$module")

    if [[ -z "$chute_name" ]]; then
        print_warning "Could not infer chute name from $module" >&2
        read -rp "Enter chute name manually: " manual_name >&2
        if [[ -z "$manual_name" ]]; then
            print_error "Chute name is required" >&2
            return 1
        fi
        chute_name="$manual_name"
    fi

    echo "$chute_name"
    return 0
}

do_build() {
    local module="$1"
    local local_flag=""
    local debug_flag=""
    
    if $LOCAL_BUILD; then
        local_flag="--local"
    fi
    
    if $DEBUG_MODE; then
        debug_flag="--debug"
    fi
    
    print_header "Building: $module"
    
    local username=$(get_username)
    if [[ -z "$username" ]]; then
        print_error "Could not determine username from config"
        return 1
    fi
    
    print_info "Username: $username"
    print_info "Module: ${module}:chute"
    if $LOCAL_BUILD; then
        print_info "Mode: Local build"
    else
        print_info "Mode: Remote build"
        print_warning "Remote builds require >= \$50 USD account balance"
    fi
    
    local manifest_file="$SCRIPT_DIR/${module}.routes.json"
    local env_prefix=""
    if [[ -f "$manifest_file" ]]; then
        print_info "Route manifest: $manifest_file"
        env_prefix="CHUTES_ROUTE_MANIFEST=\"$manifest_file\" "
    fi
    
    # Build the command string for display
    local cmd="${env_prefix}CHUTES_USERNAME=\"$username\" chutes build \"${module}:chute\""
    [[ -n "$local_flag" ]] && cmd="$cmd $local_flag"
    [[ -n "$debug_flag" ]] && cmd="$cmd $debug_flag"
    cmd="$cmd --wait"
    
    echo ""
    print_cmd "$cmd"
    echo ""
    
    if ! confirm "Proceed with build?" "y"; then
        print_warning "Build cancelled"
        return 0
    fi
    
    ensure_venv
    
    # shellcheck disable=SC2086
    set +e
    if [[ -f "$manifest_file" ]]; then
        CHUTES_BUILD_AUTO_YES=1 CHUTES_ROUTE_MANIFEST="$manifest_file" CHUTES_USERNAME="$username" chutes build "${module}:chute" $local_flag $debug_flag --wait
    else
        CHUTES_BUILD_AUTO_YES=1 CHUTES_USERNAME="$username" chutes build "${module}:chute" $local_flag $debug_flag --wait
    fi
    build_status=$?
    set -e
    
    if [[ $build_status -eq 0 ]]; then
        print_success "Build complete"
    else
        print_error "Build failed (exit $build_status)"
        return $build_status
    fi
    
    # For local builds, test chutes-inspecto.so compatibility
    if $LOCAL_BUILD; then
        local image=$(get_image_name "$module")
        if [[ -n "$image" ]] && docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^${image}$"; then
            echo ""
            if ! test_inspecto_hash "$image"; then
                print_error "Image may fail remote build due to Python/inspecto incompatibility"
            fi
        fi
    fi
}

show_running_chute_containers() {
    local containers
    containers=$(docker ps --filter "name=chute-" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null)
    
    if [[ -n "$containers" ]] && [[ $(echo "$containers" | wc -l) -gt 1 ]]; then
        echo -e "\n${CYAN}── Running Chute Containers ──${NC}"
        echo "$containers"
        echo ""
    fi
}

show_running_chute_processes() {
    local procs
    procs=$(pgrep -af "chutes run" 2>/dev/null | grep -v grep || true)
    
    if [[ -n "$procs" ]]; then
        echo -e "\n${CYAN}── Running Chute Processes ──${NC}"
        echo "$procs" | while read -r line; do
            local pid=$(echo "$line" | awk '{print $1}')
            local cmd=$(echo "$line" | cut -d' ' -f2-)
            echo -e "  ${GREEN}PID $pid:${NC} $cmd"
        done
        echo ""
    fi
}

show_running_chutes() {
    show_running_chute_containers
    show_running_chute_processes
}

do_run() {
    local module="$1"
    local dev_flag=""
    local debug_flag=""
    local background="${2:-false}"
    
    if $DEV_MODE; then
        dev_flag="--dev"
    fi
    
    if $DEBUG_MODE; then
        debug_flag="--debug"
    fi
    
    print_header "Running: $module (Dev Mode)"
    
    # Build command string for display
    local cmd="CHUTES_EXECUTION_CONTEXT=REMOTE chutes run \"${module}:chute\""
    [[ -n "$dev_flag" ]] && cmd="$cmd $dev_flag"
    [[ -n "$debug_flag" ]] && cmd="$cmd $debug_flag"
    cmd="$cmd --port $PORT"
    
    print_info "Module: ${module}:chute"
    print_info "Port: $PORT"
    print_info "Mode: Development (runs on host)"
    print_warning "This mode requires models/services to be available locally"
    
    echo ""
    print_cmd "$cmd"
    echo ""
    
    if ! confirm "Start chute?" "y"; then
        print_warning "Cancelled"
        return 0
    fi
    
    ensure_venv
    
    local log_file="/tmp/chute-${module}-$$.log"
    
    print_info "Starting chute in background..."
    print_info "Log file: $log_file"
    echo ""
    
    # Run in background
    # shellcheck disable=SC2086
    CHUTES_EXECUTION_CONTEXT=REMOTE chutes run "${module}:chute" $dev_flag $debug_flag --port "$PORT" > "$log_file" 2>&1 &
    local pid=$!
    
    print_success "Started with PID: $pid"
    
    # Monitor loop
    local ready=false
    local max_checks=20
    local check_count=0
    
    while [[ $check_count -lt $max_checks ]]; do
        # Check if process is still running
        if ! kill -0 "$pid" 2>/dev/null; then
            print_error "Process stopped unexpectedly"
            echo -e "\n${YELLOW}Last logs:${NC}"
            tail -50 "$log_file" 2>/dev/null || true
            return 1
        fi
        
        # Print last 25 lines
        echo -e "\n${CYAN}── Logs (last 25 lines) ──${NC}"
        tail -25 "$log_file" 2>/dev/null || echo "(waiting for output...)"
        echo -e "${CYAN}───────────────────────────${NC}\n"
        
        # Check if ready
        if curl -s --max-time 2 "http://127.0.0.1:${PORT}/openapi.json" > /dev/null 2>&1; then
            ready=true
            break
        fi
        
        print_info "Waiting for chute... (check $((check_count + 1))/$max_checks)"
        sleep 15
        check_count=$((check_count + 1))
    done
    
    if $ready; then
        print_success "Chute is ready!"
        verify_chute_cords "$PORT" "$module"
        
        echo -e "\n${GREEN}Chute is running (PID: $pid). Commands:${NC}"
        echo -e "  ${BLUE}View logs:${NC}  tail -f $log_file"
        echo -e "  ${BLUE}Stop:${NC}       kill $pid"
        echo -e "  ${BLUE}Test:${NC}       curl http://127.0.0.1:${PORT}/openapi.json"
        echo ""
        
        if confirm "Tail logs?"; then
            tail -f "$log_file"
        fi
    else
        print_warning "Chute did not become ready in time (may still be loading)"
        echo -e "\n${BLUE}Process is still running (PID: $pid)${NC}"
        echo -e "  ${BLUE}View logs:${NC}  tail -f $log_file"
        echo -e "  ${BLUE}Stop:${NC}       kill $pid"
        
        if confirm "Stop process?"; then
            kill "$pid" 2>/dev/null || true
        fi
    fi
}

get_image_name() {
    local module="$1"
    local py_file="$SCRIPT_DIR/${module}.py"
    
    if [[ ! -f "$py_file" ]]; then
        echo ""
        return
    fi
    
    # Try to extract image name and tag using Python (handles variable references)
    local result
    result=$(python3 -c "
import re
import sys

content = open('$py_file').read()

# Try to find CHUTE_NAME and CHUTE_TAG variables first
name_match = re.search(r'^CHUTE_NAME\s*=\s*[\"\\']([^\"\\']+)[\"\\']', content, re.MULTILINE)
tag_match = re.search(r'^CHUTE_TAG\s*=\s*[\"\\']([^\"\\']+)[\"\\']', content, re.MULTILINE)

if name_match and tag_match:
    print(f'{name_match.group(1)}:{tag_match.group(1)}')
    sys.exit(0)

# Fallback: try to find name= and tag= in Image() constructor with string literals
name_match = re.search(r'Image\s*\([^)]*name\s*=\s*[\"\\']([^\"\\']+)[\"\\']', content, re.DOTALL)
tag_match = re.search(r'Image\s*\([^)]*tag\s*=\s*[\"\\']([^\"\\']+)[\"\\']', content, re.DOTALL)

if name_match and tag_match:
    print(f'{name_match.group(1)}:{tag_match.group(1)}')
" 2>/dev/null)
    
    echo "$result"
}

test_inspecto_hash() {
    local image="$1"
    
    print_info "Testing chutes-inspecto.so compatibility..."
    print_cmd "docker run --rm --entrypoint \"\" $image bash -c 'pip install chutes --upgrade >/dev/null 2>&1 && chutes run does_not_exist:chute --generate-inspecto-hash; echo EXIT:\$?'"
    echo ""
    
    local output
    output=$(docker run --rm --entrypoint "" "$image" bash -c 'pip install chutes --upgrade >/dev/null 2>&1 && chutes run does_not_exist:chute --generate-inspecto-hash; echo EXIT:$?' 2>&1)
    local exit_line
    exit_line=$(echo "$output" | grep -oE 'EXIT:[0-9]+' | tail -1)
    local exit_code="${exit_line#EXIT:}"
    
    echo "$output"
    echo ""
    
    if [[ "$exit_code" == "0" ]]; then
        print_success "chutes-inspecto.so test passed (exit 0)"
        return 0
    elif [[ "$exit_code" == "139" ]]; then
        print_error "chutes-inspecto.so SEGFAULT (exit 139) - Conda Python issue"
        return 1
    else
        print_warning "chutes-inspecto.so test exited with code $exit_code"
        return 1
    fi
}

wait_for_chute_ready() {
    local port="$1"
    local max_wait="${2:-300}"  # 5 minutes default
    local check_interval="${3:-15}"
    local elapsed=0
    
    print_info "Waiting for chute to become ready on port $port..."
    
    while [[ $elapsed -lt $max_wait ]]; do
        # Try to hit the health endpoint or root
        if curl -s --max-time 2 "http://127.0.0.1:${port}/docs" > /dev/null 2>&1; then
            echo ""
            print_success "Chute is ready on port $port"
            return 0
        fi
        
        sleep "$check_interval"
        elapsed=$((elapsed + check_interval))
        print_info "Still waiting... (${elapsed}s elapsed)"
    done
    
    print_error "Timeout waiting for chute to become ready"
    return 1
}

verify_chute_cords() {
    local port="$1"
    local module="$2"
    
    print_header "Verifying Chute Cords"
    
    # Try to get OpenAPI schema which lists all endpoints
    local schema
    schema=$(curl -s --max-time 5 "http://127.0.0.1:${port}/openapi.json" 2>/dev/null)
    
    if [[ -n "$schema" ]]; then
        print_success "OpenAPI schema available"
        
        # Extract and display paths
        local paths
        paths=$(echo "$schema" | python3 -c "import sys, json; d=json.load(sys.stdin); print('\n'.join(d.get('paths', {}).keys()))" 2>/dev/null)
        
        if [[ -n "$paths" ]]; then
            echo -e "\n${BLUE}Available Cords:${NC}"
            echo "$paths" | while read -r path; do
                echo -e "  ${GREEN}•${NC} $path"
            done
            echo ""
            return 0
        fi
    fi
    
    print_warning "Could not verify cords (chute may still be loading)"
    return 1
}

get_api_key() {
    if [[ -n "${CHUTES_API_KEY:-}" ]]; then
        echo "$CHUTES_API_KEY"
        return 0
    fi

    local key_file="$HOME/.chutes/api_key"
    if [[ -f "$key_file" ]]; then
        local key
        key=$(head -n1 "$key_file" 2>/dev/null | tr -d '[:space:]')
        if [[ -n "$key" ]]; then
            echo "$key"
            return 0
        fi
    fi

    print_error "CHUTES_API_KEY not set and ~/.chutes/api_key missing"
    print_info "Create one with: chutes keys create <name>"
    print_info "Or export CHUTES_API_KEY=cpk_..."
    return 1
}

get_api_base_url() {
    local base_url
    base_url=$(grep -E "^base_url" "$HOME/.chutes/config.ini" 2>/dev/null | head -n1 | cut -d'=' -f2 | tr -d '[:space:]')
    if [[ -z "$base_url" ]]; then
        base_url="https://api.chutes.ai"
    fi
    echo "$base_url"
}

select_instance_id_from_output() {
    python3 - 2>/dev/null <<'PYCODE'
import json, re, sys
output = sys.stdin.read()
match = re.search(r'\{[\s\S]*\}', output)
if not match:
    sys.exit(1)
try:
    data = json.loads(match.group())
except Exception:
    sys.exit(1)
instances = data.get("instances") or []
if not instances:
    sys.exit(1)

def sort_key(inst):
    return (not inst.get("active", False), not inst.get("verified", False), inst.get("last_verified_at") or "")

instances = sorted(instances, key=sort_key)

if len(instances) == 1:
    print(instances[0]["instance_id"])
    sys.exit(0)

print("\nInstances:", file=sys.stderr)
for i, inst in enumerate(instances, 1):
    print(f"  {i}) {inst.get('instance_id')}  active={inst.get('active')} verified={inst.get('verified')}", file=sys.stderr)

if not sys.stdin.isatty():
    idx = 0
else:
    try:
        prompt = f"Select instance (1-{len(instances)}) [1]: "
        sys.stderr.write(prompt)
        sys.stderr.flush()
        choice = sys.stdin.readline().strip()
    except EOFError:
        choice = ""
    if not choice:
        idx = 0
    else:
        try:
            idx = int(choice) - 1
        except ValueError:
            sys.exit(1)
        if idx < 0 or idx >= len(instances):
            idx = 0

print(instances[idx]["instance_id"])
PYCODE
}

do_run_docker() {
    local module="$1"
    
    print_header "Run in Docker: $module"
    
    # Get image name from module
    local image=$(get_image_name "$module")
    if [[ -z "$image" ]]; then
        print_error "Could not determine image name from $module.py"
        return 1
    fi
    
    # Check if image exists
    if ! docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^${image}$"; then
        print_error "Image '$image' not found. Build it first (option 3)."
        return 1
    fi
    
    # Get username from config
    local username=$(get_username)
    
    # Check if this is a wrapped image (has passthrough in the module)
    local is_wrapped=false
    if grep -q "passthrough=True" "$SCRIPT_DIR/${module}.py" 2>/dev/null; then
        is_wrapped=true
    fi
    
    print_info "Image: $image"
    print_info "Ports: $PORT, 8001, 8020, 8080"
    print_info "GPU: enabled"
    print_info "Username: $username"
    if $is_wrapped; then
        print_info "Mode: Wrapped image (runs base services + chutes)"
    else
        print_info "Mode: Standard chute"
    fi
    echo ""
    
    if ! confirm "Start container?" "y"; then
        print_warning "Cancelled"
        return 0
    fi
    
    # Stop any existing container with same name
    docker stop "chute-${module}" 2>/dev/null || true
    docker rm "chute-${module}" 2>/dev/null || true
    
    print_info "Starting container in background..."
    
    local container_id
    if $is_wrapped; then
        # For wrapped images: run original entrypoint in background, then start chutes
        # The original entrypoint starts the base services (e.g., XTTS, Whisper)
        container_id=$(docker run -d --rm --gpus all --name "chute-${module}" \
            -p "${PORT}:${PORT}" -p 8001:8001 -p 8020:8020 -p 8080:8080 \
            -e CHUTES_EXECUTION_CONTEXT=REMOTE \
            -e CHUTES_DEV_MODE=true \
            -e CHUTES_USERNAME="$username" \
            -w /app \
            "$image" \
            bash -c 'docker-entrypoint.sh & sleep 60 && chutes run '"${module}"':chute --dev --port '"$PORT")
    else
        # Standard chute: just run chutes directly
        container_id=$(docker run -d --rm --gpus all --name "chute-${module}" \
            -p "${PORT}:${PORT}" -p 8001:8001 -p 8020:8020 -p 8080:8080 \
            -e CHUTES_EXECUTION_CONTEXT=REMOTE \
            -e CHUTES_DEV_MODE=true \
            -e CHUTES_USERNAME="$username" \
            -w /app \
            --entrypoint chutes \
            "$image" \
            run "${module}:chute" --dev --port "$PORT")
    fi
    
    if [[ -z "$container_id" ]]; then
        print_error "Failed to start container"
        return 1
    fi
    
    print_success "Container started: ${container_id:0:12}"
    echo ""
    
    # Monitor loop
    local ready=false
    local max_checks=20  # 20 * 15s = 5 minutes
    local check_count=0
    
    while [[ $check_count -lt $max_checks ]]; do
        # Check if container is still running
        if ! docker ps -q --filter "id=$container_id" | grep -q .; then
            print_error "Container stopped unexpectedly"
            echo -e "\n${YELLOW}Last logs:${NC}"
            docker logs --tail 50 "$container_id" 2>&1 || true
            return 1
        fi
        
        # Print last 25 lines
        echo -e "\n${CYAN}── Container Logs (last 25 lines) ──${NC}"
        docker logs --tail 25 "$container_id" 2>&1
        echo -e "${CYAN}─────────────────────────────────────${NC}\n"
        
        # Check if ready
        if curl -s --max-time 2 "http://127.0.0.1:${PORT}/openapi.json" > /dev/null 2>&1; then
            ready=true
            break
        fi
        
        print_info "Waiting for chute... (check $((check_count + 1))/$max_checks)"
        sleep 15
        check_count=$((check_count + 1))
    done
    
    if $ready; then
        print_success "Chute is ready!"
        verify_chute_cords "$PORT" "$module"
        
        echo -e "\n${GREEN}Container is running. Commands:${NC}"
        echo -e "  ${BLUE}View logs:${NC}  docker logs -f chute-${module}"
        echo -e "  ${BLUE}Stop:${NC}       docker stop chute-${module}"
        echo -e "  ${BLUE}Test:${NC}       curl http://127.0.0.1:${PORT}/openapi.json"
        echo ""
        
        if confirm "Attach to container logs?"; then
            docker logs -f "chute-${module}"
        fi
    else
        print_error "Chute did not become ready in time"
        if confirm "Stop container?"; then
            docker stop "chute-${module}"
        fi
    fi
}

do_deploy() {
    local module="$1"
    local fee_flag=""
    local public_flag=""
    local debug_flag=""
    
    if $ACCEPT_FEE; then
        fee_flag="--accept-fee"
    fi
    
    if $PUBLIC_DEPLOY; then
        public_flag="--public"
    fi
    
    if $DEBUG_MODE; then
        debug_flag="--debug"
    fi
    
    print_header "Deploying: $module"
    
    # Show payment info
    echo -e "${BLUE}Payment Address:${NC}"
    grep "address" "$CHUTES_CONFIG" 2>/dev/null || print_warning "Payment address not found"
    echo ""
    
    print_warning "Deployment requires TAO balance and may incur fees"
    
    if ! $ACCEPT_FEE; then
        if confirm "Accept deployment fees?"; then
            fee_flag="--accept-fee"
        else
            print_warning "Deployment cancelled (fees not accepted)"
            return 0
        fi
    fi
    
    if ! $PUBLIC_DEPLOY; then
        if confirm "Make this deployment public?"; then
            public_flag="--public"
        fi
    fi
    
    # Build command string for display
    local cmd="chutes deploy \"${module}:chute\""
    [[ -n "$fee_flag" ]] && cmd="$cmd $fee_flag"
    [[ -n "$public_flag" ]] && cmd="$cmd $public_flag"
    [[ -n "$debug_flag" ]] && cmd="$cmd $debug_flag"
    
    echo ""
    print_cmd "$cmd"
    echo ""
    
    ensure_venv
    
    # shellcheck disable=SC2086
    chutes deploy "${module}:chute" $fee_flag $public_flag $debug_flag
    
    print_success "Deployment initiated"
}

list_deployed_chutes() {
    # Get list of deployed chutes (names only)
    # Accepts optional input: either a file path or pre-fetched text
    ensure_venv
    local input="${1:-}"
    local output=""
    
    if [[ -n "$input" && -f "$input" ]]; then
        output=$(cat "$input")
    else
        output="$input"
    fi
    
    if [[ -z "$output" ]]; then
        output=$(chutes chutes list 2>&1)
    fi
    
    # Check if any chutes found
    if echo "$output" | grep -q "Found 0 matching"; then
        return 0
    fi
    
    # Try to extract chute names from table output (box-drawn or plain)
    # Prefer the Name column in the table; fallback to whitespace-delimited lines
    printf "%s\n" "$output" | awk -F '│' '
        NF >= 3 {
            gsub(/^[ \t]+|[ \t]+$/, "", $2)
            name=$2
            if (name != "" && name != "Name" && name != "NAME" && name != "NAME ") {
                print name
            }
        }
    ' | grep -v "^[0-9]" 2>/dev/null || true
    
    printf "%s\n" "$output" | grep -E "^[a-z0-9]" | awk '{print $1}' | grep -v "^[0-9]" 2>/dev/null || true
}

select_chute_to_delete() {
    local chute_ids=()
    local chute_names=()
    local choice=""
    
    {
        print_header "Select Chute to Delete"
        
        local tmp_output
        tmp_output=$(mktemp)
        chutes chutes list 2>&1 | tee "$tmp_output"
        echo ""
        
        # Parse box table: ID in col1, Name in col2
        while IFS=$'\t' read -r cid_raw cname; do
            [[ -n "$cid_raw" && -n "$cname" ]] || continue
            # Keep only UUID-safe characters (alnum and hyphen), strip whitespace/box chars
            cid=$(echo "$cid_raw" | tr -cd 'A-Za-z0-9-')
            # Normalize multiple hyphens/back-to-back punctuation
            cid=$(echo "$cid" | sed 's/--*/-/g')
            # Accept full UUID format only
            if [[ "$cid" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
                chute_ids+=("$cid")
                chute_names+=("$cname")
            fi
        done < <(
            awk -F '│' 'NF>=3{
                gsub(/^[ \t]+|[ \t]+$/, "", $1);
                gsub(/^[ \t]+|[ \t]+$/, "", $2);
                id=$1; name=$2;
                if(id!="" && name!="" && id!="ID" && name!="Name"){print id"\t"name}
            }' "$tmp_output"
        )
        rm -f "$tmp_output"
        if [[ ${#chute_ids[@]} -eq 0 ]]; then
            while read -r cid; do
                chute_ids+=("$cid")
                chute_names+=("chute-${#chute_ids[@]}")
            done < <(grep -Eo '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' "$tmp_output" | uniq)
        fi
        # Fallback: if no IDs parsed (e.g., table truncation with ellipsis), try regex on raw output
        if [[ ${#chute_ids[@]} -eq 0 ]]; then
            while read -r cid; do
                chute_ids+=("$cid")
                chute_names+=("chute-${#chute_ids[@]}")
            done < <(grep -Eo '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' "$tmp_output" | uniq)
        fi
        
        if [[ ${#chute_ids[@]} -eq 0 ]]; then
            print_warning "No deployed chutes found"
            echo ""
        else
            echo -e "${BLUE}Deployed Chutes:${NC}"
            local i=1
            while [[ $i -le ${#chute_ids[@]} ]]; do
                local idx=$((i-1))
                echo -e "  ${GREEN}$i)${NC} ${chute_names[$idx]} (id: ${chute_ids[$idx]})"
                i=$((i + 1))
            done
            echo -e "  ${YELLOW}b)${NC} Back"
            echo ""
            
            read -rp "Select chute to delete (1-${#chute_ids[@]}): " choice
        fi
    } >&2
    
    if [[ ${#chute_ids[@]} -eq 0 ]]; then
        return 1
    fi
    
    if [[ "$choice" == "b" || "$choice" == "B" ]]; then
        return 1
    fi
    
    if [[ "$choice" =~ ^[0-9]+$ ]] && [[ "$choice" -ge 1 ]] && [[ "$choice" -le ${#chute_ids[@]} ]]; then
        local idx=$((choice - 1))
        echo "${chute_ids[$idx]}|${chute_names[$idx]}"
        return 0
    else
        print_error "Invalid selection"
        return 1
    fi
}

do_delete() {
    local chute_ref="$1"
    local chute_id="${chute_ref%%|*}"
    local chute_name="${chute_ref#*|}"
    if [[ "$chute_id" == "$chute_name" ]]; then
        chute_name="$chute_id"
    fi
    
    print_header "Delete Chute: ${chute_name} (id: ${chute_id})"
    
    print_error "WARNING: This action is permanent!"
    echo ""
    
    if ! confirm "Are you sure you want to delete '${chute_name}' (id: ${chute_id})?"; then
        print_info "Deletion cancelled"
        return 0
    fi
    
    read -rp "Type the chute id to confirm: " confirm_id
    if [[ "$confirm_id" != "$chute_id" ]]; then
        print_error "IDs don't match. Deletion cancelled."
        return 1
    fi
    
    echo ""
    print_cmd "chutes chutes delete \"$chute_id\""
    echo ""
    
    ensure_venv
    chutes chutes delete "$chute_id"
    
    print_success "Chute deleted"
}

list_built_images() {
    # Get list of built images (names only from chutes images list)
    # Accepts optional input: either a file path or pre-fetched text
    local input="${1:-}"
    local output=""
    
    if [[ -n "$input" && -f "$input" ]]; then
        output=$(cat "$input")
    else
        output="$input"
    fi
    
    if [[ -z "$output" ]]; then
        ensure_venv
        output=$(chutes images list 2>&1)
    fi
    
    if printf "%s\n" "$output" | grep -q "Found 0 matching"; then
        return 0
    fi
    
    printf "%s\n" "$output" | awk -F '│' '
        NF >= 3 {
            gsub(/^[ \t]+|[ \t]+$/, "", $2)
            name=$2
            if (name != "" && name != "Name" && name != "NAME") {
                print name
            }
        }
    ' | grep -v "^[0-9]" 2>/dev/null || true
}

select_image_to_delete() {
    ensure_venv
    local tmp_output
    tmp_output=$(mktemp)
    
    local image_ids=()
    local image_names=()
    local image_tags=()
    local choice=""
    
    {
        print_header "Select Image to Delete"
        chutes images list 2>&1 | tee "$tmp_output"
        echo ""
        
        # Parse box table: ID col1, Name col2, Tag col3
        while IFS=$'\t' read -r iid_raw iname itag; do
            [[ -n "$iid_raw" && -n "$iname" ]] || continue
            iid=$(echo "$iid_raw" | tr -cd 'A-Za-z0-9-')
            iid=$(echo "$iid" | sed 's/--*/-/g')
            if [[ "$iid" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
                image_ids+=("$iid")
                image_names+=("$iname")
                image_tags+=("${itag:-unknown}")
            fi
        done < <(
            awk -F '│' 'NF>=3{
                gsub(/^[ \t]+|[ \t]+$/, "", $1);
                gsub(/^[ \t]+|[ \t]+$/, "", $2);
                gsub(/^[ \t]+|[ \t]+$/, "", $3);
                id=$1; name=$2; tag=$3;
                if(id!="" && name!="" && id!="ID" && name!="Name"){print id"\t"name"\t"tag}
            }' "$tmp_output"
        )
        if [[ ${#image_ids[@]} -eq 0 ]]; then
            while read -r iid; do
                image_ids+=("$iid")
                image_names+=("image-${#image_ids[@]}")
                image_tags+=("unknown")
            done < <(grep -Eo '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' "$tmp_output" | uniq)
        fi
        rm -f "$tmp_output"
        
        if [[ ${#image_ids[@]} -eq 0 ]]; then
            print_warning "No built images found"
            echo ""  # spacing
        else
            echo -e "${BLUE}Built Images:${NC}"
            local i=1
            while [[ $i -le ${#image_ids[@]} ]]; do
                local idx=$((i-1))
                echo -e "  ${GREEN}$i)${NC} ${image_names[$idx]}:${image_tags[$idx]} (id: ${image_ids[$idx]})"
                i=$((i + 1))
            done
            echo -e "  ${YELLOW}b)${NC} Back"
            echo ""
            
            read -rp "Select image to delete (1-${#image_ids[@]}): " choice
        fi
    } >&2
    
    if [[ ${#image_ids[@]} -eq 0 ]]; then
        return 1
    fi
    
    if [[ "$choice" == "b" || "$choice" == "B" ]]; then
        return 1
    fi
    
    if [[ "$choice" =~ ^[0-9]+$ ]] && [[ "$choice" -ge 1 ]] && [[ "$choice" -le ${#image_ids[@]} ]]; then
        local idx=$((choice - 1))
        echo "${image_ids[$idx]}|${image_names[$idx]}|${image_tags[$idx]}"
        return 0
    else
        print_error "Invalid selection"
        return 1
    fi
}

do_delete_image() {
    local image_ref="$1"
    local image_id="${image_ref%%|*}"
    local rest="${image_ref#*|}"
    local image_name="${rest%%|*}"
    local image_tag="${rest#*|}"
    
    print_header "Delete Image: ${image_name}:${image_tag} (id: ${image_id})"
    
    print_error "WARNING: This action is permanent!"
    echo ""
    
    if ! confirm "Are you sure you want to delete '${image_name}:${image_tag}' (id: ${image_id})?"; then
        print_info "Deletion cancelled"
        return 0
    fi
    
    read -rp "Type the image id to confirm: " confirm_id
    if [[ "$confirm_id" != "$image_id" ]]; then
        print_error "IDs don't match. Deletion cancelled."
        return 1
    fi
    
    echo ""
    print_cmd "chutes images delete \"$image_id\""
    echo ""
    
    ensure_venv
    chutes images delete "$image_id"
    local status=$?
    if [[ $status -eq 0 ]]; then
        print_success "Image deleted"
    else
        print_error "Failed to delete image (exit $status)"
    fi
}

do_check_logs() {
    local chute_name="$1"
    
    print_header "Instance Logs: $chute_name"
    
    ensure_venv

    local api_key
    api_key=$(get_api_key) || return 1
    local base_url
    base_url=$(get_api_base_url)

    # Get chute JSON and extract instances (avoid pipes to keep output intact)
    local tmp_output
    tmp_output=$(mktemp)
    chutes chutes get "$chute_name" >"$tmp_output" 2>&1 || true

    local interactive_mode="nontty"
    if [[ -t 0 ]]; then
        interactive_mode="tty"
    fi

    local instance_id
    instance_id=$(python - "$tmp_output" "$interactive_mode" <<'PYCODE' || true
import json, re, sys
path = sys.argv[1]
interactive = sys.argv[2] if len(sys.argv) > 2 else "nontty"
try:
    text = open(path, encoding="utf-8", errors="replace").read()
except Exception:
    sys.exit(1)
match = re.search(r'\{[\s\S]*\}', text)
if not match:
    sys.exit(1)
data = json.loads(match.group())
instances = data.get("instances") or []
if not instances:
    sys.exit(1)
def sort_key(inst):
    return (not inst.get("active", False), not inst.get("verified", False), inst.get("last_verified_at") or "")
instances = sorted(instances, key=sort_key)
if len(instances) == 1:
    print(instances[0]["instance_id"])
    sys.exit(0)
print("\nInstances:", file=sys.stderr)
for i, inst in enumerate(instances, 1):
    print(f"  {i}) {inst.get('instance_id')}  active={inst.get('active')} verified={inst.get('verified')}", file=sys.stderr)
if interactive != "tty":
    idx = 0
else:
    try:
        prompt = f"Select instance (1-{len(instances)}) [1]: "
        sys.stderr.write(prompt)
        sys.stderr.flush()
        choice = sys.stdin.readline().strip()
    except EOFError:
        choice = ""
    if not choice:
        idx = 0
    else:
        try:
            idx = int(choice) - 1
        except ValueError:
            sys.exit(1)
        if idx < 0 or idx >= len(instances):
            idx = 0
print(instances[idx]["instance_id"])
PYCODE
)
    rm -f "$tmp_output"

    if [[ -z "$instance_id" ]]; then
        print_error "No instances available for $chute_name"
        return 1
    fi

    print_info "Streaming logs from instance: $instance_id"
    local logs_url="${base_url}/instances/${instance_id}/logs"
    print_cmd "curl -N -H \"Authorization: Bearer <redacted>\" \"$logs_url\""
    echo ""
    set +e
    curl -N -H "Authorization: Bearer ${api_key}" "$logs_url"
    local curl_status=$?
    set -e
    if [[ $curl_status -ne 0 ]]; then
        print_warning "curl exited with status $curl_status"
    fi
    return 0
}

select_chute_for_warmup() {
    local chute_ids=()
    local chute_names=()
    local choice=""
    
    {
        print_header "Select Chute to Warmup"
        
        local tmp_output
        tmp_output=$(mktemp)
        chutes chutes list 2>&1 | tee "$tmp_output"
        echo ""
        
        # Parse box table: ID in col1, Name in col2
        while IFS=$'\t' read -r cid_raw cname; do
            [[ -n "$cid_raw" && -n "$cname" ]] || continue
            cid=$(echo "$cid_raw" | tr -cd 'A-Za-z0-9-')
            cid=$(echo "$cid" | sed 's/--*/-/g')
            if [[ "$cid" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
                chute_ids+=("$cid")
                chute_names+=("$cname")
            fi
        done < <(
            awk -F '│' 'NF>=3{
                gsub(/^[ \t]+|[ \t]+$/, "", $1);
                gsub(/^[ \t]+|[ \t]+$/, "", $2);
                id=$1; name=$2;
                if(id!="" && name!="" && id!="ID" && name!="Name"){print id"\t"name}
            }' "$tmp_output"
        )
        rm -f "$tmp_output"
        
        if [[ ${#chute_ids[@]} -eq 0 ]]; then
            print_warning "No deployed chutes found"
            echo ""
        else
            echo -e "${BLUE}Deployed Chutes:${NC}"
            local i=1
            while [[ $i -le ${#chute_ids[@]} ]]; do
                local idx=$((i-1))
                echo -e "  ${GREEN}$i)${NC} ${chute_names[$idx]} (id: ${chute_ids[$idx]})"
                i=$((i + 1))
            done
            echo -e "  ${YELLOW}b)${NC} Back"
            echo ""
            
            read -rp "Select chute to warm up (1-${#chute_ids[@]}): " choice
        fi
    } >&2
    
    if [[ ${#chute_ids[@]} -eq 0 ]]; then
        return 1
    fi
    
    if [[ "$choice" == "b" || "$choice" == "B" ]]; then
        return 1
    fi
    
    if [[ "$choice" =~ ^[0-9]+$ ]] && [[ "$choice" -ge 1 ]] && [[ "$choice" -le ${#chute_ids[@]} ]]; then
        local idx=$((choice - 1))
        echo "${chute_ids[$idx]}|${chute_names[$idx]}"
        return 0
    else
        print_error "Invalid selection"
        return 1
    fi
}

do_warmup() {
    local chute_ref="$1"
    local chute_id="${chute_ref%%|*}"
    local chute_name="${chute_ref#*|}"
    if [[ "$chute_id" == "$chute_name" ]]; then
        chute_name="$chute_id"
    fi
    print_header "Warmup: ${chute_name} (id: ${chute_id})"
    
    ensure_venv
    
    print_cmd "chutes warmup \"${chute_id}\""
    echo ""
    
    if chutes warmup "${chute_id}"; then
        print_success "Warmup triggered for ${chute_name}"
    else
        print_error "Warmup failed for ${chute_name}"
        return 1
    fi
}

do_keep_warm() {
    local chute_ref="$1"
    local chute_id="${chute_ref%%|*}"
    local chute_name="${chute_ref#*|}"
    if [[ "$chute_id" == "$chute_name" ]]; then
        chute_name="$chute_id"
    fi
    
    read -rp "Warmup interval seconds [300]: " interval
    interval=${interval:-300}
    if ! [[ "$interval" =~ ^[0-9]+$ ]] || [[ "$interval" -le 0 ]]; then
        print_error "Interval must be a positive integer"
        return 1
    fi
    
    print_header "Keep Warm: ${chute_name} (id: ${chute_id}) every ${interval}s"
    print_info "Press Ctrl+C to stop."
    ensure_venv
    
    while true; do
        print_cmd "chutes warmup \"${chute_id}\""
        chutes warmup "${chute_id}" || print_warning "Warmup failed for ${chute_name}"
        sleep "$interval"
    done
}

show_account_info() {
    print_header "Account Info"
    
    if [[ ! -f "$CHUTES_CONFIG" ]]; then
        print_warning "Not configured. Run setup.sh first."
        return 1
    fi
    
    local username=$(get_username)
    local payment=$(grep -E "^address\s*=" "$CHUTES_CONFIG" 2>/dev/null | cut -d'=' -f2 | tr -d ' ')
    
    echo -e "${BLUE}Username:${NC}        $username"
    echo -e "${BLUE}Payment Address:${NC} $payment"
    echo ""
    print_info "Add TAO to payment address for deployments"
    print_info "Account needs >= \$50 USD balance for remote builds"
}

do_create_from_image() {
    print_header "Create Chute from Docker Image"
    
    local image
    read -rp "Docker Image (e.g. elbios/xtts-whisper:latest): " image
    if [[ -z "$image" ]]; then
        print_error "Image is required"
        return 1
    fi
    
    local name
    read -rp "Chute Name (optional, press Enter for auto): " name
    
    local gpus
    read -rp "GPUs (e.g. all, 0, none) [${DEFAULT_DISCOVER_GPUS}]: " gpus
    gpus=${gpus:-$DEFAULT_DISCOVER_GPUS}
    
    local -a env_args=()
    echo -e "\nEnter Environment Variables (KEY=VALUE). Press Enter on empty line to finish."
    while true; do
        local env_var
        read -rp "ENV: " env_var
        if [[ -z "$env_var" ]]; then
            break
        fi
        env_args+=("--env" "$env_var")
    done
    
    echo ""
    print_info "Creating chute definition..."
    
    ensure_venv
    
    local cmd=(
        python3 "$SCRIPT_DIR/tools/create_chute_from_image.py" "$image"
        "--gpus" "$gpus"
        "--startup-delay" "$DEFAULT_STARTUP_DELAY"
        "--probe-timeout" "$DEFAULT_PROBE_TIMEOUT"
        "--interactive"
    )
    if [[ -n "$name" ]]; then
        cmd+=("--name" "$name")
    fi
    
    if [[ ${#env_args[@]} -gt 0 ]]; then
        cmd+=("${env_args[@]}")
    fi
    
    print_cmd "${cmd[*]}"
    "${cmd[@]}"
}

# =============================================================================
# Interactive Menu
# =============================================================================

show_menu() {
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  Chutes Deploy${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "  ${GREEN}1)${NC} List images"
    echo -e "  ${GREEN}2)${NC} List chutes"
    echo -e "  ${GREEN}3)${NC} Create chute file from Docker image"
    echo -e "  ${GREEN}4)${NC} Build chute"
    echo -e "  ${GREEN}5)${NC} Run in Docker (GPU, for wrapped services)"
    echo -e "  ${GREEN}6)${NC} Run dev mode (host, for Python chutes)"
    echo -e "  ${GREEN}7)${NC} Deploy chute"
    echo -e "  ${GREEN}8)${NC} Warmup chute"
    echo -e "  ${GREEN}13)${NC} Keep chute warm (loop)"
    echo -e "  ${GREEN}9)${NC} Chute status"
    echo -e "  ${GREEN}10)${NC} Instance logs"
    echo -e "  ${GREEN}11)${NC} Delete chute"
    echo -e "  ${GREEN}12)${NC} Delete image"
    echo -e "  ${GREEN}0)${NC} Account info"
    echo -e "  ${GREEN}q)${NC} Quit"
    echo ""
}

show_build_menu() {
    echo ""
    echo -e "${CYAN}── Build Options ──${NC}"
    echo ""
    echo -e "  ${GREEN}1)${NC} Local build (no upload)"
    echo -e "  ${GREEN}2)${NC} Remote build (requires \$50 balance)"
    echo -e "  ${GREEN}b)${NC} Back"
    echo ""
}

# =============================================================================
# Parse Arguments
# =============================================================================

while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            SHOW_HELP=true
            shift
            ;;
        --list-images)
            LIST_IMAGES=true
            shift
            ;;
        --list-chutes)
            LIST_CHUTES=true
            shift
            ;;
        --build)
            BUILD_MODULE="$2"
            shift 2
            ;;
        --discover)
            DISCOVER_MODULE="$2"
            shift 2
            ;;
        --deploy)
            DEPLOY_MODULE="$2"
            shift 2
            ;;
        --run-docker)
            RUN_DOCKER_MODULE="$2"
            shift 2
            ;;
        --run)
            RUN_MODULE="$2"
            shift 2
            ;;
        --status)
            STATUS_CHUTE="$2"
            shift 2
            ;;
        --delete)
            DELETE_CHUTE="$2"
            shift 2
            ;;
        --logs)
            LOGS_CHUTE="$2"
            shift 2
            ;;
        --local)
            LOCAL_BUILD=true
            shift
            ;;
        --accept-fee)
            ACCEPT_FEE=true
            shift
            ;;
        --public)
            PUBLIC_DEPLOY=true
            shift
            ;;
        --dev)
            DEV_MODE=true
            shift
            ;;
        --debug)
            DEBUG_MODE=true
            shift
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        *)
            print_error "Unknown option: $1"
            show_usage
            exit 1
            ;;
    esac
done

# =============================================================================
# Main
# =============================================================================

main() {
    # Check prerequisites
    ensure_venv
    ensure_chutes_config
    
    # Handle flags
    if $SHOW_HELP; then
        show_usage
        exit 0
    fi
    
    if $LIST_IMAGES; then
        do_list_images
        exit 0
    fi
    
    if $LIST_CHUTES; then
        do_list_chutes
        exit 0
    fi
    
    if [[ -n "$STATUS_CHUTE" ]]; then
        do_chute_status "$STATUS_CHUTE"
        exit 0
    fi
    
    if [[ -n "$BUILD_MODULE" ]]; then
        do_build "$BUILD_MODULE"
        exit 0
    fi

    if [[ -n "$DISCOVER_MODULE" ]]; then
        run_route_discovery_for_module "$DISCOVER_MODULE"
        exit $?
    fi
    
    if [[ -n "$RUN_DOCKER_MODULE" ]]; then
        do_run_docker "$RUN_DOCKER_MODULE"
        exit 0
    fi
    
    if [[ -n "$RUN_MODULE" ]]; then
        do_run "$RUN_MODULE"
        exit 0
    fi
    
    if [[ -n "$DEPLOY_MODULE" ]]; then
        do_deploy "$DEPLOY_MODULE"
        exit 0
    fi
    
    if [[ -n "$DELETE_CHUTE" ]]; then
        do_delete "$DELETE_CHUTE"
        exit 0
    fi
    
    if [[ -n "$LOGS_CHUTE" ]]; then
        do_check_logs "$LOGS_CHUTE"
        exit 0
    fi
    
    # Interactive mode
    while true; do
        show_menu
        read -rp "Select option: " choice
        
        case $choice in
            1)
                do_list_images
                ;;
            2)
                do_list_chutes
                ;;
            3)
                do_create_from_image
                ;;
            4)
                module=$(select_module "Select module to build") || continue
                if ! prompt_route_discovery_before_build "$module"; then
                    continue
                fi
                echo ""
                show_build_menu
                read -rp "Select build type: " bchoice
                case $bchoice in
                    1)
                        LOCAL_BUILD=true
                        do_build "$module"
                        LOCAL_BUILD=false
                        ;;
                    2)
                        LOCAL_BUILD=false
                        do_build "$module"
                        ;;
                    b|B) continue ;;
                    *) print_error "Invalid option" ;;
                esac
                ;;
            5)
                # Run in Docker (for wrapped services like XTTS)
                show_running_chute_containers
                module=$(select_module "Select module to run in Docker") || continue
                read -rp "Port [8000]: " port_input
                PORT="${port_input:-8000}"
                do_run_docker "$module"
                PORT=8000
                ;;
            6)
                # Run dev mode (for Python chutes, runs on host)
                show_running_chute_processes
                module=$(select_module "Select module for dev mode") || continue
                DEV_MODE=true
                read -rp "Port [8000]: " port_input
                PORT="${port_input:-8000}"
                do_run "$module"
                DEV_MODE=false
                PORT=8000
                ;;
            7)
                module=$(select_module "Select module to deploy") || continue
                do_deploy "$module"
                ;;
            8)
                chute_name=$(select_chute_for_warmup) || continue
                do_warmup "$chute_name"
                ;;
            13)
                chute_name=$(select_chute_for_warmup) || continue
                do_keep_warm "$chute_name"
                ;;
            9)
                chute_name=$(select_chute_for_status) || continue
                do_chute_status "$chute_name"
                ;;
            10)
                module=$(select_module "Select module for logs") || continue
                # Extract CHUTE_NAME from the module file
                chute_name=$(grep -oP "CHUTE_NAME\s*=\s*['\"]\\K[^'\"]*" "$SCRIPT_DIR/${module}.py" 2>/dev/null)
                if [[ -z "$chute_name" ]]; then
                    chute_name="${module#deploy_}"  # fallback: strip deploy_ prefix
                fi
                do_check_logs "$chute_name"
                ;;
            11)
                chute_name=$(select_chute_to_delete) || continue
                if [[ -n "$chute_name" ]]; then
                    do_delete "$chute_name"
                fi
                ;;
            12)
                image_name=$(select_image_to_delete) || continue
                if [[ -n "$image_name" ]]; then
                    do_delete_image "$image_name"
                fi
                ;;
            0)
                show_account_info
                ;;
            q|Q)
                echo "Goodbye!"
                exit 0
                ;;
            *)
                print_error "Invalid option"
                ;;
        esac
    done
}

main
