from fastapi import APIRouter, Request, Depends, Query
from starlette.responses import JSONResponse
import traceback
from utils.auth import get_user_id_from_jwt
from services.github.github_service import (
    get_pull_request_feedback,
    get_repo_dashboard,
    get_user_github_credentials,
    fetch_github_repos,
    get_grouped_commits,
    get_pull_requests,
    get_commit_feedback,
    fetch_github_branches,
    process_github_event
)

router = APIRouter()

@router.get("/github/repos")
def get_repos(user_id: int = Depends(get_user_id_from_jwt)):
    token, _ = get_user_github_credentials(user_id)
    return fetch_github_repos(token)

@router.get("/github/commits")
async def commits(
    repo: str = Query(..., description="Formato: owner/repo"),
    branch: str = Query("main"),
    user_id: int = Depends(get_user_id_from_jwt)
):
    token, username = get_user_github_credentials(user_id)
    return await get_grouped_commits(token, repo, branch, username)

@router.get("/github/pull-requests")
async def pull_requests(
    repo: str = Query(..., description="Formato: owner/repo"),
    user_id: int = Depends(get_user_id_from_jwt)
):
    token, username = get_user_github_credentials(user_id)
    return await get_pull_requests(token, repo, username)

@router.get("/github/commit-feedback")
async def commit_feedback(
    repo: str = Query(..., description="Formato: owner/repo"),
    sha: str = Query(...),
    user_id: int = Depends(get_user_id_from_jwt)
):
    token, _ = get_user_github_credentials(user_id)
    return await get_commit_feedback(token, repo, sha)

@router.get("/github/pull-request-feedback")
async def pull_request_feedback(
    repo: str = Query(..., description="Formato: owner/repo"),
    pr_number: int = Query(..., description="Número del Pull Request"),
    user_id: int = Depends(get_user_id_from_jwt)
):
    token, _ = get_user_github_credentials(user_id)
    return await get_pull_request_feedback(token, repo, pr_number)

@router.get("/github/branches")
async def get_branches(
    repo: str = Query(..., description="Formato: owner/repo"),
    user_id: int = Depends(get_user_id_from_jwt)
):
    token, _ = get_user_github_credentials(user_id)
    return await fetch_github_branches(token, repo)

@router.post("/github/webhook")
async def github_webhook(request: Request):
    try:
        payload = await request.json()
        event_type = request.headers.get("X-GitHub-Event", "unknown")
        await process_github_event(event_type, payload)
        return JSONResponse(status_code=200, content={"message": "✅ Event received."})
    
    except Exception as e:
        print("❌ Error in webhook endpoint:", str(e))
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"message": "❌ Failed to process webhook."})

@router.get("/github/repo-dashboard")
async def repo_dashboard(
    repo_full_name: str = Query(..., alias="repo"),
    user_id: int = Depends(get_user_id_from_jwt)
):
    token, username = get_user_github_credentials(user_id)
    return await get_repo_dashboard(repo_full_name, token, username)
