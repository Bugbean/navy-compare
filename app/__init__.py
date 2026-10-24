"""
Navy Health Policy Compare — the Flask app.

Accounts and access control: per-person accounts on Supabase Auth, an
approval gate, and per-module permissions (see app/auth). Every route must map
to a module in ROUTE_MODULES or be listed as public. Anything else is refused,
so a new page added without a mapping fails closed rather than quietly
becoming visible to everyone.
"""
from __future__ import annotations

import logging
import os
import secrets

from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from app import auth

load_dotenv()
log = logging.getLogger(__name__)

# Most specific first. The rep screen and lead tools arrive with their weeks
# of the plan; their prefixes are mapped now so they are gated from day one.
ROUTE_MODULES = [
    ("/search",          "call_screen"),
    ("/call",            "call_screen"),
    ("/card",            "call_screen"),
    ("/script",          "call_screen"),
    ("/ask",             "call_screen"),
    ("/pdf",             "call_screen"),
    ("/outcome",         "call_screen"),
    ("/flag",            "call_screen"),
    ("/review",          "review_queue"),
    ("/overrides",       "overrides"),
    ("/reports",         "pilot_reports"),
]

# Where each module lives, in landing order: someone signing in lands on the
# first they hold.
MODULE_HOME = {
    "call_screen": "/call",
    "review_queue": "/review",
    "overrides": "/overrides",
    "pilot_reports": "/reports",
}

_PUBLIC_EXACT = {"/healthz", "/login", "/logout", "/favicon.ico"}
# Anyone signed in may report a problem with the page they are looking at.
_NO_MODULE_NEEDED = {"/api/feedback"}


def _first_allowed(user) -> str | None:
    for key, url in MODULE_HOME.items():
        if user and user.can(key):
            return url
    return None


def create_app() -> Flask:
    app = Flask(__name__)

    # Trust the load balancer's X-Forwarded-Proto so url_for() builds https
    # links. The client IP for the activity log is NOT taken from
    # request.remote_addr: behind Google's load balancer the last forwarded
    # hop is the balancer itself, so auth.client_ip() reads the right entry.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

    # Signs Flask's own session, which holds only the CSRF token. Must be a
    # stable random value in production, or CSRF tokens issued by one Cloud
    # Run instance fail on another.
    app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
    if not os.environ.get("FLASK_SECRET_KEY"):
        log.warning("FLASK_SECRET_KEY unset — using a per-process key; sessions won't survive a restart")
    app.config.update(SESSION_COOKIE_SECURE=auth.COOKIE_SECURE, SESSION_COOKIE_SAMESITE="Lax")

    from app.admin.routes import bp as admin_bp
    from app.auth.routes import bp as auth_bp
    from app.auth.routes import csrf_token, log_activity, set_session
    from app.call import bp as call_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(call_bp)

    def _refuse(user, module: str | None, area: str | None = None):
        # Repeated denials for one person are a signal worth seeing: someone
        # probing for pages they weren't given, or an access setup that's wrong.
        log_activity("access", "access_denied", user=user, status=403,
                     detail={"module": module, **({"area": area} if area else {})})
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not permitted"}), 403
        label = None
        if module:
            try:
                from app.db import get_sb
                rows = get_sb().table("modules").select("label").eq("key", module).execute().data
                label = rows[0]["label"] if rows else module
            except Exception:                           # noqa: BLE001
                label = module
        return render_template("forbidden.html", module_label=label,
                               first_allowed=_first_allowed(user)), 403

    from app import devmode
    if devmode.bypass_enabled():
        log.warning("NAVY_DEV_NO_LOGIN is set: sign-in is off for this local server")

    @app.before_request
    def _authorise():
        path = request.path
        g.new_session = None
        if devmode.bypass_enabled():
            g.user = devmode.dev_user()
            return None
        resolved = auth.resolve(request.cookies.get(auth.COOKIE_ACCESS),
                                request.cookies.get(auth.COOKIE_REFRESH))
        g.user, g.new_session = resolved.user, resolved.new_session

        if path.startswith("/static/") or path in _PUBLIC_EXACT or path.startswith("/auth/"):
            return None  # these routes do any checks they need themselves

        user = g.user
        if user is None:
            if path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("auth.login", next=path))

        if not user.approved:
            log_activity("access", "not_approved", user=user, status=403,
                         detail={"account_status": user.status})
            if path.startswith("/api/"):
                return jsonify({"error": "Account not approved"}), 403
            return redirect(url_for("auth.pending"))

        if path == "/admin" or path.startswith("/admin/"):
            return None if user.is_admin else _refuse(user, None, area="admin")

        if path == "/" or path in _NO_MODULE_NEEDED:
            return None
        module = auth.module_for_path(path, ROUTE_MODULES)
        if module is None:
            return _refuse(user, None)  # unmapped: fail closed
        if not user.can(module):
            return _refuse(user, module)
        return None

    @app.after_request
    def _persist_refreshed_session(resp):
        """If the access token expired mid-visit and was refreshed, write the
        new tokens back — otherwise every request would refresh again until
        the refresh token itself ran out."""
        if getattr(g, "new_session", None):
            set_session(resp, g.new_session)
        return resp

    @app.after_request
    def _log_page_view(resp):
        """One row per page a signed-in person actually opens. Not assets,
        not background /api/ calls, not redirects. Best-effort, off the
        request path."""
        user = getattr(g, "user", None)
        if (user is not None and request.method == "GET" and resp.status_code < 300
                and resp.mimetype == "text/html"
                and not request.path.startswith(("/static/", "/api/"))):
            log_activity("page", "page_view", user=user, status=resp.status_code, background=True)
        return resp

    @app.context_processor
    def _inject_user():
        user = getattr(g, "user", None)
        return {
            "current_user": user,
            "can": (lambda m: bool(user and user.can(m))),
            "csrf_token": csrf_token,
        }

    @app.route("/")
    def home():
        # Land on the first module this person holds. Until the call screen
        # exists, nobody holds a working one, so everyone sees the home page.
        dest = _first_allowed(g.user)
        if dest and dest in {r.rule for r in app.url_map.iter_rules()}:
            return redirect(dest)
        return render_template("home.html")

    @app.route("/healthz")
    def healthz():
        """Deployment diagnostics — env presence and DB reachability, no
        secrets. Public: reports only exception class names, never messages,
        since error text can leak schema or connection details."""
        out = {
            # Which code is running. Baked in at build time; "dev" means a
            # local run. The only way to tell from outside whether a push has
            # landed, because every other route needs sign-in.
            "build": os.environ.get("APP_BUILD", "dev"),
            "env_supabase_url": bool(os.environ.get("SUPABASE_URL")),
            "env_service_key": bool(os.environ.get("SUPABASE_SERVICE_ROLE_KEY")),
        }
        try:
            from app.db import get_sb
            get_sb().table("modules").select("key", count="exact", head=True).execute()
            out["db"] = "ok"
        except Exception as exc:                        # noqa: BLE001
            out["db"] = f"error: {type(exc).__name__}"
        return jsonify(out)

    @app.route("/api/feedback", methods=["POST"])
    def api_feedback():
        """A request or a bug report from any page. Only the kind and the
        message come from the browser; who and where come from the session
        and the request."""
        from app.auth import feedback
        from app.db import get_sb
        body = request.get_json(silent=True) or {}
        try:
            result = feedback.submit(
                get_sb(), body.get("kind"), body.get("message"),
                user=g.user, site="web",
                page_key=str(body.get("page") or "")[:60],
                page_url=request.headers.get("Referer", "")[:500],
                user_agent=request.headers.get("User-Agent", "")[:300],
            )
        except feedback.Invalid as exc:
            return jsonify({"error": str(exc)}), 400
        log_activity("feedback", f"feedback_{body.get('kind')}", user=g.user,
                     detail={"id": result.get("id"), "page": body.get("page")})
        return jsonify(result)

    @app.errorhandler(403)
    def _forbidden(_exc):
        return render_template("forbidden.html", module_label=None,
                               first_allowed=_first_allowed(getattr(g, "user", None))), 403

    return app
