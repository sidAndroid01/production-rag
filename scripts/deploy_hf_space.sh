#!/usr/bin/env bash
# Deploy the committed code to a Hugging Face Docker Space.
#
#   export HF_TOKEN=hf_...            # a Hugging Face token with write access
#   scripts/deploy_hf_space.sh <hf-username>/<space-name>
#   DRY_RUN=1 scripts/deploy_hf_space.sh <hf-username>/<space-name>   # list files only
#
# Only files committed to Git are sent (never .env or secrets/). The Space repo
# is a deploy target: its history is replaced on every deploy.
set -euo pipefail

space="${1:?usage: scripts/deploy_hf_space.sh <hf-username>/<space-name>}"
root="$(git rev-parse --show-toplevel)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [[ -n "$(git -C "$root" status --porcelain)" ]]; then
  echo "note: uncommitted changes are NOT deployed; commit them first if you need them." >&2
fi

git -C "$root" archive HEAD | tar -x -C "$work"
# The Space needs the app, not the archive, tests or images (binary files would
# need Git LFS on Hugging Face).
rm -rf "$work/legacy" "$work/docs" "$work/tests" "$work/.github" "$work/deploy"
cp "$root/deploy/huggingface/README.md" "$work/README.md"

commit="$(git -C "$root" rev-parse --short HEAD)"
if [[ -n "${DRY_RUN:-}" ]]; then
  (cd "$work" && find . -type f | sort)
  echo "dry run: would deploy commit $commit to https://huggingface.co/spaces/$space"
  exit 0
fi

: "${HF_TOKEN:?export HF_TOKEN=<a Hugging Face token with write access>}"
cd "$work"
git init -q -b main
git add -A
git -c user.name=deploy -c user.email=deploy@localhost commit -qm "Deploy $commit"
# Send the token as a header so it never appears in a URL. Hugging Face's Git
# server accepts the token only as a Basic-auth password (any username).
auth="$(printf 'hf:%s' "$HF_TOKEN" | base64 | tr -d '\n')"
git -c http.extraheader="Authorization: Basic $auth" \
  push --force --quiet "https://huggingface.co/spaces/$space" main
echo "deployed $commit → https://huggingface.co/spaces/$space"
echo "the Space builds for a few minutes; watch the Logs tab"
