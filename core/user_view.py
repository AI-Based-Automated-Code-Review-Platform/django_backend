from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status,viewsets
from rest_framework.permissions import IsAuthenticated

from .models import (
    Repository as DBRepository,
)
from .serializers import (
    UserSerializer, 
    GitHubRepositorySerializer, GitHubOrganizationSerializer
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