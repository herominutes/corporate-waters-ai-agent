#!/bin/bash
# Start Saul Goodman bot + Caddy HTTPS reverse proxy
cd ~/goodman

# Kill any existing Caddy
pkill -f "caddy run" 2>/dev/null
sleep 0.5

# Start Caddy (HTTPS reverse proxy for API)
nohup ~/bin/caddy run --config Caddyfile > logs/caddy.log 2>&1 &
echo "Caddy started (PID $!)"


# Start WARP proxy (bypasses ElevenLabs geo-IP block)
pkill -f wireproxy 2>/dev/null
sleep 0.3
nohup ~/bin/wireproxy -c credentials/wireproxy.conf > logs/wireproxy.log 2>&1 &
echo "WARP proxy started (PID $!)"
sleep 1

# Start bot (will auto-kill previous instance via PID lock)
nohup bash run.sh > logs/bot.log 2>&1 &
echo "Saul started (PID $!)"
