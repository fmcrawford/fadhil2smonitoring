#!/bin/sh
set -eu
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-3000}" --workers 1
