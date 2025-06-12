from rest_framework.permissions import BasePermission

from .models import (
    RepoCollaborator,
    Thread as ThreadModel,
)

from .services import (
    get_repo_collaborators_from_github,
    get_single_pull_request_from_github,
)
import logging
# Create a logger instance
logger = logging.getLogger(__name__)

class IsRepositoryOwner(BasePermission):
    def has_object_permission(self, request, view, obj):
        return obj.owner == request.user

class IsAssignedReviewerForThread(BasePermission):
    message = "You are not an assigned reviewer for this review thread."

    def has_object_permission(self, request, view, obj):
        # Only apply this permission to ThreadModel instances
        if not isinstance(obj, ThreadModel):
            return False

        # Get the pull request linked to this thread
        pr = obj.review.pull_request
        if not pr:
            return False

        # Ensure the user has a GitHub token
        token = getattr(request.user, "github_access_token", None)
        if not token:
            return False

        try:
            # Derive owner login and repository name
            owner_login = pr.repository.owner.username
            repo_name = pr.repository.repo_name.split("/", 1)[1]

            # Fetch the PR from GitHub to inspect requested reviewers
            gh_pr_data = get_single_pull_request_from_github(
                github_token=token,
                owner_login=owner_login,
                repo_name=repo_name,
                pr_number=pr.pr_number
            )

            # Check if the current user is in the requested reviewers list
            reviewers = gh_pr_data.get("requested_reviewers", [])
            for r in reviewers:
                if (str(r.get("id")) == str(request.user.github_id)
                        or r.get("login") == request.user.username):
                    return True
        except Exception:
            logger.warning(
                f"Could not verify assigned reviewers for user {request.user.id} on PR #{pr.pr_number}"
            )

        return False

class CanAccessRepository(BasePermission):
    """
    Grants access if:
     1) user is owner
     2) user is in RepoCollaborator
     3) user actually shows up in GitHub's collaborator list (auto-sync)
    """
    def has_object_permission(self, request, view, obj):
        if obj.owner == request.user:
            return True

        # 1) already in our DB?
        if RepoCollaborator.objects.filter(repository=obj, user=request.user).exists():
            return True

        # 2) fallback to GitHub API
        token = getattr(request.user, "github_access_token", None)
        if not token:
            return False

        owner_login = obj.owner.username
        repo_name = obj.repo_name.split("/", 1)[1]
        try:
            # Fetch all pages of collaborators until we find the user
            page = 1
            per_page = 100  # Max allowed by GitHub API
            while True:
                gh_collabs = get_repo_collaborators_from_github(
                    owner_login=owner_login,
                    repo_name=repo_name,
                    github_token=token,
                    page=page,
                    per_page=per_page
                )
                
                # Check if user is in this batch
                for c in gh_collabs:
                    if str(c["id"]) == str(request.user.github_id):
                        # sync them in and allow
                        RepoCollaborator.objects.update_or_create(
                            repository=obj,
                            user=request.user,
                            defaults={"role": c.get("permissions", {}).get("push") and "member" or "read"}
                        )
                        return True
                
                # If we got fewer resnversation_history,ults than requested, we've reached the end
                if len(gh_collabs) < per_page:
                    break
                    
                page += 1
        except Exception:
            logger.warning(f"Could not verify collaborator via GitHub for {request.user}")

        return False