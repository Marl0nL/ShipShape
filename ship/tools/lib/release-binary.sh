# Install a pinned, checksum-verified single binary from a GitHub release into
# ~/.local/bin, as the agent user. Sourced by recipe scripts:
#   install_release_binary NAME VERSION URL_TEMPLATE SHA256_AMD64 SHA256_ARM64
# URL_TEMPLATE may use {version} and {arch} (amd64 or arm64); the tarball must hold
# NAME at its root. Does nothing if that version is already installed.
# shellcheck shell=bash
install_release_binary() {
  local name=$1 version=$2 template=$3 sum_amd64=$4 sum_arm64=$5
  local arch sum url dest=$HOME/.local/bin/$name tmp
  case $(uname -m) in
    x86_64) arch=amd64 sum=$sum_amd64 ;;
    aarch64) arch=arm64 sum=$sum_arm64 ;;
    *) echo "$name: no build for $(uname -m)" >&2; return 1 ;;
  esac
  if [ -x "$dest" ] && [ -f "$dest.version" ] && [ "$(cat "$dest.version")" = "$version" ]; then
    return 0
  fi
  url=${template//\{version\}/$version}
  url=${url//\{arch\}/$arch}
  tmp=$(mktemp -d)
  curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 3 \
    --connect-timeout 20 --max-time 600 -o "$tmp/release.tar.gz" "$url"
  if ! echo "$sum  $tmp/release.tar.gz" | sha256sum -c --quiet -; then
    rm -rf "$tmp"
    echo "$name: checksum mismatch for $url" >&2
    return 1
  fi
  tar -xzf "$tmp/release.tar.gz" -C "$tmp" "$name"
  mkdir -p "$HOME/.local/bin"
  install -m 0755 "$tmp/$name" "$dest"
  echo "$version" >"$dest.version"
  rm -rf "$tmp"
  echo "installed $name $version"
}
