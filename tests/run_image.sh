#!/bin/bash
# Starts the built image as a fresh install with the default options and checks, from outside:
# a token per default client is logged once, it authenticates over MCP and REST, a call without
# one is refused, a restart keeps the token without logging a new one, and the HEALTHCHECK passes.
# No token is printed.
set -euo pipefail
image=${1:?usage: run_image.sh <image> [platform]}
platform=${2:-}
name=agent-memory-test-$$
data=$(mktemp -d)
port=18765
cleanup() {
  docker rm -f "$name" > /dev/null 2>&1 || true
  rm -rf "$data"
}
trap cleanup EXIT
fail() {
  echo "FAIL $*"
  docker logs "$name" 2>&1 | sed -E 's/(shown once: |Bearer )[A-Za-z0-9_-]+/\1<token>/g' | tail -20
  exit 1
}
python3 - "$data/options.json" "$(dirname "$0")/../agent_memory/config.yaml" << 'EOF'
import json, re, sys
# The defaults a fresh install gets: the options block of config.yaml.
text = open(sys.argv[2], encoding="utf-8").read()
block = text[text.index("\noptions:\n") + 1 : text.index("\nschema:\n")]
clients, cur = [], None
for line in block.splitlines():
    m = re.match(r"^    - name: (\S+)$", line)
    if m:
        cur = {"name": m.group(1), "scopes": []}
        clients.append(cur)
    m = re.match(r"^        - (\w+)$", line)
    if m and cur is not None:
        cur["scopes"].append(m.group(1))
json.dump({"clients": clients, "project_links": [], "namespace_renames": []}, open(sys.argv[1], "w"))
print("default clients:", [c["name"] for c in clients])
EOF
start() {
  docker run -d --name "$name" ${platform:+--platform "$platform"} -v "$data:/data" -p "127.0.0.1:$port:8765" "$image" > /dev/null
  for _ in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:$port/healthz" > /dev/null 2>&1 && return 0
    sleep 1
  done
  fail "the add-on did not answer /healthz within 60 s"
}
start
logs=$(docker logs "$name" 2>&1)
[ "$(grep -c 'new token, shown once' <<< "$logs")" = 2 ] || fail "two new tokens logged on first start"
token=$(sed -nE "s/.*client 'claude-code': new token, shown once: ([A-Za-z0-9_-]+)$/\1/p" <<< "$logs")
echo "claude-code token logged, ${#token} characters"
[ "${#token}" -ge 40 ] || fail "a token of at least 40 characters"
grep -q "claude mcp add --scope user --transport http agent-memory" <<< "$logs" || fail "the Claude Code command is logged"
grep -q 'bearer_token_env_var = "AGENT_MEMORY_TOKEN"' <<< "$logs" || fail "the Codex setting is logged"
version=$(curl -fsS "http://127.0.0.1:$port/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')
grep -qx "version: \"$version\"" "$(dirname "$0")/../agent_memory/config.yaml" || fail "/health version $version matches config.yaml"
echo "health version $version"
mcp() {
  curl -sS -o "$data/reply.json" -w '%{http_code}' -H 'Content-Type: application/json' ${1:+-H "Authorization: Bearer $1"} \
    -d "$2" "http://127.0.0.1:$port/mcp"
}
[ "$(mcp "" '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')" = 401 ] || fail "a call without a token is refused"
[ "$(mcp "$token" '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')" = 200 ] || fail "initialize with the token"
grep -q "\"version\": \"$version\"" "$data/reply.json" || fail "serverInfo carries the version"
store='{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"memory_store","arguments":{"namespace":"github/octo/tools","type":"constraint","title":"Image check","content":"The image stores and finds a record."}}}'
[ "$(mcp "$token" "$store")" = 200 ] || fail "memory_store with the token"
grep -q '"agent\\": \\"claude-code\\"' "$data/reply.json" || fail "the record names the client as its writer"
code=$(curl -sS -o "$data/boot.json" -w '%{http_code}' -H "Authorization: Bearer $token" "http://127.0.0.1:$port/api/bootstrap?namespace=github/octo/tools")
[ "$code" = 200 ] || fail "REST bootstrap answers with the token"
grep -q "Image check" "$data/boot.json" || fail "REST bootstrap returns the stored record"
[ "$(stat -c %a "$data/client_tokens.json")" = 600 ] || fail "client_tokens.json is mode 600"
if grep -qF -- "$token" "$data/client_tokens.json"; then fail "the token itself is not kept"; fi
echo "first start: tokens logged, MCP and REST answer with the token, refused without"
docker restart "$name" > /dev/null
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$port/healthz" > /dev/null 2>&1 && break
  sleep 1
done
[ "$(docker logs "$name" 2>&1 | grep -c 'new token, shown once')" = 2 ] || fail "a restart logs no new token"
[ "$(mcp "$token" '{"jsonrpc":"2.0","id":3,"method":"tools/list"}')" = 200 ] || fail "the token still works after a restart"
echo "restart: no new token, the old one still works"
health=""
for _ in $(seq 1 90); do
  health=$(docker inspect --format '{{.State.Health.Status}}' "$name")
  [ "$health" = healthy ] && break
  sleep 1
done
[ "$health" = healthy ] || fail "the image's HEALTHCHECK reports healthy (got $health)"
echo "HEALTHCHECK: healthy"
echo "image test passed"
