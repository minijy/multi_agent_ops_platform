#!/bin/sh
set -eu

ops-agent-migrate
exec "$@"
