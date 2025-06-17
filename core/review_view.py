from rest_framework.response import Response
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
import json
from django.utils import timezone
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from .tasks.review_tasks import process_pr_review
from .models import (
    Review as ReviewModel,
    Thread as ThreadModel,
    LLMUsage as LLMUsageModel,
    ReviewFeedback,
)
from .serializers import (
    ReviewSerializer, ThreadSerializer, ReviewFeedbackSerializer
)
from .services import (
    LangGraphService
)
from django.conf import settings
from django.db.models import Q 
import logging
from .permissions import (CanAccessRepository)
# Create a logger instance
logger = logging.getLogger(__name__)

class ReviewViewSet(viewsets.ModelViewSet):
    serializer_class = ReviewSerializer
    permission_classes = [IsAuthenticated]
    
    @action(detail=False, methods=['get'],url_path='history', permission_classes=[IsAuthenticated, CanAccessRepository])
    def history(self, request, pk=None):
        """
        Get review history for a PR or commit with thread information.
        """
        context_param = request.query_params.get('context')  # 'pr' or 'commit'
        item_id = request.query_params.get('id')  # PR or Commit ID
        
        if not context_param or not item_id:
            return Response(
                {"detail": "Context and ID parameters are required."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        reviews_qs = ReviewModel.objects.none()
        if context_param == 'pr':
            reviews_qs = ReviewModel.objects.filter(pull_request_id=item_id)
        elif context_param == 'commit':
            reviews_qs = ReviewModel.objects.filter(commit__commit_hash=item_id)
        else:
            return Response(
                {"detail": "Invalid context. Must be 'pr' or 'commit'."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        reviews_qs = reviews_qs.prefetch_related('threads', 'threads__comments')
        
        # Use default serializer context, ReviewSerializer includes threads by default if present in Meta
        serializer_context = self.get_serializer_context()
        serializer = self.get_serializer(reviews_qs.order_by('-created_at'), many=True, context=serializer_context)
        
        response_data = serializer.data # This is a list of serialized review objects
        
        fields_to_remove_from_each_review = [
            'repository', 
            'pull_request', 
            'review_data', 
            'threads',
            'thread_count' # Also remove thread_count as it's related to threads
        ]
        
        cleaned_response_data = []
        for review_item_data in response_data:
            for key_to_remove in fields_to_remove_from_each_review:
                review_item_data.pop(key_to_remove, None)
            cleaned_response_data.append(review_item_data)
            
        return Response(cleaned_response_data)
    
    def get_queryset(self):
        # Include both repository-based reviews and VS Code reviews (repository=None)
        return ReviewModel.objects.filter(
            Q(repository__owner=self.request.user) |
            Q(repository__collaborators__user=self.request.user) |
            Q(repository__isnull=True, created_by=self.request.user)  # VS Code reviews
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
            
        # For VS Code reviews, ensure we have the workspace info in the response
        if instance.review_type == 'vscode' and instance.review_data:
            data['workspace_info'] = {
                'workspace_path': instance.review_data.get('workspace_path'),
                'repository_name': instance.review_data.get('repository_name'),
                'files_count': instance.review_data.get('files_count', 0),
                'llm_model': instance.review_data.get('llm_model'),
                'is_git_repo': instance.review_data.get('is_git_repo', False)
            }
            
        return Response(data)
    # this feedback endpoint is not gonna be used anywhere, it's just a placeholder
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
    @action(detail=False, methods=['post'], url_path='vscode-review', permission_classes=[IsAuthenticated])
    def vscode_review(self, request):
        """
        VS Code extension endpoint for direct code review.
        Accepts files and diff data directly from the extension.
        Supports real-time updates via WebSocket.
        """
        
        channel_layer = get_channel_layer()
        
        try:
            # Extract data from request
            files_json = request.data.get('files')
            diff_str = request.data.get('diff_str', '')
            llm_model = request.data.get('llm_model', settings.DEFAULT_LLM_MODEL if hasattr(settings, 'DEFAULT_LLM_MODEL') else 'gpt-4')
            standards = request.data.get('standards', [])
            metrics = request.data.get('metrics', [])
            temperature = request.data.get('temperature', 0.3)
            max_tokens = request.data.get('max_tokens', 32768)
            max_tool_calls = request.data.get('max_tool_calls', 7)
            
            # NEW: Extract local repository context
            workspace_path = request.data.get('workspace_path')
            repository_name = request.data.get('repository_name')
            git_remote_url = request.data.get('git_remote_url')
            git_branch = request.data.get('git_branch')
            is_git_repo = request.data.get('is_git_repo', False)
            
            # Validate required fields
            if not files_json:
                return Response(
                    {"detail": "Files data is required"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Parse files if it's a JSON string
            if isinstance(files_json, str):
                try:
                    files_dict = json.loads(files_json)
                except json.JSONDecodeError:
                    return Response(
                        {"detail": "Invalid JSON format for files"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            else:
                files_dict = files_json
            
            if not isinstance(files_dict, dict):
                return Response(
                    {"detail": "Files must be a dictionary"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Validate file count and size
            if len(files_dict) > 100:  # Reasonable limit
                return Response(
                    {"detail": "Too many files. Maximum 100 files allowed."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            total_size = sum(len(content.encode('utf-8')) for content in files_dict.values())
            if total_size > 10 * 1024 * 1024:  # 10MB limit
                return Response(
                    {"detail": "Total file size too large. Maximum 10MB allowed."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Create enhanced review_data with local repository context
            enhanced_review_data = {
                'files': files_dict,
                'diff_str': diff_str,
                'llm_model': llm_model,
                'standards': standards,
                'metrics': metrics,
                'temperature': temperature,
                'max_tokens': max_tokens,
                'max_tool_calls': max_tool_calls,
                # Local repository context
                'workspace_path': workspace_path,
                'repository_name': repository_name,
                'git_remote_url': git_remote_url,
                'git_branch': git_branch,
                'is_git_repo': is_git_repo,
                'files_count': len(files_dict),
                'total_size_bytes': total_size,
                'review_timestamp': timezone.now().isoformat()
            }
            
            # Create a review record for VS Code reviews
            review = ReviewModel.objects.create(
                status='pending',
                review_type='vscode',
                created_by=request.user,
                review_data=enhanced_review_data  # Store enhanced data
            )
            
            logger.info(f"VS Code review initiated by user {request.user.username}, review ID: {review.id}, workspace: {workspace_path}")
            
            # Send initial WebSocket notification
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    f'user_{request.user.github_id}',
                    {
                        'type': 'review_status_update',
                        'review_id': str(review.id),
                        'status': 'pending',
                        'progress': 0
                    }
                )
            
            # Prepare data for LangGraph service
            review_data = {
                'files': files_dict,
                'diff_str': diff_str,
                'llm_model': llm_model,
                'standards': standards,
                'metrics': metrics,
                'temperature': temperature,
                'max_tokens': max_tokens,
                'max_tool_calls': max_tool_calls,
                'user_github_token': request.user.github_access_token,
                'review_id': str(review.id),
                'user_id': str(request.user.github_id),
                # Include workspace context for the task
                'workspace_path': workspace_path,
                'repository_name': repository_name,
                'git_remote_url': git_remote_url,
                'git_branch': git_branch,
                'is_git_repo': is_git_repo
            }
            print(workspace_path, repository_name, git_remote_url, git_branch, is_git_repo)
            # Process the review asynchronously using Celery
            from .tasks.review_tasks import process_vscode_review_task
            
            # Queue the review task
            task = process_vscode_review_task.delay(review.id, review_data)
            
            # Update review with task ID
            review.status = 'processing'
            review.save()
            
            # Send processing notification
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    f'user_{request.user.github_id}',
                    {
                        'type': 'review_status_update',
                        'review_id': str(review.id),
                        'status': 'processing',
                        'progress': 10
                    }
                )
            
            logger.info(f"VS Code review queued for processing, review ID: {review.id}, task ID: {task.id}")
            
            return Response({
                'review_id': review.id,
                'status': 'processing',
                'message': 'Review has been queued for processing. You will receive real-time updates via WebSocket.',
                'websocket_url': f'ws://localhost:8000/ws/user/{request.user.github_id}/',
                'task_id': task.id,
                'workspace_info': {
                    'workspace_path': workspace_path,
                    'repository_name': repository_name,
                    'is_git_repo': is_git_repo
                }
            }, status=status.HTTP_202_ACCEPTED)
                
        except Exception as e:
            logger.error(f"VS Code review endpoint error: {str(e)}")
            
            # Send error notification if review was created
            if 'review' in locals() and channel_layer:
                async_to_sync(channel_layer.group_send)(
                    f'user_{request.user.github_id}',
                    {
                        'type': 'review_status_update',
                        'review_id': str(review.id),
                        'status': 'failed',
                        'progress': 0
                    }
                )
            
            return Response(
                {"detail": f"Error processing VS Code review: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'], url_path='vscode-history', permission_classes=[IsAuthenticated])
    def vscode_history(self, request):
        """
        Get VS Code review history with optional filtering by local repository.
        
        Query Parameters:
        - limit: Number of reviews to return (default: 20)
        - workspace_path: Optional local workspace path to filter reviews
        - repository_name: Optional repository name to filter reviews
        """
        limit = int(request.query_params.get('limit', 20))
        workspace_path = request.query_params.get('workspace_path')
        repository_name = request.query_params.get('repository_name')
        
        # Base queryset for VS Code reviews by the current user
        reviews_qs = ReviewModel.objects.filter(
            review_type='vscode',
            created_by=request.user
        )
        
        # Apply filters if provided
        if workspace_path:
            # Filter by workspace path stored in review_data
            reviews_qs = reviews_qs.filter(
                review_data__workspace_path=workspace_path
            )
        
        if repository_name:
            # Filter by repository name stored in review_data
            reviews_qs = reviews_qs.filter(
                review_data__repository_name__icontains=repository_name
            )
        
        # Order by most recent and limit results
        reviews_qs = reviews_qs.order_by('-created_at')[:limit]
        
        # Serialize the results
        serializer = self.get_serializer(reviews_qs, many=True)
        
        # Clean up the response data to remove unnecessary fields
        response_data = []
        for review_data in serializer.data:
            # Safely handle review_data that might be None
            review_data_dict = review_data.get('review_data') or {}
            
            cleaned_data = {
                'id': review_data['id'],
                'status': review_data['status'],
                'created_at': review_data['created_at'],
                'updated_at': review_data['updated_at'],
                'error_message': review_data.get('error_message'),
                # Include relevant metadata from review_data with safe access
                'workspace_info': {
                    'workspace_path': review_data_dict.get('workspace_path'),
                    'repository_name': review_data_dict.get('repository_name'),
                    'files_count': len(review_data_dict.get('files', {})),
                    'llm_model': review_data_dict.get('llm_model')
                }
            }
            response_data.append(cleaned_data)
        
        return Response(response_data)