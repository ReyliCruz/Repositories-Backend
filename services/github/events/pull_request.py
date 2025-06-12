from datetime import datetime
import json
import traceback
import requests
import re
from services.llm.gemini import call_llm
from dotenv import load_dotenv
import os

load_dotenv()
GEMINI_KEY_1 = os.getenv("GEMINI_API_KEY_1")
GEMINI_KEY_2 = os.getenv("GEMINI_API_KEY_2")
GITHUB_API = "https://api.github.com"

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
            result.append({"line": current_old, "type": "delete", "code": line[1:].strip()})
            current_old += 1
        elif line.startswith("+"):
            result.append({"line": current_new, "type": "insert", "code": line[1:].strip()})
            current_new += 1
        elif line.startswith(" "):
            result.append({"line": current_new, "type": "normal", "code": line[1:].strip()})
            current_old += 1
            current_new += 1

    return result

def clean_llm_response(raw: str) -> str:
    match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
    return match.group(1).strip() if match else raw.strip()

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
    cur = None
    try:
        print("📥 Procesando evento de Pull Request...")

        action = payload.get("action")
        if action not in ("opened", "synchronize", "reopened"):
            print(f"🔄 Acción '{action}' ignorada.")
            return

        pull_request = payload.get("pull_request", {})
        repo = payload.get("repository", {})
        pr_number = pull_request.get("number")
        repo_id = repo.get("id")
        repo_full_name = repo.get("full_name")
        author_username = pull_request.get("user", {}).get("login")

        cur = conn.cursor()
        cur.execute(
            'SELECT id, github_token FROM "Employee" WHERE github_username = %s',
            (author_username,)
        )
        result = cur.fetchone()
        print("🔍 Resultado crudo de empleado:", result)


        if not result:
            print("❌ Empleado no encontrado.")
            return

        employee_id = result["id"]
        github_token = result["github_token"]

        cur.execute(
            '''
            SELECT 1 FROM "PullRequest_Feedback"
            WHERE github_repo_id = %s AND pr_number = %s
            ''',
            (repo_id, pr_number)
        )
        exists = cur.fetchone() is not None

        if exists:
            cur.execute(
                '''
                UPDATE "PullRequest_Feedback"
                SET retro = %s,
                    created_at = %s,
                    employee_id = %s,
                    github_username = %s
                WHERE github_repo_id = %s AND pr_number = %s
                ''',
                (
                    "analyzing",
                    datetime.utcnow(),
                    employee_id,
                    author_username,
                    repo_id,
                    pr_number
                )
            )
        else:
            cur.execute(
                '''
                INSERT INTO "PullRequest_Feedback"
                (github_repo_id, pr_number, retro, created_at, employee_id, github_username)
                VALUES (%s, %s, %s, %s, %s, %s)
                ''',
                (
                    repo_id,
                    pr_number,
                    "analyzing",
                    datetime.utcnow(),
                    employee_id,
                    author_username
                )
            )
        conn.commit()

        feedback_result = []
        try:
            pr_files = fetch_pull_request_files(repo_full_name, pr_number, github_token)
        except Exception as e:
            print("❌ Error obteniendo archivos del PR:", e)
            return

        for file in pr_files:
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
                print(f"❌ Error en feedback para {file_path}:", e)
                print("🔍 Respuesta cruda:", repr(llm_response))
                comments = []

            if comments:
                feedback_result.append({
                    "filePath": file_path,
                    "comments": comments
                })

        cur.execute(
            '''
            UPDATE "PullRequest_Feedback"
            SET retro = %s,
                feedback = %s,
                analyzed_at = %s
            WHERE github_repo_id = %s AND pr_number = %s
            ''',
            (
                "analyzed" if feedback_result else "not_analyzed",
                json.dumps(feedback_result),
                datetime.utcnow(),
                repo_id,
                pr_number
            )
        )

        if feedback_result:
            summary_prompt = generate_summary_prompt(repo_full_name, f"PR-{pr_number}", feedback_result, len(feedback_result))
            try:
                summary_raw = call_llm(summary_prompt, GEMINI_KEY_2)
                cleaned_summary = clean_llm_response(summary_raw)
                summary_data = json.loads(cleaned_summary)

                cur.execute(
                    '''
                    UPDATE "PullRequest_Feedback"
                    SET summary = %s,
                        quality = %s,
                        recommended_resources = %s
                    WHERE github_repo_id = %s AND pr_number = %s
                    ''',
                    (
                        summary_data.get("summary"),
                        summary_data.get("quality"),
                        json.dumps(summary_data.get("recommended_resources", [])),
                        repo_id,
                        pr_number
                    )
                )
            except Exception as e:
                print("❌ Error en resumen del PR:", e)
                print("🔍 Resumen crudo:", repr(summary_raw))

        conn.commit()

    except Exception as e:
        print("❌ Error general en análisis de PR:")
        traceback.print_exc()
        try:
            conn.rollback()
        except Exception as rollback_error:
            print("⚠️ Error al hacer rollback:", rollback_error)
    finally:
        if cur:
            cur.close()
