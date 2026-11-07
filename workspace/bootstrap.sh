#!/usr/bin/env bash
# OnePeek workstation bootstrap (macOS / Linux): one command, safe to re-run.
#   1. installs the tools: Homebrew if this account can write to it, otherwise per-user through mise
#   2. clones or updates every live repo in the org        3. pins Node, Terraform, uv and tflint to what the repos and CI pin
#   4. installs each repo's dependencies, workspace links and git hooks
#   5. runs the backend's own `app setup` (which ends with `app doctor`)
# It wraps the repos' own tooling (backend-service `app` command, onepeek-docs local-stack.md); it does not replace it.
# Repos and versions are read, never listed here, so this file does not go stale when they change:
#   repos come from GitHub (archived ones are left out); versions from .node-version, .terraform-version,
#   .python-version, each repo's packageManager field and local_ci.py's CI pins.
#
# On a new laptop:  mkdir ~/OnePeek && cd ~/OnePeek && git clone https://github.com/OnePeek/.github.git && .github/workspace/bootstrap.sh
# Usage:  bootstrap.sh [--mode minimal|default] [--skip-stack] [--check]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$HERE" == */.github/workspace ]]; then DEFAULT_ROOT="${HERE%/.github/workspace}"; else DEFAULT_ROOT="$HOME/OnePeek"; fi
ROOT="${ONEPEEK_ROOT:-$DEFAULT_ROOT}"
ORG="OnePeek"
MODE="minimal"            # minimal = no ClamAV/observability (lightest); default adds them
SKIP_STACK=0; CHECK_ONLY=0
AWS_LOCAL_PROFILE="onepeek-local"   # local-stack.md UC-LOC-004
REPOS=()
export MISE_YES=1 COREPACK_ENABLE_DOWNLOAD_PROMPT=0

while [[ $# -gt 0 ]]; do case "$1" in
  --mode) MODE="$2"; shift 2;; --skip-stack) SKIP_STACK=1; shift;; --check) CHECK_ONLY=1; shift;;
  *) echo "unknown flag $1"; exit 2;; esac; done

G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; D=$'\e[2m'; N=$'\e[0m'; FAILS=(); WARNS=()
ok()   { echo "${G}✔${N} $*"; }
warn() { echo "${Y}!${N} $*"; WARNS+=("$*"); }
fail() { echo "${R}✘${N} $*"; FAILS+=("$*"); }
die()  { echo "${R}✘ $*${N}"; exit 1; }
step() { echo; echo "${D}── $* ──${N}"; }
have() { command -v "$1" >/dev/null 2>&1; }
OS="$(uname -s)"

quiet() {   # run a noisy step; show its output only if it fails
  local log; log="$(mktemp)"
  if "$@" >"$log" 2>&1; then rm -f "$log"; return 0; fi
  tail -20 "$log"; rm -f "$log"; return 1
}

setup_path() {   # mise's shims go first so its Node wins over any other Node on the machine
  if ! have brew; then
    local b; for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      [[ -x "$b" ]] && { eval "$("$b" shellenv)"; break; }
    done
  fi
  export PATH="$HOME/.local/share/mise/shims:$HOME/.local/bin:$PATH:$HOME/.docker/bin:/Applications/Docker.app/Contents/Resources/bin"
}

is_mac_admin() { id -Gn | tr ' ' '\n' | grep -qx admin; }

brew_usable() {   # brew can exist and still be owned by another account on the same Mac
  have brew || return 1
  local prefix; prefix="$(brew --prefix 2>/dev/null)" || return 1
  [[ -n "$prefix" && -w "$prefix/Cellar" && -w "$prefix/bin" ]]
}

install_with_brew() {
  local p
  for p in git gh awscli gitleaks lefthook mise; do
    have "${p/awscli/aws}" || quiet brew install "$p" || fail "brew install $p"
  done
}

install_with_mise() {   # needs no admin rights: everything lands in the home folder
  have mise || curl -fsSL https://mise.run | sh
  have mise || die "mise did not install. See https://mise.jdx.dev/getting-started.html, then re-run."
  local e
  for e in "gh gh" "aws aws-cli" "gitleaks gitleaks" "lefthook lefthook"; do
    have "${e%% *}" || quiet mise use -g "${e#* }@latest" || fail "mise could not install ${e#* }"
  done
  mise reshim
}

install_tools() {
  step "System tools"
  if [[ "$OS" == "Darwin" ]]; then
    if ! have brew && is_mac_admin; then
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
      setup_path
    fi
    if brew_usable; then
      ok "Homebrew works for this account"
      install_with_brew
    else
      ok "Homebrew is not usable by this account: installing per-user through mise"
      install_with_mise
    fi
  else
    sudo apt-get update -y && sudo apt-get install -y git curl unzip ca-certificates build-essential
    install_with_mise
  fi
  local t; for t in git gh mise; do have "$t" || die "$t is missing and nothing below works without it (see above)."; done
}

persist_shell() {   # so the tools are still there in a new terminal
  local rc sh
  case "$(basename "${SHELL:-}")" in
    zsh)  rc="$HOME/.zshrc"; sh=zsh;;
    bash) rc="$HOME/.bashrc"; [[ "$OS" == "Darwin" ]] && rc="$HOME/.bash_profile"; sh=bash;;
    *)    warn "unknown shell ${SHELL:-}: add ~/.local/bin to PATH and activate mise yourself"; return;;
  esac
  grep -q '# onepeek-bootstrap' "$rc" 2>/dev/null && return
  {
    echo; echo '# onepeek-bootstrap: developer tools'
    brew_usable && echo "eval \"\$($(command -v brew) shellenv)\""
    echo 'export PATH="$HOME/.local/bin:$PATH:$HOME/.docker/bin:/Applications/Docker.app/Contents/Resources/bin"'
    echo "eval \"\$(mise activate $sh)\""
  } >>"$rc"
  ok "added the tools to $rc (open a new terminal to pick them up)"
}

github_auth() {
  step "GitHub"
  if ! gh auth status >/dev/null 2>&1; then
    [[ -t 0 ]] || die "GitHub sign-in needs a terminal: run 'gh auth login -s read:packages', then re-run."
    gh auth login -h github.com -p https -w -s read:packages || die "GitHub sign-in failed."
  elif ! gh auth status 2>&1 | grep -q 'read:packages'; then   # the private @onepeek/* packages need it
    gh auth refresh -h github.com -s read:packages || warn "no read:packages scope: private @onepeek/* packages will not install"
  fi
  gh auth setup-git >/dev/null 2>&1
  ok "github auth"
}

list_repos() {   # the org is the list: a new repo appears here and an archived one drops out by itself
  local names; names="$(gh repo list "$ORG" --no-archived --limit 200 --json name --jq '.[].name')" \
    || die "could not list the $ORG repos (are you invited to the org?)"
  [[ -n "$names" ]] || die "no repos visible in $ORG (are you invited to the org?)"
  REPOS=(); local r; while IFS= read -r r; do REPOS+=("$r"); done <<<"$names"
}

clone_or_update() {
  step "Repos (${#REPOS[@]} live in $ORG)"
  mkdir -p "$ROOT"
  local r d cur def
  for r in "${REPOS[@]}"; do
    d="$ROOT/$r"
    if [[ ! -d "$d/.git" ]]; then
      gh repo clone "$ORG/$r" "$d" -- --quiet && ok "cloned $r" || fail "clone $ORG/$r"
      continue
    fi
    git -C "$d" fetch --quiet --prune || { fail "fetch $r"; continue; }
    cur="$(git -C "$d" rev-parse --abbrev-ref HEAD)"
    def="$(git -C "$d" rev-parse --abbrev-ref origin/HEAD 2>/dev/null)"; def="${def#origin/}"
    if [[ "$cur" == "$def" && -z "$(git -C "$d" status --porcelain)" ]]; then   # never touches a branch or edits in progress
      git -C "$d" pull --ff-only --quiet && ok "$r up to date ($cur)" || warn "$r: could not fast-forward $cur"
    else
      ok "$r left as it is ($cur)"
    fi
  done
}

read_pin() { [[ -f "$ROOT/$1" ]] && tr -d '[:space:]' <"$ROOT/$1"; }

ci_pin() {   # the constant CI and preflight.py use: a different uv fails preflight
  sed -n "s/^$1 = \"\([^\"]*\)\".*/\1/p" "$ROOT/.github/workspace/local_ci.py" 2>/dev/null
}

pin_one() {   # tool, version, where the version came from
  [[ -n "$2" ]] || { fail "no $1 version in $3"; return; }
  quiet mise use -g "$1@$2" && ok "$1 $2 ($3)" || fail "mise could not install $1 $2"
}

pin_tools() {
  step "Tools at the pinned versions, pnpm through corepack"
  pin_one node "$(read_pin onepeek-web/.node-version)" onepeek-web/.node-version
  pin_one terraform "$(read_pin onepeek-infra/.terraform-version)" onepeek-infra/.terraform-version
  pin_one uv "$(ci_pin UV_VERSION)" .github/workspace/local_ci.py
  pin_one tflint "$(ci_pin TFLINT_VERSION)" .github/workspace/local_ci.py
  mise reshim
  have uv || die "uv is missing and nothing below works without it (see above)."
  have corepack || quiet npm i -g corepack   # Node 25 and later no longer ship it
  mise reshim
  mkdir -p "$HOME/.local/bin"
  quiet corepack enable --install-directory "$HOME/.local/bin" pnpm && ok "pnpm via corepack (each repo's packageManager picks the version)" || fail "corepack enable pnpm"
}

install_deps() {
  step "Dependencies"
  [[ -n "${NODE_AUTH_TOKEN:-}" ]] || export NODE_AUTH_TOKEN="$(gh auth token 2>/dev/null)"   # private @onepeek/* packages
  local r d lock
  for r in "${REPOS[@]}"; do
    d="$ROOT/$r"
    if [[ -f "$d/pyproject.toml" ]]; then   # uv reads the repo's .python-version
      (cd "$d" && quiet uv sync) && ok "uv sync $r" || fail "uv sync $r"
    fi
    if [[ -f "$d/package.json" ]]; then
      lock="--no-lockfile"; [[ -f "$d/pnpm-lock.yaml" ]] && lock="--frozen-lockfile"
      (cd "$d" && quiet pnpm install "$lock") && ok "pnpm install $r" || fail "pnpm install $r"
    fi
  done
}

setup_workspace() {
  step "Workspace links and git hooks (.github/workspace/setup.py)"
  [[ -f "$ROOT/.github/workspace/setup.py" ]] || { fail "no .github/workspace/setup.py"; return; }
  (cd "$ROOT" && quiet uv run --no-project python .github/workspace/setup.py) && ok "shared skills, agents and hooks" || warn "workspace setup reported a problem (see above)"
}

aws_profile_check() {
  step "AWS (needed for 'app secrets pull' and Terraform; not for the containers)"
  have aws || { warn "aws CLI missing"; return; }
  if aws configure list-profiles 2>/dev/null | grep -qx "$AWS_LOCAL_PROFILE"; then
    if aws sts get-caller-identity --profile "$AWS_LOCAL_PROFILE" >/dev/null 2>&1; then ok "SSO session valid for $AWS_LOCAL_PROFILE"
    else echo "  Logging in..."; aws sso login --profile "$AWS_LOCAL_PROFILE" && ok "SSO login done" || warn "SSO login failed; retry: aws sso login --profile $AWS_LOCAL_PROFILE"; fi
  else
    warn "AWS profile '$AWS_LOCAL_PROFILE' missing. Copy the [profile ...] blocks from the old laptop's ~/.aws/config (SSO start URL is in IAM Identity Center), then: aws sso login --profile $AWS_LOCAL_PROFILE"
  fi
}

docker_admin_help() {
  local arch=amd64; [[ "$(uname -m)" == "arm64" ]] && arch=arm64
  echo "  Docker Desktop has to be installed once by an admin. Log in to an admin account on this Mac and run:"
  echo "    curl -fLo /tmp/Docker.dmg https://desktop.docker.com/mac/main/$arch/Docker.dmg"
  echo "    sudo hdiutil attach /tmp/Docker.dmg"
  echo "    sudo /Volumes/Docker/Docker.app/Contents/MacOS/install --accept-license --user=$(id -un)"
  echo "    sudo hdiutil detach /Volumes/Docker"
  echo "  Then log back in here and re-run this script."
}

install_docker() {
  step "Docker"
  have docker && { ok "docker installed"; return; }
  if [[ "$OS" == "Darwin" ]]; then
    if brew_usable && is_mac_admin; then quiet brew install --cask docker-desktop
    else docker_admin_help; fi
  else
    curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker "$USER" && warn "Log out/in so the docker group applies."
  fi
  have docker && ok "docker installed" || fail "Docker Desktop is not installed"
}

start_docker() {   # on a first run Docker Desktop shows a prompt to accept
  docker info >/dev/null 2>&1 && return 0
  [[ "$OS" == "Darwin" && -d /Applications/Docker.app ]] || return 1
  echo "  Starting Docker Desktop (accept its prompt if this is the first run)..."
  open -a Docker
  local i; for i in $(seq 1 90); do docker info >/dev/null 2>&1 && return 0; sleep 2; done
  return 1
}

start_stack() {
  step "Local stack (backend-service: app setup --mode $MODE, which ends with app doctor)"
  have docker || { fail "local stack not started: Docker Desktop is not installed"; return; }
  start_docker || { fail "Docker is not running. Start Docker Desktop and re-run."; return; }
  (cd "$ROOT/backend-service" && uv run app setup --mode "$MODE") && ok "app setup and app doctor" || fail "app setup or app doctor (see the lines above; each one names its fix)"
}

checks() {
  step "Connection checks"
  if have gh && gh auth status >/dev/null 2>&1; then
    ok "github auth"
    list_repos
    local r; for r in "${REPOS[@]}"; do [[ -d "$ROOT/$r/.git" ]] && ok "$r cloned" || warn "$r not cloned (re-run without --check)"; done
  else
    fail "github auth"
  fi
  local u; for u in https://pypi.org https://registry.npmjs.org https://github.com; do curl -fsS -o /dev/null -m 8 "$u" && ok "reach $u" || fail "reach $u"; done
  local pin
  pin="$(read_pin onepeek-web/.node-version)"
  [[ -n "$pin" && "$(node --version 2>/dev/null)" == "v$pin"* ]] && ok "node $pin" || warn "node is not the version in onepeek-web/.node-version ($pin)"
  pin="$(read_pin onepeek-infra/.terraform-version)"
  [[ -n "$pin" ]] && terraform version 2>/dev/null | head -1 | grep -qF "v$pin" && ok "terraform $pin" || warn "terraform is not the version in onepeek-infra/.terraform-version ($pin)"
  pin="$(ci_pin UV_VERSION)"
  [[ -n "$pin" ]] && uv --version 2>/dev/null | grep -qF "uv $pin" && ok "uv $pin" || warn "uv is not the version CI pins ($pin)"
  have pnpm && ok "pnpm" || warn "pnpm missing"
  docker info >/dev/null 2>&1 && ok "docker running" || fail "docker not running"
  if [[ $CHECK_ONLY -eq 1 && -d "$ROOT/backend-service" ]] && have uv; then   # the stack's own health report
    (cd "$ROOT/backend-service" && uv run app doctor --mode "$MODE") && ok "app doctor" || fail "app doctor (see the lines above; each one names its fix)"
  fi
  if [[ -f "$ROOT/CLAUDE.md" ]]; then ok "workspace CLAUDE.md present"; else warn "~/OnePeek/CLAUDE.md missing (personal file, not in git) — copy it from the old laptop"; fi
}

main() {
  setup_path
  if [[ $CHECK_ONLY -eq 1 ]]; then checks
  else
    install_tools; persist_shell; github_auth; list_repos; clone_or_update; pin_tools
    install_deps; setup_workspace; aws_profile_check; install_docker
    [[ $SKIP_STACK -eq 0 ]] && start_stack
    checks
  fi

  echo
  (( ${#WARNS[@]} )) && { echo "${Y}Warnings:${N}"; printf '  - %s\n' "${WARNS[@]}"; }
  if (( ${#FAILS[@]} )); then echo "${R}Failed:${N}"; printf '  - %s\n' "${FAILS[@]}"; echo "Fix and re-run bootstrap.sh (it is idempotent)."; exit 1
  else echo "${G}Ready.${N} Start everything: cd $ROOT/backend-service && uv run app dev   (web: http://localhost:20080)"; fi
}

[[ "${BASH_SOURCE[0]}" == "$0" ]] && main
