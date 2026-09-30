
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