from rest_framework.response import Response
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action

from .tasks.review_tasks import process_pr_review
from .models import (
    User,
    Repository as DBRepository,
    PullRequest as PRModel,
    Review as ReviewModel,
    Thread as ThreadModel,
)
from .serializers import (
    PRSerializer, ThreadSerializer
)
from .services import (
    get_repository_pull_requests_from_github,
    get_single_pull_request_from_github,
)
import requests
from django.shortcuts import get_object_or_404
from django.core.exceptions import PermissionDenied
import logging
from .permissions import (CanAccessRepository)
# Create a logger instance
logger = logging.getLogger(__name__)

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
