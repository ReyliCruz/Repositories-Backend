from datetime import datetime
import json

def process_push_event(payload: dict, conn):
    try:
        commits = payload.get("commits", [])
        if not commits:
            print("⚠️ No commits found in payload.")
            return

        repo = payload.get("repository", {}).get("full_name")
        cur = conn.cursor()

        for commit in commits:
            sha = commit.get("id")
            author_username = commit.get("author", {}).get("username")

            employee_id = None
            if author_username:
                cur.execute(
                    'SELECT id FROM "Employee" WHERE github_username = %s',
                    (author_username,)
                )
                result = cur.fetchone()
                if result:
                    employee_id = result["id"]

            cur.execute(
                '''
                INSERT INTO "Commit_Feedback" (sha, status, created_at, employee_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (sha) DO NOTHING
                ''',
                (sha, "analyzing", datetime.utcnow(), employee_id)
            )

            print(f"✅ Stored commit {sha} by {author_username or 'unknown'}")

        conn.commit()

    except Exception as e:
        print("❌ Error in process_push_event:", e)
        conn.rollback()
