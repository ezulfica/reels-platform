#!/usr/bin/env bash
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"
git config core.hooksPath .githooks
printf 'Installed repository hooks from %s/.githooks\n' "$root"
