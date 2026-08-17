# Resolve the Python environment the harness runs in.
#
# Sourced by every script so the environment can be named, relocated, or
# replaced by a conda env without editing call sites. Sets ZEN_BIN to the
# directory holding `python` and the `zen-*` console scripts.
#
# Resolution order:
#   1. $ZEN_BIN            — explicit override
#   2. ./zen-harness/bin   — the documented default
#   3. ./.venv/bin         — earlier layout, still supported
#   4. an active conda/virtualenv on PATH
#   5. whatever `zen-factory-run` resolves to on PATH

_zen_resolve_bin() {
    if [[ -n "${ZEN_BIN:-}" && -x "$ZEN_BIN/python" ]]; then
        printf '%s' "$ZEN_BIN"; return 0
    fi
    local root candidate
    root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    for candidate in "$root/zen-harness/bin" "$root/.venv/bin"; do
        [[ -x "$candidate/python" ]] && { printf '%s' "$candidate"; return 0; }
    done
    # A conda env or virtualenv the caller already activated.
    if [[ -n "${CONDA_PREFIX:-}" && -x "$CONDA_PREFIX/bin/zen-factory-run" ]]; then
        printf '%s' "$CONDA_PREFIX/bin"; return 0
    fi
    if [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
        printf '%s' "$VIRTUAL_ENV/bin"; return 0
    fi
    local found
    found="$(command -v zen-factory-run 2>/dev/null)"
    [[ -n "$found" ]] && { printf '%s' "$(dirname "$found")"; return 0; }
    return 1
}

if ! ZEN_BIN="$(_zen_resolve_bin)"; then
    echo "No harness environment found." >&2
    echo "Create one:  python -m venv zen-harness && zen-harness/bin/pip install -e ." >&2
    echo "Or point at an existing one:  export ZEN_BIN=/path/to/env/bin" >&2
    exit 78
fi
export ZEN_BIN
