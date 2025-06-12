from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponseRedirect, HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated, BasePermission, IsAdminUser
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.decorators import action

from .tasks.review_tasks import calculate_cost, process_commit_review, process_pr_review, process_webhook_event
from .models import (
    User,
    Repository as DBRepository,
    RepoCollaborator,
    PullRequest as PRModel,
    Commit as CommitModel,
    Review as ReviewModel,
    Thread as ThreadModel,
    Comment as CommentModel,
    LLMUsage as LLMUsageModel,
    ReviewFeedback,
    WebhookEventLog
)
from .serializers import (
    UserSerializer, RepositorySerializer, RepoCollaboratorSerializer, 
    GitHubRepositorySerializer, GitHubOrganizationSerializer, GitHubCollaboratorSerializer,
    PRSerializer, CommitSerializer, ReviewSerializer, LLMUsageSerializer,
    AdminUserUpdateSerializer, ThreadSerializer, ReviewFeedbackSerializer, CommentSerializer
)
from .services import (
    generate_oauth_state,
    validate_oauth_state,
    get_github_oauth_redirect_url,
    exchange_code_for_github_token,
    get_github_user_info,
    get_user_repos_from_github,
    get_user_orgs_from_github,
    get_repo_collaborators_from_github,
    get_repository_commits_from_github,
    get_repository_pull_requests_from_github,
    get_single_commit_from_github,
    get_single_pull_request_from_github,
    LangGraphService
)
import hashlib
import os
import hmac
import requests
import urllib.parse
from django.utils.decorators import method_decorator
from django.conf import settings
from urllib.parse import urlencode
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponseForbidden
from django.db.models import Q, Sum, Count
from django.shortcuts import get_object_or_404
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.urls import reverse
import logging
from django.views.decorators.http import require_POST
import json
from core.langgraph_client.client import LangGraphClient
from asgiref.sync import async_to_sync
import asyncio
# Create a logger instance
logger = logging.getLogger(__name__)
# Instantiate LangGraph service
langgraph_service = LangGraphService()

class GitHubLoginView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        # TODO: Implement rate limiting for request.META.get('REMOTE_ADDR')
        state = generate_oauth_state(request)
        redirect_url = get_github_oauth_redirect_url(state)
        return HttpResponseRedirect(redirect_url)

class GitHubLoginRedirectView(APIView):
    permission_classes = [AllowAny]

    async def get(self, request, *args, **kwargs):
        state = generate_oauth_state()  # Assuming this service function exists
        request.session['github_oauth_state'] = state
        # Assuming get_github_oauth_redirect_url service constructs the full URL
        # It would need GITHUB_CLIENT_ID and GITHUB_SCOPES from settings
        try:
            redirect_url = get_github_oauth_redirect_url(state) # This service might need to be async if it does I/O
            return HttpResponseRedirect(redirect_url)
        except Exception as e:
            # Log error e
            error_message = urlencode({"message": "Failed to initiate GitHub login."})
            return HttpResponseRedirect(f"{settings.FRONTEND_URL}/auth/error?{error_message}")

class GitHubCallbackView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        code = request.GET.get('code')
        state_from_callback = request.GET.get('state')

        # TODO: Implement rate limiting for request.META.get('REMOTE_ADDR')

        if not code or not state_from_callback:
            error_url = f"{settings.FRONTEND_URL}/auth/error?message={urllib.parse.quote('Missing code or state from GitHub callback.')}"
            return HttpResponseRedirect(error_url)

        if not validate_oauth_state(request, state_from_callback):
            error_url = f"{settings.FRONTEND_URL}/auth/error?message={urllib.parse.quote('Invalid OAuth state.')}"
            return HttpResponseRedirect(error_url)

        try:
            github_token = exchange_code_for_github_token(code)
            if not github_token:
                raise Exception("Failed to retrieve GitHub access token.")

            github_user_info = get_github_user_info(github_token)
            
            # Ensure email is present, if not, try to get it or handle missing email
            user_email = github_user_info.get("email")
            if not user_email:
                # Potentially raise an error or redirect with a message if email is strictly required
                # For now, we'll allow it to be null if not provided by GitHub or primary email not found
                pass 

            user, created = User.objects.update_or_create(
                github_id=str(github_user_info["id"]),
                defaults={
                    'username': github_user_info["login"],
                    'email': user_email,
                    'github_access_token': github_token,
                    # Ensure other required fields for User model are handled if any
                }
            )

            refresh = RefreshToken.for_user(user)
            access_token = str(refresh.access_token)

            # Redirect to frontend with token
            frontend_url = f"{settings.FRONTEND_URL}/auth/callback?token={access_token}&refresh_token={str(refresh)}"
            return HttpResponseRedirect(frontend_url)

        except requests.exceptions.RequestException as e:
            # Log the error: print(e) or use proper logging
            error_url = f"{settings.FRONTEND_URL}/auth/error?message={urllib.parse.quote(f'GitHub API error: {e}')}"
            return HttpResponseRedirect(error_url)
        except Exception as e:
            # Log the error: print(e) or use proper logging
            error_url = f"{settings.FRONTEND_URL}/auth/error?message={urllib.parse.quote(f'An unexpected error occurred: {e}')}"
            return HttpResponseRedirect(error_url)

class GitHubExchangeAuthTokenView(APIView):
    permission_classes = [AllowAny]

    async def post(self, request, *args, **kwargs):
        code = request.data.get('code')

        if not code:
            return Response({"detail": "Authorization code is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            github_token_data = await exchange_code_for_github_token(code)
            if isinstance(github_token_data, str):
                github_access_token = github_token_data
            elif isinstance(github_token_data, dict) and 'access_token' in github_token_data:
                github_access_token = github_token_data['access_token']
            else:
                raise ValueError("Invalid token data from GitHub service")
                
            github_user_info = await get_github_user_info(github_access_token)

            user, created = await User.objects.aget_or_create(
                github_id=str(github_user_info["id"]),
                defaults={
                    "username": github_user_info["login"],
                    "email": github_user_info.get("email"),
                    "github_access_token": github_access_token,
                }
            )

            if not created:
                user.username = github_user_info["login"]
                user.email = github_user_info.get("email")
                user.github_access_token = github_access_token
                await user.asave()
            
            refresh = RefreshToken.for_user(user)
            
            return Response({
                "access_token": str(refresh.access_token),
                "refresh_token": str(refresh),
                "token_type": "bearer"
            }, status=status.HTTP_200_OK)

        except Exception as e:
            # Log error e
            return Response({"detail": f"GitHub authentication failed: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)

class UserRepositoriesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        current_user = request.user
        if not current_user.github_access_token:
            return Response({"detail": "GitHub access token not found for user."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            page = int(request.query_params.get('page', 1))
            per_page = int(request.query_params.get('per_page', 30))

            github_repos_list = get_user_repos_from_github(
                current_user.github_access_token,
                page=page,
                per_page=per_page
            )
            
            processed_repos = []
            for gh_repo_data in github_repos_list:
                # Check if this repo is registered in our system by GitHub native ID
                db_repo = DBRepository.objects.filter(github_native_id=gh_repo_data['id']).first()
                
                # Use a temporary dict to build up the response for this repo
                repo_info_to_return = gh_repo_data.copy() # Start with all GitHub data

                if db_repo:
                    repo_info_to_return['is_registered_in_system'] = True
                    repo_info_to_return['system_id'] = db_repo.id
                else:
                    repo_info_to_return['is_registered_in_system'] = False
                    repo_info_to_return['system_id'] = None
                
                processed_repos.append(repo_info_to_return)
            
            # Serialize the processed list. 
            # GitHubRepositorySerializer is designed for this kind of mixed data.
            serializer = GitHubRepositorySerializer(processed_repos, many=True)
            return Response(serializer.data)

        except requests.exceptions.RequestException as e:
            return Response({"detail": f"Failed to fetch repositories from GitHub: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return Response({"detail": f"An unexpected error occurred: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class UserOrganizationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        current_user = request.user
        if not current_user.github_access_token:
            return Response({"detail": "GitHub access token not found for user."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            page = int(request.query_params.get('page', 1))
            per_page = int(request.query_params.get('per_page', 30))
            orgs_list = get_user_orgs_from_github(
                current_user.github_access_token,
                page=page,
                per_page=per_page
            )
            serializer = GitHubOrganizationSerializer(orgs_list, many=True)
            return Response(serializer.data)
        except requests.exceptions.RequestException as e:
            return Response({"detail": f"Failed to fetch organizations from GitHub: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return Response({"detail": f"An unexpected error occurred: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# Assuming custom permissions like IsRepositoryOwner and CanAccessRepository are defined
# For example, in a core.permissions module:
# from .permissions import IsRepositoryOwner, CanAccessRepository 

# Placeholder for custom permissions if not already defined/imported
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

class RepositoryViewSet(viewsets.ModelViewSet):
    queryset = DBRepository.objects.all()
    serializer_class = RepositorySerializer
    permission_classes = [IsAuthenticated] # Base permission for all actions

    def get_queryset(self):
        # Users can list repositories they own or are collaborators on.
        return DBRepository.objects.filter(
            Q(owner=self.request.user) | Q(collaborators__user=self.request.user, collaborators__role__in=['member', 'admin']) # Assuming 'member', 'admin' roles
        ).distinct()

    def perform_create(self, serializer):
        # Generate a unique webhook secret
        webhook_secret = hashlib.sha256(os.urandom(32)).hexdigest()

        # Construct the webhook URL
        # Ensure 'github-webhook' is the correct name of your webhook URL pattern in core/urls.py
        full_webhook_url = None
        try:
            # Assuming your webhook URL is named 'github-webhook' in your urls.py
            webhook_path = reverse('github-webhook') 
            base_url = self.request.build_absolute_uri('/').rstrip('/')
            full_webhook_url = f"{base_url}{webhook_path}"
        except Exception as e:
            logger.error(f"Could not reverse URL for 'github-webhook': {e}. Webhook URL will be null for new repo.")
            # Depending on policy, you might want to prevent repo creation if webhook URL cannot be formed.
            # For now, it will proceed with webhook_url as None.

        # Save the instance with the owner, generated secret, and webhook_url
        instance = serializer.save(
            owner=self.request.user,
            webhook_secret=webhook_secret,
            webhook_url=full_webhook_url # Pass the generated URL to be saved
        )
        
        # Add owner as a collaborator
        RepoCollaborator.objects.create(repository=instance, user=self.request.user, role='owner')

    def get_permissions(self):
        if self.action in ['update', 'partial_update', 'destroy', 'regenerate_webhook_secret', 'webhook_status']:
            self.permission_classes = [IsAuthenticated, IsRepositoryOwner]
        elif self.action in ['retrieve', 'collaborators', 'registered_collaborators']:
            self.permission_classes = [IsAuthenticated, CanAccessRepository]
        elif self.action == 'by_github_id':
            # now allow owners _and_ collaborators
            self.permission_classes = [IsAuthenticated, CanAccessRepository]
        # Remove webhook_status from here if it's handled by the more specific action decorator path
        # Remove regenerate_webhook_secret from here if it's handled by the more specific action decorator path
        else: # list, create
            self.permission_classes = [IsAuthenticated]
        return super().get_permissions()

    @action(detail=True, methods=['post'], url_path='webhook/regenerate-secret')
    def regenerate_webhook_secret(self, request, pk=None):
        """Regenerate the webhook secret for this repository."""
        repository = self.get_object() # Applies object-level permissions (IsRepositoryOwner)
        new_secret = hashlib.sha256(os.urandom(32)).hexdigest()
        repository.webhook_secret = new_secret
        repository.save(update_fields=['webhook_secret'])
        # TODO: Potentially, this should also update the webhook secret on GitHub if the app manages the webhook creation.
        # For now, it just updates the secret in the DB, and the user would need to update it on GitHub manually.
        return Response({"status": "webhook secret regenerated", "new_secret_hint": "New secret stored. Update your Git provider if necessary."})

    @action(detail=True, methods=['get'], url_path='webhook/status')
    def webhook_status(self, request, pk=None):
        """Check webhook status (e.g., last event received)."""
        repository = self.get_object() # Applies object-level permissions (IsRepositoryOwner or CanAccessRepository based on get_permissions)
        # The permission is currently set to IsRepositoryOwner in get_permissions for 'webhook_status'.

        # Fetch last 5 webhook events for this repository as an example
        recent_events = WebhookEventLog.objects.filter(repository=repository).order_by('-created_at')[:5]
        # You might want to serialize these events if you send them
        # For now, just a summary
        last_event_summary = None
        if recent_events.exists():
            last_event = recent_events.first()
            last_event_summary = {
                "type": last_event.event_type,
                "timestamp": last_event.processed_at.isoformat(),
                "status_code": last_event.status # Assuming you add status_code to WebhookEventLog
            }

        status_data = {
            "webhook_id": repository.webhook_url, # Assuming you store webhook_id from GitHub
            "webhook_url": f"{settings.FRONTEND_URL}/api/webhook/github/", # The URL they should configure
            "secret_configured": bool(repository.webhook_secret),
            "secret": repository.webhook_secret, # Don't send this in production!
            "is_active_on_github": None, # This would require a GitHub API call to check actual status
            "last_event_received": last_event_summary,
            "recent_event_count": WebhookEventLog.objects.filter(repository=repository).count()
        }
        return Response(status_data)

    def list(self, request, *args, **kwargs):
        """List repositories for which the current user is an owner or collaborator."""
        # Get repos owned by the user
        owned_repos = DBRepository.objects.filter(owner=request.user)
        # Get repos where the user is a collaborator
        collaborating_repo_ids = RepoCollaborator.objects.filter(user=request.user).values_list('repository_id', flat=True)
        collaborating_repos = DBRepository.objects.filter(id__in=collaborating_repo_ids)
        # Combine and remove duplicates
        queryset = (owned_repos | collaborating_repos).distinct()
        
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def collaborators(self, request, pk=None):
        """Get collaborators from GitHub for this repository."""
        repository = self.get_object() # Applies CanAccessRepository permission
        if not request.user.github_access_token:
            return Response({"error": "GitHub access token not available for current user."}, status=status.HTTP_400_BAD_REQUEST)
        if not repository.owner or not repository.owner.username:
             return Response({"error": "Repository owner or owner's GitHub username not found."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            page = int(request.query_params.get('page', 1))
            per_page = int(request.query_params.get('per_page', 30))
            github_collaborators_data = get_repo_collaborators_from_github(
                owner_login=repository.owner.username,
                repo_name=repository.repo_name.split("/")[1],
                github_token=request.user.github_access_token,
                page=page,
                per_page=per_page
            )
            
            # --- NEW: if the signed-in user is in that list, make sure they exist in our table ---
            for gh in github_collaborators_data:
                if str(gh["id"]) == str(request.user.github_id):
                    RepoCollaborator.objects.update_or_create(
                        repository=repository,
                        user=request.user,
                        defaults={"role": gh.get("permissions", {}).get("push") and "member" or "read"}
                    )
                    break
            # -------------------------------------------------------------------------------
            serializer = GitHubCollaboratorSerializer(github_collaborators_data, many=True)
            return Response(serializer.data)
        except Exception as e:
            # Log error e
            return Response({"error": f"Failed to fetch collaborators from GitHub: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'], url_path='registered-collaborators')
    def registered_collaborators(self, request, pk=None):
        """Get registered collaborators in the system for this repository."""
        repository = self.get_object() # Applies CanAccessRepository permission
        collaborators = RepoCollaborator.objects.filter(repository=repository)
        serializer = RepoCollaboratorSerializer(collaborators, many=True)
        return Response(serializer.data)

    # The get_repository_by_github_id from FastAPI is slightly different from DRF's default retrieve.
    # We can add it as a list route action or a separate view if preferred.
    # For now, let's assume client will use /api/v1/repositories/{id}/ (PK) or filter list view.
    # If a dedicated /repositories/by-github-id/{github_id} is needed:
    @action(detail=False, methods=['get'], url_path='by-github-id/(?P<github_id>[0-9]+)')
    def by_github_id(self, request, github_id=None):
         repository = get_object_or_404(DBRepository, github_native_id=github_id)
         # apply IsAuthenticated + CanAccessRepository
         self.check_object_permissions(request, repository)
         serializer = self.get_serializer(repository)
         return Response(serializer.data)
    @action(detail=True, methods=['get'], url_path='pulls/(?P<pr_number>[0-9]+)')
    def retrieve_pull_request(self, request, pk=None, pr_number=None):
        repository = self.get_object() # pk is repo_id, permission check done by get_object

        try:
            pr_instance = PRModel.objects.get(repository=repository, pr_number=pr_number)
            serializer = PRSerializer(pr_instance)
            data = serializer.data
            data['source'] = 'db' # Add source information
            return Response(data)
        except PRModel.DoesNotExist:
            if not request.user.github_access_token:
                return Response({"detail": "Pull Request not found in DB and no GitHub token available to fetch from GitHub."}, status=status.HTTP_404_NOT_FOUND)
            
            try:
                owner_login = repository.owner.username # Assumes User model has username as GitHub login
                repo_name_only = repository.repo_name.split('/')[-1]
                
                gh_pr_data = get_single_pull_request_from_github(
                    github_token=request.user.github_access_token,
                    owner_login=owner_login,
                    repo_name=repo_name_only,
                    pr_number=int(pr_number)
                )
                
                # Transform GitHub data to fit PRSerializer structure
                user_data = gh_pr_data.get('user', {})
                head_data = gh_pr_data.get('head', {})
                base_data = gh_pr_data.get('base', {})
                transformed_data = {
                    'pr_github_id': gh_pr_data.get('id'),
                    'pr_number': gh_pr_data.get('number'),
                    'title': gh_pr_data.get('title'),
                    'body': gh_pr_data.get('body'),
                    'author_github_id': str(user_data.get('id')) if user_data else None,
                    'status': gh_pr_data.get('state'), # 'open', 'closed'
                    'url': gh_pr_data.get('html_url'),
                    'head_sha': head_data.get('sha'),
                    'base_sha': base_data.get('sha'),
                    'user_login': user_data.get('login'), # From PRSerializer fields
                    'user_avatar_url': user_data.get('avatar_url'), # From PRSerializer fields
                    'created_at_gh': gh_pr_data.get('created_at'), # From PRSerializer fields
                    'updated_at_gh': gh_pr_data.get('updated_at'), # From PRSerializer fields
                    'closed_at_gh': gh_pr_data.get('closed_at'), # From PRSerializer fields
                    'merged_at_gh': gh_pr_data.get('merged_at'), # From PRSerializer fields
                    'repository_id': repository.id, # Link to our DB repository ID
                    # 'source' will be added after serialization if needed, or serializer can handle it
                }
                
                serializer = PRSerializer(data=transformed_data)
                if serializer.is_valid():
                    # Optionally, save this fetched PR to DB if it wasn't found
                    # pr_to_save = serializer.save() # This would create it
                    # data_to_return = PRSerializer(pr_to_save).data # Reserialize to include all fields
                    data_to_return = serializer.data # Use validated data
                    data_to_return['source'] = 'github'
                    return Response(data_to_return)
                else:
                    logger.error(f"GitHub PR data for repo {repository.id}, PR #{pr_number} not valid for serializer: {serializer.errors}")
                    return Response({"detail": "Error processing PR data from GitHub.", "errors": serializer.errors}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    return Response({"detail": f"Pull Request #{pr_number} not found on GitHub for repository {repository.repo_name}."}, status=status.HTTP_404_NOT_FOUND)
                logger.error(f"GitHub API error fetching PR #{pr_number} for repo {repository.id}: {e.response.text}")
                return Response({"detail": f"GitHub API error: {e.response.status_code}"}, status=status.HTTP_502_BAD_GATEWAY)
            except Exception as e:
                logger.error(f"Unexpected error fetching PR #{pr_number} for repo {repository.id}: {e}")
                return Response({"detail": "An unexpected error occurred while fetching PR from GitHub."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'], url_path='commits/sha/(?P<commit_sha>[0-9a-fA-F]{7,40})')
    def retrieve_commit_by_sha(self, request, pk=None, commit_sha=None): # Renamed for clarity
        repository = self.get_object() # pk is repo_id, permission check

        try:
            # For exact match, use commit_hash=commit_sha. If short SHAs are possible from client,
            # and DB stores full SHAs, this direct lookup might miss.
            # GitHub API handles short SHAs if they are unique.
            commit_instance = CommitModel.objects.get(repository=repository, commit_hash=commit_sha)
            serializer = CommitSerializer(commit_instance)
            data = serializer.data
            data['source'] = 'db'
            return Response(data)
        except CommitModel.DoesNotExist:
            # If client sent a short SHA, and it wasn't found as full SHA in DB, try GitHub
            pass # Fall through to GitHub fetch
        except CommitModel.MultipleObjectsReturned: # Should not happen if commit_hash is unique per repo
             logger.error(f"Multiple commits found for SHA {commit_sha} in repo {repository.id}. This should not happen.")
             return Response({"detail": "Internal error: Ambiguous commit SHA in database."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


        if not request.user.github_access_token:
            return Response({"detail": "Commit not found in DB and no GitHub token available to fetch from GitHub."}, status=status.HTTP_404_NOT_FOUND)

        try:
            owner_login = repository.owner.username
            repo_name_only = repository.repo_name.split('/')[-1]

            gh_commit_data = get_single_commit_from_github(
                github_token=request.user.github_access_token,
                owner_login=owner_login,
                repo_name=repo_name_only,
                commit_sha=commit_sha
            )

            # Transform GitHub data to fit CommitSerializer structure
            commit_details = gh_commit_data.get('commit', {})
            author_details = commit_details.get('author', {}) # Git author
            committer_details = commit_details.get('committer', {}) # Git committer
            gh_author_user = gh_commit_data.get('author') # GitHub user object for author
            gh_committer_user = gh_commit_data.get('committer') # GitHub user object for committer
            transformed_data = {
                'commit_hash': gh_commit_data.get('sha'),
                'message': commit_details.get('message'),
                'author_name': author_details.get('name'),
                'author_email': author_details.get('email'),
                'committer_name': committer_details.get('name'),
                'committer_email': committer_details.get('email'),
                'timestamp': author_details.get('date'), # Main timestamp from git author date
                'url': gh_commit_data.get('html_url'),
                'author_github_id': str(gh_author_user.get('id')) if gh_author_user else None,
                'committer_github_id': str(gh_committer_user.get('id')) if gh_committer_user else None,
                'repository_id': repository.id,
                'source': 'github', # Indicate source
            }
            serializer = CommitSerializer(data=transformed_data)
            if serializer.is_valid():
                # final_output will contain the representation of model fields
                final_output = serializer.data 
                
                # Now, add the non-model, read-only fields directly from the GitHub data
                # to the response. These were not processed by serializer.is_valid()
                # for input, and serializer.data wouldn't include them unless they were
                # attributes of a model instance.
                # final_output['author_name'] = transformed_data.get('author_name')
                # final_output['author_email'] = transformed_data.get('author_email')
                # final_output['committer_name'] = transformed_data.get('committer_name')
                # final_output['committer_email'] = transformed_data.get('committer_email')
                # final_output['committed_date'] = transformed_data.get("committed_date")
                
                # final_output['source'] = 'github'
                return Response(final_output)
            else:
                logger.error(f"GitHub commit data for repo {repository.id}, SHA {commit_sha} not valid for serializer: {serializer.errors}")
                return Response({"detail": "Error processing commit data from GitHub.", "errors": serializer.errors}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return Response({"detail": f"Commit with SHA {commit_sha} not found on GitHub for repository {repository.repo_name}."}, status=status.HTTP_404_NOT_FOUND)
            if e.response.status_code == 422: # Often for invalid SHA format or non-existent commit
                 return Response({"detail": f"Invalid SHA or commit {commit_sha} not found on GitHub (422)."}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
            logger.error(f"GitHub API error fetching commit SHA {commit_sha} for repo {repository.id}: {e.response.text}")
            return Response({"detail": f"GitHub API error: {e.response.status_code}"}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception as e:
            logger.error(f"Unexpected error fetching commit SHA {commit_sha} for repo {repository.id}: {e}")
            return Response({"detail": "An unexpected error occurred while fetching commit from GitHub."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@csrf_exempt
@require_POST
async def github_webhook(request):
    """Handle GitHub webhook events"""
    signature = request.headers.get('X-Hub-Signature-256')
    event_type = request.headers.get('X-GitHub-Event')
    delivery_id = request.headers.get('X-GitHub-Delivery')

    if not all([signature, event_type, delivery_id]):
        logger.warning("Webhook request missing required headers (Signature, Event, Delivery ID).")
        return HttpResponse('Missing required headers', status=400)

    # Find repository for this webhook
    repository = None
    try:
        payload = json.loads(request.body.decode('utf-8'))
        repo_full_name = payload.get('repository', {}).get('full_name')
        if repo_full_name:
            try:
                repository = await DBRepository.objects.aget(repo_name=repo_full_name)
            except DBRepository.DoesNotExist:
                logger.warning(f"Repository {repo_full_name} not found in the database.")
    except json.JSONDecodeError:
        logger.warning("Could not parse request body as JSON to identify repository.")

    log_entry, created = await WebhookEventLog.objects.aupdate_or_create(
        event_id=delivery_id,
        defaults={
            'repository':repository,
            'event_type': event_type,
            'payload': {'message': 'Event received, pending verification.'}, # Store raw body initially if possible
            'headers': dict(request.headers),
            'status': 'received'
        }
    )
    if not created:
        log_entry.status = 'received'
        log_entry.repository = repository
        log_entry.error_message = None
        log_entry.processed_at = None
        log_entry.payload = {'message': 'Event re-received, pending verification.'}
        log_entry.headers = dict(request.headers)
        await log_entry.asave()

    # Extract repo info from payload to find the correct secret
    try:
        payload = json.loads(request.body.decode('utf-8'))
        repo_full_name = payload.get('repository', {}).get('full_name')
        
        # Try to find repository-specific secret
        webhook_secret = None
        if repo_full_name:
            try:
                repo = await DBRepository.objects.aget(repo_name=repo_full_name)
                webhook_secret = repo.webhook_secret
            except DBRepository.DoesNotExist:
                pass
        
        # Fall back to global secret if no repo-specific secret found
        if not webhook_secret:
            webhook_secret = settings.GITHUB_WEBHOOK_SECRET
            
        # Verify signature with the appropriate secret
        expected_signature = hmac.new(
            webhook_secret.encode('utf-8'),
            request.body,
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(f"sha256={expected_signature}", signature):
            logger.warning(f"Invalid webhook signature for event {delivery_id}.")
            log_entry.status = 'failed'
            log_entry.error_message = "Invalid signature."
            await log_entry.asave()
            return HttpResponse('Invalid signature', status=401)
    except Exception as e:
        logger.error(f"Error during webhook signature verification for event {delivery_id}: {str(e)}")
        log_entry.status = 'failed'
        log_entry.error_message = f"Signature verification error: {str(e)}"
        await log_entry.asave()
        return HttpResponse('Error verifying signature', status=500)

    try:
        event_data = json.loads(request.body.decode('utf-8'))
        log_entry.payload = event_data # Update with parsed JSON payload
        await log_entry.asave(update_fields=['payload'])

        # Dispatch to Celery task for actual processing
        process_webhook_event.delay(event_type, event_data)
        
        log_entry.status = 'processed' # Mark as successfully enqueued
        log_entry.processed_at = timezone.now()
        await log_entry.asave(update_fields=['status', 'processed_at'])
        logger.info(f"Webhook event {delivery_id} ({event_type}) successfully enqueued for processing.")
        return HttpResponse('Webhook processed and enqueued', status=202)
                
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON payload for webhook event {delivery_id}: {str(e)}")
        log_entry.status = 'failed'
        log_entry.error_message = f"Invalid JSON payload: {str(e)}"
        await log_entry.asave()
        return HttpResponse('Invalid JSON payload', status=400)
        # except Exception as e:
        # logger.error(f"Error processing webhook event {delivery_id} after verification: {str(e)}", exc_info=True)
        # log_entry.status = 'failed'
        # log_entry.error_message = f"Internal processing error: {str(e)}"
        # await log_entry.asave()
        # return HttpResponse('Error processing webhook', status=500)

# Admin endpoints
class AdminStatsView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request, *args, **kwargs):
        user_count = User.objects.count()
        repo_count = DBRepository.objects.count()
        review_count = ReviewModel.objects.count()
        llm_usage_count = LLMUsageModel.objects.count()
        return Response({
            'users': user_count,
            'repositories': repo_count,
            'reviews': review_count,
            'llm_usages': llm_usage_count,
        })

class AdminUserListView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request, *args, **kwargs):
        users = User.objects.all()
        serializer = UserSerializer(users, many=True)
        return Response(serializer.data)

class AdminUserUpdateView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def put(self, request, user_id, *args, **kwargs):
        user = get_object_or_404(User, pk=user_id)
        serializer = AdminUserUpdateSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

# New ViewSet for User specific endpoints
class UserViewSet(viewsets.ViewSet):
    """
    ViewSet for user-related operations.
    Currently, most user operations are handled by CurrentUserView, UserRepositoriesView, etc.
    This can be expanded if other user-specific, non-admin RESTful operations are needed.
    """
    permission_classes = [IsAuthenticated]
    # Example:
    # @action(detail=False, methods=['get'], url_path='profile-settings')
    # def profile_settings(self, request):
    #     user = request.user
    #     # ... logic to get profile settings ...
    #     return Response(...)
    pass

class CommitViewSet(viewsets.ModelViewSet):
    serializer_class = CommitSerializer
    permission_classes = [IsAuthenticated] # Permissions checked in list method

    def get_queryset(self):
        # Base queryset, actual filtering by repository_id happens in list()
        return CommitModel.objects.all()

    def list(self, request, *args, **kwargs):
        repository_id = request.query_params.get('repo_id')
        if not repository_id:
            return Response({"detail": "repository_id query parameter is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            db_repo = get_object_or_404(DBRepository, pk=repository_id)
        except ValueError:
            return Response({"detail": "Invalid repository_id format."}, status=status.HTTP_400_BAD_REQUEST)

        if not CanAccessRepository().has_object_permission(request, self, db_repo):
            raise PermissionDenied("You do not have permission to access this repository.")

        db_items = CommitModel.objects.filter(repository=db_repo).order_by('-timestamp')
        
        serialized_db_items = self.get_serializer(db_items, many=True).data
        for item in serialized_db_items:
            item['source'] = 'db'

        combined_items_dict = {item['commit_hash']: item for item in serialized_db_items} # Use commit_hash

        if not request.user.github_access_token:
            logger.warning(f"User {request.user.id} has no GitHub token. Fetching commits from DB only for repo {db_repo.id}")
        else:
            try:
                page = int(request.query_params.get('page', 1))
                per_page = int(request.query_params.get('per_page', 30)) # Default to 30, can be adjusted

                owner_login = db_repo.owner.username
                repo_name_only = db_repo.repo_name.split('/')[-1]
                
                gh_items_raw = get_repository_commits_from_github(
                    github_token=request.user.github_access_token,
                    owner_login=owner_login,
                    repo_name=repo_name_only,
                    per_page=per_page,
                    page=page
                )

                for gh_commit in gh_items_raw:
                    # Use gh_commit['sha'] as the key for matching
                    if gh_commit['sha'] not in combined_items_dict:
                        commit_data = gh_commit.get('commit', {})
                        author_data = commit_data.get('author', {}) # Git author
                        committer_data = commit_data.get('committer', {}) # Git committer
                        
                        # GitHub user objects (can be different from git author/committer)
                        gh_author_user = gh_commit.get('author') # GitHub user who authored
                        gh_committer_user = gh_commit.get('committer') # GitHub user who committed

                        transformed_gh_item = {
                            'commit_hash': gh_commit.get('sha'),
                            'message': commit_data.get('message'),
                            
                            'author_name': author_data.get('name'),
                            'author_email': author_data.get('email'),
                            # 'author_date': author_data.get('date'), # Replaced by timestamp
                            
                            'committer_name': committer_data.get('name'),
                            'committer_email': committer_data.get('email'),
                            'committed_date': committer_data.get('date'), # Keep this for committer specific date
                            
                            'url': gh_commit.get('html_url'),
                            'source': 'github',
                            'id': None, 
                            'repository_id': db_repo.id,
                            'created_at': None, 
                            'updated_at': None,

                            # Align with model fields
                            'timestamp': author_data.get('date'), # Use git author date for the main commit timestamp
                            'author_github_id': str(gh_author_user.get('id')) if gh_author_user else None,
                            'committer_github_id': str(gh_committer_user.get('id')) if gh_committer_user else None,
                        }
                        # Use serializer to ensure consistent output structure, passing instance=transformed_gh_item
                        # This is tricky if serializer expects a model instance.
                        # For now, we construct a dict and assume client handles it.
                        # A better way might be to have a different serializer for GitHub-only objects
                        # or make the main serializer flexible.
                        serializer_instance = self.get_serializer(data=transformed_gh_item)
                        
                        if serializer_instance.is_valid():
                            validated_data = serializer_instance.data
                            # Ensure 'source' is present if it's part of read_only_fields but you want it in output
                            validated_data['source'] = 'github' 
                            combined_items_dict[gh_commit['sha']] = validated_data
                        else:
                             logger.error(f"GitHub commit data for {gh_commit['sha']} not valid for serializer: {serializer_instance.errors}")
                             # If not valid, store the raw transformed dict but ensure it's what you want
                             # Adding repository_id for context if it was missing and causing validation error
                             transformed_gh_item['repository_id'] = db_repo.id # Ensure this is set
                             transformed_gh_item['source'] = 'github' # Explicitly set source
                             combined_items_dict[gh_commit['sha']] = transformed_gh_item


            except requests.exceptions.RequestException as e:
                logger.error(f"GitHub API error while fetching commits for repo {db_repo.id}: {e}")
                # Proceed with DB items, or return error
            except Exception as e:
                logger.error(f"Unexpected error while fetching GitHub commits for repo {db_repo.id}: {e}")


        final_list = list(combined_items_dict.values())
        # Optionally, re-sort if mixing sources changed order
        # final_list.sort(key=lambda x: x.get('committed_date') or x.get('author_date'), reverse=True)
        return Response(final_list)

    @action(detail=True, methods=['post'])
    def trigger_review(self, request, pk=None):
        """
        Manually trigger an AI review for a commit.
        
        Args:
            pk: The ID of the Commit model instance
        
        Returns:
            Response with the review ID and status
        """
        # Get the commit
        commit = self.get_object()
        repository = commit.repository
        
        # Check permissions (must be owner or collaborator)
        if not CanAccessRepository().has_object_permission(request, self, repository):
            raise PermissionDenied("You do not have permission to trigger reviews for this repository.")
        
        # Check for existing reviews that are completed or in progress
        existing_reviews = ReviewModel.objects.filter(
            commit=commit,
            status__in=['completed', 'in_progress', 'pending']
        )
        
        if existing_reviews.exists():
            # Return the most recent review
            latest_review = existing_reviews.order_by('-created_at').first()
            return Response({
                "detail": f"A review for this commit already exists with status '{latest_review.status}'.",
                "review_id": latest_review.id,
                "status": latest_review.status
            }, status=status.HTTP_409_CONFLICT)
        
        # Create a new review
        review = ReviewModel.objects.create(
            repository=repository,
            commit=commit,
            status='pending',
            review_data={'message': 'Commit review manually triggered by user.'}
        )
        
        # Prepare data for the Celery task
        event_data = {
            'commit': {
                'id': commit.commit_hash,
                'sha': commit.commit_hash,
                'message': commit.message,
                'url': commit.url,
                'author': {
                    'id': commit.author_github_id,
                    'name': (
                        User.objects.filter(github_id=commit.author_github_id).values_list('username', flat=True).first() 
                        if commit.author_github_id and User.objects.filter(github_id=commit.author_github_id).exists()
                        else None
                    ),
                    'email': (
                        User.objects.filter(github_id=commit.author_github_id).values_list('email', flat=True).first()
                        if commit.author_github_id and User.objects.filter(github_id=commit.author_github_id).exists()
                        else None
                    )
                },
                'committer': {
                    'id': commit.committer_github_id,
                    'name': (
                        User.objects.filter(github_id=commit.committer_github_id).values_list('username', flat=True).first()
                        if commit.committer_github_id and User.objects.filter(github_id=commit.committer_github_id).exists()
                        else None
                    ),
                    'email': (
                        User.objects.filter(github_id=commit.committer_github_id).values_list('email', flat=True).first()
                        if commit.committer_github_id and User.objects.filter(github_id=commit.committer_github_id).exists()
                        else None
                    )
                },
                'timestamp': commit.timestamp.isoformat() if commit.timestamp else None
            },
            'repository': {
                'id': repository.github_native_id,
                'full_name': repository.repo_name,
                'owner': {'login': repository.owner.username}
            },
            'action': 'manual_trigger'
        }
        
        # Enqueue the review task
        process_commit_review.delay(event_data, repository.id, commit.id)
        
        # Return response
        return Response({
            "detail": "AI review has been triggered for the commit.",
            "review_id": review.id,
            "status": review.status
        }, status=status.HTTP_201_CREATED)

class PullRequestViewSet(viewsets.ModelViewSet):
    serializer_class = PRSerializer
    permission_classes = [IsAuthenticated]
    
    @action(detail=True, methods=['get'], url_path='my-threads')
    def my_threads(self, request, pk=None):
        """
        Get threads created by the current user for this pull request.
        pk here is the PullRequest ID.
        """
        pr = self.get_object()
        threads_qs = ThreadModel.objects.filter(
            review__pull_request=pr,
            created_by=request.user
        ).order_by('-created_at')
        
        page = self.paginate_queryset(threads_qs)
        if page is not None:
            serializer = ThreadSerializer(page, many=True, context={'request': request})
            return self.get_paginated_response(serializer.data)

        serializer = ThreadSerializer(threads_qs, many=True, context={'request': request})
        return Response(serializer.data)
    def get_queryset(self):
        return PRModel.objects.all()

    def list(self, request, *args, **kwargs):
        repository_id = request.query_params.get('repo_id')
        if not repository_id:
            return Response({"detail": "repository_id query parameter is required."}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            db_repo = get_object_or_404(DBRepository, pk=repository_id)
        except ValueError:
            return Response({"detail": "Invalid repository_id format."}, status=status.HTTP_400_BAD_REQUEST)

        if not CanAccessRepository().has_object_permission(request, self, db_repo):
            raise PermissionDenied("You do not have permission to access this repository.")

        db_items = PRModel.objects.filter(repository=db_repo).order_by('-pr_number')
        serialized_db_items = self.get_serializer(db_items, many=True).data
        for item in serialized_db_items:
            item['source'] = 'db'
        
        combined_items_dict = {item['pr_number']: item for item in serialized_db_items} # Use pr_number

        if not request.user.github_access_token:
            logger.warning(f"User {request.user.id} has no GitHub token. Fetching PRs from DB only for repo {db_repo.id}")
        else:
            try:
                page = int(request.query_params.get('page', 1))
                per_page = int(request.query_params.get('per_page', 30)) # Default to 30

                owner_login = db_repo.owner.username
                repo_name_only = db_repo.repo_name.split('/')[-1]
                
                gh_items_raw = get_repository_pull_requests_from_github(
                    github_token=request.user.github_access_token,
                    owner_login=owner_login,
                    repo_name=repo_name_only,
                    state="all",
                    per_page=per_page,
                    page=page
                )
                
                for gh_pr in gh_items_raw:
                    # Use gh_pr['number'] as the key for matching
                    if gh_pr['number'] not in combined_items_dict:
                        user_data = gh_pr.get('user', {})
                        head_data = gh_pr.get('head', {})
                        base_data = gh_pr.get('base', {})
                        transformed_gh_item = {
                            'pr_github_id': gh_pr.get('id'),
                            'pr_number': gh_pr.get('number'),
                            'title': gh_pr.get('title'),
                            'body': gh_pr.get('body'),
                            'user_login': user_data.get('login'),
                            'user_avatar_url': user_data.get('avatar_url'),
                            'url': gh_pr.get('html_url'),
                            'created_at_gh': gh_pr.get('created_at'),
                            'updated_at_gh': gh_pr.get('updated_at'),
                            'closed_at_gh': gh_pr.get('closed_at'),
                            'merged_at_gh': gh_pr.get('merged_at'),
                            'source': 'github',
                            'id': None,
                            'repository_id': db_repo.id, 
                            'created_at': None, 
                            'updated_at': None,

                            # Align with model fields
                            'author_github_id': str(user_data.get('id')) if user_data else None,
                            'status': gh_pr.get('state'),
                            'head_sha': head_data.get('sha'),
                            'base_sha': base_data.get('sha'),
                        }
                        # Similar to commits, using serializer for consistency if possible
                        serialized_gh_pr = self.get_serializer(data=transformed_gh_item)
                        if serialized_gh_pr.is_valid():
                            combined_items_dict[gh_pr['number']] = serialized_gh_pr.data
                        else:
                            logger.error(f"GitHub PR data for #{gh_pr['number']} not valid for serializer: {serialized_gh_pr.errors}")
                            transformed_gh_item['repository_id'] = db_repo.id # Add repository_id for context
                            combined_items_dict[gh_pr['number']] = transformed_gh_item


            except requests.exceptions.RequestException as e:
                logger.error(f"GitHub API error while fetching PRs for repo {db_repo.id}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error while fetching GitHub PRs for repo {db_repo.id}: {e}")

        final_list = list(combined_items_dict.values())
        # final_list.sort(key=lambda x: x.get('number'), reverse=True)
        return Response(final_list)

    @action(detail=False, methods=['post'], url_path='trigger-review') # MODIFIED
    def trigger_review(self, request): # MODIFIED: removed pk=None
        """
        Manually trigger an AI review for a pull request.
        
        Args:
            pk: The ID of the PullRequest model instance
        
        Returns:
            Response with the review ID and status
        """
        repository_id = request.data.get('repository_id')
        pr_number_str = request.data.get('pr_number')

        if not repository_id or pr_number_str is None:
            return Response(
                {"detail": "repository_id and pr_number are required in the request body."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            pr_number = int(pr_number_str)
        except ValueError:
            return Response({"detail": "pr_number must be an integer."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            repository = get_object_or_404(DBRepository, pk=repository_id)
        except ValueError: # Handles non-integer repository_id
            return Response({"detail": "Invalid repository_id format."}, status=status.HTTP_400_BAD_REQUEST)
        
        # Check permissions for the repository
        # The view instance 'self' is PullRequestViewSet here.
        if not CanAccessRepository().has_object_permission(request, self, repository):
            raise PermissionDenied("You do not have permission to trigger reviews for this repository.")

        try:
            pr = PRModel.objects.get(repository=repository, pr_number=pr_number)
        except PRModel.DoesNotExist:
            logger.info(f"Pull Request #{pr_number} not found in DB for repository {repository.repo_name}. Attempting to fetch from GitHub.")
            if not request.user.github_access_token:
                return Response({"detail": f"Pull Request #{pr_number} not found in DB and no GitHub token available to fetch from GitHub."}, status=status.HTTP_404_NOT_FOUND)
            
            try:
                owner_login = repository.owner.username
                repo_name_only = repository.repo_name.split('/')[-1]
                
                gh_pr_data = get_single_pull_request_from_github(
                    github_token=request.user.github_access_token,
                    owner_login=owner_login,
                    repo_name=repo_name_only,
                    pr_number=pr_number
                )
                
                # Transform and save the PR if found on GitHub
                user_data = gh_pr_data.get('user', {})
                head_data = gh_pr_data.get('head', {})
                base_data = gh_pr_data.get('base', {})

                pr, created = PRModel.objects.update_or_create(
                    repository=repository,
                    pr_number=pr_number,
                    defaults={
                        'pr_github_id': str(gh_pr_data.get('id')),
                        'title': gh_pr_data.get('title'),
                        'body': gh_pr_data.get('body'),
                        'author_github_id': str(user_data.get('id')) if user_data else None,
                        'status': gh_pr_data.get('state'),
                        'url': gh_pr_data.get('html_url'),
                        'head_sha': head_data.get('sha'),
                        'base_sha': base_data.get('sha'),
                    }
                )
                if created:
                    logger.info(f"Pull Request #{pr_number} fetched from GitHub and saved to DB for repository {repository.repo_name}.")
                else:
                    logger.info(f"Pull Request #{pr_number} fetched from GitHub and updated in DB for repository {repository.repo_name}.")

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    return Response({"detail": f"Pull Request #{pr_number} not found on GitHub for repository {repository.repo_name}."}, status=status.HTTP_404_NOT_FOUND)
                logger.error(f"GitHub API error fetching PR #{pr_number} for repo {repository.id}: {e.response.text if e.response else str(e)}")
                return Response({"detail": f"GitHub API error: {e.response.status_code if e.response else 'Unknown'}"}, status=status.HTTP_502_BAD_GATEWAY)
            except Exception as e:
                logger.error(f"Unexpected error fetching or saving PR #{pr_number} for repo {repository.id} from GitHub: {e}", exc_info=True)
                return Response({"detail": "An unexpected error occurred while fetching PR from GitHub."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # Check if PR is open
        # if pr.status != 'open':
        #     return Response(
        #         {"detail": "Cannot trigger review for a PR that is not open."},
        #         status=status.HTTP_400_BAD_REQUEST
        #     )
        
        # Check for existing reviews that are completed or in progress
        existing_reviews = ReviewModel.objects.filter(
            pull_request=pr,
            status__in=['completed', 'in_progress', 'pending']
        )
        
        if existing_reviews.exists():
            # Return the most recent review
            latest_review = existing_reviews.order_by('-created_at').first()
            return Response({
                "detail": f"A review for this PR already exists with status '{latest_review.status}'.",
                "review_id": latest_review.id,
                "status": latest_review.status
            }, status=status.HTTP_409_CONFLICT)
        
        # Create a new review
        review = ReviewModel.objects.create(
            repository=repository,
            pull_request=pr,
            status='pending',
            review_data={'message': 'Review manually triggered by user.'}
        )
        
        # Prepare data for the Celery task
        author_user = User.objects.filter(github_id=pr.author_github_id).first()
        author_login = author_user.username if author_user else None

        event_data = {
            'pull_request': {
                'number': pr.pr_number,
                'id': pr.pr_github_id, # Ensure this field is populated on PRModel
                'title': pr.title,
                'body': pr.body,
                'html_url': pr.url,
                'state': pr.status,
                'head': {'sha': pr.head_sha},
                'base': {'sha': pr.base_sha},
                'user': {
                    'id': pr.author_github_id, # Ensure this field is populated
                    'login': author_login 
                },
                'base': { 
                    'repo': {
                        'owner': {'login': repository.owner.username},
                        'name': repository.repo_name.split('/')[-1]
                    }
                }
            },
            'repository': {
                'id': repository.github_native_id, # Ensure this field is populated
                'full_name': repository.repo_name,
                'owner': {'login': repository.owner.username}
            },
            'action': 'manual_trigger_general' 
        }
        
        # Enqueue the review task
        process_pr_review.delay(event_data, repository.id, pr.id, triggering_user_id=request.user.id)
        
        # Return response
        return Response({
            "detail": "AI review has been triggered.",
            "review_id": review.id,
            "status": review.status
        }, status=status.HTTP_201_CREATED)

class ReviewViewSet(viewsets.ModelViewSet):
    serializer_class = ReviewSerializer
    permission_classes = [IsAuthenticated]
    
    @action(detail=False, methods=['get'],url_path='history', permission_classes=[IsAuthenticated, CanAccessRepository])
    def history(self, request, pk=None):
        """
        Get review history for a PR or commit with thread information.
        """
        context = request.query_params.get('context')  # 'pr' or 'commit'
        item_id = request.query_params.get('id')  # PR or Commit ID
        
        if not context or not item_id:
            return Response(
                {"detail": "Context and ID parameters are required."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        reviews_qs = ReviewModel.objects.none()
        if context == 'pr':
            reviews_qs = ReviewModel.objects.filter(pull_request_id=item_id)
        elif context == 'commit':
            reviews_qs = ReviewModel.objects.filter(commit_id=item_id)
        else:
            return Response(
                {"detail": "Invalid context. Must be 'pr' or 'commit'."},
                status=status.HTTP_400_BAD_REQUEST
            )
        # Prefetch threads to optimize queries
        reviews_qs = reviews_qs.prefetch_related('threads', 'threads__comments')
        
        # Create a custom serializer context to include thread data
        context = {'include_threads': True, 'request': request}
        serializer = self.get_serializer(reviews_qs.order_by('-created_at'), many=True, context=context)
        
        return Response(serializer.data)
    
    def get_queryset(self):
        return ReviewModel.objects.filter(
            Q(repository__owner=self.request.user) |
            Q(repository__collaborators__user=self.request.user)
        ).distinct()

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = serializer.data
        
        # Serialize and add thread information if threads exist
        # It's better to check instance.threads.exists() or instance.threads.all()
        if instance.threads.exists(): 
            # Assuming ThreadSerializer is available and imported correctly
            # Pass the request context to the ThreadSerializer if it needs it (e.g., for HyperlinkedRelatedField)
            serializer_context = self.get_serializer_context()
            threads_data = ThreadSerializer(instance.threads.all(), many=True, context=serializer_context).data
            data['threads'] = threads_data
        else:
            # Optionally, ensure 'threads' key is present even if empty
            data['threads'] = []
        # print(data)
        return Response(data)

    @action(detail=True, methods=['post'])
    def feedback(self, request, pk=None):
        review = self.get_object()
        serializer = ReviewFeedbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        feedback = serializer.save(
            review=review,
            user=request.user
        )
        
        # Process feedback with LangGraph
        try:
            langgraph_service = LangGraphService()
            feedback_result = langgraph_service.handle_feedback(
                review_id=str(review.id),
                feedback=feedback.feedback,
                thread_id=review.thread_id,
                user_id=str(request.user.github_id)
            )
            
            # Update feedback with AI response
            feedback.ai_response = feedback_result['feedback_data']
            feedback.save()
            
            return Response({
                'feedback_data': feedback_result['feedback_data'],
                'token_usage': feedback_result['token_usage']
            })
        except Exception as e:
            logger.error(f"Error processing feedback: {str(e)}")
            return Response(
                {"detail": "Error processing feedback"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    @action(detail=True, methods=['get'])
    def threads(self, request, pk=None):
        review = self.get_object() # pk is reviewId
        threads_qs = ThreadModel.objects.filter(review=review)
        serializer = ThreadSerializer(threads_qs, many=True) # Assuming ThreadSerializer exists
        return Response(serializer.data)

    @action(detail=True, methods=['post']) # For creating a new thread under a review
    def create_thread(self, request, pk=None):
        review = self.get_object()
        # Logic to create a new thread, potentially with an initial message
        # This might be complex if thread creation also involves LangGraph
        # For now, let's assume a simple thread creation.
        # The client might expect a title or initial message.
        title = request.data.get('title', f'Conversation for Review {review.id}')
        
        # If thread creation involves LangGraph to get a langgraph_thread_id:
        # langgraph_client = LangGraphClient()
        # async_to_sync(langgraph_client.initialize)()
        # lg_thread_response = async_to_sync(langgraph_client.create_thread_for_review)(review_id=review.id)
        # langgraph_native_thread_id = lg_thread_response.get('thread_id')
        # if not langgraph_native_thread_id:
        #     return Response({"detail": "Failed to create LangGraph thread."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # For simplicity, assuming thread_id is generated or not strictly needed from LangGraph at this stage
        # Or that the first message via "reply" to this new thread will establish it in LangGraph.
        
        new_thread = ThreadModel.objects.create(
            review=review,
            title=title,
            # thread_id=langgraph_native_thread_id, # If obtained
            status='open', # Default status
            created_by=request.user
        )
        serializer = ThreadSerializer(new_thread)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    @action(detail=True, methods=['post'])
    def re_review(self, request, pk=None):
        review = self.get_object()
        issues = request.data.get('issues', [])
        
        if not issues:
            return Response(
                {"detail": "No issues provided for re-review"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        try:
            # Create new review based on previous one
            new_review = ReviewModel.objects.create(
                repository=review.repository,
                pull_request=review.pull_request,
                commit=review.commit,
                status='pending',
                parent_review=review
            )
            
            # Trigger re-review process
            process_pr_review.delay({
                'pull_request': {
                    'number': review.pull_request.pr_number if review.pull_request else None,
                    'user': {'id': request.user.id},
                    'base': {
                        'repo': {
                            'owner': {'login': review.repository.owner.username},
                            'name': review.repository.repo_name.split('/')[-1]
                        }
                    }
                },
                'repository': {
                    'owner': {'login': review.repository.owner.username},
                    'name': review.repository.repo_name.split('/')[-1]
                }
            })
            
            return Response({
                'review_id': new_review.id,
                'status': 'pending'
            })
        except Exception as e:
            logger.error(f"Error requesting re-review: {str(e)}")
            return Response(
                {"detail": "Error requesting re-review"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'])
    def submit_ai_rating(self, request, pk=None):
        """
        Submit a rating and feedback about the AI review quality
        
        Args:
            pk: The ID of the Review model instance
            
        Request body:
            rating: int (1-5)
            feedback: str
            
        Returns:
            Response with success message
        """
        review = self.get_object()
        
        # Validate input
        rating = request.data.get('rating')
        feedback_text = request.data.get('feedback')
        
        if not rating or not isinstance(rating, int) or rating < 1 or rating > 5:
            return Response(
                {"detail": "Rating must be an integer between 1 and 5"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        if not feedback_text:
            return Response(
                {"detail": "Feedback text is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Create or update a ReviewFeedback with a specific feedback_type
        feedback, created = ReviewFeedback.objects.update_or_create(
            review=review,
            user=request.user,
            defaults={
                'rating': rating,
                'feedback': feedback_text
            }
        )
        
        # Track token usage for this feedback
        try:
            # Create a minimal LLMUsage entry for the rating submission
            # This helps track user engagement with the system
            LLMUsageModel.objects.create(
                review=review,
                user=request.user,
                llm_model=review.repository.llm_preference or settings.DEFAULT_LLM_MODEL,
                input_tokens=0,  # No tokens used for ratings
                output_tokens=0,  # No tokens used for ratings
                cost=0.0
            )
        except Exception as e:
            logger.warning(f"Failed to record LLM usage for rating: {str(e)}")
            # Continue even if tracking fails
            
        return Response({
            "detail": "Thank you for your feedback!",
            "review_id": review.id,
            "rating": rating
        })

class LLMUsageViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = LLMUsageSerializer
    permission_classes = [IsAuthenticated]

    def _get_base_llm_usage_queryset(self):
        """
        Helper method to get the base QuerySet of LLMUsageModel instances
        based on user permissions.
        """
        if self.request.user.is_staff:
            return LLMUsageModel.objects.all()
        else:
            return LLMUsageModel.objects.filter(
                Q(review__repository__owner=self.request.user) |
                Q(review__repository__collaborators__user=self.request.user)
            ).distinct()

    def get_queryset(self):
        """
        This method is intended to return a summary dictionary.
        It's called by the overridden 'list' and 'summary' actions.
        Standard DRF ModelViewSet 'retrieve' action will break if it relies on this
        method returning a QuerySet of model instances.
        """
        base_queryset = self._get_base_llm_usage_queryset()

        total_tokens = base_queryset.aggregate(
            total_input=Sum('input_tokens', default=0),
            total_output=Sum('output_tokens', default=0),
            total_cost=Sum('cost', default=0.0)
        )
        
        usage_by_model = list(base_queryset.values('llm_model').annotate(
            total_input=Sum('input_tokens', default=0),
            total_output=Sum('output_tokens', default=0),
            total_cost=Sum('cost', default=0.0),
            count=Count('id')
        ).order_by('llm_model'))

        # Ensure all parts of total_tokens are not None if the base_queryset is empty
        total_usage_cleaned = {
            'total_input': total_tokens['total_input'] or 0,
            'total_output': total_tokens['total_output'] or 0,
            'total_cost': total_tokens['total_cost'] or 0.0,
        }

        return {
            'total_usage': total_usage_cleaned,
            'usage_by_model': usage_by_model
        }


    def list(self, request, *args, **kwargs):
        """
        Overrides the default list action to return the summary data.
        """
        summary_data = self.get_queryset() # This now calls the method that returns the dictionary
        return Response(summary_data)

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """
        Custom action to return the summary. This is consistent with the list view.
        """
        summary_data = self.get_queryset() # Calls the method that returns the dictionary
        return Response(summary_data)

class ThreadViewSet(viewsets.ModelViewSet):
    serializer_class = ThreadSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return ThreadModel.objects.filter(
            Q(review__repository__owner=self.request.user) |
            Q(review__repository__collaborators__user=self.request.user)
        ).distinct()
    # the permission for reply should include isAssignedReviewerForThread only remove it for testing
    # @action(detail=True, methods=['post'], url_path='reply', permission_classes=[IsAuthenticated, isAssignedReviewerForThread])
    @action(detail=True, methods=['post'], url_path='reply', permission_classes=[IsAuthenticated])
    def reply(self, request, pk=None):
        """
        Reply to a thread and get an AI response.
        
        Args:
            pk: The ID of the Thread model instance
            
        Request body:
            message: str - The user's reply message
            
        Returns:
            Response with user comment and AI response
        """
        thread = self.get_object()
        # Validate the input
        message = request.data.get('message')
        parent_comment_id = request.data.get('parent_comment_id')  # Add this
         # Get parent comment if specified
        parent_comment = None
        if parent_comment_id:
            try:
                parent_comment = CommentModel.objects.get(id=parent_comment_id, thread=thread)
            except CommentModel.DoesNotExist:
                return Response(
                    {"detail": "Parent comment not found"},
                    status=status.HTTP_404_NOT_FOUND
                )
        if not message:
            return Response(
                {"detail": "Message is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Create user's comment
        user_comment = CommentModel.objects.create(
            thread=thread,
            user=request.user,
            comment=message,
            type='request',  # This is a request for AI feedback
            parent_comment=parent_comment
        )
        
        # Get AI user (create if not exists)
        ai_user, _ = User.objects.get_or_create(
            username="ai_assistant",
            defaults={
                "email": "ai@example.com",
                "is_staff": True,
                "is_ai_user": True  # Assuming this field exists
            }
        )
        
        # If no AI user ID is configured, use the first admin user
        if not ai_user and not settings.AI_USER_ID:
            ai_user = User.objects.filter(is_staff=True).first()
            if not ai_user:
                logger.error("No AI user or admin user found for AI responses")
                return Response(
                    {"detail": "Server configuration error: No AI user available"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
        elif settings.AI_USER_ID and not ai_user:
            try:
                ai_user = User.objects.get(id=settings.AI_USER_ID)
            except User.DoesNotExist:
                logger.error(f"AI_USER_ID {settings.AI_USER_ID} not found")
                # Continue with admin user
        # Manage asyncio event loop explicitly
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # Process user's message with LangGraph
        try:
            # Get thread history to provide context for the conversation
            thread_comments = CommentModel.objects.filter(thread=thread).order_by('created_at')
            conversation_history = []
            
            # for comment in thread_comments:
            #     # Skip the most recent comment (the one we just added)
            #     if comment.id == user_comment.id:
            #         continue
                    
            #     role = "ai" if comment.user.is_staff or getattr(comment.user, 'is_ai_user', False) else "user"
            #     conversation_history.append((role, comment.message))
            
            # Add the current user message
            conversation_history.append(("user", message))
            
            # Get LangGraph service
            langgraph_client_instance = LangGraphClient()
            loop.run_until_complete(langgraph_client_instance.initialize())
            # async_to_sync(langgraph_client_instance.initialize)()
            # Determine if this is the first message in the thread
            is_first_message_in_thread = thread_comments.count() <= 1 # Only our new comment
            print(f"Is first message in thread: {is_first_message_in_thread} , {thread_comments.count()} comments in thread")
            # Fetch review data and repo settings for context if it's the first message
            review_model_instance = thread.review
            review_data_for_lg = {}
            repo_settings_for_lg = {}

            if is_first_message_in_thread:
                # Serialize review and repository for context
                # This is a simplified example; you might need more detailed serialization
                review_data_for_lg = ReviewSerializer(review_model_instance).data
                repo_settings_for_lg = {
                    'llm_preference': review_model_instance.repository.llm_preference or settings.DEFAULT_LLM_MODEL,
                    'coding_standards': review_model_instance.repository.coding_standards or [],
                    'code_metrics': review_model_instance.repository.code_metrics or [],
                }
            response = loop.run_until_complete(
                langgraph_client_instance.handle_feedback(
                    feedback=message,
                    thread_id=thread.thread_id,  # This is the LangGraph native thread_id
                    user_id=str(request.user.id),
                    # conversation_history=conversation_history, # If you want to pass the history
                    is_first_message=is_first_message_in_thread,
                    review_data=review_data_for_lg,
                    repo_settings=repo_settings_for_lg
            ))
            # response = async_to_sync(langgraph_client_instance.handle_feedback)(
            #     # review_id=str(thread.review.id),
            #     feedback=message,
            #     thread_id=thread.thread_id, # This is the LangGraph native thread_id
            #     user_id=str(request.user.id),
            #     # conversation_history=conversation_history,
            #     is_first_message=is_first_message_in_thread,
            #     review_data=review_data_for_lg,
            #     repo_settings=repo_settings_for_lg
            # )
            # Extract AI response and token usage
            ai_response_content = response.get('feedback_data', {}).get('messages', [])[-1]
            # Extract the last AI message, assuming it's the latest response
            actual_ai_message = "No response generated."
            if ai_response_content and isinstance(ai_response_content, tuple) and ai_response_content[0] == 'ai':
                actual_ai_message = ai_response_content[1]
            elif isinstance(ai_response_content, dict) and ai_response_content.get('type') == 'ai': # Adjust if structure is different
                 actual_ai_message = ai_response_content.get('content', actual_ai_message)


            token_usage = response.get('token_usage', {})
            
            # Filter the feedback_data before saving
            raw_feedback_data = response.get('feedback_data', {})
            allowed_keys = [
                "repo", "user", "fixes", "pr_id", "llm_model", "metrics", 
                "reviews", "original_review", "updated_review", "messages", 
                "feedback", "standards", "re_run_plan", "reviewer_id", 
                "sufficiency", "instructions", "feedback_status", 
                "feedback_suggestion", "feedback_explanation", 
                "sufficiency_suggestion", "sufficiency_explanation"
            ]
            filtered_feedback_data = {key: raw_feedback_data[key] for key in allowed_keys if key in raw_feedback_data}
            
            ai_comment = CommentModel.objects.create(
                thread=thread,
                user=ai_user,
                comment=actual_ai_message, # Use extracted message
                type='response',
                parent_comment=user_comment,
                comment_data=filtered_feedback_data # Store the relevant part of the response
            )
            
            # Update thread with last_comment reference and timestamp
            # thread.last_comment = ai_comment
            thread.last_comment_at = timezone.now()
            thread.save(update_fields=['last_comment_at'])
            
            # Record token usage
            if token_usage:
                LLMUsageModel.objects.create(
                    review=thread.review,
                    user=request.user,
                    llm_model=thread.review.repository.llm_preference or settings.DEFAULT_LLM_MODEL,
                    input_tokens=token_usage.get('input_tokens', 0),
                    output_tokens=token_usage.get('output_tokens', 0),
                    cost=calculate_cost(token_usage, thread.review.repository.llm_preference or settings.DEFAULT_LLM_MODEL),
                )
            
            # Return both user comment and AI response
            return Response({
                'user_comment': CommentSerializer(user_comment).data,
                'ai_response': CommentSerializer(ai_comment).data,
                'token_usage': token_usage
            })
            
        except Exception as e:
            logger.error(f"Error processing thread reply: {str(e)}", exc_info=True)
            
            # If we fail, we should still show the user's comment but explain the error
            error_message = f"I'm sorry, I couldn't process your request: {str(e)}"
            
            # Try to create an error response from the AI
            try:
                ai_comment = CommentModel.objects.create(
                    thread=thread,
                    user=ai_user,
                    parent_comment=user_comment,
                    comment=error_message,
                    type='error',
                    comment_data={'error': str(e)}
                )
                
                # thread.last_comment = ai_comment
                thread.last_comment_at = timezone.now()
                thread.save(update_fields=['last_comment_at'])
                
                return Response({
                    'user_comment': CommentSerializer(user_comment).data,
                    'ai_response': CommentSerializer(ai_comment).data,
                    'error': str(e)
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                
            except Exception as inner_e:
                logger.error(f"Failed to create error comment: {str(inner_e)}")
                # Return just the user comment if everything else fails
                return Response({
                    'user_comment': CommentSerializer(user_comment).data,
                    'error': str(e)
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# Add this class for GitHub webhook handling
class GitHubWebhookView(APIView):
    permission_classes = [AllowAny]
    
    def post(self, request, *args, **kwargs):
        # Placeholder for webhook processing
        return JsonResponse({"status": "ok", "message": "Webhook received"})

# GitHubWebhookView and other views follow...
# Ensure these new ViewSets are registered in your urls.py
# Example in core/urls.py:
# from rest_framework.routers import DefaultRouter
# router = DefaultRouter()
# router.register(r'repositories', RepositoryViewSet, basename='repository')
# router.register(r'commits', CommitViewSet, basename='commit')
# router.register(r'pullrequests', PullRequestViewSet, basename='pullrequest')
# ... urlpatterns += router.urls
