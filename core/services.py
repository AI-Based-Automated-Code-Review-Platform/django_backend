import requests
from django.conf import settings
import secrets
import urllib.parse
import aiohttp
import hmac
import hashlib

GITHUB_OAUTH_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_USER_URL = "https://api.github.com/user"
GITHUB_API_BASE_URL = "https://api.github.com"

# GitHub OAuth scopes needed (mirroring your FastAPI setup)
GITHUB_SCOPES = [
    "read:user",
    "user:email",
    "repo",
    "repo:status",
    "repo_deployment",
    "public_repo",
    "read:org",
    "repo:invite",
    "security_events"
]

def generate_oauth_state(request):
    """Generate a random state string and store it in the session."""
    state = secrets.token_urlsafe(32)
    request.session['oauth_state'] = state
    return state

def validate_oauth_state(request, state_from_callback):
    """Validate the state from callback against the one stored in session."""
    state_in_session = request.session.pop('oauth_state', None)
    return state_in_session is not None and state_in_session == state_from_callback

def get_github_oauth_redirect_url(state):
    """Constructs the GitHub OAuth redirect URL."""
    params = {
        "client_id": settings.GITHUB_CLIENT_ID,
        "redirect_uri": settings.GITHUB_CALLBACK_URL,
        "scope": " ".join(GITHUB_SCOPES),
        "state": state,
    }
    return f"{GITHUB_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

def exchange_code_for_github_token(code):
    """Exchanges the authorization code for a GitHub access token."""
    payload = {
        "client_id": settings.GITHUB_CLIENT_ID,
        "client_secret": settings.GITHUB_CLIENT_SECRET,
        "code": code,
    }
    headers = {"Accept": "application/json"}
    response = requests.post(GITHUB_OAUTH_TOKEN_URL, data=payload, headers=headers)
    response.raise_for_status()  # Raise an exception for bad status codes
    return response.json().get("access_token")

def get_github_user_info(github_token):
    """Fetches user information from GitHub API using the access token."""
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    response = requests.get(GITHUB_API_USER_URL, headers=headers)
    response.raise_for_status()
    
    user_data = response.json()
    
    # Attempt to get primary email if available
    email_data = requests.get(f"{GITHUB_API_USER_URL}/emails", headers=headers)
    if email_data.status_code == 200:
        for email_entry in email_data.json():
            if email_entry.get('primary') and email_entry.get('verified'):
                user_data['email'] = email_entry['email']
                break
        if 'email' not in user_data and email_data.json(): # Fallback to first email if no primary
             user_data['email'] = email_data.json()[0]['email']

    return user_data

def get_user_repos_from_github(github_token, page=1, per_page=30):
    """Fetches user\'s repositories from GitHub API."""
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    params = {"per_page": per_page, "page": page, "sort": "updated", "direction": "desc"}
    response = requests.get(f"{GITHUB_API_USER_URL}/repos", headers=headers, params=params)
    response.raise_for_status()
    return response.json()

def get_user_orgs_from_github(github_token, page=1, per_page=30):
    """Fetches user\'s organizations from GitHub API."""
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    params = {"per_page": per_page, "page": page}
    response = requests.get(f"{GITHUB_API_USER_URL}/orgs", headers=headers, params=params)
    response.raise_for_status()
    return response.json()

def get_all_repo_collaborators_from_github(owner_login: str, repo_name: str, github_token: str) -> list:
    """Fetch all repository collaborators from GitHub, handling pagination."""
    collaborators = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{owner_login}/{repo_name}/collaborators?page={page}&per_page=100" # Max per_page is 100
        headers = {
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()  # Raise an exception for HTTP errors
        current_page_collaborators = response.json()
        if not current_page_collaborators:
            break
        collaborators.extend(current_page_collaborators)
        if len(current_page_collaborators) < 100: # Break if last page was not full
            break
        page += 1
    return collaborators

def get_repo_collaborators_from_github(github_token, owner_login, repo_name, page=1, per_page=30):
    """Fetches repository collaborators from the GitHub API."""
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    params = {"per_page": per_page, "page": page}
    url = f"https://api.github.com/repos/{owner_login}/{repo_name}/collaborators"
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json()

def get_repository_commits_from_github(github_token: str, owner_login: str, repo_name: str, per_page: int = 30, page: int = 1):
    """
    Fetches commits for a specific repository from the GitHub API.
    """
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    params = {"per_page": per_page, "page": page}
    
    url = f"https://api.github.com/repos/{owner_login}/{repo_name}/commits"
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()  # Raise an exception for bad status codes
    return response.json()

def get_repository_pull_requests_from_github(github_token: str, owner_login: str, repo_name: str, state: str = "all", sort: str = "created", direction: str = "desc", per_page: int = 30, page: int = 1):
    """
    Fetches pull requests for a specific repository from the GitHub API.
    'state' can be 'open', 'closed', or 'all'.
    'sort' can be 'created', 'updated', 'popularity', 'long-running'.
    'direction' can be 'asc' or 'desc'.
    """
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    params = {
        "state": state,
        "sort": sort,
        "direction": direction,
        "per_page": per_page,
        "page": page,
    }
    url = f"https://api.github.com/repos/{owner_login}/{repo_name}/pulls"
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()  # Raise an exception for bad status codes
    return response.json()

def get_single_pull_request_from_github(github_token: str, owner_login: str, repo_name: str, pr_number: int):
    """
    Fetches a single pull request by its number for a specific repository from the GitHub API.
    """
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    url = f"{GITHUB_API_BASE_URL}/repos/{owner_login}/{repo_name}/pulls/{pr_number}"
    response = requests.get(url, headers=headers)
    response.raise_for_status()  # Raise an exception for bad status codes (404 if not found)
    return response.json()

def get_single_commit_from_github(github_token: str, owner_login: str, repo_name: str, commit_sha: str):
    """
    Fetches a single commit by its SHA for a specific repository from the GitHub API.
    """
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json", # Or "application/vnd.github.sha" for just the SHA
    }
    url = f"{GITHUB_API_BASE_URL}/repos/{owner_login}/{repo_name}/commits/{commit_sha}"
    response = requests.get(url, headers=headers)
    response.raise_for_status()  # Raise an exception for bad status codes (404 if not found, 422 for invalid SHA)
    return response.json()

# LangGraph service wrapper
class LangGraphService:
    """Wrapper for LangGraph API calls."""
    def __init__(self):
        self.base_url = settings.LANGGRAPH_API_URL.rstrip('/')

    def initialize_review(self, user_github_id, repo_name, pr_number, standards=None, metrics=None, llm_model=None):
        """Initialize a review thread in LangGraph."""
        # TODO: Implement actual API call to LangGraph service
        raise NotImplementedError("initialize_review method not implemented")

    def get_thread_state(self, thread_id):
        """Get the current state of a LangGraph thread."""
        # TODO: Implement actual API call to LangGraph service
        raise NotImplementedError("get_thread_state method not implemented")

    def get_review_feedback(self, thread_id, user_feedback, original_review, reviewer_id, user, repo, pr_id):
        """Submit user feedback to LangGraph and get AI response."""
        # TODO: Implement actual API call to LangGraph service
        raise NotImplementedError("get_review_feedback method not implemented")

class GitHubService:
    """Service class for interacting with the GitHub API."""
    
    def __init__(self, user_token=None):
        """
        Initialize the GitHub service with optional user token.
        
        Args:
            user_token (str, optional): GitHub access token. If None, use app-level authentication.
        """
        self.token = user_token
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
        }
        
        if self.token:
            self.headers["Authorization"] = f"token {self.token}"
            
    async def get_user_info(self):
        """Get authenticated user information."""
        if not self.token:
            raise ValueError("Authentication token required for this operation")
            
        async with aiohttp.ClientSession() as session:
            async with session.get(GITHUB_API_USER_URL, headers=self.headers) as response:
                response.raise_for_status()
                return await response.json()
    
    async def get_repositories(self, page=1, per_page=30):
        """Get user repositories."""
        if not self.token:
            raise ValueError("Authentication token required for this operation")
            
        params = {"per_page": per_page, "page": page, "sort": "updated", "direction": "desc"}
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{GITHUB_API_USER_URL}/repos", headers=self.headers, params=params) as response:
                response.raise_for_status()
                return await response.json()
    
    async def get_pull_request(self, owner_login, repo_name, pr_number):
        """Get specific pull request details."""
        url = f"{GITHUB_API_BASE_URL}/repos/{owner_login}/{repo_name}/pulls/{pr_number}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                response.raise_for_status()
                return await response.json()
    
    async def get_commit(self, owner_login, repo_name, commit_sha):
        """Get specific commit details."""
        url = f"{GITHUB_API_BASE_URL}/repos/{owner_login}/{repo_name}/commits/{commit_sha}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                response.raise_for_status()
                return await response.json()
    
    async def post_pr_comment(self, owner_login, repo_name, pr_number, body):
        """Post a comment on a pull request."""
        url = f"{GITHUB_API_BASE_URL}/repos/{owner_login}/{repo_name}/issues/{pr_number}/comments"
        payload = {"body": body}
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=payload) as response:
                response.raise_for_status()
                return await response.json()
    
    async def post_commit_comment(self, owner_login, repo_name, commit_sha, body):
        """Post a comment on a commit."""
        url = f"{GITHUB_API_BASE_URL}/repos/{owner_login}/{repo_name}/commits/{commit_sha}/comments"
        payload = {"body": body}
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=payload) as response:
                response.raise_for_status()
                return await response.json()
    
    async def verify_webhook_signature(self, payload, signature, secret):
        """Verify the webhook signature from GitHub."""
        expected_signature = hmac.new(
            secret.encode('utf-8'),
            payload,
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(f"sha256={expected_signature}", signature)
