#!/usr/bin/env bash
#
# One-time provisioning for a fresh Ubuntu VPS.
#
# Idempotent: safe to re-run. Every step checks current state before acting and
# treats "already done" as success, so a partial run can simply be repeated.
#
#   sudo ./deploy/provision.sh
#
# Installs Docker and the Compose plugin, restricts the firewall to SSH/HTTP/
# HTTPS, and creates an unprivileged deploy user.

set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"

log() { printf '\n==> %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

require_root() {
	if [ "$(id -u)" -ne 0 ]; then
		echo "This script must run as root (use sudo)." >&2
		exit 1
	fi
}

install_docker() {
	if have docker && docker compose version >/dev/null 2>&1; then
		log "Docker and the Compose plugin are already installed — skipping."
		return
	fi

	log "Installing Docker and the Compose plugin"
	export DEBIAN_FRONTEND=noninteractive
	apt-get update -qq
	apt-get install -y -qq ca-certificates curl gnupg

	install -m 0755 -d /etc/apt/keyrings
	if [ ! -f /etc/apt/keyrings/docker.asc ]; then
		curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
			-o /etc/apt/keyrings/docker.asc
		chmod a+r /etc/apt/keyrings/docker.asc
	fi

	# shellcheck source=/dev/null
	. /etc/os-release
	printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
		"$(dpkg --print-architecture)" "${VERSION_CODENAME}" \
		>/etc/apt/sources.list.d/docker.list

	apt-get update -qq
	apt-get install -y -qq \
		docker-ce docker-ce-cli containerd.io \
		docker-buildx-plugin docker-compose-plugin

	systemctl enable --now docker
}

configure_firewall() {
	if ! have ufw; then
		log "Installing ufw"
		DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw
	fi

	log "Configuring firewall (allow 22, 80, 443; deny everything else inbound)"
	ufw --force reset >/dev/null

	ufw default deny incoming >/dev/null
	ufw default allow outgoing >/dev/null

	# 22 keeps this session alive; 80 is required for Caddy's ACME HTTP
	# challenge, not just for redirecting to HTTPS.
	ufw allow 22/tcp >/dev/null
	ufw allow 80/tcp >/dev/null
	ufw allow 443/tcp >/dev/null

	ufw --force enable >/dev/null
	ufw status verbose

	cat <<-'WARNING'

		NOTE: ufw does not filter Docker-published ports.
		Docker inserts its rules into DOCKER-USER and the nat table, which are
		evaluated before ufw's filter chain, so a published port stays reachable
		even when ufw shows it denied. docker-compose.prod.yml therefore
		publishes no ports except Caddy's 80 and 443 — that overlay, not this
		firewall, is what keeps Postgres off the internet. Always deploy with
		both compose files.
	WARNING
}

create_deploy_user() {
	if id -u "$DEPLOY_USER" >/dev/null 2>&1; then
		log "User '${DEPLOY_USER}' already exists — leaving it alone."
	else
		log "Creating unprivileged user '${DEPLOY_USER}'"
		adduser --disabled-password --gecos "" "$DEPLOY_USER"
	fi

	if ! getent group docker >/dev/null 2>&1; then
		groupadd docker
	fi

	if id -nG "$DEPLOY_USER" | tr ' ' '\n' | grep -qx docker; then
		log "'${DEPLOY_USER}' is already in the docker group."
	else
		log "Adding '${DEPLOY_USER}' to the docker group"
		usermod -aG docker "$DEPLOY_USER"
	fi

	# Carry root's authorised keys over so the new user is reachable before
	# password login is disabled. Membership of the docker group is equivalent
	# to root, so this user is unprivileged only in the sense of not being root.
	local root_keys="/root/.ssh/authorized_keys"
	local user_ssh="/home/${DEPLOY_USER}/.ssh"
	if [ -f "$root_keys" ] && [ ! -f "${user_ssh}/authorized_keys" ]; then
		log "Copying root's authorised SSH keys to '${DEPLOY_USER}'"
		install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$user_ssh"
		install -m 0600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
			"$root_keys" "${user_ssh}/authorized_keys"
	fi
}

main() {
	require_root
	install_docker
	configure_firewall
	create_deploy_user

	log "Provisioning complete."
	cat <<-NEXT

		Next steps (as ${DEPLOY_USER}, not root):
		  1. Clone the repository.
		  2. cp .env.example .env                       # fill in real values
		     cp .env.production.example .env.production # DUCKDNS_DOMAIN, ACME_EMAIL
		  3. docker compose --env-file .env --env-file .env.production \\
		       -f docker-compose.yml -f docker-compose.prod.yml up -d

		See deploy/README.md for the DuckDNS and Meta webhook steps.
	NEXT
}

main "$@"
