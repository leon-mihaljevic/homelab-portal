import os
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


class Server(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    host_type = db.Column(db.String(20), nullable=False)  # "physical" or "vm"
    ip_address = db.Column(db.String(45))
    cpu = db.Column(db.String(100))
    ram_gb = db.Column(db.Integer)
    disk_gb = db.Column(db.Integer)
    notes = db.Column(db.Text)
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


# Creates tables on startup if they don't exist yet.
# Fine for this stage; a real migration tool (Alembic) is a good Phase-2
# follow-up once the schema starts changing after data already exists.
with app.app_context():
    db.create_all()


@app.route("/")
def index():
    servers = Server.query.order_by(Server.name).all()
    return render_template("index.html", servers=servers)


@app.route("/servers/new", methods=["GET", "POST"])
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
        )
        db.session.add(server)
        db.session.commit()
        return redirect(url_for("index"))
    return render_template("new_server.html")


@app.route("/servers/<int:server_id>/services/new", methods=["GET", "POST"])
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
        return redirect(url_for("index"))
    return render_template("new_service.html", server=server)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
