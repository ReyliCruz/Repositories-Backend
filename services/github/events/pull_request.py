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

    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {}).get("full_name", "")
    repo_id = payload.get("repository", {}).get("id")
    pr_number = pr.get("number")
    author_username = pr.get("user", {}).get("login")

    print("📘 PR Title:", pr.get("title"))
    print("🔗 URL:", pr.get("html_url"))

    if not repo or not pr_number or not author_username:
        print("⚠️ Missing repo, PR number or author.")
        return

    cur = None
    try:
        cur = conn.cursor()

        # Buscar empleado y token
        cur.execute(
            'SELECT id, github_token FROM "Employee" WHERE github_username = %s',
            (author_username,)
        )
        result = cur.fetchone()
        if not result:
            print("⚠️ No GitHub token found for author:", author_username)
            return

        employee_id, github_token = result

        # Verificar existencia previa en PullRequest_Feedback
        cur.execute(
            '''
            SELECT 1 FROM "PullRequest_Feedback"
            WHERE github_repo_id = %s AND pr_number = %s
            ''',
            (repo_id, pr_number)
        )
        exists = cur.fetchone()
        if not exists:
            print(f"⚠️ No entry found for PR #{pr_number} in PullRequest_Feedback.")
            return  # No actualización posible

        # Obtener archivos del PR
        try:
            files = fetch_pull_request_files(repo, pr_number, github_token)
        except Exception as e:
            print(f"❌ Error fetching PR files for #{pr_number}:", e)
            return

        feedback_result = []

        for file in files:
            patch = file.get("patch")
            file_path = file.get("filename")

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
                print(f"❌ Error in file {file_path}:", e)
                print("🔍 Raw Gemini response:", repr(llm_response))
                comments = []

            if comments:
                feedback_result.append({
                    "filePath": file_path,
                    "comments": comments
                })

        # Generar resumen si hay feedback
        summary = None
        quality = None
        resources = []
        analyzed_at = datetime.utcnow()

        if feedback_result:
            try:
                summary_prompt = generate_summary_prompt(repo, f"PR-{pr_number}", feedback_result, diff_lines=len(feedback_result))
                summary_raw = call_llm(summary_prompt, GEMINI_KEY_2)
                cleaned_summary = clean_llm_response(summary_raw)
                summary_data = json.loads(cleaned_summary)

                summary = summary_data.get("summary")
                quality = summary_data.get("quality")
                resources = summary_data.get("recommended_resources", [])
            except Exception as e:
                print(f"❌ Error generating summary for PR #{pr_number}:", e)
                print("🔍 Raw summary response:", repr(summary_raw))

        # Actualizar la fila existente
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
        print(f"✅ PR #{pr_number} actualizado correctamente.")

    except Exception as e:
        print("❌ Error in process_pull_request_event:", e)
        try:
            conn.rollback()
        except Exception as rollback_error:
            print("⚠️ Rollback error:", rollback_error)
        raise
    finally:
        if cur:
            cur.close()
