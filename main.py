import os
import socket
import docker
import re
from flask import Flask, session, redirect, url_for, has_request_context
from authlib.integrations.flask_client import OAuth
from dash import Dash, html, dcc, Input, Output, State, no_update
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
app = Dash(__name__, server=server, external_stylesheets=[dbc.themes.BOOTSTRAP])

# --- HELPER FUNCTIONS ---
def get_free_port():
    for port in PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('localhost', port)) != 0:
                return port
    return None

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
        'port': get_free_port()
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
        dbc.Row(dbc.Col(html.H1("App Builder Login"), className="text-center mt-5")),
        dbc.Row(dbc.Col(
            # FIX 1: external_link=True forces a real HTTP request to Flask
            dbc.Button("Login with Google", href="/login", external_link=True, color="primary"), 
            className="text-center"
        ))
    ])

def get_dashboard_layout(user):
    # If user is None (startup check), provide dummy data to avoid errors
    name = user['name'] if user else ""
    picture = user['picture'] if user else ""
    current_repo = None

    if user and docker_client:
        try:
            container_name = sanitize_container_name(user['email'])
            container = docker_client.containers.get(container_name)
            
            # Read the label we saved earlier
            current_repo = container.labels.get("user_repo")
            
        except docker.errors.NotFound:
            pass # No container running, keep selection empty
        except Exception as e:
            print(f"Error checking container state: {e}")
    
    return dbc.Container([
        dbc.Row([
            dbc.Col(html.H4(f"Welcome, {name}")),
            dbc.Col(html.Img(src=picture, height="40px", style={'borderRadius': '50%'})),
            dbc.Col(dbc.Button("Logout", href="/logout", external_link=True, color="danger", size="sm"), width="auto")
        ], className="py-3 border-bottom"),

        dbc.Row([
            dbc.Col([
                html.H5("Controls", className="mt-3"),
                html.Label("Select Repo:"),
                dcc.Dropdown(
                    id='repo-selector',
                    options=[{'label': k, 'value': v['url']} for k, v in REPOS.items()],
                    placeholder="Select repo...",
                    value=current_repo,
                ),
                html.Div(id='deploy-status', className="mt-2 mb-4 text-muted small"),
                html.Hr(),
                dbc.Button("Sync Workflow", id='btn-sync', color="info", className="w-100 mb-2"),
                html.Div(id='sync-status', className="text-muted small")
            ], width=3, className="bg-light border-end vh-100"),

            dbc.Col([
                dbc.Tabs([
                    dbc.Tab(label="App Preview", tab_id="tab-preview"),
                    dbc.Tab(label="Live System Logs", tab_id="tab-logs"),
                ], id="viewport-tabs", active_tab="tab-preview", className="mt-3"),
                html.Div(id="tab-content", className="p-3 border border-top-0")
            ], width=9)
        ]),
        # Disable interval by default, enable via callback or if logged in
        dcc.Interval(id='log-poller', interval=2000, n_intervals=0, disabled=False) 
    ], fluid=True)

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
    
    kill_user_resources(user['email'])
    
    cmd = (
        # f"export DEPLOYMENT_PRIVATE_KEY=\"{repo_key}\" && "
        f"yarn workflow deployment import {repo_url} -y --private-key '{repo_key}' && "
        f"yarn start:docker"
    )

    try:
        # try:
        #     for container in docker_client.containers.list(all=True, filters={'name': sanitize_container_name(user['email'])}):
        #         if container.name == sanitize_container_name(user['email']):
        #             container.remove()
        # except Exception as e:
        #     print(e)
        docker_client.containers.run(
            DOCKER_IMAGE,
            entrypoint="/bin/sh",
            command=["-c", cmd],
            name=sanitize_container_name(user['email']),
            ports={'4200/tcp': user['port']},
            labels={"user_repo": repo_url},
            detach=True,
            remove=False,
        )
        return f"Started on port {user['port']}."
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

    user = session['user']
    c_name = sanitize_container_name(user['email'])
    
    if active_tab == "tab-preview":
        return html.Iframe(src=f"http://localhost:{user['port']}", style={"width": "100%", "height": "80vh", "border": "none"})
    
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

if __name__ == '__main__':
    # SSL usually needed for Google OAuth, or set OAUTHLIB_INSECURE_TRANSPORT for dev
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1' 
    app.run(debug=True, port=8050)