import os
import socket
import docker
import re
from flask import Flask, session, redirect, url_for, has_request_context, Response, request
from authlib.integrations.flask_client import OAuth
from dash import Dash, html, dcc, Input, Output, State, no_update, callback_context, MATCH
from dash.exceptions import PreventUpdate
import dash_bootstrap_components as dbc
import json
from ansi2html import Ansi2HTMLConverter
import time
from datetime import datetime, UTC
import threading
from dotenv import load_dotenv
import shlex
import requests
import secrets

# --- CONFIGURATION ---
load_dotenv()
DOCKER_IMAGE = "ghcr.io/idemsinternational/open-app-builder:latest"
# Ideally load these from environment variables
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "super_secret_dev_key")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
NETWORK_NAME = os.environ.get("DOCKER_NETWORK_NAME", "app-net")
DOMAIN = os.environ.get("DOMAIN", None)

HEARTBEAT_TIMEOUT = 10  # Seconds to wait before killing container (buffer for 2s poll)

with open("repo_config.json", 'r') as json_file:
    REPOS =json.load(json_file)

STATE_FILE = "testhub_state.json"
class StateManager:
    @staticmethod
    def read():
        if not os.path.exists(STATE_FILE): return {}
        try:
            with open(STATE_FILE, 'r') as f: return json.load(f)
        except: return {}

    @staticmethod
    def write(state):
        with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=4)

    @staticmethod
    def get_user(email):
        return StateManager.read().get(email, {})

    @staticmethod
    def update_user(email, **kwargs):
        """Updates top-level user data (like heartbeats)"""
        state = StateManager.read()
        if email not in state: state[email] = {"repos": {}}
        for k, v in kwargs.items(): state[email][k] = v
        StateManager.write(state)

    @staticmethod
    def get_repo(email, repo_url):
        user_data = StateManager.get_user(email)
        return user_data.get("repos", {}).get(repo_url, {})

    @staticmethod
    def update_repo(email, repo_url, **kwargs):
        """Updates data for a specific repository"""
        state = StateManager.read()
        if email not in state: state[email] = {"repos": {}}
        if "repos" not in state[email]: state[email]["repos"] = {}
        if repo_url not in state[email]["repos"]: state[email]["repos"][repo_url] = {}
        
        for k, v in kwargs.items(): 
            state[email]["repos"][repo_url][k] = v
        StateManager.write(state)

# --- ACCESS CONTROL SETUP ---
ACL_FILE = "access_control.json"

def load_acl():
    """Loads the ACL file, creating a default one if it doesn't exist."""
    if not os.path.exists(ACL_FILE):
        default_acl = {"admin": []}
        with open(ACL_FILE, 'w') as f:
            json.dump(default_acl, f, indent=4)
        return default_acl
    with open(ACL_FILE, 'r') as f:
        return json.load(f)

def save_acl(acl_data):
    """Saves the ACL data back to the file (for future Admin UI)."""
    with open(ACL_FILE, 'w') as f:
        json.dump(acl_data, f, indent=4)

def is_admin(email):
    """Checks if a user has the admin role."""
    # Automatically grant admin to the local development mock user
    if email == "localhost@example.com": 
        return True
    
    acl = load_acl() # Load fresh to catch any manual file edits
    return email in acl.get("admin", [])

def get_allowed_repos(email):
    """Returns a filtered dictionary of REPOS the user is allowed to access."""
    if is_admin(email):
        return REPOS

    acl = load_acl()
    allowed_repos = {}
    
    for repo_name, repo_data in REPOS.items():
        # Look for a key like "access:My Repo Name"
        acl_key = f"access:{repo_name}"
        if email in acl.get(acl_key, []):
            allowed_repos[repo_name] = repo_data
            
    return allowed_repos

# --- SETUP ---
server = Flask(__name__)
server.secret_key = SECRET_KEY

docker_client = docker.from_env()

oauth = OAuth(server)
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/drive.metadata.readonly'},
)

conv = Ansi2HTMLConverter()#bg="#0d1117", fg="#c9d1d9", inline=True)

app = Dash(
    __name__,
    server=server,
    assets_folder='site_assets', 
    assets_url_path='site_assets',
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css"
    ],
    update_title=None,
    suppress_callback_exceptions=True,
)
app.title = "TestHub"
app._favicon = ("cropped-IDEMS_logomark_with_border_circle-32x32.png") 

# --- HELPER FUNCTIONS ---

def sanitize_container_name(email):
    return re.sub(r'[^a-zA-Z0-9]', '-', email)

def kill_user_resources(email, remove=True):
    if not docker_client: return
    try:
        container = docker_client.containers.get(sanitize_container_name(email))
        container.stop()
        if remove:
            container.remove()
    except:
        pass

# --- FLASK ROUTES ---
@server.route('/login')
def login():
    if DOMAIN in ["localhost", "local"]:
        if "user" not in session:
            session["user"] = {
                "sub": "localdev",
                "email": "localhost@example.com",
                "name": "Local Developer",
                "picture": None,
            }
            session['oauth_token'] = os.environ.get("LOCAL_OAUTH_CRED", None)
        print("Logging in as localhost")
        return redirect('/')

    redirect_uri = url_for('auth', _external=True)
    return google.authorize_redirect(redirect_uri, access_type='offline', prompt='consent')

@server.route('/auth/callback')
def auth():
    token = google.authorize_access_token()
    user_info = token.get('userinfo')
    session['user'] = {
        'email': user_info['email'],
        'name': user_info['name'],
        'picture': user_info['picture'],
    }
    session['oauth_token'] = token
    return redirect('/')

@server.route('/logout')
def logout():
    if 'user' in session:
        kill_user_resources(session['user']['email'])
    session.pop('user', None)
    return redirect('/')

@server.route('/webhook/preview-ready', methods=['POST'])
def webhook_preview_ready():
    data = request.json
    email = data.get('email')
    token = data.get('token')
    urls = data.get('urls', '')
    status = data.get('status')

    if not email or not token: return "Missing payload", 400

    user_state = StateManager.get_user(email)
    target_repo = None
    
    # Scan the user's repos to find which one initiated this webhook
    for repo_url, repo_data in user_state.get('repos', {}).items():
        if repo_data.get('webhook_token') == token:
            target_repo = repo_url
            break

    if not target_repo:
        return "Unauthorized: Invalid or Expired Token", 401

    # Parse the Firebase URL safely
    final_url = None
    if urls and status == 'success':
        try:
            arr = json.loads(urls)
            if isinstance(arr, list) and len(arr) > 0: final_url = arr[0]
        except:
            final_url = urls.split(',')[0].replace('"', '').replace("'", "").strip()

    StateManager.update_repo(
        email, 
        target_repo, 
        status=status, 
        preview_url=final_url,
        last_updated=time.time()
    )
    return "OK", 200

# --- COMPONENT LAYOUTS ---

def get_login_layout():
    return dbc.Container([
        dbc.Row(dbc.Col(html.H1("Open App Builder TestHub Login"), className="text-center mt-5")),
        dbc.Row(dbc.Col(
            # FIX 1: external_link=True forces a real HTTP request to Flask
            dbc.Button("Login with Google", href="/login", external_link=True, color="primary"), 
            className="text-center"
        ))
    ])

def get_navbar(user, pathname="/"):
    name = user['name'] if user else ""
    picture = user['picture'] if user else "https://via.placeholder.com/40"
    email = user['email'] if user else ""

    is_on_admin_page = (pathname == '/admin')
    
    if is_on_admin_page:
        nav_btn = dbc.Button("Back to App", href="/", external_link=True, color="secondary", size="sm", className="ms-3")
    elif is_admin(email):
        nav_btn = dbc.Button("Admin Panel", href="/admin", external_link=True, color="info", size="sm", className="ms-3")
    else:
        nav_btn = None

    return dbc.Navbar(
        dbc.Container([
            html.A(
                dbc.Row([
                    dbc.Col(html.Img(src="/site_assets/idems-logo.png", height="40px")),
                    dbc.Col(dbc.NavbarBrand("Open App Builder TestHub", className="ms-3 fs-4 fw-bold text-white")),
                ], align="center", className="g-0"),
                href="/",
                style={"textDecoration": "none"},
            ),
            dbc.Row([
                dbc.Col(html.Span(f"Welcome, {name}", className="text-white me-3 d-none d-md-block")),
                dbc.Col(html.Img(src=picture, height="35px", className="rounded-circle border border-secondary")),
                dbc.Col(nav_btn),
                dbc.Col(dbc.Button("Logout", href="/logout", external_link=True, color="danger", size="sm", className="ms-3")),
            ], align="center", className="g-0"),
        ], fluid=True),
        color="#1e1e1e",
        dark=True,
        className="border-bottom py-2"
    )

def get_dashboard_layout(user, pathname="/"):
    name = user['name'] if user else ""
    # Use a generic avatar if google picture fails, or keep user['picture']
    picture = user['picture'] if user else "https://via.placeholder.com/40"

    allowed_repos = get_allowed_repos(user.get('email', ''))
    
    # Current Repo Logic (Existing)
    user_state = StateManager.get_user(session['user']['email'])
    current_repo = user_state.get('active_repo')

    if current_repo not in [v['url'] for v in allowed_repos.values()]:
        current_repo = None

    navbar = get_navbar(user, pathname)

    return html.Div([
            navbar,
            
            dbc.Container([
        dbc.Row([
            dbc.Col([
                html.Div([

                    html.H5("Controls", className="mt-3"),
                    html.Label("Select Repo:"),
                    dcc.Dropdown(
                        id='repo-selector',
                        options=[{'label': k, 'value': v['url']} for k, v in allowed_repos.items()],
                        placeholder="Select repo..." if allowed_repos else "No repos assigned, contact Admin for access.",
                        value=current_repo,
                        
                    ),
                    html.Div(id='deploy-status', className="mb-4 text-muted small"),
                    html.Hr(className="border-secondary"),
                    dbc.Button([
                        html.I(className="bi bi-arrow-repeat me-2"), 
                        "Sync Workflow"
                    ], id='btn-sync', color="primary", className="w-100 mb-2 shadow-sm"),
                    dbc.Button([
                        html.I(className="bi bi-exclamation-triangle-fill me-2"), 
                        "Force Rebuild"
                    ], id='btn-rebuild', color="warning", className="w-100 mb-2 shadow-sm text-dark"),
                    html.Div(id='sync-status', className="text-muted small text-center"),
                    html.Hr(className="border-secondary mt-4"),
                    html.H6("Preview Environment", className="mt-3 text-white"),
                    html.P("Select which version of the app to view. Clear to return to local container.", className="small text-muted mb-2"),
                    dcc.Dropdown(
                        id='env-selector',
                        placeholder="Select an environment...",
                        options=[],
                        disabled=True,
                        clearable=False, # We always want an environment selected
                        className="mb-2"
                    ),
                    html.Div(id='env-status', className="text-muted small mt-2 fw-bold text-center")
                ], className="p-4 h-100") # Padding for the panel
                
            ], width=3, className="bg-dark-panel vh-100 p-0"), # Remove default Col padding

            dbc.Col([
                dbc.Tabs([
                    dbc.Tab(label="App Preview", tab_id="tab-preview", label_class_name="fs-5"),
                    dbc.Tab(label="Live System Logs", tab_id="tab-logs", label_class_name="fs-5"),
                ], id="viewport-tabs", active_tab="tab-preview", className="mt-3 border-0"),
                
                html.Div(
                    id="tab-content", 
                    className="bg-dark border border-secondary rounded p-1 mt-2", 
                    style={"minHeight": "80vh"}
                )
            ], width=9, className="main-content ps-4")
        ], className="g-0"), # Remove gutter spacing for full-width split
        dcc.Store(id='env-url-store', data=None),
        dcc.Interval(id='log-poller', interval=2000, n_intervals=0, disabled=False) 
    ], fluid=True, className="p-0")])

def get_admin_layout(user, pathname="/"):
    email = user['email'] if user else ""
    if not is_admin(email):
        return html.Div([get_navbar(user, pathname), dbc.Container(html.H3("Unauthorized", className="text-danger mt-5"))])

    current_acl = json.dumps(load_acl(), indent=4)

    return html.Div([
        get_navbar(user, pathname),
        dbc.Container([
            dbc.Row([
                dbc.Col([
                    html.H4("Access Control Editor", className="mt-4 text-white"),
                    html.P("Edit raw JSON. Must remain valid JSON formatting.", className="text-muted small mb-1"),
                    dcc.Textarea(
                        id='acl-editor',
                        value=current_acl,
                        style={'width': '100%', 'height': '400px', 'fontFamily': 'monospace', 'backgroundColor': '#1e1e1e', 'color': '#c9d1d9', 'border': '1px solid #3e3e42'}
                    ),
                    dbc.Button("Save Configuration", id='save-acl-btn', color="success", className="mt-2 w-100"),
                    html.Div(id='acl-save-status', className="mt-2 text-center fw-bold")
                ], width=4),
                
                dbc.Col([
                    html.H4("Active Environment Resources", className="mt-4 text-white"),
                    html.P("Live view of user containers, resources, and background processes.", className="text-muted small mb-1"),
                    # The table target and the polling interval
                    html.Div(id="admin-table-container", className="mt-2"),
                    dcc.Interval(id='admin-poller', interval=2000, n_intervals=0) 
                ], width=8)
            ])
        ], fluid=True, className="px-5")
    ])

# --- MAIN LAYOUT FUNCTION ---

def serve_layout():
    is_logged_in = False
    if has_request_context() and 'user' in session:
        is_logged_in = True

    login_style = {'display': 'none'} if is_logged_in else {'display': 'block'}
    dashboard_style = {'display': 'block'} if is_logged_in else {'display': 'none'}

    return html.Div([
        dcc.Location(id='url', refresh=False), # Tracks the current URL
        html.Div(get_login_layout(), id='login-wrapper', style=login_style),
        html.Div(id='page-content', style=dashboard_style) # Target for our callback
    ])

app.layout = serve_layout

#region --- CALLBACKS ---

@app.callback(
    Output('deploy-status', 'children'),
    Input('env-selector', 'value'),
    State('repo-selector', 'value'),
    prevent_initial_call=True
)
def deploy_repo(env_value, repo_url):
    if env_value != 'local':
        # Don't waste VM resources starting containers if they are just looking at the Main branch or a PR!
        return no_update 
        
    if 'user' not in session or not repo_url: return no_update

    user = session['user']

    # If user already has a container set up for this repo, start it
    try:
        c_name = sanitize_container_name(user['email'])
        existing_c = docker_client.containers.get(c_name)
        
        # If the container exists and is for the same repo...
        if existing_c.labels.get("user_repo") == repo_url:
            # If it was stopped, wake it up.
            if existing_c.status != 'running':
                existing_c.start()
                threading.Thread(target=setup_container, args=(user['email'], repo_url)).start()
                return "Resumed existing session."
            
            # If it's already running, do nothing.
            return "Container is active."
            
    except docker.errors.NotFound:
        pass # No container exists, proceed to full deploy
    except Exception as e:
        print(f"Status check error: {e}")

    return launch_container(repo_url)

@app.callback(
    Output('deploy-status', 'children', allow_duplicate=True),
    State('repo-selector', 'value'),
    Input('btn-rebuild', 'n_clicks'),
    prevent_initial_call=True
)
def force_rebuild(repo_url, n_clicks):
    return launch_container(repo_url)

def launch_container(repo_url):
    """
    Destroys any existing container for the user and starts a fresh one.
    """
    user = session['user']
    repo_key = next((v['key'] for k, v in REPOS.items() if v['url'] == repo_url), "")

    try:
        kill_user_resources(user['email'], remove=True)
        StateManager.update_repo(user['email'], repo_url, docker_stage="Container Created (Waiting)")
        docker_client.containers.run(
            DOCKER_IMAGE,
            entrypoint="/bin/sh",
            command=["-c", "sleep infinity"],
            name=sanitize_container_name(user['email']),
            network=NETWORK_NAME,
            labels={"user_repo": repo_url},
            detach=True,
            remove=False,
            # volumes={
            #     f"yarn_cache_{safe_name}": {'bind': '/usr/local/share/.cache/yarn', 'mode': 'rw'},
            #     f"angular_cache_{safe_name}": {'bind': '/app/.angular/cache', 'mode': 'rw'}
            # },
            environment={
                "DEPLOYMENT_PRIVATE_KEY": repo_key,
                "NODE_OPTIONS": "--max-old-space-size=4608",
            }
        )

        threading.Thread(target=setup_container, args=(user['email'], repo_url)).start()

        return "Container created. Provisioning environment... see Live System Logs for status"
    except Exception as e:
        return f"Error: {str(e)}"


def setup_container(email, repo_url):
    """Background task to orchestrate the Node environment via PM2."""
    try:
        c = docker_client.containers.get(sanitize_container_name(email))
        
        c.exec_run(["/bin/sh", "-c", "echo '--- Initializing Environment ---' > /proc/1/fd/1"])

        # 1. Check if the deployment is already imported
        # (This is much safer in Python than inside a Docker CMD string)
        check = c.exec_run(["/bin/sh", "-c", "[ -d './idems_app/deployments' ]"])
        
        if check.exit_code != 0:
            StateManager.update_repo(email, repo_url, docker_stage="Importing Repository...")
            c.exec_run(["/bin/sh", "-c", f"echo '--- Importing Repository: {repo_url} ---' > /proc/1/fd/1"])
            
            # Run import and stream output to Docker logs
            c.exec_run(["/bin/sh", "-c", f"yarn workflow deployment import {repo_url} -y > /proc/1/fd/1 2>&1"])

        StateManager.update_repo(email, repo_url, docker_stage="Starting Preview Server...")
        c.exec_run(["/bin/sh", "-c", "echo '--- Starting Preview Server (PM2) ---' > /proc/1/fd/1"])
        
        # 2. Start PM2.
        # We use --out and --error to pipe PM2's background logs directly to Docker's PID 1 stream!
        start_cmd = (
            "npx pm2 start yarn "
            "--name 'preview_app' "
            "--output /proc/1/fd/1 "
            "--error /proc/1/fd/1 "
            "-- start:docker > /proc/1/fd/1 2>&1"
        )
        c.exec_run(["/bin/sh", "-c", start_cmd])
        StateManager.update_repo(email, repo_url, docker_stage="App Running")

    except Exception as e:
        print(f"Setup thread error for {email}: {e}")


@app.callback(
    Output('sync-status', 'children'),
    Input('btn-sync', 'n_clicks'),
    State('env-selector', 'value'),
    State('repo-selector', 'value'),
    prevent_initial_call=True
)
def sync_workflow(n, env_value, repo_url):
    if 'user' not in session or not env_value or not repo_url: return no_update

    user_email = session['user']['email']
    
    # --- CLOUD SYNC LOGIC ---
    if env_value == 'cloud':
        pat = get_pat_for_repo(repo_url)
        repo_path = get_repo_path(repo_url)
        
        # 1. Generate a secure, one-time use token
        secure_token = secrets.token_urlsafe(16)

        webhook_url = f"https://{DOMAIN}/webhook/preview-ready"
        
        # 2. Clear old state immediately so the UI shows Loading
        StateManager.update_repo(
            user_email,
            repo_url,
            status="pending", 
            preview_url=None, 
            webhook_token=secure_token,
            last_updated=None
        )
        
        # 3. Dispatch the GitHub Action
        url = f"https://api.github.com/repos/{repo_path}/actions/workflows/synced-preview.yml/dispatches"
        headers = {"Authorization": f"token {pat}", "Accept": "application/vnd.github.v3+json"}
        data = {
            "ref": "main",
            "inputs": {
                "user_email": user_email,
                "webhook_url": webhook_url,
                "webhook_token": secure_token
            }
        }
        
        res = requests.post(url, headers=headers, json=data)
        if res.status_code == 204:
            return html.Span([html.I(className="bi bi-cloud-upload me-1"), "Cloud build started!"], className="text-success fw-bold")
        else:
            return html.Span(f"GitHub Error: {res.text}", className="text-danger fw-bold")

    # --- LOCAL DOCKER SYNC LOGIC ---
    elif env_value == 'local':
        token_data = session.get('oauth_token', {})

        # 1. Construct the Application Credentials (credentials.json)
        client_config = {
            "client_id": GOOGLE_CLIENT_ID,
            "project_id": "open-app-builder",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uris": ["http://localhost"] 
        }

        creds_data = {
            "web": client_config,
            "installed": client_config
        }

        # 2. Adjust token timestamp formatting (Google Node scripts often expect ms)
        if 'expires_at' in token_data:
            token_data['expiry_date'] = token_data['expires_at'] * 1000

        creds_json = json.dumps(creds_data)
        token_json = json.dumps(token_data)

        try:
            c = docker_client.containers.get(sanitize_container_name(user_email))

            # 3. Use shlex.quote to safely escape the JSON strings for the bash shell
            safe_creds = shlex.quote(creds_json)
            safe_token = shlex.quote(token_json)

            # 4. Construct the unified command.
            # - Creates the config directory
            # - Writes both credentials.json and token.json
            # - Redirects standard output and standard error (2>&1) directly to Docker's PID 1 stream
            cmd = f"""
            mkdir -p /app/packages/scripts/config && \
            echo {safe_creds} > /app/packages/scripts/config/credentials.json && \
            echo {safe_token} > /app/packages/scripts/config/token.json && \
            echo "--- Starting Yarn Workflow Sync ---" > /proc/1/fd/1 && \
            yarn workflow sync > /proc/1/fd/1 2>&1 && \
            echo "--- Stopping PM2 Wrapper ---" > /proc/1/fd/1 && \
            npx pm2 stop preview_app > /proc/1/fd/1 2>&1 && \
            echo "--- Clearing Orphaned Port 4200 ---" > /proc/1/fd/1 && \
            fuser -k 4200/tcp > /proc/1/fd/1 2>&1 || true && \
            echo "--- Restarting Preview Server ---" > /proc/1/fd/1 && \
            npx pm2 start preview_app > /proc/1/fd/1 2>&1
            """

            # 5. Execute synchronously (detach=False is the default). 
            # This blocks the UI slightly but guarantees we get the actual exit code.
            StateManager.update_repo(user_email, repo_url, docker_stage="Syncing Workflow...")
            exec_log = c.exec_run(["/bin/sh", "-c", cmd])

            # 6. Return improved contextual feedback based on the exact exit code
            if exec_log.exit_code == 0:
                StateManager.update_repo(user_email, repo_url, docker_stage="App Running (Synced)")
                return html.Span(
                    [html.I(className="bi bi-check-circle-fill me-1"), "Sync completed successfully. See logs."], 
                    className="text-success fw-bold"
                )
            else:
                StateManager.update_repo(user_email, repo_url, docker_stage="Sync Failed")
                return html.Span(
                    [html.I(className="bi bi-exclamation-triangle-fill me-1"), f"Sync failed (Exit code: {exec_log.exit_code}). Check logs tab."], 
                    className="text-danger fw-bold"
                )

        except Exception as e:
            return html.Span(
                [html.I(className="bi bi-x-circle-fill me-1"), f"System Error: {str(e)}"], 
                className="text-danger fw-bold"
            )

@app.callback(
    Output('tab-content', 'children'),
    [Input('viewport-tabs', 'active_tab'),
     Input('env-url-store', 'data'),
     Input('env-selector', 'value'), # Added to know which logs to show
     Input('log-poller', 'n_intervals')],
    State('repo-selector', 'value')
)
def update_viewport(active_tab, env_url, env_value, n, repo_url):
    if 'user' not in session: return no_update
    
    # Unified Heartbeat
    email = session['user']['email']
    StateManager.update_user(email, last_heartbeat=time.time())

    ctx = callback_context
    triggered_ids = [t['prop_id'] for t in ctx.triggered] if ctx.triggered else []
    
    # Priority check: If tab changed or URL arrived, ignore the poller's 'no_update' rule
    is_priority = 'viewport-tabs.active_tab' in triggered_ids or 'env-url-store.data' in triggered_ids
    if not is_priority and 'log-poller.n_intervals' in triggered_ids and active_tab == "tab-preview":
        return no_update

    # --- TAB: PREVIEW ---
    if active_tab == "tab-preview":
        if env_value == 'local':
            return html.Iframe(src=f"/preview/?t={int(time.time())}", style={"width": "100%", "height": "80vh", "border": "none"})
        elif env_url:
            return html.Iframe(src=env_url, key=env_url, style={"width": "100%", "height": "80vh", "border": "none"})
        else:
            return html.Div([
                html.I(className="bi bi-cloud-arrow-up display-4 text-muted mb-3"), 
                html.P("Waiting for environment to be ready...", className="text-muted")
            ], className="d-flex flex-column justify-content-center align-items-center h-100")

    # --- TAB: LOGS ---
    elif active_tab == "tab-logs":
        # Case A: Local Docker Logs (Existing Logic)
        if env_value == 'local':
            c_name = sanitize_container_name(email)
            try:
                c = docker_client.containers.get(c_name)
                logs = c.logs(tail=200).decode('utf-8')
                cleaned_logs = re.sub(r'\x1b\[\d*[A-KG]', '', logs)
                log_html = conv.convert(cleaned_logs, full=False)
                
                # Inject JS for "Sticky Scrolling"
                # Logic: 
                # - On load, if 'wasAtBottom' (from sessionStorage) is true, scroll down.
                # - On scroll, update 'wasAtBottom' based on position.
                full_html = f"""
                <html>
                <head>
                    <style>body {{ background-color: #0d1117; color: #c9d1d9; font-family: monospace; white-space: pre-wrap; }}</style>
                </head>
                <body>
                    {log_html}
                    <script>
                        const body = document.body;
                        const html = document.documentElement;
                        
                        // 1. Check if we should scroll to bottom (default to true on first load)
                        const wasAtBottom = sessionStorage.getItem('log_pos') !== 'false';

                        if (wasAtBottom) {{
                            window.scrollTo(0, body.scrollHeight);
                        }} else {{
                            // Restore previous scroll position if needed (optional complexity, usually just staying put is enough)
                            const lastScroll = sessionStorage.getItem('scroll_val');
                            if (lastScroll) window.scrollTo(0, lastScroll);
                        }}

                        // 2. Listen for scroll events to update state
                        window.addEventListener('scroll', () => {{
                            // Tolerance of 50px
                            const distanceToBottom = body.scrollHeight - window.innerHeight - window.scrollY;
                            const isAtBottom = distanceToBottom < 50;
                            
                            sessionStorage.setItem('log_pos', isAtBottom);
                            sessionStorage.setItem('scroll_val', window.scrollY);
                        }});
                    </script>
                </body>
                </html>
                """

                return html.Iframe(srcDoc=full_html, style={"width": "100%", "height": "80vh", "border": "none"})
            except:
                return html.Div("No local container running.", className="p-4 text-muted")

        # Case B: Cloud/PR/Main - Redirect to GitHub
        else:
            repo_path = get_repo_path(repo_url) if repo_url else ""
            actions_url = f"https://github.com/{repo_path}/actions"
            
            return html.Div([
                html.I(className="bi bi-github display-1 text-muted mb-4"),
                html.H4("Cloud Build Logs", className="text-white"),
                html.P("Live logs for Cloud builds are hosted on GitHub Actions.", className="text-muted mb-4"),
                dbc.Button([
                    html.I(className="bi bi-box-arrow-up-right me-2"),
                    "View Build Progress on GitHub"
                ], href=actions_url, target="_blank", color="primary", size="lg")
            ], className="d-flex flex-column justify-content-center align-items-center h-100 p-5 text-center")

    return html.Div("Select a tab")

#region Admin Callbacks

@app.callback(
    Output('acl-save-status', 'children'),
    Output('acl-save-status', 'className'),
    Input('save-acl-btn', 'n_clicks'),
    State('acl-editor', 'value'),
    prevent_initial_call=True
)
def save_acl_callback(n, acl_text):
    if 'user' not in session or not is_admin(session['user']['email']):
        return "Unauthorized", "mt-2 text-danger fw-bold"
    try:
        new_acl = json.loads(acl_text)
        save_acl(new_acl)
        return "ACL Saved Successfully.", "mt-2 text-success fw-bold"
    except json.JSONDecodeError as e:
        return f"Invalid JSON format: {e}", "mt-2 text-warning fw-bold"
    except Exception as e:
        return f"System Error: {e}", "mt-2 text-danger fw-bold"

@app.callback(
    Output({'type': 'kill-status', 'index': MATCH}, 'children'),
    Input({'type': 'kill-btn', 'index': MATCH}, 'n_clicks'),
    prevent_initial_call=True
)
def admin_kill_container(n, btn_id):
    container_name = btn_id['index']
    if 'user' not in session or not is_admin(session['user']['email']):
        return "Unauthorized"
    try:
        c = docker_client.containers.get(container_name)
        c.stop()
        c.remove()
        return "Terminated"
    except Exception as e:
        return "Failed"

@app.callback(
    Output('admin-table-container', 'children'),
    Input('admin-poller', 'n_intervals')
)
def update_admin_table(n):
    if not has_request_context() or 'user' not in session or not is_admin(session['user']['email']):
        return no_update

    rows = []
    now = time.time()

    state = StateManager.read()

    try:
        # Fast query, no stats=True
        for c in docker_client.containers.list(all=True, filters={"network": NETWORK_NAME}):
            if c.name in ['control-plane', 'gateway']: continue
            
            repo = c.labels.get("user_repo", "None")
            status_badge = dbc.Badge(c.status, color="success" if c.status == "running" else "secondary")

            email_match = next((m for m in state if sanitize_container_name(m) == c.name), None)
            user_data = state.get(email_match, {}) if email_match else {}
            
            # 1. Calculate Heartbeat Health
            last_seen = user_data.get('last_heartbeat')
            if last_seen is None:
                hb_text = "Idle / Offline"
                hb_color = "text-muted"
            else:
                seconds_ago = int(now - last_seen)
                hb_text = f"{seconds_ago}s ago"
                
                # Color code the heartbeat warning
                if seconds_ago > HEARTBEAT_TIMEOUT:
                    hb_color = "text-danger" # About to be killed
                elif seconds_ago > (HEARTBEAT_TIMEOUT / 2):
                    hb_color = "text-warning"
                else:
                    hb_color = "text-success"

            # 2. Get Current Stage
            repo_info = user_data.get("repos", {}).get(repo, {})
            stage = repo_info.get("docker_stage", "Unknown")
            if c.status != "running":
                stage = "Stopped"

            rows.append(html.Tr([
                html.Td([html.Strong(c.name), html.Br(), html.Small(repo, className="text-muted")]), 
                html.Td(status_badge),
                html.Td(html.Span(stage, className="small text-info")),
                html.Td(html.Span(hb_text, className=f"small fw-bold {hb_color}")),
                html.Td([
                    dbc.Button("Kill", id={'type': 'kill-btn', 'index': c.name}, color="danger", size="sm", className="py-0"),
                    html.Div(id={'type': 'kill-status', 'index': c.name}, className="small text-danger mt-1")
                ])
            ]))
    except Exception as e:
        return html.Div(f"Error loading containers: {e}", className="text-danger")

    return dbc.Table([
        html.Thead(html.Tr([
            html.Th("Container / Repo"), 
            html.Th("Status"),
            html.Th("Current Stage"), 
            html.Th("Last Heartbeat"), 
            html.Th("Actions")
        ])),
        html.Tbody(rows)
    ], bordered=True, hover=True, striped=True, className="mt-2")

@app.callback(
    Output('page-content', 'children'),
    Input('url', 'pathname')
)
def display_page(pathname):
    # Guard clause in case a layout request fires when logged out
    if not has_request_context() or 'user' not in session:
        return html.Div()

    user_data = session['user']

    # Route to the correct layout
    if pathname == '/admin':
        return get_admin_layout(user_data, pathname)
    else:
        return get_dashboard_layout(user_data, pathname)

@server.route('/_auth_check')
def auth_check():
    if 'user' not in session: return Response("Unauthorized", status=401)

    email = session['user']['email']
    container_name = sanitize_container_name(email)
    
    # Check if running
    try:
        c = docker_client.containers.get(container_name)
        if c.status != 'running': raise Exception
    except:
        return Response("Container not running", status=404)

    resp = Response("OK", status=200)
    
    # Instead of a PORT, we return the CONTAINER NAME (Hostname)
    # Nginx will resolve "gabe-idems-international" to an IP address
    resp.headers['X-Target-Host'] = container_name
    return resp

#endregion
#region Existing Build Preview
def get_pat_for_repo(repo_url):
    """Finds the repo in REPOS, looks up its pat_env key, and fetches it from the environment."""
    for name, data in REPOS.items():
        if data.get('url') == repo_url:
            env_key = data.get('pat_env', 'GITHUB_PAT') # Fallback to standard name
            return os.environ.get(env_key)
    return os.environ.get('GITHUB_PAT')

def get_repo_path(repo_url):
    """Converts 'https://github.com/IDEMSInternational/repo.git' to 'IDEMSInternational/repo'"""
    clean_url = repo_url.replace(".git", "").rstrip("/")
    parts = clean_url.split("/")
    return f"{parts[-2]}/{parts[-1]}"

@app.callback(
    Output('env-selector', 'options'),
    Output('env-selector', 'disabled'),
    Output('env-selector', 'value'),
    Input('repo-selector', 'value'),
)
def populate_env_dropdown(repo_url):
    if not repo_url: return [], True, None
    
    options = [
        {'label': '🟢 Main Branch (Live)', 'value': 'main'},
        {'label': '☁️ Cloud Draft (Fast Sync)', 'value': 'cloud'},
        {'label': '💻 Local Draft (Docker)', 'value': 'local'}
    ]
    
    pat = get_pat_for_repo(repo_url)
    if pat:
        headers = {"Authorization": f"token {pat}", "Accept": "application/vnd.github.v3+json"}
        try:
            res = requests.get(f"https://api.github.com/repos/{get_repo_path(repo_url)}/pulls?state=open", headers=headers)
            if res.status_code == 200:
                prs = res.json()
                pr_opts = [{'label': f"🟣 PR #{pr['number']} - {pr['title']}", 'value': pr['number']} for pr in prs]
                options.extend(pr_opts)
        except Exception as e:
            print(f"GitHub API Error fetching PRs: {e}")
            
    email = session['user']['email']
    repo_state = StateManager.get_repo(email, repo_url)
    
    # Default to their last used environment (local/cloud), 
    # falling back to 'main' if it's their first time.
    last_env = repo_state.get('last_env', 'main')
    
    return options, False, last_env

@app.callback(
    Output('env-url-store', 'data'),
    Output('env-status', 'children'),
    Input('env-selector', 'value'),
    Input('log-poller', 'n_intervals'),
    State('repo-selector', 'value'),
)
def resolve_env_url(env_value, n_intervals, repo_url):
    if not env_value or not repo_url: return None, ""
    
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else None

    # Protect API Limits: Only process poller ticks if we are waiting for Cloud/PRs
    if trigger_id == 'log-poller':
        if env_value in ['main', 'local']: 
            raise PreventUpdate
        if n_intervals % 5 != 0: # Throttle to every 10 seconds
            raise PreventUpdate

    if env_value == 'main':
        return get_gh_pages_url(repo_url), html.Span([html.I(className="bi bi-globe me-1"), "Showing Live Main Branch"], className="text-success")
        
    if env_value == 'local':
        return 'local', html.Span([html.I(className="bi bi-pc-display me-1"), "Showing Local Docker Build"], className="text-info")

    # Fetch PAT & Repo
    pat = get_pat_for_repo(repo_url)
    repo_path = get_repo_path(repo_url)
    headers = {"Authorization": f"token {pat}", "Accept": "application/vnd.github.v3+json"}
    
    # --- CLOUD ENVIRONMENT RESOLUTION ---
    if env_value == 'cloud':
        email = session['user']['email']
        repo_state = StateManager.get_repo(email, repo_url)
        
        status = repo_state.get('status')
        url = repo_state.get('preview_url')
        last_updated = repo_state.get('last_updated')
        
        if not status:
            return None, html.Span("Click Sync to build Cloud Draft", className="text-info")
            
        if status == 'success' and url:
            time_str = time.strftime('%H:%M', time.gmtime(last_updated)) if last_updated else "Unknown"
            
            return url, html.Span([
                html.I(className="bi bi-check-circle-fill me-1"), 
                f"Cloud Preview Ready (Built at {time_str}UTC)"
            ], className="text-success")
            
        elif status == 'failure':
            return None, html.Span([html.I(className="bi bi-exclamation-triangle-fill me-1"), "Build Failed. Please check logs."], className="text-danger")
        else:
            return None, html.Span([html.I(className="bi bi-hourglass-split me-1"), "Building in Cloud..."], className="text-warning")

    # --- PR RESOLUTION ---
    try:
        pr_res = requests.get(f"https://api.github.com/repos/{repo_path}/pulls/{env_value}", headers=headers)
        pr_branch = pr_res.json()['head']['ref']
        
        deps = requests.get(f"https://api.github.com/repos/{repo_path}/deployments?ref={pr_branch}", headers=headers).json()
        if deps:
            statuses = requests.get(deps[0]['statuses_url'], headers=headers).json()
            if statuses and statuses[0]['state'] == 'success':
                return statuses[0].get('environment_url'), html.Span([html.I(className="bi bi-cloud-check me-1"), "Firebase PR Preview"], className="text-success")
            elif statuses and statuses[0]['state'] in ['pending', 'in_progress']:
                return None, html.Span([html.I(className="bi bi-hourglass-split me-1"), "Firebase build in progress..."], className="text-warning")
                
        return None, html.Span([html.I(className="bi bi-exclamation-triangle me-1"), "No successful Firebase deployment found."], className="text-danger")
    except Exception as e:
        return None, f"API Error: {str(e)}"

def get_gh_pages_url(repo_url):
    """Infers the GitHub Pages URL, or grabs an override from repo_config.json"""
    for name, data in REPOS.items():
        if data.get('url') == repo_url:
            # Check if an explicit override exists in repo_config.json
            if 'gh_pages' in data:
                return data['gh_pages']
                
    # Otherwise, infer it: IDEMSInternational/app-debug-content -> idemsinternational.github.io/app-debug-content
    repo_path = get_repo_path(repo_url)
    owner, repo_name = repo_path.split('/')
    return f"https://{owner.lower()}.github.io/{repo_name}/"

@app.callback(
    Output('env-url-store', 'data', allow_duplicate=True),
    Input('repo-selector', 'value'),
    prevent_initial_call=True
)
def clear_url_on_repo_change(repo_url):
    return None

@app.callback(
    Output('repo-selector', 'className'), # Using a dummy output (className)
    Input('repo-selector', 'value'),
    prevent_initial_call=True
)
def sync_active_repo_to_state(repo_url):
    """Saves the currently selected repository to the user's global state."""
    if 'user' in session and repo_url:
        StateManager.update_user(session['user']['email'], active_repo=repo_url)
    return no_update

@app.callback(
    Output('env-selector', 'className'), # Dummy output
    Input('env-selector', 'value'),
    State('repo-selector', 'value'),
    prevent_initial_call=True
)
def save_env_choice_to_state(env_value, repo_url):
    if 'user' in session and repo_url and env_value:
        StateManager.update_repo(session['user']['email'], repo_url, last_env=env_value)
    return no_update
#endregion
#endregion
#region Container Monitoring

def is_container_running(email):
    try:
        container = docker_client.containers.get(sanitize_container_name(email))
        return container.status == 'running'
    except:
        return False

def monitor_user_activity():
    """
    Background loop to clean up Docker resources for inactive users.
    1. Stops containers if the heartbeat is lost (> HEARTBEAT_TIMEOUT).
    2. Fully removes containers that have been 'exited' for more than 24 hours.
    """
    while True:
        time.sleep(5)  # Check every 5 seconds
        now = time.time()

        # Load the entire state at the start of the tick
        full_state = StateManager.read()

        for email, user_data in full_state.items():
            last_seen = user_data.get('last_heartbeat')
            c_name = sanitize_container_name(email)

            # --- PHASE 1: HEARTBEAT EXPIRATION (Stop Container) ---
            if last_seen and (now - last_seen > HEARTBEAT_TIMEOUT):
                try:
                    # Check if this user actually has a container before trying to kill
                    container = docker_client.containers.get(c_name)

                    if container.status == "running":
                        print(f"Reaper: Heartbeat lost for {email}. Stopping {c_name}...")
                        container.stop()

                        # Update state: Set heartbeat to None and stage to Stopped
                        # We find which repo was active by looking for the one with a docker_stage
                        for repo_url, repo_info in user_data.get("repos", {}).items():
                            if repo_info.get("docker_stage") and repo_info.get("docker_stage") != "Stopped":
                                StateManager.update_repo(email, repo_url, docker_stage="Stopped")

                        StateManager.update_user(email, last_heartbeat=None)
                except docker.errors.NotFound:
                    # Container already gone, just clean up state
                    StateManager.update_user(email, last_heartbeat=None)
                except Exception as e:
                    print(f"Reaper Error (Stop Phase): {e}")

            # --- PHASE 2: LONG-TERM CLEANUP (Remove Container) ---
            # If the user has been offline for a long time, remove the container to save disk space
            if last_seen is None:
                try:
                    container = docker_client.containers.get(c_name)
                    if container.status == "exited":
                        finished_at_str = container.attrs['State']['FinishedAt']
                        # Convert Docker ISO timestamp to datetime object
                        finished_at = datetime.fromisoformat(finished_at_str.replace('Z', '+00:00'))
                        current_time = datetime.now(UTC)

                        # Remove if exited for more than 24 hours
                        if (current_time - finished_at).total_seconds() > 24 * 3600:
                            print(f"Reaper: Removing stale container {c_name} (Exited > 24h)")
                            container.remove()

                            # Clean up docker_stage for all repos for this user
                            for repo_url in user_data.get("repos", {}).keys():
                                StateManager.update_repo(email, repo_url, docker_stage=None)

                except docker.errors.NotFound:
                    pass 
                except Exception as e:
                    print(f"Reaper Error (Remove Phase): {e}")

# Start the daemon thread
threading.Thread(target=monitor_user_activity, daemon=True).start()

#endregion

if __name__ == '__main__':
    # SSL usually needed for Google OAuth, or set OAUTHLIB_INSECURE_TRANSPORT for dev
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1' 
    app.run(debug=False, port=8050, host='0.0.0.0')