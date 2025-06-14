from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status,viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action

from .models import (
    Repository as DBRepository,
    PullRequest,
    Commit,
    Review,
    WebhookEventLog,
)
from .serializers import (
    UserSerializer, 
    GitHubRepositorySerializer, GitHubOrganizationSerializer,
    WebhookEventLogSerializer
)
from .services import (
    get_user_repos_from_github,
    get_user_orgs_from_github,
)
import requests
import logging
# Create a logger instance
logger = logging.getLogger(__name__)

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

# New ViewSet for User specific endpoints
class UserDashboardStatsView(APIView):
    """
    ViewSet for user-related operations.
    Currently, most user operations are handled by CurrentUserView, UserRepositoriesView, etc.
    This can be expanded if other user-specific, non-admin RESTful operations are needed.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        user = request.user
        # Total repositories (owned or collaborated)
        repo_ids = set(DBRepository.objects.filter(owner=user).values_list('id', flat=True))
        collab_repo_ids = set(DBRepository.objects.filter(collaborators__user=user).values_list('id', flat=True))
        all_repo_ids = repo_ids | collab_repo_ids
        total_repositories = len(all_repo_ids)

        # Total reviews (for repos the user owns or collaborates on)
        total_reviews = Review.objects.filter(repository_id__in=all_repo_ids).count()

        # Total pull requests (for repos the user owns or collaborates on)
        total_pull_requests = PullRequest.objects.filter(repository_id__in=all_repo_ids).count()

        # Total commits (for repos the user owns or collaborates on)
        total_commits = Commit.objects.filter(repository_id__in=all_repo_ids).count()

        # Recent webhook event logs (for repos the user owns or collaborates on)
        recent_events = WebhookEventLog.objects.filter(repository_id__in=all_repo_ids).order_by('-created_at')[:10]
        events_data = WebhookEventLogSerializer(recent_events, many=True).data

        return Response({
            'total_reviews': total_reviews,
            'total_repositories': total_repositories,
            'total_pull_requests': total_pull_requests,
            'total_commits': total_commits,
            'recent_activity': events_data,
        })