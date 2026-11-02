#!/usr/bin/env bash
# firstmate profile setup (run as agent, after the profile's tools): clone the repo
# into ~/work/firstmate once. An existing checkout is left alone: it is the agents'.
# A private repo needs the GitHub sign-in; until then this fails and the Ship
# retries it after the next sign-in.
set -euo pipefail
url=${SHIPSHAPE_OPT_REPO_URL:?repo_url is not set}
dest=$HOME/work/firstmate
if [ -d "$dest/.git" ]; then
  exit 0
fi
mkdir -p "$HOME/work"
tmp=$(mktemp -d "$HOME/work/.firstmate.XXXXXX")
if ! GIT_TERMINAL_PROMPT=0 git clone --quiet "$url" "$tmp/repo"; then
  rm -rf "$tmp"
  echo "firstmate: could not clone the repo (is GitHub signed in?)" >&2
  exit 1
fi
mv "$tmp/repo" "$dest"
rmdir "$tmp"
echo "cloned firstmate into ~/work/firstmate"
