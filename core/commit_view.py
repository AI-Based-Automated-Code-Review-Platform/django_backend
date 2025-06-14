from rest_framework.response import Response
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action

from .tasks.review_tasks import process_commit_review
from .models import (
    User,
    Repository as DBRepository,
    Commit as CommitModel,
    Review as ReviewModel,
)
from .serializers import (
    CommitSerializer
)
from .services import (
    get_repository_commits_from_github,
    get_single_commit_from_github,
)
import requests
from django.shortcuts import get_object_or_404
from django.core.exceptions import PermissionDenied
import logging
from .permissions import (CanAccessRepository)
from django.utils.dateparse import parse_datetime
# Create a logger instance
logger = logging.getLogger(__name__)

class CommitViewSet(viewsets.ModelViewSet):
    serializer_class = CommitSerializer
    permission_classes = [IsAuthenticated] # Permissions checked in list method
    lookup_field = 'commit_hash'
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
        page = int(request.query_params.get('page', 1))
        per_page = int(request.query_params.get('per_page', 30))
        # Prepare filters for GitHub API call
        author_filter = request.query_params.get('author')
        since_filter_str = request.query_params.get('date_from')
        until_filter_str = request.query_params.get('date_to')

        formatted_since_filter = None
        if since_filter_str:
            # Assuming frontend sends YYYY-MM-DD. Convert to YYYY-MM-DDTHH:MM:SSZ.
            try:
                # Validate and ensure it's a date before formatting
                parsed_date = parse_datetime(since_filter_str + "T00:00:00Z")
                if parsed_date:
                    formatted_since_filter = since_filter_str + "T00:00:00Z"
                else: # Should not happen if parse_datetime raises ValueError on failure
                    logger.warning(f"Could not parse date_from: {since_filter_str}. Ignoring filter.")
            except ValueError:
                logger.warning(f"Invalid date_from format: {since_filter_str}. Expected YYYY-MM-DD. Ignoring filter.")
                # Optionally, you could return a 400 error:
                # return Response({"detail": f"Invalid date_from format: {since_filter_str}. Expected YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

        formatted_until_filter = None
        if until_filter_str:
            # Assuming frontend sends YYYY-MM-DD. Convert to YYYY-MM-DDTHH:MM:SSZ.
            try:
                # Validate and ensure it's a date before formatting
                parsed_date = parse_datetime(until_filter_str + "T23:59:59Z")
                if parsed_date:
                    formatted_until_filter = until_filter_str + "T23:59:59Z"
                else: # Should not happen if parse_datetime raises ValueError on failure
                    logger.warning(f"Could not parse date_to: {until_filter_str}. Ignoring filter.")
            except ValueError:
                logger.warning(f"Invalid date_to format: {until_filter_str}. Expected YYYY-MM-DD. Ignoring filter.")
                # Optionally, you could return a 400 error:
                # return Response({"detail": f"Invalid date_to format: {until_filter_str}. Expected YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        combined_items_dict = {}
        # Only include DB items if it's the first page of a general listing 
        # or if specific filters that might only hit the DB are applied (not covered here).
        # For simple pagination, page 1 gets DB items.
        if page == 1:
            db_items = CommitModel.objects.filter(repository=db_repo).order_by('-timestamp')
            if since_filter_str:
                db_items = db_items.filter(timestamp__gte=formatted_since_filter)
            if until_filter_str:
                db_items = db_items.filter(timestamp__lte=formatted_until_filter)
            # Consider paginating DB items as well if there are many, for now, taking all for page 1
            serialized_db_items = self.get_serializer(db_items, many=True).data
            for item in serialized_db_items:
                item['source'] = 'db'
                combined_items_dict[item['commit_hash']] = item
        
        github_items_for_current_page = []

        if not request.user.github_access_token:
            logger.warning(f"User {request.user.id} has no GitHub token. Fetching commits from DB only for repo {db_repo.id}")
            if page > 1: # If no GitHub token and asking for page > 1, return empty if DB items were only on page 1
                return Response([])
        else:
            try:
                owner_login = db_repo.owner.username
                repo_name_only = db_repo.repo_name.split('/')[-1]
                gh_items_raw = get_repository_commits_from_github(
                    github_token=request.user.github_access_token,
                    owner_login=owner_login,
                    repo_name=repo_name_only,
                    per_page=per_page,
                    page=page,
                    author=author_filter, 
                    since=formatted_since_filter,
                    until=formatted_until_filter
                )

                for gh_commit in gh_items_raw:
                    # If on page 1, add GitHub item only if not already present from DB
                    # If on page > 1, combined_items_dict is initially empty (or only contains GH items from previous GH pages if backend paginated GH internally)
                    # So, this check effectively de-duplicates against DB items on page 1.
                    if gh_commit['sha'] not in combined_items_dict:
                        commit_data = gh_commit.get('commit', {})
                        author_data = commit_data.get('author', {})
                        committer_data = commit_data.get('committer', {})
                        gh_author_user = gh_commit.get('author')
                        gh_committer_user = gh_commit.get('committer')

                        transformed_gh_item = {
                            'commit_hash': gh_commit.get('sha'),
                            'message': commit_data.get('message'),
                            'author_name': author_data.get('name'),
                            'author_email': author_data.get('email'),
                            'committer_name': committer_data.get('name'),
                            'committer_email': committer_data.get('email'),
                            'committed_date': committer_data.get('date'),
                            'url': gh_commit.get('html_url'),
                            'source': 'github',
                            'id': None, 
                            'repository_id': db_repo.id,
                            'created_at': None, 
                            'updated_at': None,
                            'timestamp': author_data.get('date'),
                            'author_github_id': str(gh_author_user.get('id')) if gh_author_user else None,
                            'committer_github_id': str(gh_committer_user.get('id')) if gh_committer_user else None,
                        }
                        serializer_instance = self.get_serializer(data=transformed_gh_item)
                        if serializer_instance.is_valid():
                            validated_data = serializer_instance.data
                            validated_data['source'] = 'github'
                            # For page > 1, we add directly to a list that will be returned.
                            # For page 1, we add to combined_items_dict to merge with DB items.
                            if page == 1:
                                combined_items_dict[gh_commit['sha']] = validated_data
                            else:
                                github_items_for_current_page.append(validated_data)
                        else:
                             logger.error(f"GitHub commit data for {gh_commit['sha']} not valid for serializer: {serializer_instance.errors}")
                             transformed_gh_item['repository_id'] = db_repo.id
                             transformed_gh_item['source'] = 'github'
                             if page == 1:
                                 combined_items_dict[gh_commit['sha']] = transformed_gh_item
                             else:
                                 github_items_for_current_page.append(transformed_gh_item)
            except requests.exceptions.RequestException as e:
                logger.error(f"GitHub API error while fetching commits for repo {db_repo.id}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error while fetching GitHub commits for repo {db_repo.id}: {e}")

        if page == 1:
            final_list = list(combined_items_dict.values())
            # Optionally sort combined list if order matters and DB/GitHub items interleave
            # final_list.sort(key=lambda x: (parse_datetime(x['timestamp']) if x.get('timestamp') else None), reverse=True)

        else:
            final_list = github_items_for_current_page
            # Sort if necessary, GitHub usually returns in order
            # final_list.sort(key=lambda x: (parse_datetime(x['timestamp']) if x.get('timestamp') else None), reverse=True)
        return Response(final_list)

    @action(detail=False,url_path='trigger-review', methods=['post'])
    def trigger_review(self, request):
        """
        Manually trigger an AI review for a commit.
        If the commit doesn't exist in the DB, it's fetched from GitHub and created.
        Requires 'repository_id' and 'commit_hash' in the request body.
        """
        repository_id_str = request.data.get('repository_id')
        commit_hash_from_request = request.data.get('commit_hash')

        if not repository_id_str or not commit_hash_from_request:
            return Response(
                {"detail": "repository_id and commit_hash are required in the request body."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            repository_id = int(repository_id_str)
        except ValueError:
            return Response({"detail": "repository_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            repository = get_object_or_404(DBRepository, pk=repository_id)
        except ValueError: # Should be caught by get_object_or_404 for non-int pk
             return Response({"detail": "Invalid repository_id format."}, status=status.HTTP_400_BAD_REQUEST)
        
        # Check permissions for the repository
        if not CanAccessRepository().has_object_permission(request, self, repository):
            raise PermissionDenied("You do not have permission to trigger reviews for this repository.")

        commit_instance = None
        try:
            commit_instance = CommitModel.objects.get(repository=repository, commit_hash=commit_hash_from_request)
        except CommitModel.DoesNotExist:
            logger.info(f"Commit {commit_hash_from_request} not found in DB for repository {repository.repo_name}. Attempting to fetch from GitHub.")
            if not request.user.github_access_token:
                return Response(
                    {"detail": f"Commit {commit_hash_from_request} not found in DB and no GitHub token available to fetch from GitHub."},
                    status=status.HTTP_403_FORBIDDEN # 403 as token is missing for required action
                )
            
            try:
                owner_login = repository.owner.username
                repo_name_only = repository.repo_name.split('/')[-1]
                
                gh_commit_data = get_single_commit_from_github(
                    github_token=request.user.github_access_token,
                    owner_login=owner_login,
                    repo_name=repo_name_only,
                    commit_sha=commit_hash_from_request
                )
                
                # Transform and save/update the commit
                commit_payload = gh_commit_data.get('commit', {})
                author_payload = commit_payload.get('author', {})
                # committer_payload = commit_payload.get('committer', {}) # If you store separate committer date
                
                gh_author_user = gh_commit_data.get('author')  # GitHub user object for author
                gh_committer_user = gh_commit_data.get('committer') # GitHub user object for committer

                timestamp_str = author_payload.get('date')
                parsed_timestamp = None
                if timestamp_str:
                    parsed_timestamp = parse_datetime(timestamp_str)
                
                commit_defaults = {
                    'message': commit_payload.get('message'),
                    'author_github_id': str(gh_author_user.get('id')) if gh_author_user and gh_author_user.get('id') is not None else None,
                    'committer_github_id': str(gh_committer_user.get('id')) if gh_committer_user and gh_committer_user.get('id') is not None else None,
                    'url': gh_commit_data.get('html_url'),
                    'timestamp': parsed_timestamp,
                }
                # Remove None values from defaults to prevent overriding existing valid fields with None
                commit_defaults_cleaned = {k: v for k, v in commit_defaults.items() if v is not None}

                commit_instance, created = CommitModel.objects.update_or_create(
                    repository=repository,
                    commit_hash=gh_commit_data.get('sha'), # Use sha from response
                    defaults=commit_defaults_cleaned
                )
                if created:
                    logger.info(f"Commit {commit_instance.commit_hash} fetched from GitHub and saved to DB for repository {repository.repo_name}.")
                else:
                    logger.info(f"Commit {commit_instance.commit_hash} fetched from GitHub and updated in DB for repository {repository.repo_name}.")

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    return Response({"detail": f"Commit {commit_hash_from_request} not found on GitHub for repository {repository.repo_name}."}, status=status.HTTP_404_NOT_FOUND)
                elif e.response.status_code == 422: # Invalid SHA format
                    return Response({"detail": f"Invalid commit SHA format: {commit_hash_from_request}."}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
                logger.error(f"GitHub API error fetching commit {commit_hash_from_request} for repo {repository.id}: {e.response.text if e.response else str(e)}")
                return Response({"detail": f"GitHub API error: {e.response.status_code if e.response else 'Unknown'}"}, status=status.HTTP_502_BAD_GATEWAY)
            except Exception as e:
                logger.error(f"Unexpected error fetching or saving commit {commit_hash_from_request} for repo {repository.id} from GitHub: {e}", exc_info=True)
                return Response({"detail": "An unexpected error occurred while fetching commit from GitHub."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # Check for existing reviews that are completed or in progress
        existing_reviews = ReviewModel.objects.filter(
            commit=commit_instance,
            status__in=['completed', 'in_progress', 'pending', 'processing', 'pending_analysis']
        ).order_by('-created_at') # Order to get the latest if multiple exist
        
        if existing_reviews.exists():
            latest_review = existing_reviews.first()
            return Response({
                "detail": f"A review for this commit already exists or is in progress with status '{latest_review.status}'.",
                "review_id": latest_review.id,
                "status": latest_review.status
            }, status=status.HTTP_409_CONFLICT)
        
        # Create a new review
        review = ReviewModel.objects.create(
            repository=repository,
            commit=commit_instance,
            status='pending',
            review_data={'message': 'Commit review manually triggered by user.'}
        )
        
        # Prepare data for the Celery task
        author_username, author_email = None, None
        if commit_instance.author_github_id:
            author_user = User.objects.filter(github_id=commit_instance.author_github_id).first()
            if author_user:
                author_username = author_user.username
                author_email = author_user.email

        committer_username, committer_email = None, None
        if commit_instance.committer_github_id:
            committer_user = User.objects.filter(github_id=commit_instance.committer_github_id).first()
            if committer_user:
                committer_username = committer_user.username
                committer_email = committer_user.email
        
        event_data = {
            'commit': {
                'id': commit_instance.commit_hash, # Using commit_hash as 'id' for consistency
                'sha': commit_instance.commit_hash,
                'message': commit_instance.message,
                'url': commit_instance.url,
                'author': {
                    'id': commit_instance.author_github_id,
                    'name': author_username, 
                    'email': author_email   
                },
                'committer': {
                    'id': commit_instance.committer_github_id,
                    'name': committer_username, 
                    'email': committer_email    
                },
                'timestamp': commit_instance.timestamp.isoformat() if commit_instance.timestamp else None
            },
            'repository': {
                'id': repository.github_native_id,
                'full_name': repository.repo_name,
                'owner': {'login': repository.owner.username}
            },
            'action': 'manual_trigger_commit_review',
            'triggered_by_user_id': request.user.id 
        }
        
        process_commit_review.delay(event_data, repository.id, commit_instance.id)
        
        return Response({
            "detail": "AI review has been triggered for the commit.",
            "review_id": review.id,
            "status": review.status
        }, status=status.HTTP_201_CREATED)