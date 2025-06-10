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

def generate_summary_prompt(repo_name: str, sha: str, feedback_by_file: list[dict], diff_lines: int) -> str:
    joined_feedback = "\n".join(
        f"- {f['filePath']}: " + "; ".join(c['comment'] for c in f['comments'])
        for f in feedback_by_file
    )
    return f"""
        You are a senior engineer reviewing commit `{sha}` in the repository `{repo_name}`.
        You will receive several file comments and a count of changed lines.

        Here are the feedback comments across all files:
        {joined_feedback}

        🧠 TASKS:
        1. Write a brief but useful **summary** of the commit quality. Include insights, best practices, and red flags.
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

def fetch_commit_data(sha: str, repo: str, token: str) -> dict:
    url = f"{GITHUB_API}/repos/{repo}/commits/{sha}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }
    res = requests.get(url, headers=headers)
    res.raise_for_status()
    return res.json()

def process_push_event(payload: dict, conn):
    cur = None
    try:
        commits = payload.get("commits", [])
        repo = payload.get("repository", {}).get("full_name", "")
        if not commits:
            return

        cur = conn.cursor()

        for commit in commits:
            sha = commit.get("id")

            author_username = commit.get("author", {}).get("username")
            employee_id = None
            github_token = None

            if author_username:
                cur.execute(
                    'SELECT id, github_token FROM "Employee" WHERE github_username = %s',
                    (author_username,)
                )
                result = cur.fetchone()
                if result:
                    employee_id = result[0] if isinstance(result, tuple) else result.get("id")
                    github_token = result[1] if isinstance(result, tuple) else result.get("github_token")

            if not github_token:
                continue  # No token, no análisis

            # Verificar si ya existe
            cur.execute('SELECT 1 FROM "Commit_Feedback" WHERE sha = %s', (sha,))
            already_exists = cur.fetchone() is not None

            if not already_exists:
                # Insertar registro inicial
                repo_id = payload.get("repository", {}).get("id")

                cur.execute(
                    '''
                    INSERT INTO "Commit_Feedback" (sha, status, created_at, employee_id, github_username, github_repo_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ''',
                    (sha, "analyzing", datetime.utcnow(), employee_id, author_username, repo_id)    
                )


            # Obtener datos del commit
            try:
                commit_data = fetch_commit_data(sha, repo, github_token)
            except Exception as e:
                print(f"❌ Error fetching commit data for {sha}:", e)
                continue

            feedback_result = []

            for file in commit_data.get("files", []):
                file_path = file.get("filename")
                patch = file.get("patch")

                if not patch:
                    continue

                structured_lines = parse_diff_to_lines(patch)
                if not structured_lines:
                    continue

                prompt = generate_prompt(structured_lines)

                try:
                    llm_response = call_llm(prompt, GEMINI_KEY_1)
                    cleaned = clean_llm_response(llm_response)
                    comments = json.loads(cleaned)
                except Exception as e:
                    print(f"❌ Error generating or parsing feedback for {file_path}:", e)
                    print("🔍 Raw response from Gemini:", repr(llm_response))  # <-- agrega esta línea
                    comments = []

                if comments:
                    feedback_result.append({
                        "filePath": file_path,
                        "comments": comments
                    })

            # Actualizar con feedback final
            cur.execute(
                '''
                UPDATE "Commit_Feedback"
                SET
                    status = %s,
                    feedback = %s,
                    analyzed_at = %s
                WHERE sha = %s
                ''',
                (
                    "analyzed" if feedback_result else "not_analyzed",
                    json.dumps(feedback_result),
                    datetime.utcnow(),
                    sha
                )
            )

            if feedback_result:
                summary_prompt = generate_summary_prompt(repo, sha, feedback_result, diff_lines=len(feedback_result))

                try:
                    summary_raw = call_llm(summary_prompt, GEMINI_KEY_2)
                    cleaned_summary = clean_llm_response(summary_raw)
                    summary_data = json.loads(cleaned_summary)

                    cur.execute(
                        '''
                        UPDATE "Commit_Feedback"
                        SET
                            summary = %s,
                            quality = %s,
                            recommended_resources = %s
                        WHERE sha = %s
                        ''',
                        (
                            summary_data.get("summary"),
                            summary_data.get("quality"),
                            json.dumps(summary_data.get("recommended_resources", [])),
                            sha
                        )
                    )
                except Exception as e:
                    print(f"❌ Error generating/parsing summary for commit {sha}:", e)
                    print("🔍 Raw summary response:", repr(summary_raw))
                    
        conn.commit()

    except Exception as e:
        print("❌ Error in process_push_event:", e)
        try:
            conn.rollback()
        except Exception as rollback_error:
            print("⚠️ Failed to rollback transaction:", rollback_error)
        raise
    finally:
        if cur:
            cur.close()