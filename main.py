import os
import socket
import docker
import re
from flask import Flask, session, redirect, url_for, has_request_context, Response
from authlib.integrations.flask_client import OAuth
from dash import Dash, html, dcc, Input, Output, State, no_update, callback_context
import dash_bootstrap_components as dbc
import json
from ansi2html import Ansi2HTMLConverter

# --- CONFIGURATION ---
PORT_RANGE = range(5000, 5050)
DOCKER_IMAGE = "open-app-builder"
# Ideally load these from environment variables
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "super_secret_dev_key")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
NETWORK_NAME = os.environ.get("DOCKER_NETWORK_NAME", "app-net")

with open("repo_config.json", 'r') as json_file:
    REPOS =json.load(json_file)

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
    client_kwargs={'scope': 'openid email profile'}
)

conv = Ansi2HTMLConverter()#bg="#0d1117", fg="#c9d1d9", inline=True)

# NOTE: Removed suppress_callback_exceptions=True
app = Dash(
    __name__,
    server=server,
    assets_folder='site_assets', 
    assets_url_path='site_assets',
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css"
    ]
)

# --- HELPER FUNCTIONS ---

def sanitize_container_name(email):
    return re.sub(r'[^a-zA-Z0-9]', '-', email)

def kill_user_resources(email):
    if not docker_client: return
    try:
        container = docker_client.containers.get(sanitize_container_name(email))
        container.stop()
        container.remove()
    except:
        pass

# --- FLASK ROUTES ---
@server.route('/login')
def login():
    redirect_uri = url_for('auth', _external=True)
    return google.authorize_redirect(redirect_uri)

@server.route('/auth/callback')
def auth():
    token = google.authorize_access_token()
    user_info = token.get('userinfo')
    session['user'] = {
        'email': user_info['email'],
        'name': user_info['name'],
        'picture': user_info['picture'],
    }
    return redirect('/')

@server.route('/logout')
def logout():
    if 'user' in session:
        kill_user_resources(session['user']['email'])
    session.pop('user', None)
    return redirect('/')

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

def get_dashboard_layout(user):
    name = user['name'] if user else ""
    # Use a generic avatar if google picture fails, or keep user['picture']
    picture = user['picture'] if user else "https://via.placeholder.com/40"
    
    # Current Repo Logic (Existing)
    current_repo = None
    if user and docker_client:
        try:
            c = docker_client.containers.get(sanitize_container_name(user['email']))
            current_repo = c.labels.get("user_repo")
        except docker.errors.NotFound:
            pass # No container running, keep selection empty
        except Exception as e:
            print(f"Error checking container state: {e}")

    # --- NAVBAR ---
    navbar = dbc.Navbar(
        dbc.Container([
            # Left: IDEMS Logo
            html.A(
                dbc.Row([
                    dbc.Col(html.Img(src="/site_assets/idems-logo.png", height="40px")),
                    dbc.Col(dbc.NavbarBrand("Open App Builder TestHub", className="ms-3 fs-4 fw-bold text-white")),
                ], align="center", className="g-0"),
                href="/",
                style={"textDecoration": "none"},
            ),
            
            # Right: User Info & Logout
            dbc.Row([
                dbc.Col(html.Span(f"Welcome, {name}", className="text-white me-3 d-none d-md-block")),
                dbc.Col(html.Img(src=picture, height="35px", className="rounded-circle border border-secondary")),
                dbc.Col(dbc.Button("Logout", href="/logout", external_link=True, color="danger", size="sm", className="ms-3")),
            ], align="center", className="g-0"),
        ], fluid=True),
        color="#1e1e1e", # Matches custom CSS var
        dark=True,
        className="border-bottom py-2"
    )

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
                        options=[{'label': k, 'value': v['url']} for k, v in REPOS.items()],
                        placeholder="Select repo...",
                        value=current_repo,
                        
                    ),
                    html.Div(id='deploy-status', className="mb-4 text-muted small"),
                    html.Hr(className="border-secondary"),
                    dbc.Button([
                        html.I(className="bi bi-arrow-repeat me-2"), 
                        "Sync Workflow"
                    ], id='btn-sync', color="primary", className="w-100 mb-2 shadow-sm"),
                    html.Div(id='sync-status', className="text-muted small text-center")
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
        dcc.Interval(id='log-poller', interval=2000, n_intervals=0, disabled=False) 
    ], fluid=True, className="p-0")])

# --- MAIN LAYOUT FUNCTION ---

def serve_layout():
    # Check if we are in a request (user loading page) or startup (Dash validating)
    # Default to "Logged Out" state
    is_logged_in = False
    user_data = None

    if has_request_context() and 'user' in session:
        is_logged_in = True
        user_data = session['user']

    # FIX 2: Render BOTH layouts, but hide one using CSS 'display'.
    # This ensures all IDs (repo-selector, etc.) exist in the DOM at startup.
    login_style = {'display': 'none'} if is_logged_in else {'display': 'block'}
    dashboard_style = {'display': 'block'} if is_logged_in else {'display': 'none'}

    return html.Div([
        html.Div(get_login_layout(), id='login-wrapper', style=login_style),
        html.Div(get_dashboard_layout(user_data), id='dashboard-wrapper', style=dashboard_style)
    ])

app.layout = serve_layout

# --- CALLBACKS ---

@app.callback(
    Output('deploy-status', 'children'),
    Input('repo-selector', 'value'),
    prevent_initial_call=True
)
def deploy_repo(repo_url):
    # Guard: If callback fires but no user is in session (shouldn't happen but good practice)
    if 'user' not in session: return no_update
    
    user = session['user']
    repo_key = next((v['key'] for k, v in REPOS.items() if v['url'] == repo_url), "")

    cmd = (
        # f"export DEPLOYMENT_PRIVATE_KEY=\"{repo_key}\" && "
        f"yarn workflow deployment import {repo_url} -y --private-key '{repo_key}' && "
        f"yarn start:docker"
    )

    try:
        kill_user_resources(user['email'])
        docker_client.containers.run(
            DOCKER_IMAGE,
            entrypoint="/bin/sh",
            command=["-c", cmd],
            name=sanitize_container_name(user['email']),
            network=NETWORK_NAME,
            labels={"user_repo": repo_url},
            detach=True,
            remove=False,
        )
        return "Started container, see Live System Logs for status."
    except Exception as e:
        return f"Error: {str(e)}"

@app.callback(
    Output('sync-status', 'children'),
    Input('btn-sync', 'n_clicks'),
    prevent_initial_call=True
)
def sync_workflow(n):
    if 'user' not in session: return no_update
    try:
        c = docker_client.containers.get(sanitize_container_name(session['user']['email']))
        c.exec_run("yarn workflow sync", detach=True)
        return "Sync sent."
    except Exception as e:
        return f"Failed: {e}"

@app.callback(
    Output('tab-content', 'children'),
    [Input('viewport-tabs', 'active_tab'),
     Input('log-poller', 'n_intervals')]
)
def update_viewport(active_tab, n):
    # Guard: Stop updates if not logged in
    if 'user' not in session: return no_update

    # This prevents the iframe from reloading/flashing.
    ctx = callback_context
    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0]
    if trigger_id == 'log-poller' and active_tab == 'tab-preview':
        return no_update

    user = session['user']
    c_name = sanitize_container_name(user['email'])
    
    if active_tab == "tab-preview":
        # Nginx will intercept '/preview/' and route it
        # We add a random query param to bust iframe caching if the container restarts
        import time
        return html.Iframe(
            src=f"/preview/?t={int(time.time())}", 
            style={"width": "100%", "height": "80vh", "border": "none"}
        )
    
    elif active_tab == "tab-logs":
        try:
            c = docker_client.containers.get(c_name)
            # Fetch last 100 lines for context
            logs = c.logs(tail=200).decode('utf-8')
            
            # 1. Clean up cursor movement codes (A, K, G), keep colors (m)
            cleaned_logs = re.sub(r'\x1b\[\d*[A-KG]', '', logs)

            # 2. Convert logs to HTML body
            log_html = conv.convert(cleaned_logs, full=False)

            # 3. Inject JS for "Sticky Scrolling"
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

        except Exception as e:
            return html.Div(f"Log Error: {e}")
            
    return html.Div("Select tab")


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


def is_container_running(email):
    try:
        container = docker_client.containers.get(sanitize_container_name(email))
        return container.status == 'running'
    except:
        return False


if __name__ == '__main__':
    # SSL usually needed for Google OAuth, or set OAUTHLIB_INSECURE_TRANSPORT for dev
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1' 
    app.run(debug=True, port=8050, host='0.0.0.0')