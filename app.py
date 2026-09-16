import os
import socket
from datetime import datetime
from functools import wraps

import paramiko
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash
from dotenv import load_dotenv

# Proxmox uses a self-signed cert on your LAN, so we skip verification below.
# This silences the (expected) warning requests would otherwise print every call.
requests.packages.urllib3.disable_warnings()

load_dotenv()

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]

db = SQLAlchemy(app)

PROXMOX_HOST = os.environ.get("PROXMOX_HOST")
PROXMOX_NODE = os.environ.get("PROXMOX_NODE")
PROXMOX_TOKEN_ID = os.environ.get("PROXMOX_TOKEN_ID")
PROXMOX_TOKEN_SECRET = os.environ.get("PROXMOX_TOKEN_SECRET")

DOCKER_HOST_IP = os.environ.get("DOCKER_HOST_IP")
SSH_AGENT_USER = os.environ.get("SSH_AGENT_USER")
SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", "/app/ssh_keys/homelab_portal_key")

PORTAL_USERNAME = os.environ.get("PORTAL_USERNAME")
PORTAL_PASSWORD_HASH = os.environ.get("PORTAL_PASSWORD_HASH")


def login_required(view):
    """Wrap a route so it redirects to /login instead of rendering
    anything, unless the current session is authenticated. Applied to
    every route below except /login itself."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        valid = (
            username == PORTAL_USERNAME
            and PORTAL_PASSWORD_HASH
            and check_password_hash(PORTAL_PASSWORD_HASH, password)
        )
        if valid:
            session["logged_in"] = True
            return redirect(url_for("index"))
        flash("Invalid username or password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("login"))


class Server(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    host_type = db.Column(db.String(20), nullable=False)  # "physical" or "vm"
    ip_address = db.Column(db.String(45))
    cpu = db.Column(db.String(100))
    ram_gb = db.Column(db.Integer)
    disk_gb = db.Column(db.Integer)
    notes = db.Column(db.Text)
    proxmox_vmid = db.Column(db.Integer)  # nullable - only set for actual Proxmox VMs
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    services = db.relationship(
        "Service", backref="server", cascade="all, delete-orphan"
    )


class Service(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey("server.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    port = db.Column(db.Integer)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


with app.app_context():
    db.create_all()


def check_host_alive(ip_address, port=22, timeout=1.0):
    """Best-effort liveness check: can we open a TCP connection to this
    host at all? This is not a real health check of the *service* running
    there, just "is something listening and reachable on the network" -
    the same basic idea Uptime Kuma uses, simplified down to one line.
    Used as a fallback for anything without a known Proxmox VMID."""
    if not ip_address:
        return False
    try:
        with socket.create_connection((ip_address, port), timeout=timeout):
            return True
    except OSError:
        return False


def get_proxmox_vm_status(vmid):
    """Query real VM status + uptime from Proxmox for a given VMID.
    Returns None (not an exception) on any failure - missing config,
    network issue, VM ID that doesn't exist - so the caller can cleanly
    fall back to the simple port-check instead of crashing the page."""
    if not (PROXMOX_HOST and PROXMOX_NODE and PROXMOX_TOKEN_ID and PROXMOX_TOKEN_SECRET):
        return None
    url = f"https://{PROXMOX_HOST}:8006/api2/json/nodes/{PROXMOX_NODE}/qemu/{vmid}/status/current"
    headers = {"Authorization": f"PVEAPIToken={PROXMOX_TOKEN_ID}={PROXMOX_TOKEN_SECRET}"}
    try:
        resp = requests.get(url, headers=headers, verify=False, timeout=3)
        resp.raise_for_status()
        data = resp.json()["data"]
        return {"status": data["status"], "uptime_seconds": data.get("uptime", 0)}
    except (requests.RequestException, KeyError, ValueError):
        return None


def format_uptime(seconds):
    if not seconds:
        return None
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h uptime"
    if hours:
        return f"{hours}h {minutes}m uptime"
    return f"{minutes}m uptime"


def _run_agent_command(host_ip, command, timeout=5):
    """Runs one of the pre-approved commands on a remote host via the
    dedicated, forced-command-restricted homelab-portal SSH key.
    Returns raw stdout, or None on any failure - missing config, host
    unreachable, key rejected - so callers degrade gracefully."""
    if not (host_ip and SSH_AGENT_USER and os.path.exists(SSH_KEY_PATH)):
        return None
    try:
        key = paramiko.Ed25519Key.from_private_key_file(SSH_KEY_PATH)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(host_ip, username=SSH_AGENT_USER, pkey=key, timeout=timeout)
        _, stdout, _ = client.exec_command(command, timeout=timeout)
        output = stdout.read().decode()
        client.close()
        return output
    except Exception:
        return None


def get_docker_status(host_ip):
    raw = _run_agent_command(host_ip, "docker-status")
    if raw is None:
        return None
    containers = []
    for line in raw.strip().splitlines():
        parts = line.split("|")
        if len(parts) == 3:
            containers.append({"name": parts[0], "status": parts[1], "ports": parts[2]})
    return containers


def get_disk_usage(host_ip):
    raw = _run_agent_command(host_ip, "disk-usage")
    if raw is None:
        return None
    disks = []
    for line in raw.strip().splitlines():
        parts = line.split("|")
        if len(parts) == 5:
            disks.append({
                "target": parts[0],
                "size": parts[1],
                "used": parts[2],
                "avail": parts[3],
                "pcent": parts[4],
            })
    return disks


@app.route("/")
@login_required
def index():
    servers = Server.query.order_by(Server.name).all()
    for server in servers:
        proxmox_info = get_proxmox_vm_status(server.proxmox_vmid) if server.proxmox_vmid else None
        if proxmox_info:
            server.is_live = proxmox_info["status"] == "running"
            server.uptime_display = format_uptime(proxmox_info["uptime_seconds"])
            server.status_source = "proxmox"
        else:
            server.is_live = check_host_alive(server.ip_address)
            server.uptime_display = None
            server.status_source = "port-check"

        if server.ip_address and server.ip_address == DOCKER_HOST_IP:
            server.docker_containers = get_docker_status(server.ip_address)
            server.disk_lines = get_disk_usage(server.ip_address)
        else:
            server.docker_containers = None
            server.disk_lines = None
    return render_template("index.html", servers=servers)


# --- Servers ---------------------------------------------------------------

@app.route("/servers/new", methods=["GET", "POST"])
@login_required
def new_server():
    if request.method == "POST":
        server = Server(
            name=request.form["name"],
            host_type=request.form["host_type"],
            ip_address=request.form.get("ip_address"),
            cpu=request.form.get("cpu"),
            ram_gb=request.form.get("ram_gb") or None,
            disk_gb=request.form.get("disk_gb") or None,
            notes=request.form.get("notes"),
            proxmox_vmid=request.form.get("proxmox_vmid") or None,
        )
        db.session.add(server)
        db.session.commit()
        flash(f'Added server "{server.name}".')
        return redirect(url_for("index"))
    return render_template("new_server.html", server=None)


@app.route("/servers/<int:server_id>/edit", methods=["GET", "POST"])
@login_required
def edit_server(server_id):
    server = Server.query.get_or_404(server_id)
    if request.method == "POST":
        server.name = request.form["name"]
        server.host_type = request.form["host_type"]
        server.ip_address = request.form.get("ip_address")
        server.cpu = request.form.get("cpu")
        server.ram_gb = request.form.get("ram_gb") or None
        server.disk_gb = request.form.get("disk_gb") or None
        server.notes = request.form.get("notes")
        server.proxmox_vmid = request.form.get("proxmox_vmid") or None
        db.session.commit()
        flash(f'Updated server "{server.name}".')
        return redirect(url_for("index"))
    return render_template("new_server.html", server=server)


@app.route("/servers/<int:server_id>/delete", methods=["POST"])
@login_required
def delete_server(server_id):
    server = Server.query.get_or_404(server_id)
    name = server.name
    db.session.delete(server)  # cascades to its services automatically
    db.session.commit()
    flash(f'Deleted server "{name}" and its services.')
    return redirect(url_for("index"))


# --- Services ----------------------------------------------------------------

@app.route("/servers/<int:server_id>/services/new", methods=["GET", "POST"])
@login_required
def new_service(server_id):
    server = Server.query.get_or_404(server_id)
    if request.method == "POST":
        service = Service(
            server_id=server.id,
            name=request.form["name"],
            port=request.form.get("port") or None,
            notes=request.form.get("notes"),
        )
        db.session.add(service)
        db.session.commit()
        flash(f'Added service "{service.name}" to {server.name}.')
        return redirect(url_for("index"))
    return render_template("new_service.html", server=server, service=None)


@app.route("/services/<int:service_id>/edit", methods=["GET", "POST"])
@login_required
def edit_service(service_id):
    service = Service.query.get_or_404(service_id)
    if request.method == "POST":
        service.name = request.form["name"]
        service.port = request.form.get("port") or None
        service.notes = request.form.get("notes")
        db.session.commit()
        flash(f'Updated service "{service.name}".')
        return redirect(url_for("index"))
    return render_template("new_service.html", server=service.server, service=service)


@app.route("/services/<int:service_id>/delete", methods=["POST"])
@login_required
def delete_service(service_id):
    service = Service.query.get_or_404(service_id)
    name = service.name
    db.session.delete(service)
    db.session.commit()
    flash(f'Deleted service "{name}".')
    return redirect(url_for("index"))


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)
