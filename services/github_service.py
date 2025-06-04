import psycopg2
import psycopg2.extras
from database import get_connection
from fastapi import HTTPException
import requests
import httpx
from collections import defaultdict
from datetime import datetime

def get_user_github_credentials(user_id: int):
    try:
        conn = get_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute('SELECT "github_token", "github_username" FROM "Employee" WHERE "id" = %s', (user_id,))
        row = cur.fetchone()

        if not row:
            print("❌ Credenciales no encontradas")
            raise HTTPException(status_code=404, detail="GitHub credentials not found")

        token = row.get("github_token")
        username = row.get("github_username")

        if not token:
            raise HTTPException(status_code=404, detail="GitHub token is empty")
        if not username:
            raise HTTPException(status_code=404, detail="GitHub username is missing")

        return token, username

    except Exception as e:
        print("❌ DB error:", e)
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        conn.close()

def fetch_github_repos(token: str):
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json"
    }

    response = requests.get("https://api.github.com/user/repos?per_page=100", headers=headers)
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail="GitHub API error")

    repos = response.json()
    return [
        {
            "id": repo["id"],
            "full_name": repo["full_name"],
            "description": repo.get("description"),
            "language": repo.get("language"),
            "updated_at": repo["updated_at"][:10],
            "visibility": "private" if repo["private"] else "public"
        }
        for repo in repos
    ]

def human_date(date: datetime) -> str:
    return date.strftime("%B %d, %Y")

def relative_day(date: datetime) -> str:
    delta = datetime.utcnow() - date
    if delta.days == 0:
        return "today"
    elif delta.days == 1:
        return "yesterday"
    else:
        return f"{delta.days} days ago"

def humanize_date(date_str):
    date = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ")
    delta = datetime.utcnow() - date
    if delta.days == 0:
        return "today"
    elif delta.days == 1:
        return "yesterday"
    else:
        return f"{delta.days} days ago"

async def get_grouped_commits(token: str, repo: str, branch: str, username: str):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }

    url = f"https://api.github.com/repos/{repo}/commits?sha={branch}&per_page=100"

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Error fetching commits")
        data = response.json()

    all_shas = [item["sha"] for item in data]

    sha_status_map = {}
    try:
        conn = get_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            'SELECT sha, status FROM "Commit_Feedback" WHERE sha = ANY(%s)',
            (all_shas,)
        )
        results = cur.fetchall()
        for row in results:
            sha_status_map[row["sha"]] = row["status"]
    except Exception as e:
        print("❌ Error fetching commit statuses:", e)
    finally:
        conn.close()

    grouped = defaultdict(list)

    for item in data:
        sha = item["sha"]
        author_login = item["author"]["login"] if item.get("author") else None
        commit_author_name = item["commit"]["author"]["name"]

        if author_login != username and commit_author_name != username:
            continue

        date_str = item["commit"]["author"]["date"]
        date = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ")
        group_key = human_date(date)

        status = sha_status_map.get(sha, "not_analyzed")

        grouped[group_key].append({
            "message": item["commit"]["message"],
            "author": author_login or commit_author_name,
            "date": relative_day(date),
            "hash": sha,
            "verified": item["commit"].get("verification", {}).get("verified", False),
            "branch": branch,
            "status": status
        })

    return dict(grouped)

async def get_pull_requests(token: str, repo: str, username: str):
    url = f"https://api.github.com/repos/{repo}/pulls?state=all&per_page=100"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Error fetching PRs")
        prs = response.json()

        pull_requests = []

        for pr in prs:
            is_author = pr["user"]["login"] == username

            #Get assigned reviewers
            reviewers_url = pr["_links"]["self"]["href"] + "/requested_reviewers"
            reviewers_resp = await client.get(reviewers_url, headers=headers)
            reviewers_data = reviewers_resp.json() if reviewers_resp.status_code == 200 else {}
            reviewers = [r["login"] for r in reviewers_data.get("users", [])]

            is_reviewer = username in reviewers

            if not (is_author or is_reviewer):
                continue

            if pr.get("merged_at"):
                status = "merged"
                date_label = f"merged {humanize_date(pr['merged_at'])}"
            elif pr.get("closed_at"):
                status = "closed"
                date_label = f"closed {humanize_date(pr['closed_at'])}"
            else:
                status = "open"
                date_label = f"open {humanize_date(pr['created_at'])}"

            comments_count = pr.get("comments", 0) + pr.get("review_comments", 0)

            pull_requests.append({
                "title": pr["title"],
                "number": pr["number"],
                "author": pr["user"]["login"],
                "date": date_label,
                "status": status,
                "retro": "Analizando", #Hardcoded
                "comments": comments_count
            })

    return pull_requests

def build_file_tree(files):
    tree = {}

    for file in files:
        path_parts = file['filename'].split('/')
        current = tree

        for i, part in enumerate(path_parts):
            if part not in current:
                current[part] = {
                    "_children": {},
                    "_file": None,
                    "_status": None
                }

            if i == len(path_parts) - 1:
                # Último nodo: es un archivo
                current[part]["_file"] = file
                current[part]["_status"] = file.get("status")
            current = current[part]["_children"]

    def to_array(node):
        result = []
        for name, data in sorted(node.items()):
            is_file = data["_file"] is not None
            entry = {
                "name": name,
                "type": "file" if is_file else "folder",
            }
            if is_file:
                entry["status"] = data["_status"]
                entry["file"] = data["_file"]
            else:
                entry["children"] = to_array(data["_children"])
            result.append(entry)
        return result

    return to_array(tree)

async def get_commit_feedback(token: str, repo: str, sha: str):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }

    url = f"https://api.github.com/repos/{repo}/commits/{sha}"

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers)
        data = res.json()

    files_data = data.get("files", [])
    file_tree = build_file_tree(files_data)
    commit_data = data.get("commit", {})
    author_data = commit_data.get("author", {})

    title = commit_data.get("message", "No message").split("\n")[0]
    date_raw = author_data.get("date", "")
    date_str = (
        datetime.fromisoformat(date_raw.replace("Z", "")).strftime("%b %d, %Y")
        if date_raw else ""
    )
    stats = data.get("stats", {})

    summary = "This commit has not been analyzed yet."
    feedback = []
    status = "not_analyzed"
    recommended_resources = []
    created_at = None
    analyzed_at = None
    quality = None

    try:
        conn = get_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            'SELECT summary, feedback, status, recommended_resources, created_at, analyzed_at, quality FROM "Commit_Feedback" WHERE sha = %s',
            (sha,)
        )
        row = cur.fetchone()
        if row:
            summary = row["summary"]
            feedback = row["feedback"] if isinstance(row["feedback"], list) else []
            recommended_resources = row.get("recommended_resources", []) if isinstance(row.get("recommended_resources"), list) else []
            status = row["status"]
            created_at = row.get("created_at")
            analyzed_at = row.get("analyzed_at")
            quality = row.get("quality")
    except Exception as e:
        print("❌ Error fetching Commit_Feedback:", e)
    finally:
        conn.close()

    return {
        "info": {
            "title": title,
            "date": date_str,
            "author": author_data.get("name", ""),
            "avatar": data.get("author", {}).get("avatar_url", ""),
            "branch": "main", #Hardcoded
            "created_at": created_at,
            "analyzed_at": analyzed_at,
            "quality": quality
        },
        "stats": {
            "files_changed": len(data.get("files", [])),
            "additions": stats.get("additions", 0),
            "deletions": stats.get("deletions", 0),
            "total": stats.get("total", 0)
        },
        "summary": summary,
        "feedback": feedback,
        "status": status,
        "recommended_resources": recommended_resources,
        "files": files_data,
        "file_tree": file_tree
    }

async def fetch_github_branches(token: str, repo: str):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }

    async with httpx.AsyncClient() as client:
        repo_url = f"https://api.github.com/repos/{repo}"
        repo_response = await client.get(repo_url, headers=headers)
        if repo_response.status_code != 200:
            raise HTTPException(status_code=repo_response.status_code, detail="Error fetching repo info")
        repo_data = repo_response.json()

        branches_url = f"https://api.github.com/repos/{repo}/branches"
        branches_response = await client.get(branches_url, headers=headers)
        if branches_response.status_code != 200:
            raise HTTPException(status_code=branches_response.status_code, detail="Error fetching branches")
        branches_data = branches_response.json()
        branches = [b["name"] for b in branches_data]

        default_branch = (
            repo_data.get("default_branch") or
            (branches[0] if branches else "main")
        )

        return {
            "branches": branches,
            "default_branch": default_branch
        }