#!/usr/bin/env bash
# Runbook Part B3 + B4 (HE-132): harden a fresh Ubuntu 24.04 VM and install Docker.
# Idempotent: every step checks state first, so re-running changes nothing.
#
#   sudo ./provision.sh --check   # report what would change, change nothing
#   sudo ./provision.sh           # apply
#
# Run it ON the new VM only (never on a shared server). B1/B2 (Oracle console) come first,
# B5 (app) after. Keep your current SSH session open until a NEW session as `deploy` works.
set -euo pipefail

DEPLOY_USER=deploy
SSHD_DROPIN=/etc/ssh/sshd_config.d/00-hackevents.conf
CHECK=0
CHANGES=0

case "${1:-}" in
  --check) CHECK=1 ;;
  "") ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
# shellcheck source=/dev/null
. /etc/os-release
[[ ${ID:-} == ubuntu ]] || { echo "Ubuntu only (Runbook B2: 24.04 LTS)" >&2; exit 1; }

# do <description> <command...>: run it, or only report it in --check mode.
do_() {
  local what=$1; shift
  CHANGES=$((CHANGES + 1))
  if ((CHECK)); then echo "would: $what"; else echo "==> $what"; "$@"; fi
}
ok() { echo " ok: $1"; }

export DEBIAN_FRONTEND=noninteractive

# --- B3. Host hardening -----------------------------------------------------------------
if ((CHECK)); then
  echo "would: apt-get update && apt-get -y upgrade (always runs on apply)"
else
  echo "==> apt-get update && apt-get -y upgrade"
  apt-get update -q
  apt-get -y -q upgrade
fi

missing=()
for pkg in ufw fail2ban unattended-upgrades; do
  dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
done
if ((${#missing[@]})); then
  do_ "install ${missing[*]}" apt-get -y -q install "${missing[@]}"
else
  ok "ufw fail2ban unattended-upgrades installed"
fi

if id "$DEPLOY_USER" >/dev/null 2>&1; then
  ok "user $DEPLOY_USER exists"
else
  do_ "create user $DEPLOY_USER (no password)" adduser --disabled-password --gecos "" "$DEPLOY_USER"
fi
if id -nG "$DEPLOY_USER" 2>/dev/null | grep -qw sudo; then
  ok "$DEPLOY_USER in sudo group"
else
  do_ "add $DEPLOY_USER to sudo group" usermod -aG sudo "$DEPLOY_USER"
fi

# authorized_keys: copy from the account that ran sudo (Oracle's default is `ubuntu`).
src_user=${SUDO_USER:-ubuntu}
src_keys=$(getent passwd "$src_user" | cut -d: -f6)/.ssh/authorized_keys
dst_home=/home/$DEPLOY_USER
dst_keys=$dst_home/.ssh/authorized_keys
install_keys() {
  install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$dst_home/.ssh"
  install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$src_keys" "$dst_keys"
}
if [[ -s $dst_keys ]]; then
  ok "$dst_keys present"
elif [[ -s $src_keys ]]; then
  do_ "copy $src_user's authorized_keys to $DEPLOY_USER" install_keys
else
  echo "no SSH key in $src_keys or $dst_keys: refusing to disable password login (lock-out)" >&2
  exit 1
fi

# Ubuntu reads sshd_config.d/*.conf first and the first value wins, so 00- beats
# cloud-init's 50-cloud-init.conf.
sshd_want=$'PermitRootLogin no\nPasswordAuthentication no'
if [[ -f $SSHD_DROPIN && $(cat "$SSHD_DROPIN") == "$sshd_want" ]]; then
  ok "sshd: root login and password auth off"
else
  apply_sshd() {
    printf '%s\n' "$sshd_want" >"$SSHD_DROPIN"
    sshd -t || { rm -f "$SSHD_DROPIN"; echo "sshd config invalid, reverted" >&2; return 1; }
    systemctl restart ssh
  }
  do_ "sshd: PermitRootLogin no, PasswordAuthentication no; restart ssh" apply_sshd
fi

ufw_status=$(ufw status verbose 2>/dev/null || true)
ufw_ok=1
grep -q "Status: active" <<<"$ufw_status" || ufw_ok=0
grep -q "deny (incoming)" <<<"$ufw_status" || ufw_ok=0
for port in 22 80 443; do
  grep -qE "^$port/tcp +ALLOW IN" <<<"$ufw_status" || ufw_ok=0
done
if ((ufw_ok)); then
  ok "ufw: deny incoming, allow 22/80/443"
else
  apply_ufw() {
    ufw default deny incoming
    ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp
    ufw --force enable
  }
  do_ "ufw: deny incoming, allow 22/80/443, enable" apply_ufw
fi

# Oracle Ubuntu images ship their own iptables INPUT rules ending in REJECT, which drop
# 80/443 before ufw sees them. Accept them explicitly and persist (Runbook B3 note).
for port in 80 443; do
  if iptables -C INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT 2>/dev/null; then
    ok "iptables: $port/tcp accepted"
  else
    do_ "iptables: accept $port/tcp" iptables -I INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT
  fi
done
if ((!CHECK)) && command -v netfilter-persistent >/dev/null; then
  netfilter-persistent save >/dev/null
elif ((!CHECK)); then
  echo "WARNING: netfilter-persistent missing: the 80/443 rules are lost on reboot." >&2
  echo "         Install it (apt-get install iptables-persistent) and re-run." >&2
fi

# Same file `dpkg-reconfigure -plow unattended-upgrades` writes, without the prompt.
auto_want=$'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";'
if [[ -f /etc/apt/apt.conf.d/20auto-upgrades && $(cat /etc/apt/apt.conf.d/20auto-upgrades) == "$auto_want" ]]; then
  ok "unattended-upgrades enabled"
else
  write_auto() { printf '%s\n' "$auto_want" >/etc/apt/apt.conf.d/20auto-upgrades; }
  do_ "enable unattended-upgrades" write_auto
fi

# --- B4. Docker Engine + Compose plugin from Docker's apt repository ----------------------
docker_list=/etc/apt/sources.list.d/docker.list
docker_repo="deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable"
if [[ -s /etc/apt/keyrings/docker.asc && -f $docker_list && $(cat "$docker_list") == "$docker_repo" ]]; then
  ok "Docker apt repository configured"
else
  add_docker_repo() {
    apt-get -y -q install ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    printf '%s\n' "$docker_repo" >"$docker_list"
    apt-get update -q
  }
  do_ "add Docker's apt repository" add_docker_repo
fi

docker_pkgs=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
missing=()
for pkg in "${docker_pkgs[@]}"; do
  dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
done
if ((${#missing[@]})); then
  do_ "install ${missing[*]}" apt-get -y -q install "${missing[@]}"
else
  ok "Docker Engine + Compose plugin installed"
fi

if getent group docker >/dev/null && id -nG "$DEPLOY_USER" 2>/dev/null | grep -qw docker; then
  ok "$DEPLOY_USER in docker group"
else
  do_ "add $DEPLOY_USER to docker group" usermod -aG docker "$DEPLOY_USER"
fi

echo
if ((CHECK)); then
  echo "$CHANGES change(s) pending. Re-run without --check to apply."
else
  echo "Done ($CHANGES change(s)). Now: open a NEW ssh session as $DEPLOY_USER, confirm it works,"
  echo "check 'sudo iptables -L INPUT -n' and the Oracle security list (80/443 open, 22 from your IP),"
  echo "then continue with Runbook B5. ($DEPLOY_USER has no password, so sudo from the original"
  echo "account, or set one with 'sudo passwd $DEPLOY_USER'. Docker needs no sudo after re-login.)"
fi
