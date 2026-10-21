#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$SCRIPT_DIR"
BIN_DIR="${PRMPT_BIN_DIR:-$HOME/.local/bin}"
VENV_DIR="$HOME/.local/share/prmpt/venv"
MARKER_BEGIN="# >>> prmpt CLI >>>"
MARKER_END="# <<< prmpt CLI <<<"
MODE="release"

usage() {
  cat <<'EOF'
Usage: bash install.sh [--editable] [--uninstall]

Installs the `prmpt` command in an isolated environment. Prefers uv tool,
then pipx, then a private virtualenv at ~/.local/share/prmpt/venv.

By default prmpt is installed as a normal (non-editable) package: the code is
copied out of this directory, so `prmpt` keeps working regardless of what
happens to the source tree. Pass --editable for development mode, where the
installed command imports this source tree directly.

Do not use --editable from a cloud-synced folder (iCloud Drive, Dropbox,
OneDrive). Every import then goes through the sync daemon, and `prmpt` can
stall for many seconds before it prints anything.
EOF
}

warn_editable_cloud_sync() {
  command -v xattr >/dev/null 2>&1 || return 0
  local dir="$PKG_DIR"
  while [[ "$dir" != "/" && -n "$dir" ]]; do
    if xattr -p com.apple.file-provider-domain-id "$dir" >/dev/null 2>&1; then
      echo "Warning: this source tree is inside a cloud-synced folder ($dir)." >&2
      [[ "$MODE" == "editable" ]] && \
        echo "Warning: --editable was requested; prmpt startup may block on sync." >&2
      return 0
    fi
    dir="$(dirname "$dir")"
  done
}

remove_path_block() {
  local rc="$1"
  [[ -f "$rc" ]] || return 0
  python3 - "$rc" "$MARKER_BEGIN" "$MARKER_END" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
begin, end = sys.argv[2], sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
out, skipping = [], False
for line in lines:
    if line.strip() == begin:
        skipping = True
        continue
    if skipping and line.strip() == end:
        skipping = False
        continue
    if not skipping:
        out.append(line)
path.write_text("".join(out), encoding="utf-8")
PY
}

uninstall() {
  echo "Uninstalling prmpt..."
  if command -v uv >/dev/null 2>&1; then
    local uv_bin
    uv_bin="$(uv tool dir --bin 2>/dev/null || true)"
    [[ -n "$uv_bin" ]] && BIN_DIR="$uv_bin"
    uv tool uninstall prmpt-cli >/dev/null 2>&1 || true
  fi
  if command -v pipx >/dev/null 2>&1; then
    local pipx_bin
    pipx_bin="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)"
    [[ -n "$pipx_bin" ]] && BIN_DIR="$pipx_bin"
    pipx uninstall prmpt-cli >/dev/null 2>&1 || true
  fi
  rm -rf "$VENV_DIR"
  rm -f "$BIN_DIR/prmpt"
  remove_path_block "$HOME/.zshrc"
  remove_path_block "$HOME/.bash_profile"
  echo "Removed prmpt. Open a new terminal to refresh PATH."
}

ensure_path() {
  local target="$1"
  if [[ ":$PATH:" == *":$target:"* ]]; then
    return 0
  fi
  local changed=0
  for rc in "$HOME/.zshrc" "$HOME/.bash_profile"; do
    [[ -f "$rc" ]] || continue
    if grep -qF "$MARKER_BEGIN" "$rc"; then
      continue
    fi
    {
      printf '\n%s\n' "$MARKER_BEGIN"
      printf 'export PATH="%s:$PATH"\n' "$target"
      printf '%s\n' "$MARKER_END"
    } >>"$rc"
    changed=1
    echo "Added $target to PATH in $rc"
  done
  if [[ "$changed" -eq 0 ]]; then
    echo "Add this to your shell profile: export PATH=\"$target:\$PATH\""
  else
    echo "Open a new terminal or run: source ~/.zshrc"
  fi
}

check_python() {
  python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required.")
print(f"Using Python {sys.version.split()[0]}")
PY
}

# Stale bytecode and build metadata from an old editable install can pin the
# import path to a directory that no longer exists, or be unusable if the tree
# was moved. Remove them before installing.
clean_artifacts() {
  find "$PKG_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$PKG_DIR" -type f -name '*.pyc' -delete 2>/dev/null || true
  rm -rf "$PKG_DIR"/src/*.egg-info "$PKG_DIR"/build "$PKG_DIR"/dist 2>/dev/null || true
}

install_with_uv() {
  command -v uv >/dev/null 2>&1 || return 1
  local editable_flag=""
  [[ "$MODE" == "editable" ]] && editable_flag="--editable"
  echo "Installing with uv tool ($MODE)..."
  set +e
  # --reinstall matters: uv caches local-directory builds, so a plain --force
  # can silently reinstall the previous wheel after you edit src/.
  uv tool install --force --reinstall $editable_flag "$PKG_DIR"
  local rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    echo "uv tool could not complete the install; trying another method." >&2
    return 1
  fi
  local uv_bin
  uv_bin="$(uv tool dir --bin 2>/dev/null || true)"
  if [[ -n "$uv_bin" ]]; then
    BIN_DIR="$uv_bin"
  fi
  return 0
}

install_with_pipx() {
  command -v pipx >/dev/null 2>&1 || return 1
  local editable_flag=""
  [[ "$MODE" == "editable" ]] && editable_flag="--editable"
  echo "Installing with pipx ($MODE, pip backend)..."
  set +e
  pipx install --backend pip --force $editable_flag "$PKG_DIR"
  local rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    echo "pipx could not complete the install; falling back to a private virtualenv." >&2
    return 1
  fi
  local pipx_bin
  pipx_bin="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)"
  if [[ -n "$pipx_bin" ]]; then
    BIN_DIR="$pipx_bin"
  fi
  return 0
}

install_with_venv() {
  local editable_flag=""
  [[ "$MODE" == "editable" ]] && editable_flag="--editable"
  echo "Creating a private virtualenv ($MODE)..."
  rm -rf "$VENV_DIR"
  mkdir -p "$BIN_DIR"
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV_DIR"
    uv pip install --reinstall --python "$VENV_DIR/bin/python" $editable_flag "$PKG_DIR"
  else
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
    "$VENV_DIR/bin/python" -m pip install $editable_flag "$PKG_DIR"
  fi
  ln -sf "$VENV_DIR/bin/prmpt" "$BIN_DIR/prmpt"
}

main() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --help|-h)
        usage
        return 0
        ;;
      --uninstall)
        uninstall
        return 0
        ;;
      --editable|-e|--dev)
        MODE="editable"
        shift
        ;;
      *)
        usage >&2
        return 2
        ;;
    esac
  done

  check_python
  warn_editable_cloud_sync
  clean_artifacts
  mkdir -p "$BIN_DIR"

  if ! install_with_uv; then
    if ! install_with_pipx; then
      install_with_venv
    fi
  fi

  ensure_path "$BIN_DIR"

  if [[ ! -x "$BIN_DIR/prmpt" ]]; then
    echo "Install finished, but $BIN_DIR/prmpt was not found." >&2
    echo "Try: pipx install --force \"$PKG_DIR\"" >&2
    return 1
  fi

  echo
  "$BIN_DIR/prmpt" --version
  echo "Installed at: $BIN_DIR/prmpt (mode: $MODE)"
  echo
  echo "Try:"
  echo "  prmpt \"make our onboarding email less corporate\""
  echo "  prmpt --show-config"
  echo "  prmpt \"make this clearer\" --print-prompt"
  if [[ "$MODE" == "release" ]]; then
    echo
    echo "Editing files here will not affect the installed command."
    echo "Use \`bash install.sh --editable\` while developing."
  fi
}

main "$@"
