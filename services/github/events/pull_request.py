def process_pull_request_event(payload: dict, conn):
    # Similar pero con lógica para PRs
    pr = payload.get("pull_request", {})
    title = pr.get("title")
    url = pr.get("html_url")
    print("📘 PR Title:", title)
    print("🔗 URL:", url)