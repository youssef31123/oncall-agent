import azure.functions as func
import json
import logging
import os
import time
import requests
from datetime import timedelta
from azure.identity import AzureCliCredential, DefaultAzureCredential
from azure.monitor.query import LogsQueryClient, LogsQueryStatus

app = func.FunctionApp()

WORKSPACE_ID = os.environ.get("LOG_ANALYTICS_WORKSPACE_ID")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")


def get_credential():
    # DefaultAzureCredential tries managed identity first (works in Azure),
    # then falls back to environment/CLI/etc (works locally after az login).
    return DefaultAzureCredential()


def fetch_recent_errors():
    client = LogsQueryClient(get_credential())
    query = """
    ContainerLogV2
    | where PodName startswith "oncall-demo"
    | where LogMessage has_any ("Traceback", "KeyError", "Error", "Exception")
    | order by TimeGenerated desc
    | take 30
    | project TimeGenerated, LogMessage
    """
    response = client.query_workspace(
        workspace_id=WORKSPACE_ID,
        query=query,
        timespan=timedelta(minutes=30)
    )
    if response.status != LogsQueryStatus.SUCCESS:
        return []
    rows = []
    for table in response.tables:
        for row in table.rows:
            rows.append({"time": str(row[0]), "message": row[1]})
    return rows


def fetch_latest_diff():
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/commits"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"}
    commits = requests.get(url, headers=headers, params={"per_page": 2}, timeout=15).json()
    if not isinstance(commits, list) or len(commits) < 2:
        return "", commits[0]["sha"] if isinstance(commits, list) and commits else None
    latest_sha = commits[0]["sha"]
    diff_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/compare/{commits[1]['sha']}...{latest_sha}"
    diff = requests.get(diff_url, headers=headers, timeout=15).json()
    files_summary = ""
    for f in diff.get("files", []):
        files_summary += f"\n--- {f['filename']} ---\n{f.get('patch', '')}\n"
    return files_summary, latest_sha


def ask_gemini(logs, diff):
    log_text = "\n".join(f"[{r['time']}] {r['message']}" for r in logs[:15])
    prompt = f"""You are an SRE assistant. An alert fired for a Python Flask app running on Kubernetes.

RECENT ERROR LOGS:
{log_text}

RECENT CODE CHANGE (diff):
{diff}

Respond in this exact format:
ROOT CAUSE: <one or two sentences>
SUGGESTED FIX: <the code fix as a unified diff or clear before/after snippet>
CONFIDENCE: <low/medium/high>
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent?key={GEMINI_API_KEY}"
    body = {"contents": [{"parts": [{"text": prompt}]}]}

    last_error = None
    for attempt in range(3):
        try:
            resp = requests.post(url, json=body, timeout=45)
            if resp.status_code == 200:
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
            if resp.status_code in (503, 429):
                time.sleep(3 * (attempt + 1))
                last_error = resp
                continue
            resp.raise_for_status()
        except requests.exceptions.Timeout as e:
            last_error = e
            time.sleep(3 * (attempt + 1))
            continue
    if isinstance(last_error, requests.Response):
        last_error.raise_for_status()
    raise last_error


def open_github_issue(diagnosis, logs):
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/issues"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"}
    sample_logs = chr(10).join(l['message'][:200] for l in logs[:5]) if logs else "No logs captured"
    body = f"""## Auto-detected incident

An alert fired due to application errors.

### Diagnosis (Gemini)
{diagnosis}

### Sample logs


*This issue was opened automatically by the on-call agent.*
"""
    payload = {"title": "[Auto-Incident] Application errors detected", "body": body}
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    return resp.json()


@app.route(route="AlertReceiver", auth_level=func.AuthLevel.FUNCTION)
def AlertReceiver(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Alert received, starting diagnosis pipeline")
    try:
        logs = fetch_recent_errors()
        diff, sha = fetch_latest_diff()
        diagnosis = ask_gemini(logs, diff)
        issue = open_github_issue(diagnosis, logs)
        return func.HttpResponse(
            json.dumps({"status": "ok", "issue_url": issue.get("html_url"), "diagnosis": diagnosis}),
            status_code=200,
            mimetype="application/json"
        )
    except Exception as e:
        logging.exception("Pipeline failed")
        return func.HttpResponse(json.dumps({"status": "error", "message": str(e)}), status_code=500)
