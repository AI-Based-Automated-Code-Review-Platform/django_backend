from rest_framework.response import Response
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action

from .models import (
    Repository as DBRepository,
    RepoCollaborator,
    PullRequest as PRModel,
    Commit as CommitModel,
    WebhookEventLog
)
from .serializers import (
    RepositorySerializer, RepoCollaboratorSerializer, 
    GitHubCollaboratorSerializer,
    PRSerializer, CommitSerializer
)
from .services import (
    get_repo_collaborators_from_github,
    get_single_commit_from_github,
    get_single_pull_request_from_github,
)
import hashlib
import os
import requests
from django.conf import settings
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.urls import reverse
import logging
from .permissions import (IsRepositoryOwner,CanAccessRepository)
# Create a logger instance
logger = logging.getLogger(__name__)

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