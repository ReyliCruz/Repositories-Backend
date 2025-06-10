import traceback
import psycopg2
import psycopg2.extras
import requests
import httpx
import json
from database import get_connection
from fastapi import HTTPException
from collections import defaultdict
from datetime import datetime
from services.github.events.pull_request import process_pull_request_event
from services.github.events.push import process_push_event

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

        repo_id = prs[0]["base"]["repo"]["id"] if prs else None
        pr_numbers = [pr["number"] for pr in prs]
        retro_map = {}

        if repo_id is not None:
            try:
                conn = get_connection()
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute(
                    '''
                    SELECT pr_number, retro
                    FROM "PullRequest_Feedback"
                    WHERE github_repo_id = %s AND pr_number = ANY(%s)
                    ''',
                    (repo_id, pr_numbers)
                )
                results = cur.fetchall()
                for row in results:
                    retro_map[row["pr_number"]] = row["retro"]
            except Exception as e:
                print("❌ Error fetching PR retro info:", e)
            finally:
                conn.close()

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
                "retro": retro_map.get(pr["number"], "not_analyzed"),
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

async def get_pull_request_feedback(token: str, repo: str, pr_number: int):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }

    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers)
        data = res.json()

    title = data.get("title", "No title")
    date_raw = data.get("created_at", "")
    date_str = (
        datetime.fromisoformat(date_raw.replace("Z", "")).strftime("%b %d, %Y")
        if date_raw else ""
    )
    user = data.get("user", {})
    author_name = user.get("login", "")
    avatar_url = user.get("avatar_url", "")

    if data.get("merged_at"):
        pr_status = "merged"
    elif data.get("closed_at"):
        pr_status = "closed"
    else:
        pr_status = "open"

    source_branch = data.get("head", {}).get("ref", "unknown")
    target_branch = data.get("base", {}).get("ref", "unknown")
    github_repo_id = data.get("base", {}).get("repo", {}).get("id")

    files_url = data.get("url")
    if not files_url or not files_url.startswith("http"):
        files_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files"
    else:
        files_url = files_url + "/files"

    stats = {
        "files_changed": 0,
        "additions": 0,
        "deletions": 0,
        "total": 0
    }
    files_data = []
    async with httpx.AsyncClient() as client:
        files_res = await client.get(files_url, headers=headers)
        try:
            files_data = files_res.json()
        except Exception as e:
            print("❌ Error decoding GitHub PR files response:", e)
            files_data = []

    if isinstance(files_data, list):
        for f in files_data:
            if isinstance(f, dict):
                stats["files_changed"] += 1
                stats["additions"] += f.get("additions", 0)
                stats["deletions"] += f.get("deletions", 0)
        stats["total"] = stats["additions"] + stats["deletions"]
    else:
        print("⚠️ Warning: files_data is not a list.")
        files_data = []

    file_tree = build_file_tree(files_data)

    summary = "This pull request has not been analyzed yet."
    feedback = []
    retro = "not_analyzed"
    recommended_resources = []
    created_at = None
    analyzed_at = None
    quality = None

    try:
        conn = get_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute(
            '''
            SELECT summary, feedback, retro, recommended_resources,
                   created_at, analyzed_at, quality
            FROM "PullRequest_Feedback"
            WHERE github_repo_id = %s AND pr_number = %s
            ''',
            (github_repo_id, pr_number)
        )
        row = cur.fetchone()

        if row:
            summary = row["summary"]
            feedback = row["feedback"] if isinstance(row["feedback"], list) else []
            recommended_resources = row.get("recommended_resources", []) if isinstance(row.get("recommended_resources"), list) else []
            retro = row["retro"]
            created_at = row.get("created_at")
            analyzed_at = row.get("analyzed_at")
            quality = row.get("quality")
    except Exception as e:
        print("❌ Error fetching PullRequest_Feedback:", e)
    finally:
        conn.close()

    return {
        "info": {
            "title": title,
            "date": date_str,
            "author": author_name,
            "avatar": avatar_url,
            "branch_from": source_branch,
            "branch_to": target_branch,
            "created_at": created_at,
            "analyzed_at": analyzed_at,
            "quality": quality
        },
        "stats": stats,
        "summary": summary,
        "feedback": feedback,
        "status": pr_status,
        "retro": retro,
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

async def process_github_event(event_type: str, payload: dict):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # 1. Save raw event
        cur.execute(
            '''
            INSERT INTO "Github_Event" (event_type, payload, status, created_at)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            ''',
            (event_type, json.dumps(payload), "pending", datetime.utcnow())
        )

        result = cur.fetchone()
        if result is None:
            conn.rollback()
            raise Exception("No se pudo obtener el ID del evento insertado.")

        event_id = result["id"]
        conn.commit()

        # 2. Handle event
        if event_type == "push":
            process_push_event(payload, conn)
        elif event_type == "pull_request":
            process_pull_request_event(payload, conn)

        # 3. Mark event as done
        cur.execute(
            '''UPDATE "Github_Event" SET status = %s, processed_at = %s WHERE id = %s''',
            ("done", datetime.utcnow(), event_id)
        )
        conn.commit()

    except Exception as e:
        print("❌ Error processing GitHub event:", str(e))
        traceback.print_exc()
        raise

    finally:
        if conn:
            conn.close()

from collections import defaultdict, Counter
from datetime import datetime
from fastapi import HTTPException
from psycopg2.extras import RealDictCursor
import httpx

async def get_repo_dashboard(repo_full_name: str, token: str, username: str):
    try:
        conn = get_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1. Buscar el ID del repositorio
        cur.execute('SELECT github_repo_id FROM "Repositories" WHERE repo_full_name = %s', (repo_full_name,))
        repo = cur.fetchone()
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not found")
        github_id = repo["github_repo_id"]

        # 2. PRs y Commits del usuario
        cur.execute('''
            SELECT * FROM "PullRequest_Feedback"
            WHERE github_repo_id = %s AND github_username = %s
        ''', (github_id, username))
        pr_feedback = cur.fetchall()

        cur.execute('''
            SELECT * FROM "Commit_Feedback"
            WHERE github_repo_id = %s AND github_username = %s
        ''', (github_id, username))
        commit_feedback = cur.fetchall()

        # 3. Inicialización de KPIs y estructuras
        kpis = {
            "total_prs": 0,
            "analyzed_prs": len(pr_feedback),
            "total_commits": 0,
            "analyzed_commits": len(commit_feedback),
            "avg_quality_prs": 0,
            "avg_quality_commits": 0,
            "total_recommendations": 0,
            "total_resources": 0,
            "reviewers_count": 0,
            "total_lines_added": 0,
            "total_lines_deleted": 0,
            "avg_merge_time_days": 0
        }

        prs_quality_sum = 0
        commits_quality_sum = 0
        prs_timeline = defaultdict(list)
        commits_timeline = defaultdict(list)
        recent_prs = []
        recent_commits = []
        recommendations = set()
        resources = set()
        modified_files_counter = Counter()
        feedback_distribution = Counter()
        total_merge_days = 0
        merge_count = 0

        async with httpx.AsyncClient() as client:
            for pr in pr_feedback:
                try:
                    pr_number = pr.get("pr_number")
                    if not pr_number:
                        continue

                    res = await client.get(
                        f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}",
                        headers={"Authorization": f"Bearer {token}"}
                    )
                    if res.status_code != 200:
                        continue

                    gh = res.json()
                    kpis["total_prs"] += 1

                    quality = pr.get("quality")
                    retro = pr.get("retro") or "not_analyzed"
                    feedback_distribution[retro] += 1

                    created_str = gh.get("created_at")
                    created = datetime.strptime(created_str, "%Y-%m-%dT%H:%M:%SZ").date() if created_str else None

                    if quality and created:
                        prs_quality_sum += quality
                        prs_timeline[created].append(quality)

                    for f in pr.get("feedback") or []:
                        text = f.get("text", "").strip()
                        if text:
                            recommendations.add(text)

                    for r in pr.get("recommended_resources") or []:
                        title = r.get("title", "").strip()
                        link = r.get("link", "").strip()
                        if title and link:
                            resources.add((title, link))

                    merged_str = gh.get("merged_at")
                    if created and merged_str:
                        merged = datetime.strptime(merged_str, "%Y-%m-%dT%H:%M:%SZ")
                        merge_days = (merged.date() - created).days
                        total_merge_days += merge_days
                        merge_count += 1

                    recent_prs.append({
                        "title": gh.get("title", "Untitled PR"),
                        "file": pr.get("main_file", "unknown.js"),
                        "retro": retro,
                        "comments": gh.get("comments", 0) + gh.get("review_comments", 0),
                        "created_at": created_str,
                        "merged_at": merged_str,
                        "state": gh.get("state", "unknown")
                    })
                except Exception as e:
                    print(f"⚠️ Error processing PR #{pr.get('pr_number')}: {e}")

            for commit in commit_feedback:
                try:
                    kpis["total_commits"] += 1
                    quality = commit.get("quality")
                    if quality:
                        commits_quality_sum += quality

                    created = commit.get("created_at")
                    if isinstance(created, datetime):
                        commits_timeline[created.date()].append(quality)

                    files = commit.get("files") or []
                    for f in files:
                        filename = f.get("filename")
                        if filename:
                            modified_files_counter[filename] += 1
                        kpis["total_lines_added"] += f.get("additions", 0)
                        kpis["total_lines_deleted"] += f.get("deletions", 0)

                    recent_commits.append({
                        "sha": commit.get("sha"),
                        "message": commit.get("summary", "No message"),
                        "status": commit.get("status", "not_analyzed"),
                        "created_at": created.isoformat() if isinstance(created, datetime) else "unknown"
                    })
                except Exception as e:
                    print(f"⚠️ Error processing commit {commit.get('sha')}: {e}")

        # 4. KPIs Finales
        if pr_feedback:
            kpis["avg_quality_prs"] = round(prs_quality_sum / len(pr_feedback), 2)
        if commit_feedback:
            kpis["avg_quality_commits"] = round(commits_quality_sum / len(commit_feedback), 2)
        if merge_count:
            kpis["avg_merge_time_days"] = round(total_merge_days / merge_count, 2)

        kpis["total_recommendations"] = len(recommendations)
        kpis["total_resources"] = len(resources)

        # 5. Formato timeline
        def format_timeline(source):
            days = sorted(source.keys())[-30:]
            return {
                "days": [day.isoformat() for day in days],
                "quality": [round(sum(values)/len(values), 2) if values else 0 for values in [source[d] for d in days]],
                "count": [len(source[d]) for d in days]
            }

        timeline = {
            "prs": format_timeline(prs_timeline),
            "commits": format_timeline(commits_timeline)
        }

        top_files = [{"file": file, "count": count} for file, count in modified_files_counter.most_common(5)]

        user = {
            "username": username,
            "avatar_url": f"https://github.com/{username}.png",
            "email": f"{username}@example.com"
        }

        return {
            "user": user,
            "kpis": kpis,
            "timeline": timeline,
            "feedback_distribution": dict(feedback_distribution),
            "top_modified_files": top_files,
            "recommendations": list(recommendations)[:10],
            "resources": [{"title": r[0], "link": r[1]} for r in resources],
            "recent": {
                "prs": recent_prs[:5],
                "commits": recent_commits[:5]
            }
        }

    except Exception as e:
        print("❌ Error in get_repo_dashboard:", e)
        raise HTTPException(status_code=500, detail="Internal server error in dashboard")

    finally:
        if conn:
            conn.close()
