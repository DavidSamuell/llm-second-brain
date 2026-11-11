#!/bin/sh
# ssh and git need a passwd entry for the uid the service runs as (PUID varies per host).
getent passwd "$(id -u)" >/dev/null || echo "kit:x:$(id -u):$(id -g)::${HOME:-/tmp}:/bin/sh" >> /etc/passwd
exec "$@"
