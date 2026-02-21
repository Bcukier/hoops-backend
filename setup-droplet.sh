#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# 🏀 HOOPS — DigitalOcean Droplet Setup Script
# ═══════════════════════════════════════════════════════════════
#
# USAGE:
#   1. Create an Ubuntu 24.04 Droplet on DigitalOcean
#   2. SSH in:  ssh root@YOUR_DROPLET_IP
#   3. Run:    bash setup-droplet.sh
#
# WHAT THIS DOES:
#   - Updates the system & configures firewall
#   - Creates a dedicated 'hoops' user
#   - Installs Python 3, nginx, certbot
#   - Clones your repo and sets up a virtualenv
#   - Generates a secure secret key
#   - Creates a systemd service (auto-start on boot)
#   - Configures nginx as a reverse proxy
#   - Optionally sets up SSL with Let's Encrypt
#   - Configures daily database backups
#   - Installs a 'hoops-deploy' helper command
#
# PREREQUISITES:
#   - Fresh Ubuntu 24.04 LTS Droplet
#   - SSH access as root
#   - (Optional) A domain name pointed at the Droplet's IP
#
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# ── Colors ────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[  OK]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

# ── Pre-flight checks ────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Run this script as root: sudo bash setup-droplet.sh"

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  🏀 HOOPS — Pickup Basketball Manager Setup${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo ""

# ── Configuration prompts ─────────────────────────────────────
REPO_URL="https://github.com/Bcukier/hoops-backend.git"
APP_DIR="/opt/hoops/app"
DATA_DIR="/opt/hoops/data"
BACKUP_DIR="/opt/hoops/data/backups"
APP_USER="hoops"

read -rp "Domain name (leave blank to use IP only): " DOMAIN
DOMAIN=${DOMAIN:-"_"}

if [[ "$DOMAIN" != "_" ]]; then
  read -rp "Set up free SSL with Let's Encrypt? (y/n): " SETUP_SSL
  if [[ "$SETUP_SSL" =~ ^[Yy] ]]; then
    read -rp "Email for SSL certificate notifications: " SSL_EMAIL
  fi
else
  SETUP_SSL="n"
fi

echo ""
info "Configuration:"
info "  Repository:  $REPO_URL"
info "  Domain:      ${DOMAIN/_/[none — using IP]}"
info "  SSL:         ${SETUP_SSL:-n}"
echo ""
read -rp "Proceed? (y/n): " CONFIRM
[[ ! "$CONFIRM" =~ ^[Yy] ]] && echo "Aborted." && exit 0

# ═══════════════════════════════════════════════════════════════
# STEP 1: SYSTEM UPDATE & PACKAGES
# ═══════════════════════════════════════════════════════════════
info "Updating system packages..."
export DEBIAN_FRONTEND=noninteractive
apt update -qq && apt upgrade -y -qq
success "System updated"

info "Installing dependencies..."
apt install -y -qq python3 python3-pip python3-venv nginx git curl \
  certbot python3-certbot-nginx ufw fail2ban >/dev/null 2>&1
success "Packages installed"

# ═══════════════════════════════════════════════════════════════
# STEP 2: FIREWALL
# ═══════════════════════════════════════════════════════════════
info "Configuring firewall..."
ufw --force reset >/dev/null 2>&1
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
success "Firewall configured (SSH, HTTP, HTTPS)"

# ═══════════════════════════════════════════════════════════════
# STEP 3: FAIL2BAN (SSH brute-force protection)
# ═══════════════════════════════════════════════════════════════
info "Configuring fail2ban..."
cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
bantime  = 3600
findtime = 600
maxretry = 5

[sshd]
enabled = true
port    = ssh
filter  = sshd
logpath = /var/log/auth.log
EOF
systemctl enable fail2ban >/dev/null 2>&1
systemctl restart fail2ban
success "fail2ban configured"

# ═══════════════════════════════════════════════════════════════
# STEP 4: APP USER
# ═══════════════════════════════════════════════════════════════
info "Creating application user..."
if id "$APP_USER" &>/dev/null; then
  warn "User '$APP_USER' already exists, skipping"
else
  adduser --disabled-password --gecos "" "$APP_USER" >/dev/null
  success "User '$APP_USER' created"
fi

mkdir -p "$APP_DIR" "$DATA_DIR" "$BACKUP_DIR"
chown -R "$APP_USER":"$APP_USER" /opt/hoops

# ═══════════════════════════════════════════════════════════════
# STEP 5: CLONE & INSTALL APPLICATION
# ═══════════════════════════════════════════════════════════════
info "Cloning repository..."
if [[ -d "$APP_DIR/.git" ]]; then
  warn "Repository already exists, pulling latest..."
  cd "$APP_DIR" && sudo -u "$APP_USER" git pull
else
  rm -rf "$APP_DIR"
  sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR"
fi
success "Repository cloned"

info "Setting up Python virtual environment..."
cd "$APP_DIR"
sudo -u "$APP_USER" python3 -m venv venv
sudo -u "$APP_USER" bash -c "source venv/bin/activate && pip install --quiet -r requirements.txt"
success "Dependencies installed"

# ═══════════════════════════════════════════════════════════════
# STEP 6: ENVIRONMENT FILE
# ═══════════════════════════════════════════════════════════════
info "Generating production environment file..."
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

if [[ "$DOMAIN" != "_" ]]; then
  ALLOWED_ORIGINS="https://$DOMAIN,http://$DOMAIN"
  SERVER_NAME="$DOMAIN"
else
  DROPLET_IP=$(curl -4 -s ifconfig.me || echo "YOUR_IP")
  ALLOWED_ORIGINS="http://$DROPLET_IP"
  SERVER_NAME="$DROPLET_IP"
fi

cat > "$APP_DIR/.env" << EOF
HOOPS_SECRET_KEY=$SECRET_KEY
HOOPS_DEMO_MODE=0
HOOPS_SUPERUSER_EMAIL=
HOOPS_DB_PATH=$DATA_DIR/hoops.db
HOOPS_ALLOWED_ORIGINS=$ALLOWED_ORIGINS

# ── Email (SendGrid API) ──
# Get API key at: https://app.sendgrid.com/settings/api_keys
SENDGRID_API_KEY=
SENDGRID_FROM_EMAIL=hoops@goatcommish.com
SENDGRID_FROM_NAME=GOATcommish

# ── SMS (Twilio) ──
# Get credentials at: https://www.twilio.com/console
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=
EOF

chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
success "Environment file created (secret key generated)"

# ═══════════════════════════════════════════════════════════════
# STEP 7: SYSTEMD SERVICE
# ═══════════════════════════════════════════════════════════════
info "Creating systemd service..."
cat > /etc/systemd/system/hoops.service << EOF
[Unit]
Description=GOATcommish Pickup Basketball
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --log-level info
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

# Security hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$DATA_DIR $APP_DIR

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable hoops >/dev/null 2>&1
systemctl start hoops
sleep 3

if systemctl is-active --quiet hoops; then
  success "Hoops service is running"
else
  error "Hoops service failed to start. Check: journalctl -u hoops -n 30"
fi

# ═══════════════════════════════════════════════════════════════
# STEP 8: NGINX REVERSE PROXY
# ═══════════════════════════════════════════════════════════════
info "Configuring nginx..."

# Rate limiting zone must be in http context (not inside server block)
cat > /etc/nginx/conf.d/rate-limit.conf << 'EOF'
limit_req_zone $binary_remote_addr zone=api:10m rate=30r/s;
EOF

cat > /etc/nginx/sites-available/hoops << EOF
server {
    listen 80;
    server_name $SERVER_NAME;

    # Security headers (supplementing app-level headers)
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;

    # Max upload size (for CSV import)
    client_max_body_size 2M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";

        # Timeouts
        proxy_connect_timeout 10s;
        proxy_send_timeout 30s;
        proxy_read_timeout 30s;
    }

    # Apply rate limiting to auth endpoints
    location /api/auth/ {
        limit_req zone=api burst=10 nodelay;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # Cache static assets
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff2?)\$ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }
}
EOF

rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/hoops /etc/nginx/sites-enabled/

nginx -t 2>&1 || error "Nginx config test failed: $(nginx -t 2>&1)"
systemctl restart nginx
success "Nginx configured"

# ═══════════════════════════════════════════════════════════════
# STEP 9: SSL (Optional)
# ═══════════════════════════════════════════════════════════════
if [[ "$SETUP_SSL" =~ ^[Yy] ]] && [[ "$DOMAIN" != "_" ]]; then
  info "Setting up SSL with Let's Encrypt..."
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$SSL_EMAIL" --redirect
  success "SSL configured — HTTPS is live"

  # Update allowed origins to HTTPS
  sed -i "s|http://$DOMAIN|https://$DOMAIN|g" "$APP_DIR/.env"
  systemctl restart hoops
fi

# ═══════════════════════════════════════════════════════════════
# STEP 10: DATABASE BACKUPS
# ═══════════════════════════════════════════════════════════════
info "Setting up automated backups..."
cat > /etc/cron.d/hoops-backup << 'EOF'
# Daily backup at 3 AM, keep 30 days
0 3 * * * hoops cp /opt/hoops/data/hoops.db /opt/hoops/data/backups/hoops-$(date +\%Y\%m\%d-\%H\%M).db 2>/dev/null
# Cleanup backups older than 30 days (weekly on Sunday)
0 4 * * 0 hoops find /opt/hoops/data/backups/ -name "*.db" -mtime +30 -delete 2>/dev/null
EOF
chmod 644 /etc/cron.d/hoops-backup
success "Daily backups configured"

# ═══════════════════════════════════════════════════════════════
# STEP 11: DEPLOY HELPER COMMAND
# ═══════════════════════════════════════════════════════════════
info "Installing deploy helper..."
cat > /usr/local/bin/hoops-deploy << 'SCRIPT'
#!/usr/bin/env bash
# Quick deploy: pulls latest code and restarts the service
set -euo pipefail
echo "🏀 Deploying GOATcommish..."
cd /opt/hoops/app
sudo -u hoops git pull
sudo -u hoops bash -c "source venv/bin/activate && pip install --quiet -r requirements.txt"
systemctl restart hoops
sleep 2
if systemctl is-active --quiet hoops; then
  echo "✅ Deploy complete — service is running"
  echo "   Health: $(curl -s http://127.0.0.1:8000/api/health)"
else
  echo "❌ Deploy failed — check: journalctl -u hoops -n 30"
  exit 1
fi
SCRIPT
chmod +x /usr/local/bin/hoops-deploy
success "Deploy helper installed (run: hoops-deploy)"

# ═══════════════════════════════════════════════════════════════
# STEP 12: LOG ROTATION
# ═══════════════════════════════════════════════════════════════
cat > /etc/logrotate.d/hoops << 'EOF'
/var/log/hoops/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
}
EOF

# ═══════════════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════════════
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  🏀 HOOPS IS LIVE!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""

if [[ "$DOMAIN" != "_" ]]; then
  PROTO="http"
  [[ "$SETUP_SSL" =~ ^[Yy] ]] && PROTO="https"
  echo -e "  App URL:     ${CYAN}${PROTO}://${DOMAIN}${NC}"
else
  DROPLET_IP=$(curl -4 -s ifconfig.me || echo "YOUR_IP")
  echo -e "  App URL:     ${CYAN}http://${DROPLET_IP}${NC}"
fi

echo ""
echo -e "  ${YELLOW}Useful commands:${NC}"
echo "    hoops-deploy              Pull latest code & restart"
echo "    systemctl status hoops    Check service status"
echo "    journalctl -u hoops -f    Stream live logs"
echo "    systemctl restart hoops   Restart the service"
echo ""
echo -e "  ${YELLOW}Files:${NC}"
echo "    App:       $APP_DIR"
echo "    Database:  $DATA_DIR/hoops.db"
echo "    Backups:   $BACKUP_DIR/"
echo "    Env:       $APP_DIR/.env"
echo "    Logs:      journalctl -u hoops"
echo ""
echo -e "  ${YELLOW}After your first login, remember to:${NC}"
echo "    1. Change the default demo passwords"
echo "    2. Point your domain's DNS A record to this server"
echo "    3. Revoke the GitHub PAT you used earlier"
echo ""
echo -e "  ${GREEN}Happy hooping! 🏀${NC}"
echo ""
