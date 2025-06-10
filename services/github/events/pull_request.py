from datetime import datetime
import json
from dotenv import load_dotenv
import requests
from services.llm.gemini import call_llm
import re
import os

load_dotenv()
GEMINI_KEY_1 = os.getenv("GEMINI_API_KEY_1")
GEMINI_KEY_2 = os.getenv("GEMINI_API_KEY_2")
GITHUB_API = "https://api.github.com"

def generate_prompt(structured_lines: list[dict]) -> str:
    lines_formatted = "\n".join(
        f'Line {l["line"]} ({l["type"]}): {l["code"]}' for l in structured_lines
    )

    return f"""
        You are a senior software engineer reviewing changes made to a code file.

        Below are the modified lines, each with its line number, type (insert, delete, or normal), and the code content:

        {lines_formatted}

        Please analyze these lines and return constructive feedback for any line you consider relevant.

        Return ONLY a JSON array. Each item in the array must have:

        - "type": insert, delete, or normal (depending on the line analyzed)
        - "comment": a short and clear code review message
        - "lineNumber": the number of the line being commented

        ⚠️ DO NOT return the original input or code again.
        ⚠️ DO NOT include any explanation.
        ⚠️ ONLY return a clean JSON array as shown below.

        Example:

        [
        {{
            "type": "insert",
            "comment": "✅ Good use of try/except block.",
            "lineNumber": 3
        }},
        {{
            "type": "delete",
            "comment": "⚠️ This function was removed — was it intentional?",
            "lineNumber": 12
        }}
        ]
        """.strip()

def generate_summary_prompt(repo_name: str, ref_id: str, feedback_by_file: list[dict], diff_lines: int) -> str:
    joined_feedback = "\n".join(
        f"- {f['filePath']}: " + "; ".join(c['comment'] for c in f['comments'])
        for f in feedback_by_file
    )
    return f"""
        You are a senior engineer reviewing pull request `{ref_id}` in the repository `{repo_name}`.
        You will receive several file comments and a count of changed lines.

        Here are the feedback comments across all files:
        {joined_feedback}

        🧠 TASKS:
        1. Write a brief but useful **summary** of the pull request quality. Include insights, best practices, and red flags.
        2. Provide a **code quality rating from 0 to 10** (float allowed), based on clean code, structure, modularity, and naming.
        3. Suggest **at least 3 recommended resources** (videos, articles, docs) for the author to improve.

        ⚠️ FORMAT STRICTLY AS JSON:

        {{
        "summary": "Your summary here...",
        "quality": 8.5,
        "recommended_resources": [
            {{
            "link": "https://example.com",
            "title": "Clean Code Guide"
            }},
            {{
            "link": "https://example.com",
            "title": "FastAPI Best Practices"
            }}
        ]
        }}
        """.strip()

def parse_diff_to_lines(diff_text: str):
    lines = diff_text.splitlines()
    result = []

    current_old = None
    current_new = None

    for line in lines:
        header_match = re.match(r"@@ -(\d+),?\d* \+(\d+),?\d* @@", line)
        if header_match:
            current_old = int(header_match.group(1))
            current_new = int(header_match.group(2))
            continue

        if line.startswith("-"):
            result.append({
                "line": current_old,
                "type": "delete",
                "code": line[1:].strip()
            })
            current_old += 1
        elif line.startswith("+"):
            result.append({
                "line": current_new,
                "type": "insert",
                "code": line[1:].strip()
            })
            current_new += 1
        elif line.startswith(" "):
            result.append({
                "line": current_new,
                "type": "normal",
                "code": line[1:].strip()
            })
            current_old += 1
            current_new += 1
        else:
            continue  # Skips index/hash/file headers

    return result

def clean_llm_response(raw: str) -> str:
    """
    Extrae el contenido JSON de una respuesta en bloque de código markdown.
    """
    match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
    return match.group(1).strip() if match else raw.strip()

def fetch_pull_request_files(repo: str, pr_number: int, token: str) -> list[dict]:
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }
    res = requests.get(url, headers=headers)
    res.raise_for_status()
    return res.json()

def process_pull_request_event(payload: dict, conn):
    from datetime import datetime
    import json

    print("📥 Incoming payload:")
    print(json.dumps(payload, indent=2))

    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {}).get("full_name", "")
    repo_id = payload.get("repository", {}).get("id")
    pr_number = pr.get("number")
    author_username = pr.get("user", {}).get("login")

    print(f"\n📘 PR Title: {pr.get('title')}")
    print(f"🔗 PR URL: {pr.get('html_url')}")
    print(f"📂 Repo: {repo} | 🆔 Repo ID: {repo_id} | 🔢 PR Number: {pr_number} | 👤 Author: {author_username}")

    if not repo or not pr_number or not author_username:
        print("⚠️ Missing repo, PR number, or author in the payload.")
        return

    cur = None
    try:
        cur = conn.cursor()

        print("🧠 Checking for employee GitHub token...")
        cur.execute(
            'SELECT id, github_token FROM "Employee" WHERE github_username = %s',
            (author_username,)
        )
        result = cur.fetchone()

        if not result or not result[1]:
            print(f"⚠️ No GitHub token found in Employee table for user: {author_username}")
            github_token = os.getenv("GITHUB_PAT")
            employee_id = None
            if not github_token:
                print("❌ No fallback token (`GITHUB_PAT`) found. Aborting.")
                return
            print("🔑 Using fallback token from environment.")
        else:
            employee_id, github_token = result
            print(f"✅ Found token for employee ID {employee_id}")

        print("🔐 Token prefix:", github_token[:10])

        # Buscar si ya existe registro del PR
        print("🔎 Verifying if PullRequest_Feedback entry exists...")
        cur.execute(
            '''
            SELECT id FROM "PullRequest_Feedback"
            WHERE github_repo_id = %s AND pr_number = %s
            ''',
            (repo_id, pr_number)
        )
        row = cur.fetchone()
        if not row:
            print("🆕 Entry not found — inserting new blank row for processing.")
            cur.execute(
                '''
                INSERT INTO "PullRequest_Feedback" (github_repo_id, pr_number)
                VALUES (%s, %s)
                RETURNING id
                ''',
                (repo_id, pr_number)
            )
            conn.commit()
            row = cur.fetchone()
            print("✅ Inserted row ID:", row[0])

        print("📡 Fetching PR files from GitHub API...")
        print("🔗 URL:", f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files")
        try:
            files = fetch_pull_request_files(repo, pr_number, github_token)
            print(f"📄 Files retrieved: {len(files)}")
        except Exception as e:
            print(f"❌ Error fetching PR files: {e}")
            return

        feedback_result = []
        for file in files:
            file_path = file.get("filename")
            patch = file.get("patch")

            print(f"\n📁 Processing file: {file_path}")
            if not patch:
                print("⚠️ Skipping file — no patch found.")
                continue

            structured_lines = parse_diff_to_lines(patch)
            print(f"🔍 Parsed {len(structured_lines)} lines.")

            if not structured_lines:
                print("⚠️ No lines to analyze.")
                continue

            prompt = generate_prompt(structured_lines)

            try:
                print("🤖 Sending prompt to Gemini...")
                llm_response = call_llm(prompt, GEMINI_KEY_1)
                cleaned = clean_llm_response(llm_response)
                comments = json.loads(cleaned)
                print(f"✅ Got {len(comments)} comments.")
            except Exception as e:
                print(f"❌ Error analyzing {file_path}: {e}")
                print("🛑 Raw response:", repr(llm_response) if 'llm_response' in locals() else "Not available")
                comments = []

            if comments:
                feedback_result.append({
                    "filePath": file_path,
                    "comments": comments
                })

        summary = None
        quality = None
        resources = []
        analyzed_at = datetime.utcnow()

        if feedback_result:
            try:
                print("📝 Generating summary...")
                summary_prompt = generate_summary_prompt(repo, f"PR-{pr_number}", feedback_result, diff_lines=len(feedback_result))
                summary_raw = call_llm(summary_prompt, GEMINI_KEY_2)
                cleaned_summary = clean_llm_response(summary_raw)
                summary_data = json.loads(cleaned_summary)

                summary = summary_data.get("summary")
                quality = summary_data.get("quality")
                resources = summary_data.get("recommended_resources", [])
                print("✅ Summary complete.")
            except Exception as e:
                print(f"❌ Summary generation failed: {e}")
                print("🛑 Raw summary response:", repr(summary_raw) if 'summary_raw' in locals() else "N/A")

        print("💾 Updating PullRequest_Feedback entry...")
        cur.execute(
            '''
            UPDATE "PullRequest_Feedback"
            SET
                analyzed_at = %s,
                github_username = %s,
                employee_id = %s,
                feedback = %s,
                summary = %s,
                quality = %s,
                recommended_resources = %s
            WHERE github_repo_id = %s AND pr_number = %s
            ''',
            (
                analyzed_at,
                author_username,
                employee_id,
                json.dumps(feedback_result),
                summary,
                quality,
                json.dumps(resources),
                repo_id,
                pr_number
            )
        )
        conn.commit()
        print(f"✅ PR #{pr_number} processed and updated.")

    except Exception as e:
        print("❌ Fatal error:", e)
        try:
            conn.rollback()
            print("↩️ Rollback successful.")
        except Exception as rollback_error:
            print("⚠️ Rollback failed:", rollback_error)
        raise
    finally:
        if cur:
            cur.close()
            print("🔚 Cursor closed.")
