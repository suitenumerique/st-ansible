#!/bin/sh
# st-cli container entrypoint.
#
# Ensures files st-cli writes into the bind mount are owned by the host user,
# without the caller passing -u / -e HOME / any ANSIBLE_* var.
#
# POSIX sh only (no bashisms).
set -e

MOUNT=/st-cli

if [ "$(id -u)" = 0 ]; then
	# Started as root: align on the bind-mount owner so files st-cli writes into
	# the mount (.st-cli/, ssh/known_hosts, ...) are owned by the host user. No
	# mount, or a root-owned mount (e.g. podman rootless, where the mount already
	# maps back to the host user), means nothing to align: stay root.
	if [ -d "$MOUNT" ]; then
		owner="$(stat -c '%u' "$MOUNT")"
		group="$(stat -c '%g' "$MOUNT")"
	else
		owner=0
		group=0
	fi

	if [ "$owner" != 0 ]; then
		if ! getent group "$group" >/dev/null 2>&1; then
			groupadd -g "$group" st
		fi
		if ! getent passwd "$owner" >/dev/null 2>&1; then
			useradd -o -u "$owner" -g "$group" -m -d /home/st -s /bin/sh st
		fi
		export HOME=/home/st
		# setpriv preserves the environment (HOME, PATH, ANSIBLE_*), so no further
		# fixup is needed.
		exec setpriv --reuid "$owner" --regid "$group" --clear-groups st-cli "$@"
	fi
fi

# Root staying root, or a non-root uid the runtime already set up: make sure HOME
# is a writable path so ansible/ssh don't fall back to '/'.
if [ -z "$HOME" ] || [ "$HOME" = / ]; then
	export HOME=/tmp
fi

exec st-cli "$@"
