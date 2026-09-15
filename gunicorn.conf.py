bind = "0.0.0.0:5000"
workers = 2
preload_app = True


def post_fork(server, worker):
    """preload_app runs the module (including db.create_all()) once in
    the master process before forking - which is what fixes the race
    condition two workers hit trying to create tables simultaneously.
    The tradeoff: forked workers inherit the master's already-open
    SQLAlchemy connection, which isn't safe to share across processes.
    Disposing it here forces each worker to open its own fresh one."""
    from app import db
    db.engine.dispose()
