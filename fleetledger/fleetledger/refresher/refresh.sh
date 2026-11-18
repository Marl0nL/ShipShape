#!/bin/sh
# ShipShape's refresher: keeps this computer's ShipShape SSH setup current by asking
# FleetLedger for this machine's own bundle over ssh (ship-fleet machine-bundle NAME,
# contracts/machine-bundle.schema.json) and applying it as data.
#
#   ~/.ssh/shipshape/refresh                 fetch the bundle and apply it (the timer runs this)
#   ~/.ssh/shipshape/refresh remove-timer    stop and delete the timer the first install left
#   sh refresh install NAME KEY USER HOSTS HOST_KEY [--no-timer] [--no-hc] [--lan]
#                                            the first install (the line ship-fleet trust prints)
#
# What a refresh may change, and nothing else: in ~/.ssh/shipshape, the certificate
# (NAME-cert.pub), known_hosts, config (written from the template below, never from text
# in the bundle), fleet.json (the bundle as checked, without the refresher or the looks),
# state.json, hc-added (the hc machines it added) and looks-applied (the digests of the
# looks folder it wrote); hc's machines, adding herdr-core Ships and removing only those it
# added itself; and, for a machine FleetLedger sends the fleet's Desk looks to (ship-fleet
# trust NAME --looks on), the folder desk-looks in Herdr's folder (~/.config/herdr-app, or
# $HERDR_APP_HOME), only while it is the folder this refresher wrote there (never one of
# your own). It never runs, sources or writes as config any text from the bundle: every
# value is checked against a tight pattern first, and a bundle with one value out of place
# is refused whole. It never replaces itself, so a FleetLedger that was taken over cannot
# run code here through a refresh. A refresh that fails or is refused leaves the last good
# files as they were, says why in one sentence on stderr and in state.json, and exits 1.
#
# Its settings are in ~/.ssh/shipshape/machine.conf, written by the first install: data,
# read line by line, never sourced. See docs/runbooks/trust-machines.md.

set -u
umask 077
LC_ALL=C
export LC_ALL

# 2: reads the fleet's Desk looks (a bundle's `looks`); 1 refuses a bundle that has them.
VERSION=2
D="$HOME/.ssh/shipshape"
CONF="$D/machine.conf"
STATE="$D/state.json"
ADDED="$D/hc-added"
LOOKS_MARK="$D/looks-applied"
UNIT=shipshape-refresh
LABEL=local.shipshape.refresh
MAX_BUNDLE=1048576

stage=
locked=
warning=
looks_mode=
b64d=
sha=

# --- checks: every value from the bundle, the settings or the command line passes one ----------
is_name() { # a Ship or machine name, a tag: a-z, 0-9 and -, up to 31
  case $1 in '' | -* | *[!a-z0-9-]*) return 1 ;; esac
  [ "${#1}" -le 31 ]
}
is_host() { # a host name or IPv4 address (no wildcard, no %, no space)
  case $1 in '' | [!A-Za-z0-9]* | *[!A-Za-z0-9.-]*) return 1 ;; esac
  [ "${#1}" -le 253 ]
}
is_user() {
  case $1 in '' | [!a-z_]* | *[!a-z0-9_-]*) return 1 ;; esac
  [ "${#1}" -le 32 ]
}
is_blob() { # an ed25519 key's base64
  case $1 in AAAAC3NzaC1lZDI1NTE5* ) ;; *) return 1 ;; esac
  case $1 in *[!A-Za-z0-9+/=]*) return 1 ;; esac
  [ "${#1}" -le 120 ]
}
is_member() { # a file's name in a looks folder, as contracts/desk-looks.md names one
  case $1 in
    looks.json) return 0 ;;
    palettes/looks-*.json) m=${1#palettes/looks-}; m=${m%.json} ;;
    logos/*.png | logos/*.jpg) m=${1#logos/}; m=${m%.???} ;;
    *) return 1 ;;
  esac
  is_name "$m"
}
is_path() { # an absolute path, with characters ssh_config and a unit file take as they are
  case $1 in /*) ;; *) return 1 ;; esac
  case $1 in *[!A-Za-z0-9._/@+-]* | */../* | */.. ) return 1 ;; esac
  [ "${#1}" -le 1024 ]
}

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

json_str() { # a JSON string from a sentence of ours (printable ASCII only)
  printf '"%s"' "$(printf '%s' "$1" | tr -cd ' -~' | sed 's/\\/\\\\/g; s/"/\\"/g')"
}

# The last success and certificate end from state.json (written only by this script).
state_field() {
  [ -f "$STATE" ] || return 0
  sed -n "s/.*\"$1\":\"\\([0-9TZ:.-]*\\)\".*/\\1/p" "$STATE" | head -n 1
}

# state.json, for the Fleet panel: when it last tried and last worked, the certificate's end,
# how many Ships, and the last error (null after a success) or a warning (hc).
write_state() { # ok|failed, sentence
  attempt=$(now)
  if [ "$1" = ok ]; then
    success=$attempt
    until_=${cert_until:-}
    count=${ship_count:-0}
    err=null
  else
    success=$(state_field last_success)
    until_=$(state_field certificate_valid_before)
    count=$(sed -n 's/.*"ships":\([0-9]*\).*/\1/p' "$STATE" 2>/dev/null | head -n 1)
    err=$(json_str "$2")
  fi
  [ -n "$success" ] && success="\"$success\"" || success=null
  [ -n "$until_" ] && until_="\"$until_\"" || until_=null
  [ -n "$count" ] || count=null
  if [ -n "$warning" ]; then warn=$(json_str "$warning"); else warn=null; fi
  if is_name "${name:-}"; then who="\"$name\""; else who=null; fi
  tmp="$D/.state.$$"
  printf '{"schema":"shipshape.refresh-state/1","refresher":%s,"machine":%s,"last_attempt":"%s","last_success":%s,"certificate_valid_before":%s,"ships":%s,"last_error":%s,"warning":%s}\n' \
    "$VERSION" "$who" "$attempt" "$success" "$until_" "$count" "$err" "$warn" >"$tmp" \
    && mv -f "$tmp" "$STATE"
}

cleanup() {
  [ -n "$stage" ] && rm -rf "$stage"
  [ -n "$locked" ] && rm -rf "$D/.lock"
  stage=
  locked=
}

# One plain sentence on stderr and in state.json; the last good files stay as they were.
fail() {
  printf '%s\n' "$1" >&2
  [ -d "$D" ] && [ ! -L "$D" ] && write_state failed "$1"
  cleanup
  exit 1
}

usage() {
  printf '%s\n' "$1" >&2
  exit 2
}

# --- the settings -------------------------------------------------------------------------------
read_conf() {
  [ -f "$CONF" ] || fail "There is no $CONF: paste the line ship-fleet trust prints for this machine on the FleetLedger computer."
  name='' key='' user='' hosts='' host_key='' hc=on host_names=overlay hc_path=''
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in '' | '#'*) continue ;; esac
    k=${line%%=*}
    v=${line#*=}
    case $k in
      name) name=$v ;;
      key) key=$v ;;
      user) user=$v ;;
      hosts) hosts=$v ;;
      host_key) host_key=$v ;;
      hc) hc=$v ;;
      hc_path) hc_path=$v ;;
      host_names) host_names=$v ;;
      *) fail "$CONF has a line this refresher does not know; paste the line from ship-fleet trust again." ;;
    esac
  done <"$CONF"
  is_name "$name" || fail "$CONF: name is not a machine name."
  is_path "$key" || fail "$CONF: key is not a path this refresher uses (absolute; letters, digits and ._/@+-)."
  is_user "$user" || fail "$CONF: user is not a user name."
  n=0
  for h in $hosts; do
    is_host "$h" || fail "$CONF: hosts has a name that is not a host name."
    n=$((n + 1))
  done
  if [ "$n" -lt 1 ] || [ "$n" -gt 8 ]; then fail "$CONF: hosts names 1 to 8 host names."; fi
  case $host_key in "ssh-ed25519 "*) ;; *) fail "$CONF: host_key is not an ed25519 host key." ;; esac
  is_blob "${host_key#ssh-ed25519 }" || fail "$CONF: host_key is not an ed25519 host key."
  case $hc in on | off) ;; *) fail "$CONF: hc is on or off." ;; esac
  case $host_names in overlay | lan) ;; *) fail "$CONF: host_names is overlay or lan." ;; esac
  [ -z "$hc_path" ] || is_path "$hc_path" || fail "$CONF: hc_path is not an absolute path."
}

# Only one refresh at a time (the timer and the Fleet panel may start one together).
take_lock() {
  tries=0
  until mkdir "$D/.lock" 2>/dev/null; do
    pid=$(cat "$D/.lock/pid" 2>/dev/null)
    case $pid in '' | *[!0-9]*) pid= ;; esac
    # Left by a refresh that is no longer running: its process is gone, or the lock is older
    # than any refresh may take (systemd stops one after 5 minutes; a PID can be reused).
    if { [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; } \
      || [ -n "$(find "$D/.lock" -maxdepth 0 -mmin +15 2>/dev/null)" ]; then
      rm -rf "$D/.lock"
      continue
    fi
    tries=$((tries + 1))
    [ "$tries" -le 90 ] || fail "Another refresh is still running; this one gave up after 90 seconds."
    sleep 1
  done
  locked=1
  echo $$ >"$D/.lock/pid"
}

# --- reading the bundle -------------------------------------------------------------------------
# A strict JSON reader: one value, objects with keys of a-z and _ (no key twice), no control
# character in a string, nothing after the value, at most 8 levels deep. It prints each
# value as PATH<tab>TYPE<tab>TEXT (strings as they were written, escapes and all), and
# writes the document again, compact, without the top-level "refresher" and "looks", to the
# file CANON names (so fleet.json never grows with the looks).
# shellcheck disable=SC2016  # an awk program: its $ are awk's
JSON_AWK='
function fail(why) { print "! " why; failed = 1; exit 2 }
function out(x) { if (!mute && !quiet) cs = cs x }
function emit(path, type, text) { if (!mute) printf "%s\t%s\t%s\n", path, type, text }
function ws(   c) {
  while (p <= n) {
    c = substr(s, p, 1)
    if (c == " " || c == "\t" || c == "\n" || c == "\r") p++
    else break
  }
}
# Strings with no escape and no line end in them are taken to their closing quote at once,
# within the next w characters: some awks pay for each substr by the length of the whole
# document, so one call a string, not one a character. Anything else, character by character.
function str(w,   c, start, h, rest, q) {
  p++
  start = p
  rest = substr(s, p, w)
  q = index(rest, "\"")
  if (q > 0) {
    rest = substr(rest, 1, q - 1)
    if (index(rest, "\\") == 0 && index(rest, "\t") == 0 && index(rest, "\n") == 0 && index(rest, "\r") == 0) {
      tok = rest
      p += q
      out("\"" tok "\"")
      return
    }
  }
  for (;;) {
    if (p > n) fail("a string does not end")
    c = substr(s, p, 1)
    if (c == "\"") break
    if (c == "\\") {
      c = substr(s, p + 1, 1)
      if (c == "\"" || c == "\\" || c == "/" || c == "b" || c == "f" || c == "n" || c == "r" || c == "t") { p += 2; continue }
      if (c == "u") {
        h = substr(s, p + 2, 4)
        if (h !~ /^[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]$/) fail("a bad \\u escape")
        p += 6
        continue
      }
      fail("a bad escape")
    }
    if (c == "\t" || c == "\n" || c == "\r") fail("a control character in a string")
    p++
  }
  tok = substr(s, start, p - start)
  p++
  out("\"" tok "\"")
}
function value(path, depth,   c, t) {
  if (depth > 8) fail("too deep")
  if (p > 65536) { s = substr(s, p); n = n - p + 1; p = 1 } # what is read already goes
  ws()
  if (p > n) fail("it ends early")
  c = substr(s, p, 1)
  if (c == "{") { object(path, depth); return }
  if (c == "[") { array(path, depth); return }
  # The long strings (the refresher, the files of the looks) in one step; others within 512.
  if (c == "\"") { str((mute || quiet) ? n : 512); emit(path, "s", tok); return }
  if (substr(s, p, 4) == "true") { p += 4; out("true"); emit(path, "b", "true"); return }
  if (substr(s, p, 5) == "false") { p += 5; out("false"); emit(path, "b", "false"); return }
  if (substr(s, p, 4) == "null") { p += 4; out("null"); emit(path, "z", "null"); return }
  t = ""
  while (p <= n && index("-+.eE0123456789", substr(s, p, 1)) > 0) { t = t substr(s, p, 1); p++ }
  if (t !~ /^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?$/) fail("not JSON")
  out(t)
  emit(path, "n", t)
}
function object(path, depth,   first, key, kp, skip, hide, c, m) {
  p++
  out("{")
  emit(path, "o", "")
  first = 1
  ws()
  if (substr(s, p, 1) == "}") { p++; out("}"); return }
  for (;;) {
    ws()
    if (substr(s, p, 1) != "\"") fail("a key is not a string")
    m = mute
    mute = 1
    str(64)
    mute = m
    key = tok
    if (key !~ /^[a-z_]+$/ || length(key) > 40) fail("a key that is not a-z and _")
    kp = (path == "" ? key : path "." key)
    if (kp in seen) fail("a key given twice")
    seen[kp] = 1
    ws()
    if (substr(s, p, 1) != ":") fail("not JSON")
    p++
    skip = (path == "" && key == "refresher") # not read at all
    hide = (path == "" && key == "looks") # read and checked, but not kept in CANON
    if (!skip && !hide) { out((first ? "" : ",") "\"" key "\":"); first = 0 }
    if (skip) mute++
    if (hide) quiet++
    value(kp, depth + 1)
    if (skip) mute--
    if (hide) quiet--
    ws()
    c = substr(s, p, 1)
    if (c == ",") { p++; continue }
    if (c == "}") { p++; out("}"); return }
    fail("not JSON")
  }
}
function array(path, depth,   i, c) {
  p++
  out("[")
  i = 0
  ws()
  if (substr(s, p, 1) == "]") { p++; out("]"); emit(path, "a", 0); return }
  for (;;) {
    if (i >= 1000) fail("a list that is too long")
    if (i > 0) out(",")
    value(path "." i, depth + 1)
    i++
    ws()
    c = substr(s, p, 1)
    if (c == ",") { p++; continue }
    if (c == "]") { p++; out("]"); emit(path, "a", i); return }
    fail("not JSON")
  }
}
{ s = s $0 "\n" }
END {
  if (failed) exit 2
  n = length(s)
  p = 1
  mute = 0
  quiet = 0
  ws()
  if (substr(s, p, 1) != "{") fail("not a JSON object")
  value("", 0)
  ws()
  if (p <= n) fail("something after the document")
  printf "%s\n", cs > ENVIRON["CANON"]
}
'

# The bundle, field by field, against contracts/machine-bundle.schema.json and tighter: every
# value this script writes anywhere is checked here. Prints what the shell applies, one record
# a line, each word already checked (so none holds a space, a quote or a glob it should not):
#   M machine | A at | C type blob valid_before (or C - - -) | N renewed code (or -)
#   U user | K a known_hosts line | S name runtime lan overlay(-) host_cert tags(a,b or -)
#   L index name size (a file of the fleet's looks; its base64 is written to the file
#   LOOKS_DATA/index, named by its index alone) | G digest (or G - : FleetLedger keeps none);
#   no L or G when the bundle has no looks.
# shellcheck disable=SC2016  # an awk program: its $ are awk's
CHECK_AWK='
function bad(why) { print "! " why; failed = 1; exit 3 }
function name_ok(x) { return x ~ /^[a-z0-9][a-z0-9-]*$/ && length(x) <= 31 }
function host_ok(x) { return x ~ /^[A-Za-z0-9][A-Za-z0-9.-]*$/ && length(x) <= 253 }
function stamp_ok(x) { return x ~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9](\.[0-9]+)?Z$/ }
# Base64 patterns as strings: some awks (busybox) read \/ in a bracket as a backslash too.
function blob_ok(x) { return x ~ "^AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]*=?=?$" && length(x) <= 120 }
function certblob_ok(x) { return x ~ "^AAAAIHNzaC1lZDI1NTE5LWNlcnQtdjAxQG9wZW5zc2guY29t[A-Za-z0-9+/]*=?=?$" && length(x) <= 4000 }
function fl_host(x) { return index(" " tolower(flhosts) " ", " " tolower(x) " ") > 0 }
function need(path, type) { if (!(path in T)) bad("no " path); if (T[path] != type) bad(path " is not what it should be") }
# A looks file'"'"'s name (contracts/desk-looks.md): looks.json, palettes/looks-<id>.json or
# logos/<name>.png|.jpg, the id or name a-z, 0-9 and - (first not -), up to 31.
function member_ok(x,   m) {
  if (x == "looks.json") return 1
  if (x ~ "^palettes/looks-[a-z0-9][a-z0-9-]*[.]json$") m = substr(x, 16, length(x) - 20)
  else if (x ~ "^logos/[a-z0-9][a-z0-9-]*[.](png|jpg)$") m = substr(x, 7, length(x) - 10)
  else return 0
  return length(m) >= 1 && length(m) <= 31
}
function b64_ok(x) { return x ~ "^[A-Za-z0-9+/]*=?=?$" && length(x) % 4 == 0 && length(x) <= 87384 }
function names_ok(list,   k, i, a) {
  k = split(list, a, ",")
  if (k < 1 || k > 400) return 0
  for (i = 1; i <= k; i++) if (!host_ok(a[i]) || fl_host(a[i])) return 0
  return 1
}
BEGIN { FS = "\t" }
/^! / { print; failed = 1; exit 3 }
{
  if (NF != 3) bad("an odd value")
  path = $1; t = $2; v = $3
  if (path in T) bad("a field given twice")
  T[path] = t
  V[path] = v
}
END {
  if (failed) exit 3
  # Every field, by where it is; anything else is refused.
  for (path in T) {
    k = split(path, a, ".")
    t = T[path]; v = V[path]
    if (path == "") { if (t != "o") bad("not an object"); continue }
    top = a[1]
    if (k == 1) {
      if (top == "schema") { if (t != "s" || v != "shipshape.machine-bundle/1") bad("not a machine bundle (version 1)") }
      else if (top == "machine") { if (t != "s" || v != me) bad("it is not this machine'"'"'s bundle") }
      else if (top == "at") { if (t != "s" || !stamp_ok(v)) bad("at") }
      else if (top == "certificate" || top == "ssh" || top == "idle_limit") { if (t != "o") bad(top) }
      else if (top == "scope" || top == "logins" || top == "known_hosts" || top == "ships") { if (t != "a") bad(top) }
      else if (top == "fleetledger") { if (t != "o" && t != "z") bad(top) }
      else if (top == "looks") { if (t != "o" && t != "z") bad(top) }
      else bad("an unknown field, " path)
      continue
    }
    if (top == "looks") {
      if (k == 2 && a[2] == "digest") { if (t != "s" || v !~ /^[0-9a-f]+$/ || length(v) != 64) bad("looks.digest"); continue }
      if (k == 2 && a[2] == "files") { if (t != "a") bad("looks.files"); continue }
      if (k == 3 && a[2] == "files") { if (t != "o") bad("a looks file"); continue }
      if (k == 4 && a[2] == "files" && a[4] == "name") { if (t != "s" || !member_ok(v)) bad("a looks file'"'"'s name"); continue }
      if (k == 4 && a[2] == "files" && a[4] == "size") { if (t != "n" || v !~ /^[1-9][0-9]*$/ || length(v) > 6) bad("a looks file'"'"'s size"); continue }
      if (k == 4 && a[2] == "files" && a[4] == "data") { if (t != "s" || !b64_ok(v)) bad("a looks file'"'"'s data"); continue }
      bad("an unknown field, " path)
    }
    if (top == "certificate") {
      f = a[2]
      if (k == 2 && f == "line") { if (t == "z") continue; if (t != "s") bad("certificate.line"); continue }
      if (k == 2 && f == "valid_before") { if (t == "z") continue; if (t != "s" || !stamp_ok(v)) bad("certificate.valid_before"); continue }
      if (k == 2 && f == "renewed") { if (t != "b") bad("certificate.renewed"); continue }
      if (k == 2 && f == "not_renewed") { if (t != "z" && t != "o") bad("certificate.not_renewed"); continue }
      if (k == 3 && a[2] == "not_renewed" && a[3] == "code") { if (t != "s" || (v != "vault-locked" && v != "sign-failed")) bad("not_renewed.code"); continue }
      if (k == 3 && a[2] == "not_renewed" && a[3] == "why") { if (t != "s" || length(v) < 1 || length(v) > 6000) bad("not_renewed.why"); continue }
      bad("an unknown field, " path)
    }
    if (top == "scope" && k == 2) { if (t != "s" || v !~ /^(all|tag:[a-z0-9][a-z0-9-]*|ship:[a-z0-9][a-z0-9-]*)$/ || length(v) > 36) bad("scope"); continue }
    if (top == "logins" && k == 2) { if (t != "s" || (v != "agent" && v != "owner")) bad("logins"); continue }
    if (top == "ssh" && k == 2 && a[2] == "user") { if (t != "s" || v !~ /^[a-z_][a-z0-9_-]*$/ || length(v) > 32) bad("ssh.user"); continue }
    if (top == "known_hosts" && k == 2) { if (t != "s") bad("known_hosts"); continue }
    if (top == "fleetledger" && k == 2) {
      if (a[2] == "name") { if (t != "s" || !name_ok(v)) bad("fleetledger.name"); continue }
      if (a[2] == "known_hosts") { if (t != "s" || v !~ "^[A-Za-z0-9][A-Za-z0-9.,-]* ssh-ed25519 [A-Za-z0-9+/]+=?=?$") bad("fleetledger.known_hosts"); continue }
      bad("an unknown field, " path)
    }
    if (top == "idle_limit" && k == 2) {
      if (a[2] == "on") { if (t != "b") bad("idle_limit.on"); continue }
      if (a[2] == "days") { if (t != "n" || v !~ /^[1-9][0-9]*$/ || length(v) > 5) bad("idle_limit.days"); continue }
      bad("an unknown field, " path)
    }
    if (top == "ships" && k >= 3) {
      f = a[3]
      if (k == 3 && f == "name") { if (t != "s" || !name_ok(v) || fl_host(v) || v == "localhost") bad("a Ship name"); continue }
      if (k == 3 && f == "tags") { if (t != "a") bad("a Ship'"'"'s tags"); continue }
      if (k == 4 && f == "tags") { if (t != "s" || !name_ok(v)) bad("a Ship'"'"'s tag"); continue }
      if (k == 3 && f == "runtime") { if (t != "s" || (v != "core" && v != "herdr")) bad("a Ship'"'"'s runtime"); continue }
      if (k == 3 && f == "host_names") { if (t != "o") bad("a Ship'"'"'s host names"); continue }
      if (k == 4 && f == "host_names" && a[4] == "lan") { if (t != "s" || !host_ok(v) || fl_host(v)) bad("a Ship'"'"'s host name"); continue }
      if (k == 4 && f == "host_names" && a[4] == "overlay") { if (t == "z") continue; if (t != "s" || !host_ok(v) || fl_host(v)) bad("a Ship'"'"'s host name"); continue }
      if (k == 3 && f == "host_certificate") { if (t != "b") bad("a Ship'"'"'s host_certificate"); continue }
      bad("an unknown field, " path)
    }
    if (top == "ships" && k == 2) { if (t != "o") bad("a Ship"); continue }
    bad("an unknown field, " path)
  }
  need("schema", "s"); need("machine", "s"); need("at", "s")
  need("certificate", "o"); need("certificate.renewed", "b")
  if (!("certificate.line" in T) || !("certificate.valid_before" in T) || !("certificate.not_renewed" in T)) bad("no certificate")
  need("scope", "a"); need("logins", "a"); need("ssh", "o"); need("ssh.user", "s")
  need("known_hosts", "a"); need("ships", "a"); need("idle_limit", "o")
  need("idle_limit.on", "b"); need("idle_limit.days", "n")
  if (!("fleetledger" in T)) bad("no fleetledger")
  if (V["scope"] < 1 || V["logins"] < 1) bad("no scope")
  if (T["certificate.not_renewed"] == "o") need("certificate.not_renewed.code", "s")
  if (T["certificate.not_renewed"] == "o") need("certificate.not_renewed.why", "s")
  if (T["fleetledger"] == "o") { need("fleetledger.name", "s"); need("fleetledger.known_hosts", "s") }

  print "M " V["machine"]
  print "A " V["at"]
  if (T["certificate.line"] == "s") {
    k = split(V["certificate.line"], c, " ")
    if (k < 2 || c[1] != "ssh-ed25519-cert-v01@openssh.com" || !certblob_ok(c[2])) bad("certificate.line is not an ed25519 certificate")
    if (T["certificate.valid_before"] != "s") bad("certificate.valid_before")
    print "C " c[1] " " c[2] " " V["certificate.valid_before"]
  } else {
    if (T["certificate.valid_before"] != "z") bad("certificate.valid_before")
    print "C - - -"
  }
  code = (T["certificate.not_renewed"] == "o") ? V["certificate.not_renewed.code"] : "-"
  print "N " V["certificate.renewed"] " " code
  print "U " V["ssh.user"]

  cas = 0
  for (i = 0; i < V["known_hosts"]; i++) {
    x = V["known_hosts." i]
    k = split(x, w, " ")
    if (k < 4) bad("a known_hosts line")
    if (w[1] == "@cert-authority" && names_ok(w[2]) && w[3] == "ssh-ed25519" && blob_ok(w[4])) {
      if (++cas > 1) bad("more than one @cert-authority line")
      print "K @cert-authority " w[2] " ssh-ed25519 " w[4]
    } else if (w[1] == "@revoked" && w[2] == "*" && w[3] == "ssh-ed25519" && blob_ok(w[4])) {
      print "K @revoked * ssh-ed25519 " w[4]
    } else bad("a known_hosts line")
  }

  if (V["ships"] > 500) bad("too many Ships")
  for (i = 0; i < V["ships"]; i++) {
    b = "ships." i
    need(b ".name", "s"); need(b ".tags", "a"); need(b ".runtime", "s"); need(b ".host_names", "o")
    need(b ".host_names.lan", "s"); need(b ".host_certificate", "b")
    if (!((b ".host_names.overlay") in T)) bad("a Ship'"'"'s host names")
    nm = V[b ".name"]
    if (nm in seenship) bad("a Ship listed twice")
    seenship[nm] = 1
    tags = ""
    for (j = 0; j < V[b ".tags"]; j++) tags = tags (j ? "," : "") V[b ".tags." j]
    if (tags == "") tags = "-"
    ov = (T[b ".host_names.overlay"] == "s") ? V[b ".host_names.overlay"] : "-"
    print "S " nm " " V[b ".runtime"] " " V[b ".host_names.lan"] " " ov " " V[b ".host_certificate"] " " tags
  }

  # The fleet'"'"'s Desk looks: only in the bundle of a machine FleetLedger sends them to.
  if (("looks" in T) && T["looks"] == "z") print "G -"
  if (("looks" in T) && T["looks"] == "o") {
    need("looks.digest", "s"); need("looks.files", "a")
    nf = V["looks.files"]
    if (nf < 1 || nf > 33) bad("looks.files")
    total = 0; np = 0; nl = 0; nj = 0
    for (i = 0; i < nf; i++) {
      b = "looks.files." i
      need(b ".name", "s"); need(b ".size", "n"); need(b ".data", "s")
      nm = V[b ".name"]; sz = V[b ".size"] + 0; d = V[b ".data"]
      if (nm in seenlook) bad("a looks file listed twice")
      seenlook[nm] = 1
      if (nm == "looks.json") { nj++; most = 65536 }
      else if (substr(nm, 1, 9) == "palettes/") { np++; most = 16384 }
      else { nl++; most = 65536 }
      if (sz > most) bad("a looks file over its limit, " nm)
      total += sz
      if (length(d) != 4 * int((sz + 2) / 3)) bad("a looks file'"'"'s data is not its size, " nm)
      f = ENVIRON["LOOKS_DATA"] "/" i
      printf "%s", d > f
      close(f)
      print "L " i " " nm " " sz
    }
    if (nj != 1) bad("no looks.json in looks")
    if (np > 16 || nl > 16) bad("more looks files than a folder may have")
    if (total > 524288) bad("looks over 512 KiB")
    print "G " V["looks.digest"]
  }
}
'

# --- fetching -----------------------------------------------------------------------------------
# ssh to FleetLedger with this machine's key alone, its host key pinned from machine.conf, and
# no ssh_config (so nothing this script writes, and nothing in ~/.ssh/config, changes where it
# connects or what it trusts). The command it asks for is fixed: a key whose authorized_keys
# line forces ship-fleet machine-bundle NAME runs that whatever is asked; your own key, which
# has a shell there, runs it as asked. Its output is cut off at 1 MiB, its stderr at 4 KiB.
fetch() {
  pin="$stage/pin"
  printf '%s %s\n' "$(printf '%s' "$hosts" | tr -s ' ' ',')" "$host_key" >"$pin"
  said=
  for h in $hosts; do
    rm -f "$stage/out" "$stage/err" "$stage/rc"
    {
      {
        ssh -F none -T -x -a -i "$key" -l "$user" \
          -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
          -o UserKnownHostsFile="$pin" -o GlobalKnownHostsFile=/dev/null \
          -o UpdateHostKeys=no -o HostKeyAlgorithms=ssh-ed25519 -o CheckHostIP=no \
          -o VerifyHostKeyDNS=no -o ConnectTimeout=15 -o ServerAliveInterval=15 \
          -o ServerAliveCountMax=2 -o ClearAllForwardings=yes -o ForwardAgent=no \
          -o PermitLocalCommand=no -o ControlMaster=no -o ControlPath=none \
          -o ProxyCommand=none -o RequestTTY=no -o LogLevel=ERROR \
          -- "$h" ship-fleet machine-bundle "$name" </dev/null 2>&1 1>&3 3>&-
        echo $? >"$stage/rc"
      } | head -c 4096 >"$stage/err"
    } 3>&1 | head -c $((MAX_BUNDLE + 1)) >"$stage/out"
    rc=$(cat "$stage/rc" 2>/dev/null)
    [ "$(wc -c <"$stage/out")" -le "$MAX_BUNDLE" ] || fail "FleetLedger's answer is over 1 MiB; refused, and the files here are unchanged."
    # FleetLedger's refusal is its first line; ssh's own reason is its last (after any banner).
    first=$(grep -v '^[[:space:]]*$' "$stage/err" 2>/dev/null | head -n 1 | tr -cd ' -~' | cut -c 1-300)
    last=$(grep -v '^[[:space:]]*$' "$stage/err" 2>/dev/null | tail -n 1 | tr -cd ' -~' | cut -c 1-300)
    case $rc in
      0) return 0 ;;
      255) said="${said:+$said; }ssh to $user@$h: ${last:-it gave no reason}" ;;
      '') fail "FleetLedger's answer was cut off: more than 1 MiB, or ssh was stopped." ;;
      *) fail "FleetLedger refused this machine's bundle: ${first:-it gave no reason} (the files here are unchanged)." ;;
    esac
  done
  fail "Could not reach FleetLedger ($said); the files here are unchanged."
}

check_bundle() {
  [ -s "$stage/out" ] || fail "FleetLedger answered with nothing; the files here are unchanged."
  [ "$(wc -c <"$stage/out")" -le "$MAX_BUNDLE" ] || fail "FleetLedger's answer is over 1 MiB; refused, and the files here are unchanged."
  # Printable ASCII, tabs and line ends only (FleetLedger writes JSON with \u escapes).
  [ "$(tr -d '\011\012\015 -~' <"$stage/out" | wc -c)" -eq 0 ] \
    || fail "FleetLedger's answer holds bytes JSON from it never has; refused, and the files here are unchanged."
  flhosts=$(printf '%s' "$hosts" | tr -s ' ' ' ')
  CANON="$stage/fleet.json" awk "$JSON_AWK" <"$stage/out" >"$stage/flat"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    why=$(sed -n 's/^! //p' "$stage/flat" | head -n 1)
    fail "FleetLedger's answer is not a document this refresher takes (${why:-unreadable}); refused, and the files here are unchanged."
  fi
  mkdir "$stage/lk" || fail "Could not make a folder in $stage."
  LOOKS_DATA="$stage/lk" awk -v me="$name" -v flhosts="$flhosts" "$CHECK_AWK" <"$stage/flat" >"$stage/records"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    why=$(sed -n 's/^! //p' "$stage/records" | head -n 1)
    fail "FleetLedger's bundle has a value out of place (${why:-unreadable}); refused, and the files here are unchanged."
  fi
}

# --- writing ------------------------------------------------------------------------------------
# ~ for this computer's home in the SSH config, so a home with odd characters still works.
tilde() {
  # shellcheck disable=SC2088  # ~ is for ssh_config, which expands it
  case $1 in "$HOME"/*) printf '~/%s' "${1#"$HOME"/}" ;; *) printf '%s' "$1" ;; esac
}

build_files() {
  cert_type='' cert_blob='' cert_until='' renewed='' code='' bundle_user='' ship_count=0
  : >"$stage/known_hosts.ca"
  : >"$stage/ships"
  while read -r kind a b c d e f; do
    case $kind in
      C) cert_type=$a cert_blob=$b cert_until=$c ;;
      N) renewed=$a code=$b ;;
      U) bundle_user=$a ;;
      K) printf '%s %s %s %s\n' "$a" "$b" "$c" "$d" >>"$stage/known_hosts.ca" ;;
      S) printf '%s %s %s %s %s %s\n' "$a" "$b" "$c" "$d" "$e" "$f" >>"$stage/ships"
         ship_count=$((ship_count + 1)) ;;
    esac
  done <"$stage/records"
  : "$renewed"
  case $code in
    vault-locked) fail "FleetLedger's vault is locked, so it signed no new certificate for $name; run ship-fleet unlock there. The files here are unchanged${cert_until_old:+ (the certificate here lasts until $cert_until_old)}." ;;
    sign-failed) fail "FleetLedger could not sign a new certificate for $name (its audit log says why); the files here are unchanged." ;;
  esac
  [ "$cert_type" != - ] || fail "FleetLedger has no certificate for $name; the files here are unchanged."

  printf '%s %s %s\n' "$cert_type" "$cert_blob" "$name" >"$stage/cert"
  got=$(ssh-keygen -L -f "$stage/cert" 2>/dev/null | sed -n 's/^ *Public key: [A-Z0-9-]* //p' | head -n 1)
  [ -n "$got" ] || fail "FleetLedger's certificate does not read as one; refused, and the files here are unchanged."
  if [ -f "$key.pub" ]; then
    want=$(ssh-keygen -l -f "$key.pub" 2>/dev/null | cut -d ' ' -f 2)
    [ "$want" = "$got" ] || fail "FleetLedger's certificate is for another key than $key; refused, and the files here are unchanged."
  fi

  {
    echo "# ShipShape: trust Ships' host certificates (written by ~/.ssh/shipshape/refresh)."
    cat "$stage/known_hosts.ca"
  } >"$stage/known_hosts"

  # A Ship whose name is a Host of your own in ~/.ssh/config is left out (it would take that
  # alias over, since the Include comes first), and said.
  own=$(awk 'tolower($1) == "host" { for (i = 2; i <= NF; i++) print tolower($i) }' \
    "$HOME/.ssh/config" 2>/dev/null | tr '\n' ' ')
  : >"$stage/ships.kept"
  left=''
  while read -r s_name rest; do
    if has "$s_name" "$own"; then left="$left $s_name"; else echo "$s_name $rest" >>"$stage/ships.kept"; fi
  done <"$stage/ships"
  mv -f "$stage/ships.kept" "$stage/ships"
  [ -n "$left" ] && warning="Left out:$left (each is a Host of your own in ~/.ssh/config)."

  ident=$(tilde "$key")
  {
    echo "# ShipShape Ships for $name (written by ~/.ssh/shipshape/refresh from FleetLedger's bundle)."
    echo "# Rewritten at every refresh: change machine.conf, not this file."
    while read -r s_name s_rt s_lan s_ov s_cert s_tags; do
      host=$s_lan
      [ "$host_names" = overlay ] && [ "$s_ov" != - ] && host=$s_ov
      : "$s_rt" "$s_cert" "$s_tags"
      printf '\nHost %s\n' "$s_name"
      printf '  HostName %s\n' "$host"
      printf '  User %s\n' "$bundle_user"
      printf '  IdentityFile %s\n' "$ident"
      printf '  CertificateFile ~/.ssh/shipshape/%s-cert.pub\n' "$name"
      printf '  IdentitiesOnly yes\n'
      printf '  UserKnownHostsFile ~/.ssh/shipshape/known_hosts\n'
      printf '  GlobalKnownHostsFile /dev/null\n'
      printf '  StrictHostKeyChecking yes\n'
      printf '  HostKeyAlgorithms ssh-ed25519-cert-v01@openssh.com\n'
      printf '  UpdateHostKeys no\n'
      printf '  ForwardAgent no\n'
      printf '  ForwardX11 no\n'
    done <"$stage/ships"
  } >"$stage/config"
}

put() { # stage file -> its place in ~/.ssh/shipshape, by rename
  { chmod 600 "$stage/$1" && mv -f "$stage/$1" "$D/$2"; } || fail "Could not write $D/$2; the other files here may be from this refresh."
}

apply_files() {
  put cert "$name-cert.pub"
  put known_hosts known_hosts
  put config config
  put fleet.json fleet.json
}

# --- hc (herdr-core) ----------------------------------------------------------------------------
# Adds each herdr-core Ship that has a host certificate and that hc does not have; removes
# only the machines this script added (listed in hc-added) once their Ship has left the
# bundle or no longer runs herdr-core. A machine you or install.sh added is never removed.
find_hc() {
  if [ -n "$hc_path" ] && [ -x "$hc_path" ]; then printf '%s' "$hc_path"; return; fi
  command -v hc 2>/dev/null && return
  [ -x "$HOME/.local/bin/hc" ] && printf '%s' "$HOME/.local/bin/hc"
}

has() { # word list
  case " $2 " in *" $1 "*) return 0 ;; esac
  return 1
}

sync_hc() {
  [ "$hc" = on ] || return 0
  ours=
  if [ -f "$ADDED" ]; then
    while IFS= read -r x; do is_name "$x" && ours="$ours $x"; done <"$ADDED"
  fi
  wanted='' new=''
  if [ "$bundle_user" = agent ]; then # hc attaches to the agent
    while read -r s_name s_rt s_lan s_ov s_cert s_tags; do
      : "$s_lan" "$s_ov" "$s_tags"
      [ "$s_rt" = core ] || continue
      wanted="$wanted $s_name"
      [ "$s_cert" = true ] && new="$new $s_name"
    done <"$stage/ships"
  fi
  bin=$(find_hc)
  if [ -z "$bin" ]; then
    [ -n "$wanted" ] && warning="hc (herdr-core) is not installed here, so no Ship was added to it."
    return 0
  fi
  if ! known=$("$bin" machine ls --json </dev/null 2>/dev/null); then
    warning="hc machine ls did not answer, so hc's machines were left as they were."
    return 0
  fi
  inhc=$(printf '%s\n' "$known" | grep -oE '"name" *: *"[a-z0-9-]+"' | sed 's/.*"\([a-z0-9-]*\)"$/\1/' | tr '\n' ' ')
  keep='' problems=''
  for s in $new; do
    if has "$s" "$inhc"; then
      has "$s" "$ours" && keep="$keep $s"
    elif "$bin" machine add "$s" --name "$s" </dev/null >/dev/null 2>&1; then
      keep="$keep $s"
      echo "hc added $s."
    else
      problems="$problems $s"
    fi
  done
  for s in $ours; do
    has "$s" "$keep" && continue
    if has "$s" "$wanted"; then
      keep="$keep $s" # still its Ship's, not added again yet (no host certificate)
    elif has "$s" "$inhc"; then
      if "$bin" machine rm "$s" </dev/null >/dev/null 2>&1; then
        echo "hc removed $s: it has left this machine's Ships."
      else
        keep="$keep $s"
        problems="$problems $s"
      fi
    fi
  done
  [ -n "$problems" ] && warning="hc could not add or remove:$problems (try ssh NAME true)."
  tmp="$stage/hc-added"
  : >"$tmp"
  for s in $keep; do echo "$s" >>"$tmp"; done
  mv -f "$tmp" "$ADDED"
}

# --- the fleet's Desk looks ---------------------------------------------------------------------
# Only for a machine FleetLedger sends them to (ship-fleet trust NAME --looks on): the folder of
# contracts/desk-looks.md, each file base64 under a name the format gives it, and the folder's
# digest. Every file is checked before anything is written (decoded to the size it says, each
# logo by its first bytes, each JSON file by the strict reader above, the folder by its digest),
# and a bundle with one out of place is refused whole like any other. The folder is written into
# Herdr's folder as desk-looks, whole, by rename, only where there is none or where the one there
# is a folder this refresher wrote (its digest is in looks-applied): a desk-looks of your own is
# never changed or removed. Nothing in it is run, and the only paths made from it are the names
# checked here.

add_warning() { warning="${warning:+$warning }$1"; }

# The tools the checks need; when one is missing the looks are left (said), never unchecked.
find_looks_tools() {
  if [ "$(printf 'QUJD' | base64 --decode 2>/dev/null)" = ABC ]; then b64d='base64 --decode'
  elif [ "$(printf 'QUJD' | base64 -d 2>/dev/null)" = ABC ]; then b64d='base64 -d'
  else return 1; fi
  if [ "$(printf '' | sha256sum 2>/dev/null | cut -c 1-8)" = e3b0c442 ]; then sha='sha256sum'
  elif [ "$(printf '' | shasum -a 256 2>/dev/null | cut -c 1-8)" = e3b0c442 ]; then sha='shasum -a 256'
  else return 1; fi
  [ "$(printf 'A' | od -An -tx1 -N1 2>/dev/null | tr -d ' \n')" = 41 ]
}

first_bytes() { # file count -> those bytes in hex
  od -An -tx1 -N"$2" "$1" 2>/dev/null | tr -d ' \n'
}

# The digest FleetLedger names (looks.py Folder.sha256): over each file's name, a newline, its
# size, a newline and its bytes, in name order. Only for a real folder holding nothing but the
# format's folders and files (no link, nothing else); otherwise it prints nothing and fails.
folder_digest() { # folder
  [ -d "$1" ] && [ ! -L "$1" ] || return 1
  ( cd "$1" 2>/dev/null && find . ! -name . -print ) >"$stage/lk.list" || return 1
  : >"$stage/lk.files"
  while IFS= read -r e; do
    e=${e#./}
    case $e in
      palettes | logos) [ -d "$1/$e" ] && [ ! -L "$1/$e" ] || return 1 ;;
      *) { is_member "$e" && [ -f "$1/$e" ] && [ ! -L "$1/$e" ]; } || return 1
         echo "$e" >>"$stage/lk.files" ;;
    esac
  done <"$stage/lk.list"
  [ -s "$stage/lk.files" ] || return 1
  sort "$stage/lk.files" >"$stage/lk.sorted" || return 1
  fd=$(
    while IFS= read -r e; do
      printf '%s\n%s\n' "$e" "$(wc -c <"$1/$e" | tr -cd 0-9)"
      cat "$1/$e"
    done <"$stage/lk.sorted" | $sha | cut -c 1-64
  )
  case $fd in *[!0-9a-f]* | '') return 1 ;; esac
  [ "${#fd}" -eq 64 ] || return 1
  printf '%s' "$fd"
}

refuse_looks() {
  fail "FleetLedger's bundle has looks this refresher refuses ($1); refused, and the files here are unchanged."
}

# The looks in the bundle (records L and G from the check), decoded into $stage/looks and
# checked; sets looks_mode: empty (the bundle has none: leave what is here), none (FleetLedger
# keeps none) or the folder's digest.
check_looks() {
  looks_mode=$(sed -n 's/^G //p' "$stage/records")
  [ -n "$looks_mode" ] || return 0
  if ! find_looks_tools; then
    looks_mode=
    add_warning "The fleet's looks were left as they are: this computer has no base64, sha256sum (or shasum) or od to check them with."
    return 0
  fi
  if [ "$looks_mode" = - ]; then
    looks_mode=none
    return 0
  fi
  mkdir "$stage/looks" "$stage/looks/palettes" "$stage/looks/logos" || fail "Could not make a folder in $stage."
  tab=$(printf '\t')
  while read -r kind i nm sz; do
    [ "$kind" = L ] || continue
    case $i in '' | *[!0-9]*) refuse_looks "an odd record" ;; esac
    case $sz in '' | *[!0-9]*) refuse_looks "an odd record" ;; esac
    is_member "$nm" || refuse_looks "a name that is not a looks file's"
    [ ! -e "$stage/looks/$nm" ] || refuse_looks "$nm twice"
    $b64d <"$stage/lk/$i" >"$stage/looks/$nm" 2>/dev/null || refuse_looks "$nm is not base64"
    [ "$(wc -c <"$stage/looks/$nm" | tr -cd 0-9)" = "$sz" ] || refuse_looks "$nm is not the size it says"
    case $nm in
      *.png) [ "$(first_bytes "$stage/looks/$nm" 8)" = 89504e470d0a1a0a ] || refuse_looks "$nm is not a PNG picture" ;;
      *.jpg) [ "$(first_bytes "$stage/looks/$nm" 3)" = ffd8ff ] || refuse_looks "$nm is not a JPEG picture" ;;
      *.json)
        # No control byte but tab and line ends (the reader takes those only between values).
        [ "$(tr -d '\011\012\015\040-\176\200-\377' <"$stage/looks/$nm" | wc -c)" -eq 0 ] \
          || refuse_looks "$nm holds a control character"
        CANON=/dev/null awk "$JSON_AWK" <"$stage/looks/$nm" >"$stage/lk.flat" || refuse_looks "$nm is not JSON this refresher reads"
        if [ "$nm" = looks.json ]; then
          grep -qx "schema${tab}s${tab}shipshape.desk-looks/1" "$stage/lk.flat" || refuse_looks "looks.json is not shipshape.desk-looks/1"
        fi ;;
    esac
  done <"$stage/records"
  got=$(folder_digest "$stage/looks") || refuse_looks "the folder is not one this refresher writes"
  [ "$got" = "$looks_mode" ] || refuse_looks "the files do not match their digest"
}

herdr_home() {
  hh=${HERDR_APP_HOME:-$HOME/.config/herdr-app}
  case $hh in /*) ;; *) hh=$HOME/.config/herdr-app ;; esac
  printf '%s' "$hh"
}

# The digest of the desk-looks folder there when it is one this refresher wrote; fails for a
# folder of your own (or anything else in its place).
ours() { # folder
  [ -f "$LOOKS_MARK" ] || return 1
  dg=$(folder_digest "$1") || return 1
  grep -qxF "$dg" "$LOOKS_MARK" || return 1
  printf '%s' "$dg"
}

# looks-applied: the digests of the folders this refresher wrote, the latest first, at most 8
# (so a folder it wrote under another Herdr folder, with HERDR_APP_HOME set or not, stays its
# own). Each line is 64 hex digits; anything else in the file is dropped.
mark_looks() { # digest... to put first
  : >"$D/.looks-applied.tmp" || return 1
  for x in "$@"; do [ -n "$x" ] && echo "$x" >>"$D/.looks-applied.tmp"; done
  [ -f "$LOOKS_MARK" ] && grep -xE '[0-9a-f]{64}' "$LOOKS_MARK" >>"$D/.looks-applied.tmp"
  awk '!seen[$0]++' "$D/.looks-applied.tmp" | head -n 8 >"$D/.looks-applied.new" \
    && mv -f "$D/.looks-applied.new" "$LOOKS_MARK"
  rm -f "$D/.looks-applied.tmp" "$D/.looks-applied.new"
}

# A refresh stopped between the two renames of a replacement leaves no desk-looks and the old
# folder aside: put it back (only a folder of this refresher's own, by its digest).
recover_looks() {
  [ -e "$target" ] || [ -L "$target" ] && return 0
  for x in "$hh"/.shipshape-looks-old.*; do
    [ -d "$x" ] && [ ! -L "$x" ] || continue
    dg=$(folder_digest "$x") || continue
    grep -qxF "$dg" "$LOOKS_MARK" 2>/dev/null || continue
    mv "$x" "$target" && return 0
  done
  return 0
}

apply_looks() {
  hh=$(herdr_home)
  target="$hh/desk-looks"
  if [ -d "$hh" ] && [ -f "$LOOKS_MARK" ] && find_looks_tools; then
    recover_looks
    # What a refresh stopped half way left: only the names this refresher gives its own.
    find "$hh" -maxdepth 1 \( -name '.shipshape-looks-new.??????' -o -name '.shipshape-looks-old.*' \) \
      -mmin +15 -exec rm -rf {} + 2>/dev/null
  fi
  [ -n "$looks_mode" ] || return 0
  if [ ! -d "$hh" ]; then
    [ "$looks_mode" = none ] || add_warning "The fleet's looks were not written: there is no Herdr folder here ($hh)."
    return 0
  fi
  have=
  if [ -e "$target" ] || [ -L "$target" ]; then
    if ! have=$(ours "$target"); then
      [ "$looks_mode" = none ] \
        || add_warning "The fleet's looks were not written: $target is a folder of your own (move it away to receive them)."
      return 0
    fi
  fi
  old="$hh/.shipshape-looks-old.$$"
  if [ "$looks_mode" = none ]; then
    if [ -n "$have" ]; then
      if ! mv "$target" "$old"; then
        add_warning "Could not remove the fleet's looks from $target."
        return 0
      fi
      rm -rf "$old"
      echo "Removed the fleet's looks from $target: FleetLedger keeps none now."
    fi
    return 0
  fi
  if [ "$have" = "$looks_mode" ]; then
    [ "$(head -n 1 "$LOOKS_MARK")" = "$looks_mode" ] || mark_looks "$looks_mode"
    return 0
  fi
  new=$(mktemp -d "$hh/.shipshape-looks-new.XXXXXX") || { add_warning "Could not write the fleet's looks in $hh."; return 0; }
  if ! cp -R "$stage/looks/." "$new/" || [ "$(folder_digest "$new")" != "$looks_mode" ] \
    || ! mark_looks "$looks_mode" "$have"; then
    rm -rf "$new"
    add_warning "Could not write the fleet's looks to $target; what was there is unchanged."
    return 0
  fi
  if [ -n "$have" ] && ! mv "$target" "$old"; then
    rm -rf "$new"
    add_warning "Could not replace the fleet's looks in $target; what was there is unchanged."
    return 0
  fi
  # Two renames: for a moment there is no desk-looks (the Desk then wears its own looks), never
  # half of one. A desk-looks that appeared meanwhile is yours: mv would put this one inside it.
  if [ -e "$target" ] || [ -L "$target" ] || ! mv "$new" "$target" || [ -e "$target/${new##*/}" ]; then
    [ -d "$target/${new##*/}" ] && mv "$target/${new##*/}" "$new"
    [ -n "$have" ] && [ ! -e "$target" ] && [ ! -L "$target" ] && mv "$old" "$target"
    rm -rf "$new"
    add_warning "Could not write the fleet's looks to $target."
    return 0
  fi
  [ -n "$have" ] && rm -rf "$old"
  echo "Wrote the fleet's looks to $target (the Desk wears them when its Look popup opens, or at herdr-app do desk.looks-reload)."
}

# --- refresh ------------------------------------------------------------------------------------
refresh() {
  if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then echo "HOME is not set." >&2; exit 1; fi
  if [ ! -d "$D" ] || [ -L "$D" ]; then echo "There is no folder $D (or it is a link): paste the line ship-fleet trust prints for this machine." >&2; exit 1; fi
  read_conf
  take_lock
  trap 'cleanup; exit 1' HUP INT TERM
  find "$D" -maxdepth 1 -name '.stage.*' -mmin +15 -exec rm -rf {} + 2>/dev/null
  stage=$(mktemp -d "$D/.stage.XXXXXX") || fail "Could not make a folder in $D."
  cert_until_old=$(state_field certificate_valid_before)
  fetch
  check_bundle
  check_looks
  build_files
  apply_files
  sync_hc
  apply_looks
  write_state ok ""
  cleanup
  echo "Refreshed $name: $ship_count Ship(s), certificate until $cert_until."
  [ -n "$warning" ] && printf '%s\n' "$warning" >&2
  return 0
}

# --- the timer ----------------------------------------------------------------------------------
units_dir() {
  dir=${XDG_CONFIG_HOME:-$HOME/.config}
  case $dir in /*) ;; *) dir=$HOME/.config ;; esac
  printf '%s/systemd/user' "$dir"
}

SERVICE_TEXT='[Unit]
Description=ShipShape: refresh this computer'"'"'s Ships from FleetLedger

[Service]
Type=oneshot
ExecStart=%h/.ssh/shipshape/refresh
TimeoutStartSec=5min
'

TIMER_TEXT='[Unit]
Description=ShipShape: refresh this computer'"'"'s Ships hourly and at login

[Timer]
OnStartupSec=1min
OnCalendar=hourly
RandomizedDelaySec=5min
Persistent=true

[Install]
WantedBy=timers.target
'

plist_text() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$1/.ssh/shipshape/refresh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>3600</integer>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
EOF
}

install_timer() {
  case $(uname -s) in
    Darwin)
      is_path "$HOME" || { echo "No timer: launchd needs a home folder of letters, digits and ._/@+- ($HOME). Run $D/refresh hourly some other way."; return 0; }
      agents="$HOME/Library/LaunchAgents"
      mkdir -p "$agents"
      plist_text "$HOME" >"$agents/$LABEL.plist.tmp" && chmod 644 "$agents/$LABEL.plist.tmp" \
        && mv -f "$agents/$LABEL.plist.tmp" "$agents/$LABEL.plist"
      uid=$(id -u)
      launchctl bootout "gui/$uid/$LABEL" >/dev/null 2>&1
      if launchctl bootstrap "gui/$uid" "$agents/$LABEL.plist" >/dev/null 2>&1; then
        echo "A launchd agent ($LABEL) runs the refresher at login and hourly. To remove it: $D/refresh remove-timer"
      else
        echo "Wrote $agents/$LABEL.plist, but launchctl did not load it; it loads at your next login. To remove it: $D/refresh remove-timer"
      fi ;;
    Linux)
      if ! command -v systemctl >/dev/null 2>&1; then
        echo "No timer: this computer has no systemd. Run $D/refresh hourly some other way (cron, say)."
        return 0
      fi
      units=$(units_dir)
      mkdir -p "$units"
      printf '%s' "$SERVICE_TEXT" >"$units/$UNIT.service.tmp" && mv -f "$units/$UNIT.service.tmp" "$units/$UNIT.service"
      printf '%s' "$TIMER_TEXT" >"$units/$UNIT.timer.tmp" && mv -f "$units/$UNIT.timer.tmp" "$units/$UNIT.timer"
      if systemctl --user daemon-reload >/dev/null 2>&1 \
        && systemctl --user enable --now "$UNIT.timer" >/dev/null 2>&1; then
        echo "A systemd user timer ($UNIT.timer) runs the refresher at login and hourly. To remove it: $D/refresh remove-timer"
      else
        echo "Wrote $units/$UNIT.timer, but systemd did not start it (no user session here?): run systemctl --user enable --now $UNIT.timer in your desktop session. To remove it: $D/refresh remove-timer"
      fi ;;
    *)
      echo "No timer: this refresher knows systemd and launchd only. Run $D/refresh hourly some other way." ;;
  esac
}

remove_timer() {
  case $(uname -s) in
    Darwin)
      plist="$HOME/Library/LaunchAgents/$LABEL.plist"
      launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1
      rm -f "$plist"
      echo "Removed the launchd agent $LABEL; the refresher stays at $D/refresh, run by hand." ;;
    *)
      units=$(units_dir)
      if command -v systemctl >/dev/null 2>&1; then
        systemctl --user disable --now "$UNIT.timer" >/dev/null 2>&1
      fi
      rm -f "$units/$UNIT.timer" "$units/$UNIT.service"
      command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload >/dev/null 2>&1
      echo "Removed the systemd user timer $UNIT.timer; the refresher stays at $D/refresh, run by hand." ;;
  esac
}

# --- the first install --------------------------------------------------------------------------
# Run once, by the line ship-fleet trust prints, as `sh <this file> install ...`: it copies this
# file to ~/.ssh/shipshape/refresh, writes machine.conf, puts the Include line at the top of
# ~/.ssh/config as install.sh does, leaves the timer, and refreshes.
first_install() {
  [ "$#" -ge 5 ] || usage "Usage: sh refresh install NAME KEY USER HOSTS HOST_KEY [--no-timer] [--no-hc] [--lan]"
  i_name=$1 i_key=$2 i_user=$3 i_hosts=$4 i_hostkey=$5
  shift 5
  timer=1 i_hc=on i_names=overlay
  for opt in "$@"; do
    case $opt in
      --no-timer) timer=0 ;;
      --no-hc) i_hc=off ;;
      --lan) i_names=lan ;;
      *) usage "Unknown option $opt (--no-timer, --no-hc, --lan)." ;;
    esac
  done
  is_name "$i_name" || usage "Not a machine name: $i_name"
  is_path "$i_key" || usage "The key's path must be absolute, of letters, digits and ._/@+-: $i_key"
  [ -f "$i_key" ] || usage "No private key at $i_key."
  is_user "$i_user" || usage "Not a user name: $i_user"
  for h in $i_hosts; do is_host "$h" || usage "Not a host name: $h"; done
  case $i_hostkey in "ssh-ed25519 "*) is_blob "${i_hostkey#ssh-ed25519 }" || usage "Not an ed25519 host key." ;; *) usage "Not an ed25519 host key." ;; esac
  if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then usage "HOME is not set."; fi
  [ ! -L "$D" ] || usage "$D is a link; this refresher writes only into a real folder."
  { mkdir -p "$HOME/.ssh" "$D" && chmod 700 "$HOME/.ssh" "$D"; } || usage "Could not make $D."

  me=$0
  if [ "$me" != "$D/refresh" ]; then
    { cp "$me" "$D/.refresh.tmp" && chmod 700 "$D/.refresh.tmp" && mv -f "$D/.refresh.tmp" "$D/refresh"; } \
      || usage "Could not install $D/refresh."
  fi
  rm -f "$D/.first-pin" "$D/.first-bundle" "$D/.first-refresh"
  hcp=$(command -v hc 2>/dev/null)
  is_path "$hcp" || hcp=
  {
    echo "# ShipShape refresher settings, written by its first install. Data, read line by line."
    echo "name=$i_name"
    echo "key=$i_key"
    echo "user=$i_user"
    echo "hosts=$i_hosts"
    echo "host_key=$i_hostkey"
    echo "host_names=$i_names"
    echo "hc=$i_hc"
    echo "hc_path=$hcp"
  } >"$D/.machine.conf.tmp" || usage "Could not write $CONF."
  mv -f "$D/.machine.conf.tmp" "$CONF" || usage "Could not write $CONF."
  echo "This computer refreshes itself as $i_name, with the key $i_key, from $i_user@$(printf '%s' "$i_hosts" | sed 's/ / or /g')."

  include="Include $D/config"
  cfg="$HOME/.ssh/config"
  if [ -L "$cfg" ]; then
    cfg=$(readlink -f "$cfg") || usage "Cannot follow the link ~/.ssh/config."
  fi
  if [ ! -f "$cfg" ] || ! grep -qxF "$include" "$cfg"; then
    tmp="$cfg.shipshape-tmp"
    if { echo "$include"; if [ -f "$cfg" ]; then cat "$cfg"; fi; } >"$tmp" && chmod 600 "$tmp"; then
      mv "$tmp" "$cfg" || usage "Could not write ~/.ssh/config."
    else
      rm -f "$tmp"
      usage "Could not write ~/.ssh/config (a full disk?); it is unchanged."
    fi
    echo "Added '$include' to the top of ~/.ssh/config."
  fi

  if [ "$timer" = 1 ]; then install_timer; else echo "No timer (--no-timer): run $D/refresh when you want it current."; fi
  "$D/refresh"
  rc=$?
  herdr_hint
  return "$rc"
}

# After the pasted line: with Herdr here and no Fleet panel yet, the one command left. Not
# when the installer (shipshape-setup) runs this install: it does that part itself.
herdr_hint() {
  [ -n "${SHIPSHAPE_SETUP:-}" ] && return 0
  hh=${HERDR_APP_HOME:-$HOME/.config/herdr-app}
  command -v herdr-app >/dev/null 2>&1 || [ -d "$hh" ] || return 0
  [ -d "$hh/mods/fleet" ] && return 0
  echo "Herdr is here: for the Desk and the Fleet panel, run shipshape-setup --herdr-client (at the root of a ShipShape checkout)."
}

case ${1:-} in
  '') refresh ;;
  install) shift; first_install "$@" ;;
  remove-timer) remove_timer ;;
  --version) echo "ShipShape refresher $VERSION" ;;
  *) usage "Usage: refresh [install ... | remove-timer | --version]" ;;
esac
